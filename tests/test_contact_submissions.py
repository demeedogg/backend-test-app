from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.contact_submissions import (
    ContactSubmissionHandler,
    ContactSubmissionServer,
    SubmissionStore,
)


@contextmanager
def running_api(
    database_path: str,
    before_commit: Any = None,
    handler_class: type[ContactSubmissionHandler] = ContactSubmissionHandler,
) -> Iterator[tuple[str, ContactSubmissionServer]]:
    store = SubmissionStore(database_path, before_commit=before_commit)
    server = ContactSubmissionServer(("127.0.0.1", 0), store, handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post_json(base_url: str, payload: Any, raw: bytes | None = None) -> tuple[int, dict[str, Any]]:
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}/api/contact-submissions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def stored_rows(database_path: str) -> list[tuple[int, str, str]]:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT id, name, email FROM contact_submissions ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def email_with_length(length: int) -> str:
    domain_length = length - 65
    labels = [63, 63, domain_length - 128]
    domain = ".".join("d" * label_length for label_length in labels)
    return f"{'a' * 64}@{domain}"


class ContactSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "submissions.sqlite")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_accepts_name_and_email_boundaries_and_returns_committed_pair(self) -> None:
        with running_api(self.database_path) as (base_url, _server):
            one_character_name = post_json(
                base_url,
                {"name": "A", "email": "person@example.com"},
            )
            hundred_character_name = post_json(
                base_url,
                {"name": "N" * 100, "email": email_with_length(254)},
            )

        self.assertEqual(one_character_name[0], 201)
        self.assertEqual(one_character_name[1]["name"], "A")
        self.assertEqual(hundred_character_name[0], 201)
        self.assertEqual(len(hundred_character_name[1]["email"]), 254)
        self.assertEqual(len(stored_rows(self.database_path)), 2)

    def test_trims_surrounding_whitespace_and_stores_same_pair_as_response(self) -> None:
        with running_api(self.database_path) as (base_url, _server):
            status, response = post_json(
                base_url,
                {"name": "  Ada Lovelace  ", "email": "  ADA@example.com  "},
            )

        self.assertEqual(status, 201)
        self.assertEqual(response["name"], "Ada Lovelace")
        self.assertEqual(response["email"], "ADA@example.com")
        self.assertEqual(stored_rows(self.database_path)[0][1:], ("Ada Lovelace", "ADA@example.com"))

    def test_duplicate_pairs_create_distinct_rows(self) -> None:
        pair = {"name": "Ada", "email": "ada@example.com"}
        with running_api(self.database_path) as (base_url, _server):
            responses = [post_json(base_url, pair) for _ in range(2)]

        self.assertEqual([response[0] for response in responses], [201, 201])
        self.assertEqual(len(stored_rows(self.database_path)), 2)
        self.assertNotEqual(responses[0][1]["id"], responses[1][1]["id"])

    def test_invalid_values_are_rejected_without_storage(self) -> None:
        invalid_payloads = [
            ({"email": "person@example.com"}, "name"),
            ({"name": "", "email": "person@example.com"}, "name"),
            ({"name": "N" * 101, "email": "person@example.com"}, "name"),
            ({"name": 7, "email": "person@example.com"}, "name"),
            ({"name": "Ada"}, "email"),
            ({"name": "Ada", "email": ""}, "email"),
            ({"name": "Ada", "email": "not-an-email"}, "email"),
            ({"name": "Ada", "email": "a" * 255}, "email"),
            ({"name": "Ada", "email": 12}, "email"),
            (["Ada", "person@example.com"], "body"),
        ]
        with running_api(self.database_path) as (base_url, _server):
            results = [post_json(base_url, payload) for payload, _field in invalid_payloads]
            malformed = post_json(base_url, None, raw=b"{not json")

        for (status, response), (_payload, field) in zip(results, invalid_payloads, strict=True):
            self.assertEqual(status, 400)
            self.assertEqual(response["error"], "validation_error")
            self.assertIn(field, response["details"])
        self.assertEqual(malformed[0], 400)
        self.assertEqual(malformed[1]["error"], "malformed_json")
        self.assertEqual(stored_rows(self.database_path), [])

    def test_request_body_size_is_bounded(self) -> None:
        with running_api(self.database_path) as (base_url, _server):
            status, response = post_json(base_url, None, raw=b"x" * (16 * 1024 + 1))

        self.assertEqual(status, 413)
        self.assertEqual(response["error"], "request_too_large")
        self.assertEqual(stored_rows(self.database_path), [])

    def test_invalid_attempt_leaves_all_three_slots_available_then_cap_rejects(self) -> None:
        with running_api(self.database_path) as (base_url, _server):
            invalid = post_json(base_url, {"name": "", "email": "bad"})
            accepted = [
                post_json(base_url, {"name": f"Person {index}", "email": f"p{index}@example.com"})
                for index in range(3)
            ]
            exhausted = post_json(
                base_url,
                {"name": "Fourth", "email": "fourth@example.com"},
            )

        self.assertEqual(invalid[0], 400)
        self.assertEqual([response[0] for response in accepted], [201, 201, 201])
        self.assertEqual(exhausted[0], 409)
        self.assertEqual(exhausted[1]["error"], "capacity_reached")
        self.assertEqual(len(stored_rows(self.database_path)), 3)

    def test_concurrent_requests_never_exceed_three_committed_rows(self) -> None:
        number_of_requests = 12
        barrier = threading.Barrier(number_of_requests)
        with running_api(self.database_path) as (base_url, _server):
            def submit(index: int) -> tuple[int, dict[str, Any]]:
                barrier.wait(timeout=10)
                return post_json(
                    base_url,
                    {"name": f"Concurrent {index}", "email": f"p{index}@example.com"},
                )

            with ThreadPoolExecutor(max_workers=number_of_requests) as executor:
                responses = list(executor.map(submit, range(number_of_requests)))

        self.assertEqual(sum(status == 201 for status, _body in responses), 3)
        self.assertEqual(sum(status == 409 for status, _body in responses), 9)
        self.assertEqual(len(stored_rows(self.database_path)), 3)

    def test_precommit_failure_rolls_back_and_does_not_use_capacity(self) -> None:
        failure_count = 0

        def fail_before_commit() -> None:
            nonlocal failure_count
            failure_count += 1
            if failure_count == 1:
                raise RuntimeError("injected commit failure details")

        with running_api(self.database_path, before_commit=fail_before_commit) as (base_url, _server):
            with self.assertLogs("src.contact_submissions", level="ERROR") as logs:
                failed = post_json(
                    base_url,
                    {"name": "Ada", "email": "ada@example.com"},
                )
            after_failure = stored_rows(self.database_path)
            accepted = post_json(
                base_url,
                {"name": "Ada", "email": "ada@example.com"},
            )

        self.assertEqual(failed[0], 500)
        self.assertEqual(failed[1]["error"], "persistence_failure")
        self.assertNotIn("injected commit", json.dumps(failed[1]))
        self.assertIn("injected commit failure details", "\n".join(logs.output))
        self.assertEqual(after_failure, [])
        self.assertEqual(accepted[0], 201)
        self.assertEqual(len(stored_rows(self.database_path)), 1)

    def test_unsupported_content_type_is_rejected_without_storage(self) -> None:
        with running_api(self.database_path) as (base_url, _server):
            request = Request(
                f"{base_url}/api/contact-submissions",
                data=b"{}",
                headers={"Content-Type": "text/plain"},
                method="POST",
            )
            try:
                urlopen(request, timeout=10)
            except HTTPError as error:
                status = error.code
                body = json.loads(error.read())

        self.assertEqual(status, 415)
        self.assertEqual(body["error"], "unsupported_media_type")
        self.assertEqual(stored_rows(self.database_path), [])

    def test_persisted_submission_uses_capacity_even_if_response_is_lost(self) -> None:
        class DropSuccessResponseHandler(ContactSubmissionHandler):
            response_dropped = False

            def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                if status == HTTPStatus.CREATED and not type(self).response_dropped:
                    type(self).response_dropped = True
                    self.connection.close()
                    return
                super().send_json(status, payload)

        with running_api(self.database_path, handler_class=DropSuccessResponseHandler) as (base_url, _server):
            with self.assertRaises((RemoteDisconnected, URLError)):
                post_json(base_url, {"name": "First", "email": "first@example.com"})
            self.assertEqual(len(stored_rows(self.database_path)), 1)
            later_successes = [
                post_json(base_url, {"name": f"Person {index}", "email": f"p{index}@example.com"})
                for index in range(2)
            ]
            exhausted = post_json(
                base_url,
                {"name": "Fourth", "email": "fourth@example.com"},
            )

        self.assertEqual([response[0] for response in later_successes], [201, 201])
        self.assertEqual(exhausted[0], 409)
        self.assertEqual(len(stored_rows(self.database_path)), 3)


if __name__ == "__main__":
    unittest.main()
