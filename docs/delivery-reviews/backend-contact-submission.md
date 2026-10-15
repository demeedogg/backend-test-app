# Delivery review: backend-contact-submission

## Reviewed candidate

- **Delivery unit:** `backend-contact-submission` — Persist and constrain contact submissions
- **Repository:** `https://github.com/demeedogg/backend-test-app.git`
- **Base commit:** `e32f25d9ddb0612877f778f00e2de22bf0e1107c`
- **Supplied Candidate head:** `89e6809d8d22ba13ce9ea1a707996fe171dbd130`
- **Review correction commits:** `debeb6a1ae0865ede4e2cff12e2d000d18be4802`, `99ec6655dd15ea17d8fb959b31baf139e8d325dd`
- **Code state reviewed:** `99ec6655dd15ea17d8fb959b31baf139e8d325dd` (the review document is committed separately)
- **Agent Content release:** `agent-content-v8` at `033164001ac9d720166c6697b8dd2daa160ee97f`

## Scope and authoritative evidence

Reviewed the complete backend change from the base commit through the supplied
Candidate head, the review corrections, the working tree, the HTTP handler,
validation, SQLite persistence, tests, README contract, Dockerfile, and image
manifest. Only the assigned Backend Repository was changed or verified.

The binding product scope is PRD `REQ-004`–`REQ-011`; the executable Task is
`context/tasks/persist-and-constrain-contact-submissions.md`. The PRD and Task
define the behavior. The backend, frontend, and requirements analyses in
`context/requirements-review/` and the backend repository analysis in
`context/analysis/backend-test-app-7cad18d3.md` were treated as supporting
evidence. No separate API specification was supplied and the Task lists no
OpenAPI operations. The backend Requirements Review is not an API contract.
The README documents the Task's first-party API contract for frontend
consumption: public `POST /api/contact-submissions`, JSON `name`/`email`,
`201` only after commit, field validation details, and unsuccessful capacity,
operational, and request-limit outcomes.

The Task has `design_references: []`. The indexed design description and
render inventory were checked for scope; all listed renders describe frontend
views and there is no backend visual requirement to compare. The capture
manifest and render files named by `design/source.json` are not present in the
supplied context, so no render was treated as backend acceptance evidence.

## Requirement and Task review

- **REQ-004 / REQ-008:** The handler accepts the pair at the documented route,
  stores both fields together, and sends `201` with the normalized pair only
  after SQLite commit. A test observes storage before the success response.
- **REQ-005:** Backend validation rejects missing/non-string values, names
  outside 1–100 characters, malformed email, and email longer than 254
  characters. Whitespace trimming, email syntax, and the same accepted pair in
  storage and response are documented and tested.
- **REQ-006:** Each accepted request creates a row with an independent identity;
  duplicate pairs are not deduplicated and consume separate slots.
- **REQ-007:** `BEGIN IMMEDIATE` serializes count/check/insert against the
  authoritative SQLite file. The cap is three committed rows; invalid requests
  and rolled-back writes do not count. Concurrent requests and the lost-response
  case are covered. The README documents the configured persistent file and
  single-backend-instance deployment assumption.
- **REQ-009:** Validation, exhausted capacity, oversized input, overload, and
  persistence failures return unsuccessful outcomes. Persistence details are
  logged server-side; the operational response does not expose internals.
- **REQ-010 / REQ-011:** The single submission route is public and adds no
  authentication, authorization, external APIs, email delivery, analytics, or
  third-party integration. No listing, lookup, or extra user-facing page was
  introduced.
- **Task constraints:** Work is confined to the Backend Repository. The Task
  explicitly selects Python 3.13 and SQLite, initializes storage at startup,
  stores the database at a configurable path, uses a pinned non-root runtime
  image, and keeps image build independent of the frontend and a running
  database. The backend skill's Node/Express reference was read; its packaging
  and trust-boundary criteria were applied while retaining the Task's explicit
  Python/SQLite implementation choice.

## Defects found and corrected

1. The original `ThreadingHTTPServer` could create an unbounded number of
   request threads and had no socket timeout. Replaced it with bounded handling
   (32 active requests), a 10-second socket-operation timeout, and deliberate
   JSON `503 service_busy` rejection before request validation or persistence.
   Added tests proving overload does not invoke persistence and an idle client
   times out and releases its worker slot.
2. A body within the 16 KiB limit containing deeply nested JSON or an integer
   beyond Python's configured conversion limit could raise outside the normal
   client-error path. These parser failures now return `400 invalid_json`.
   Added focused regression cases.
3. The existing 16 KiB request-body limit lacked a focused boundary assertion.
   Added a test proving a larger body is rejected with `413` before validation
   or persistence runs.

## Verification

- `python3.13 -m unittest discover -s tests -v` — **passed**, 14 tests. The
  suite covers validation boundaries, malformed and complex JSON, persistence,
  duplicates, commit-before-success, rollback, lost responses, capacity,
  concurrent cap enforcement, oversized body rejection before application
  behavior, bounded-overload rejection, and idle-client timeout. Tests used
  temporary SQLite files and local test HTTP servers; no database service or
  other application repository was used. The local interpreter reported
  Python 3.13.5.
- `docker build --pull=false --network=none -t
  backend-test-app:persist-and-constrain-contact-submissions .` — **passed**
  from this repository using the cached pinned base image; no container was
  started and Docker networking was disabled. The image resolves to
  `sha256:ff2279f62fa939fb67d9bf029d85e33d1d90bd226ac8017f2490243f974df419`,
  uses UID/GID `10001:10001`, and starts `python server.py` from `/app`.
- `build-manifest.json` matches the built image name and tag:
  `backend-test-app:persist-and-constrain-contact-submissions`.
- `git diff --check e32f25d9ddb0612877f778f00e2de22bf0e1107c..99ec6655dd15ea17d8fb959b31baf139e8d325dd` — **passed**.

The corrected backend candidate satisfies the listed backend requirements and
Task acceptance criteria.
