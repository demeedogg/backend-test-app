"""HTTP and persistence implementation for public contact submissions."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 16 * 1024
MAX_SUBMISSIONS = 3
MAX_CONCURRENT_REQUESTS = 32
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_DATABASE_PATH = "/data/contact-submissions.sqlite"
EMAIL_LOCAL_PART = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
EMAIL_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ValidationError(Exception):
    """Request data cannot be accepted; fields maps names to safe messages."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("Contact submission validation failed")
        self.fields = fields


class CapacityExceeded(Exception):
    """The service has already stored its lifetime maximum."""


def _is_valid_email(value: str) -> bool:
    if len(value) > 254 or value.count("@") != 1:
        return False
    local_part, domain = value.rsplit("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not EMAIL_LOCAL_PART.fullmatch(local_part)
        or len(domain) > 253
    ):
        return False
    labels = domain.split(".")
    return (
        len(labels) >= 2
        and all(EMAIL_DOMAIN_LABEL.fullmatch(label) for label in labels)
    )


def validate_submission(payload: Any) -> tuple[str, str]:
    """Trim surrounding whitespace and validate the accepted name/email pair."""
    if not isinstance(payload, dict):
        raise ValidationError({"request": "A JSON object is required."})

    errors: dict[str, str] = {}
    name_value = payload.get("name")
    email_value = payload.get("email")

    if not isinstance(name_value, str):
        errors["name"] = "Name must be a string."
        name = ""
    else:
        name = name_value.strip()
        if not 1 <= len(name) <= 100:
            errors["name"] = "Name must contain 1 to 100 characters."

    if not isinstance(email_value, str):
        errors["email"] = "Email must be a string."
        email = ""
    else:
        email = email_value.strip()
        if not _is_valid_email(email):
            errors["email"] = "Enter a valid email address of at most 254 characters."

    if errors:
        raise ValidationError(errors)
    return name, email


def initialize_database(database_path: str | Path) -> None:
    """Create the persistent submission table before the HTTP server starts."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        with connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
                    email TEXT NOT NULL CHECK(length(email) BETWEEN 1 AND 254)
                )
                """
            )


def persist_submission(database_path: str | Path, name: str, email: str) -> None:
    """Atomically check lifetime capacity and commit one distinct submission."""
    connection = sqlite3.connect(database_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        # IMMEDIATE obtains SQLite's cross-process write reservation before the
        # count, so concurrent service instances cannot pass the cap together.
        connection.execute("BEGIN IMMEDIATE")
        count = connection.execute(
            "SELECT COUNT(*) FROM contact_submissions"
        ).fetchone()[0]
        if count >= MAX_SUBMISSIONS:
            connection.rollback()
            raise CapacityExceeded
        connection.execute(
            "INSERT INTO contact_submissions (name, email) VALUES (?, ?)",
            (name, email),
        )
        connection.commit()
    except CapacityExceeded:
        raise
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Limit active request threads and the time each client can hold one."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._request_timeout_seconds = request_timeout_seconds
        super().__init__(server_address, request_handler)

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(self._request_timeout_seconds)
        return request, client_address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            body = b'{"error":"service_busy"}'
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            try:
                request.sendall(response)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return

        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class ContactSubmissionHandler(BaseHTTPRequestHandler):
    """Handle the single public contact-submission operation."""

    database_path = DEFAULT_DATABASE_PATH
    protocol_version = "HTTP/1.1"
    server_version = "ContactSubmissionService"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/api/contact-submissions":
            self._send_json(404, {"error": "not_found"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "unsupported_media_type"})
            return

        try:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self._send_json(
                    400,
                    {
                        "error": "invalid_request",
                        "message": "Content-Length is required.",
                    },
                )
                return
            try:
                content_length = int(length_header)
            except ValueError:
                self._send_json(
                    400,
                    {
                        "error": "invalid_request",
                        "message": "Content-Length must be an integer.",
                    },
                )
                return
            if content_length < 0:
                self._send_json(
                    400,
                    {
                        "error": "invalid_request",
                        "message": "Content-Length must not be negative.",
                    },
                )
                return
            if content_length > MAX_REQUEST_BYTES:
                self._send_json(413, {"error": "request_too_large"})
                return

            raw_body = self.rfile.read(content_length)
            if len(raw_body) != content_length:
                self._send_json(
                    400,
                    {
                        "error": "invalid_request",
                        "message": "Request body is incomplete.",
                    },
                )
                return
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (ValueError, RecursionError):
                self._send_json(
                    400,
                    {
                        "error": "invalid_json",
                        "message": "Request body must be valid UTF-8 JSON.",
                    },
                )
                return

            name, email = validate_submission(payload)
        except ValidationError as error:
            self._send_json(400, {"error": "validation_error", "fields": error.fields})
            return

        try:
            persist_submission(self.database_path, name, email)
        except CapacityExceeded:
            self._send_json(409, {"error": "submission_limit_reached"})
            return
        except Exception:
            LOGGER.exception("Contact submission persistence failed")
            self._send_json(
                500,
                {
                    "error": "internal_error",
                    "message": "Unable to process submission.",
                },
            )
            return

        # Persistence has committed before any success response is constructed.
        self._send_json(201, {"name": name, "email": email})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def create_server(
    host: str,
    port: int,
    database_path: str | Path,
    max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> BoundedThreadingHTTPServer:
    handler = type(
        "ConfiguredContactSubmissionHandler",
        (ContactSubmissionHandler,),
        {"database_path": str(database_path)},
    )
    server = BoundedThreadingHTTPServer(
        (host, port),
        handler,
        max_concurrent_requests=max_concurrent_requests,
        request_timeout_seconds=request_timeout_seconds,
    )
    server.daemon_threads = True
    return server


def serve() -> None:
    database_path = os.environ.get("CONTACT_SUBMISSIONS_DB", DEFAULT_DATABASE_PATH)
    port_text = os.environ.get("PORT", "3000")
    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as error:
        raise SystemExit("PORT must be an integer between 1 and 65535") from error

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    initialize_database(database_path)
    server = create_server("0.0.0.0", port, database_path)
    LOGGER.info("Contact submissions listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
