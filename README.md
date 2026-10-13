# backend-test-app

A small public contact-submission API implemented with Python 3.13's standard
library HTTP server and SQLite. It stores at most three successful submissions
for the lifetime of the configured database file.

## Run locally

Use Python 3.13.5, create a virtual environment, and install the exact pinned
application dependencies:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
set -a
. ./.env
set +a
.venv/bin/python -m src.server
```

The service listens on `0.0.0.0:8000` by default. It reads `PORT` and
`DATABASE_PATH` directly from the process environment. `.env` is a local
operator convenience; the server does not read dotenv files itself. Tests use
temporary database files and do not start a database service or access the
network:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Contact submission contract

### `POST /api/contact-submissions`

This endpoint is public and requires no authentication or authorization. Send
`Content-Type: application/json` and a JSON object containing both string
fields:

```json
{
  "name": "Ada Lovelace",
  "email": "ada@example.com"
}
```

The body is limited to 16 KiB. The endpoint ignores unknown object fields and
rejects a non-object JSON body.

The service trims surrounding whitespace from `name` and `email` before
validation and storage. It preserves internal name whitespace. Names must have
1–100 Unicode characters after trimming. Emails must have 1–254 characters
after trimming and pass `email-validator` 2.2.0 syntax validation with
deliverability/DNS checks disabled. The validator's normalized email value is
stored and returned; the normalized value must also be no longer than 254
characters. No DNS lookup, email sending, or other external service is used.

Success returns HTTP `201 Created` only after SQLite commits the full pair:

```json
{
  "id": 1,
  "name": "Ada Lovelace",
  "email": "ada@example.com"
}
```

Each accepted request inserts a new row and receives a generated identity.
Duplicate name/email pairs are separate submissions and consume separate
capacity. The service serializes each capacity check and insert using a
SQLite `BEGIN IMMEDIATE` write transaction. The transaction counts existing
rows, inserts only when fewer than three exist, and commits before returning
success. Invalid submissions and rolled-back writes do not use a slot. A
response lost after commit does not undo the row or return its capacity.

### Unsuccessful responses

- `400 Bad Request` for malformed JSON or invalid fields. Validation responses
  include field-specific details where possible:

  ```json
  {
    "error": "validation_error",
    "details": {
      "name": "Name must contain 1 to 100 characters.",
      "email": "Enter a valid email address."
    }
  }
  ```

  Malformed UTF-8/JSON returns `{"error":"malformed_json","message":"..."}`.
- `409 Conflict` after three successful rows exist:

  ```json
  {"error":"capacity_reached","message":"No submission capacity remains."}
  ```

- `415 Unsupported Media Type` unless the content type is `application/json`.
- `413 Request Entity Too Large` for a body over 16 KiB.
- `500 Internal Server Error` for persistence failures. The response is
  `{"error":"persistence_failure","message":"Unable to submit information."}`
  and does not expose internal error details.

Every unsuccessful result remains unsuccessful; this API has no listing or
lookup endpoint.

## Persistence and runtime configuration

`DATABASE_PATH` defaults to `/data/contact-submissions.sqlite`. The service
creates the parent directory and schema at startup; the `contact_submissions`
table stores a generated integer identity, the complete name/email pair, and a
UTC creation timestamp. There is no unique constraint on the pair, expiry, or
reset. The database file is the authoritative record of service-lifetime
capacity. Keep the same file on writable persistent storage across restarts to
preserve the count. The deployment assumes one backend service instance using
that persistent file. The container process runs as UID/GID `10001`, so a
mounted data directory must be writable by that user.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | HTTP listen port, from 1 through 65535 |
| `DATABASE_PATH` | `/data/contact-submissions.sqlite` | Authoritative SQLite file path |

Copy `.env.example` to `.env` for local configuration. Do not put `.env` in a
container image or commit secrets.

## Runtime image

The standalone image uses
`python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419`,
runs as a non-root user, and needs only this repository and package-index
access during image build. The build does not start the service, require a
database, or use the frontend. Build it from this directory with:

```sh
docker build -t backend-test-app:task-persist-and-constrain-contact-submissions .
```

The production command is `python -m src.server`. The image sets `PORT=8000`
and `DATABASE_PATH=/data/contact-submissions.sqlite`; override either at
runtime as needed. Mount persistent writable storage at `/data` to retain
submissions and the lifetime limit.
