"""Hermetic HTTP and SQLite tests for contact submissions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import (
    MAX_REQUEST_BYTES,
    ContactSubmissionHandler,
    create_server,
    initialize_database,
)


class ContactSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = f"{self.temporary_directory.name}/submissions.sqlite"
        initialize_database(self.database_path)
        self.server = create_server("127.0.0.1", 0, self.database_path)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)

    def request(
        self,
        payload: object,
        base_url: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{base_url or self.base_url}/api/contact-submissions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def raw_request(
        self,
        body: bytes,
        content_type: str = "application/json",
        base_url: str | None = None,
    ) -> int:
        request = Request(
            f"{base_url or self.base_url}/api/contact-submissions",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status

    def row_count(self) -> int:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM contact_submissions"
            ).fetchone()[0]

    def test_accepts_boundaries_and_returns_normalized_values(self) -> None:
        domain = f"{'a' * 63}.{'b' * 63}.{'c' * 57}.com"
        # 64 local-part characters + @ + 189 domain characters = 254.
        maximum_email = f"{'x' * 64}@{domain}"
        self.assertEqual(len(maximum_email), 254)

        cases = [
            (
                {"name": "  A  ", "email": " person@example.com "},
                {"name": "A", "email": "person@example.com"},
            ),
            (
                {"name": "N" * 100, "email": "person@example.com"},
                {"name": "N" * 100, "email": "person@example.com"},
            ),
            (
                {"name": "Name", "email": maximum_email},
                {"name": "Name", "email": maximum_email},
            ),
        ]
        for payload, expected in cases:
            with self.subTest(
                name_length=len(payload["name"]),
                email_length=len(payload["email"]),
            ):
                status, response = self.request(payload)
                self.assertEqual(status, 201)
                self.assertEqual(response, expected)
        self.assertEqual(self.row_count(), 3)

    def test_invalid_requests_do_not_persist_or_consume_slots(self) -> None:
        invalid_payloads = [
            {},
            {"name": "", "email": "person@example.com"},
            {"name": "   ", "email": "person@example.com"},
            {"name": "N" * 101, "email": "person@example.com"},
            {"name": "Name"},
            {"name": "Name", "email": "not-an-email"},
            {
                "name": "Name",
                "email": f"{'x' * 64}@{'a' * 63}.{'b' * 63}.{'c' * 58}.com",
            },
            {"name": 12, "email": "person@example.com"},
            {"name": "Name", "email": None},
            [],
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, response = self.request(payload)
                self.assertEqual(status, 400)
                self.assertIn("error", response)

        self.assertEqual(self.raw_request(b"{not-json"), 400)
        self.assertEqual(self.row_count(), 0)
        for index in range(3):
            self.assertEqual(
                self.request(
                    {"name": f"Person {index}", "email": "person@example.com"}
                )[0],
                201,
            )
        self.assertEqual(self.row_count(), 3)

    def test_oversized_body_is_rejected_before_validation_or_persistence(self) -> None:
        with patch("app.validate_submission") as validate, patch(
            "app.persist_submission"
        ) as persist:
            status = self.raw_request(b"x" * (MAX_REQUEST_BYTES + 1))

        self.assertEqual(status, 413)
        validate.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(self.row_count(), 0)

    def test_json_parser_complexity_errors_are_client_errors(self) -> None:
        deeply_nested = b"[" * 2_000 + b"]" * 2_000
        oversized_integer = (
            b'{"name":' + b"9" * 5_000 + b',"email":"person@example.com"}'
        )

        self.assertLessEqual(len(deeply_nested), MAX_REQUEST_BYTES)
        self.assertLessEqual(len(oversized_integer), MAX_REQUEST_BYTES)
        self.assertEqual(self.raw_request(deeply_nested), 400)
        self.assertEqual(self.raw_request(oversized_integer), 400)
        self.assertEqual(self.row_count(), 0)

    def test_concurrency_limit_rejects_before_persistence(self) -> None:
        server = create_server(
            "127.0.0.1",
            0,
            self.database_path,
            max_concurrent_requests=1,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(
            lambda: (server.shutdown(), server_thread.join(timeout=5))
        )
        server_url = f"http://127.0.0.1:{server.server_port}"
        persistence_started = threading.Event()
        release_persistence = threading.Event()

        def block_persistence(*_args: object) -> None:
            persistence_started.set()
            if not release_persistence.wait(timeout=5):
                raise TimeoutError("test did not release persistence")

        with patch("app.persist_submission", side_effect=block_persistence) as persist:
            with ThreadPoolExecutor(max_workers=1) as executor:
                accepted_request = executor.submit(
                    self.request,
                    {"name": "First", "email": "first@example.com"},
                    server_url,
                )
                self.assertTrue(persistence_started.wait(timeout=5))
                try:
                    status, response = self.request(
                        {"name": "Second", "email": "second@example.com"},
                        server_url,
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(response, {"error": "service_busy"})
                    persist.assert_called_once()
                finally:
                    release_persistence.set()

                self.assertEqual(accepted_request.result(timeout=5)[0], 201)

        self.assertEqual(self.row_count(), 0)

    def test_missing_fields_return_field_specific_validation_details(self) -> None:
        status, response = self.request({})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "validation_error")
        self.assertEqual(set(response["fields"]), {"name", "email"})
        self.assertEqual(self.row_count(), 0)

    def test_duplicate_pairs_are_persisted_as_distinct_rows(self) -> None:
        pair = {"name": "Same Person", "email": "same@example.com"}
        self.assertEqual(self.request(pair), (201, pair))
        self.assertEqual(self.request(pair), (201, pair))
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT id, name, email FROM contact_submissions ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        pairs = [(row[1], row[2]) for row in rows]
        self.assertEqual(pairs, [("Same Person", "same@example.com")] * 2)

    def test_success_response_is_sent_only_after_the_row_is_committed(self) -> None:
        observed_row_counts: list[int] = []
        original_send_json = ContactSubmissionHandler._send_json

        def observe_before_success_response(
            handler: ContactSubmissionHandler,
            status: int,
            payload: dict[str, object],
        ) -> None:
            if status == 201:
                observed_row_counts.append(self.row_count())
            original_send_json(handler, status, payload)

        with patch.object(
            ContactSubmissionHandler,
            "_send_json",
            observe_before_success_response,
        ):
            status, _ = self.request(
                {"name": "Committed", "email": "person@example.com"}
            )

        self.assertEqual(status, 201)
        self.assertEqual(observed_row_counts, [1])

    def test_capacity_is_three_successful_submissions(self) -> None:
        self.assertEqual(
            self.request({"name": "", "email": "person@example.com"})[0],
            400,
        )
        for index in range(3):
            self.assertEqual(
                self.request(
                    {"name": f"Person {index}", "email": "person@example.com"}
                )[0],
                201,
            )
        self.assertEqual(
            self.request({"name": "Fourth", "email": "person@example.com"})[0],
            409,
        )
        self.assertEqual(self.row_count(), 3)

    def test_concurrent_requests_commit_exactly_three_rows(self) -> None:
        def submit(index: int) -> int:
            return self.request(
                {"name": f"Concurrent {index}", "email": "person@example.com"}
            )[0]

        with ThreadPoolExecutor(max_workers=12) as executor:
            statuses = list(executor.map(submit, range(12)))
        self.assertEqual(statuses.count(201), 3)
        self.assertEqual(statuses.count(409), 9)
        self.assertEqual(self.row_count(), 3)

    def test_failed_insert_rolls_back_and_does_not_consume_capacity(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_test_submission
                    BEFORE INSERT ON contact_submissions
                    BEGIN
                        SELECT RAISE(ABORT, 'injected persistence failure');
                    END
                    """
                )
        status, response = self.request(
            {"name": "Failure", "email": "person@example.com"}
        )
        self.assertEqual(status, 500)
        self.assertEqual(
            response,
            {"error": "internal_error", "message": "Unable to process submission."},
        )
        self.assertEqual(self.row_count(), 0)

        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute("DROP TRIGGER reject_test_submission")
        self.assertEqual(
            self.request({"name": "Recovered", "email": "person@example.com"})[0],
            201,
        )
        self.assertEqual(self.row_count(), 1)

    def test_lost_success_response_still_consumes_capacity(self) -> None:
        class DropSuccessResponseHandler(ContactSubmissionHandler):
            database_path = self.database_path

            def _send_json(self, status: int, payload: dict[str, object]) -> None:
                if status == 201:
                    self.close_connection = True
                    self.connection.close()
                    return
                super()._send_json(status, payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), DropSuccessResponseHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=5)))

        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/contact-submissions",
            data=json.dumps(
                {"name": "Committed", "email": "person@example.com"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises((URLError, OSError)):
            urlopen(request, timeout=10)
        self.assertEqual(self.row_count(), 1)
        server.shutdown()
        thread.join(timeout=5)

        for index in range(2):
            self.assertEqual(
                self.request(
                    {"name": f"Later {index}", "email": "person@example.com"}
                )[0],
                201,
            )
        self.assertEqual(
            self.request({"name": "Fourth", "email": "person@example.com"})[0],
            409,
        )
        self.assertEqual(self.row_count(), 3)

    def test_wrong_media_type_and_unknown_route_are_rejected(self) -> None:
        self.assertEqual(self.raw_request(b"{}", "text/plain"), 415)
        request = Request(f"{self.base_url}/not-an-api", method="GET")
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.status, 404)
        self.assertEqual(self.row_count(), 0)


if __name__ == "__main__":
    unittest.main()
