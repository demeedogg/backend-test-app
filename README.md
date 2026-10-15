# backend-test-app

Minimal public HTTP service for the Details contact form. It stores submissions
in SQLite and accepts at most three successfully committed rows over the
service lifetime.

## Run locally

Requires Python 3.13.16. The service uses only the Python standard library, so
no dependency installation or database service is needed.

```sh
CONTACT_SUBMISSIONS_DB=./data/contact-submissions.sqlite PORT=3000 python3.13 server.py
```

Configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTACT_SUBMISSIONS_DB` | `/data/contact-submissions.sqlite` | Authoritative SQLite database file. Its parent directory is created at startup. |
| `PORT` | `3000` | HTTP listen port, from 1 through 65535. |
| `LOG_LEVEL` | `INFO` | Python logging level. Persistence failures include details only in server logs. |

The service accepts request bodies up to 16 KiB, handles at most 32 client
requests concurrently, and closes a client connection after 10 seconds without
socket activity. When all request slots are occupied, additional requests are
rejected with HTTP `503` and `{"error":"service_busy"}` before validation or
persistence runs. These finite transport limits keep malformed or stalled
clients from consuming unbounded request memory, processing, or worker threads.

The schema is initialized at service startup. Keep the configured database file
on writable persistent storage to preserve the service-lifetime count across
restarts. The deployment assumes one backend instance; all workers within that
instance share the SQLite file. SQLite's `BEGIN IMMEDIATE` transaction serializes
the capacity check and insert, including concurrent requests. There is no
expiry, reset, deletion, lookup API, or deduplication. Every committed row
counts, even if its HTTP response is lost.

## HTTP contract

### `POST /api/contact-submissions`

Public endpoint; no authentication or authorization is required. Send
`Content-Type: application/json` and a JSON object:

```json
{
  "name": "Alex Example",
  "email": "alex@example.com"
}
```

Both fields are required strings. The service trims leading and trailing
whitespace from both values before validation, storage, and response. A
normalized name must contain 1–100 Unicode characters. Email must be at most
254 characters and use conventional ASCII dot-atom local-part syntax with a
domain containing at least two valid DNS-style labels. Syntax validation is
local and does not perform DNS or deliverability checks. For example, spaces
around `" Alex "` and `" alex@example.com "` are removed; internal whitespace
in an email is invalid. These rules are enforced by the backend independently
of browser validation.

On a successful database commit, the service returns HTTP `201 Created` with
the normalized stored pair:

```json
{
  "name": "Alex Example",
  "email": "alex@example.com"
}
```

Errors are always unsuccessful responses:

| Status | Condition | Response |
| --- | --- | --- |
| `400 Bad Request` | Malformed UTF-8/JSON | `{"error":"invalid_json","message":"Request body must be valid UTF-8 JSON."}` |
| `400 Bad Request` | Non-object JSON body | `{"error":"validation_error","fields":{"request":"A JSON object is required."}}` |
| `400 Bad Request` | Missing, non-string, empty, or out-of-range fields | `{"error":"validation_error","fields":{"name":"Name must contain 1 to 100 characters."}}`; the `fields` object contains all invalid fields. Missing/non-string fields say `Name must be a string.` or `Email must be a string.`; invalid email syntax/length says `Enter a valid email address of at most 254 characters.` |
| `400 Bad Request` | Missing, invalid, or incomplete `Content-Length` | `{"error":"invalid_request","message":"Content-Length is required."}` (message distinguishes the invalid or incomplete cases) |
| `409 Conflict` | Three submissions have already committed | `{"error":"submission_limit_reached"}` |
| `503 Service Unavailable` | All 32 request slots are occupied | `{"error":"service_busy"}` |
| `413 Content Too Large` | Request body exceeds 16 KiB | `{"error":"request_too_large"}` |
| `415 Unsupported Media Type` | Request is not JSON | `{"error":"unsupported_media_type"}` |
| `500 Internal Server Error` | Persistence or other operational failure | `{"error":"internal_error","message":"Unable to process submission."}` |
| `404 Not Found` | Any other route | `{"error":"not_found"}` |

Invalid attempts and failed transactions do not consume a slot. Duplicate
name/email pairs are stored as separate rows and consume separate slots. The
count/check and insert run in one SQLite write transaction. The transaction
commits before the `201` response is constructed; an operational failure is
rolled back when possible and never returned as success.

## Tests

Run the hermetic server tests with a temporary SQLite file and no network or
external database:

```sh
python3.13 -m unittest discover -s tests -v
```

## Runtime image

Build without starting a container or requiring a frontend/database service:

```sh
docker build -t backend-test-app:persist-and-constrain-contact-submissions .
```

The image is based on the exact Python `3.13.16-slim-bookworm` OCI image index
digest `sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641`.
The application has no third-party dependencies and runs as UID/GID `10001`,
not root. The container listens on port `3000`; mount a writable persistent
volume at `/data` (or set `CONTACT_SUBMISSIONS_DB` to another writable path)
and retain the same database file for the lifetime of the service.
