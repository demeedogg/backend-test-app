from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from email_validator import EmailNotValidError, validate_email

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 16 * 1024
MAX_SUBMISSIONS = 3


class CapacityReached(Exception):
    """Raised when all service-lifetime submission slots have been used."""


class SubmissionStore:
    """SQLite persistence for contact submissions and the service-wide cap."""

    def __init__(
        self,
        database_path: str,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        self.database_path = database_path
        self.before_commit = before_commit
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
                    email TEXT NOT NULL CHECK (length(email) BETWEEN 1 AND 254),
                    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        finally:
            connection.close()

    def submit(self, name: str, email: str) -> dict[str, str | int]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row_count = connection.execute(
                "SELECT COUNT(*) AS count FROM contact_submissions"
            ).fetchone()["count"]
            if row_count >= MAX_SUBMISSIONS:
                connection.rollback()
                raise CapacityReached

            cursor = connection.execute(
                "INSERT INTO contact_submissions (name, email) VALUES (?, ?)",
                (name, email),
            )
            submission = {"id": cursor.lastrowid, "name": name, "email": email}
            if self.before_commit is not None:
                self.before_commit()
            connection.commit()
            return submission
        except CapacityReached:
            raise
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                logger.exception("Failed to roll back contact submission transaction")
            raise
        finally:
            connection.close()


def validate_submission(payload: Any) -> tuple[dict[str, str] | None, dict[str, str]]:
    if not isinstance(payload, dict):
        return None, {"body": "Request body must be a JSON object."}

    details: dict[str, str] = {}
    name_value = payload.get("name")
    email_value = payload.get("email")

    normalized_name: str | None = None
    if not isinstance(name_value, str):
        details["name"] = "Name is required and must be a string."
    else:
        normalized_name = name_value.strip()
        if not 1 <= len(normalized_name) <= 100:
            details["name"] = "Name must contain 1 to 100 characters."

    normalized_email: str | None = None
    if not isinstance(email_value, str):
        details["email"] = "Email is required and must be a string."
    else:
        trimmed_email = email_value.strip()
        if not 1 <= len(trimmed_email) <= 254:
            details["email"] = "Email must contain 1 to 254 characters."
        else:
            try:
                normalized_email = validate_email(
                    trimmed_email,
                    check_deliverability=False,
                ).normalized
                if len(normalized_email) > 254:
                    details["email"] = "Email must contain at most 254 characters."
                    normalized_email = None
            except EmailNotValidError:
                details["email"] = "Enter a valid email address."

    if details:
        return None, details
    assert normalized_name is not None
    assert normalized_email is not None
    return {"name": normalized_name, "email": normalized_email}, {}


class ContactSubmissionHandler(BaseHTTPRequestHandler):
    server: ContactSubmissionServer

    def do_POST(self) -> None:
        if self.path != "/api/contact-submissions":
            self.send_error_response(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
            return

        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", maxsplit=1)[0].strip().lower() != "application/json":
            self.send_error_response(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )
            return

        content_length = self.headers.get("Content-Length")
        try:
            body_length = int(content_length) if content_length is not None else -1
        except ValueError:
            body_length = -1
        if body_length < 0:
            self.send_error_response(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "A valid Content-Length is required.",
            )
            return
        if body_length > MAX_REQUEST_BYTES:
            self.send_error_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "Request body exceeds the 16384-byte limit.",
            )
            return

        try:
            raw_body = self.rfile.read(body_length)
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_response(
                HTTPStatus.BAD_REQUEST,
                "malformed_json",
                "Request body must contain valid UTF-8 JSON.",
            )
            return

        submission, details = validate_submission(payload)
        if details:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "validation_error", "details": details})
            return

        assert submission is not None
        try:
            stored = self.server.store.submit(submission["name"], submission["email"])
        except CapacityReached:
            self.send_error_response(
                HTTPStatus.CONFLICT,
                "capacity_reached",
                "No submission capacity remains.",
            )
            return
        except Exception:
            logger.exception("Failed to persist contact submission")
            self.send_error_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "persistence_failure",
                "Unable to submit information.",
            )
            return

        # The transaction commits inside submit() before this response is created.
        self.send_json(HTTPStatus.CREATED, stored)

    def send_error_response(
        self,
        status: HTTPStatus,
        error: str,
        message: str,
    ) -> None:
        self.send_json(status, {"error": error, "message": message})

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format_string: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format_string % args)


class ContactSubmissionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: SubmissionStore,
        handler_class: type[ContactSubmissionHandler] = ContactSubmissionHandler,
    ) -> None:
        self.store = store
        super().__init__(address, handler_class)
