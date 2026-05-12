"""One-shot helper: reset a Firestore ticket back to ``status="open"``.

This is the operational counterpart to ``create_test_ticket.py``: after a
pipeline run flipped the test ticket to ``mr_opened``, run this script
to flip it back so the next run will pick it up again.

Idempotent: safe to run repeatedly. Only the ``status`` field is touched
(per Talya's policy that immutable customer fields must not change).
"""
from __future__ import annotations

import sys

import httpx

PROJECT = "service-tickets-cb56a"
API_KEY = "AIzaSyDKeyW89Ruf44_DHo2yWzBhsixvXe3gNj0"
TICKET_ID = "VjgrhaB9WIN4vmvKqVq3"

URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
    f"/databases/(default)/documents/tickets/{TICKET_ID}"
)
PAYLOAD: dict = {"fields": {"status": {"stringValue": "open"}}}


def main() -> int:
    resp = httpx.patch(
        URL,
        params={"key": API_KEY, "updateMask.fieldPaths": "status"},
        json=PAYLOAD,
        timeout=15,
    )
    if resp.status_code >= 400:
        print(f"FAIL {resp.status_code}: {resp.text}")
        return 1
    print(f"OK status reset to 'open' on ticket {TICKET_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
