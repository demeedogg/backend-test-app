from __future__ import annotations

import logging
import os
from http.server import ThreadingHTTPServer

from src.contact_submissions import ContactSubmissionServer, SubmissionStore

DEFAULT_DATABASE_PATH = "/data/contact-submissions.sqlite"
DEFAULT_PORT = 8000


def read_port() -> int:
    raw_port = os.environ.get("PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("PORT must be an integer between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    return port


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database_path = os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH)
    port = read_port()
    store = SubmissionStore(database_path)
    server: ThreadingHTTPServer = ContactSubmissionServer(("0.0.0.0", port), store)
    logging.getLogger(__name__).info("Contact submission API listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
