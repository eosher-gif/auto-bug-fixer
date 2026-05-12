"""One-shot helper: create a single safe test ticket in Talya's Firestore.

This writes ONE document to collection `tickets` and prints the new doc id.
Idempotency is handled by setting a stable `test_marker` field so we can
filter / clean up later if needed.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import httpx

PROJECT = "service-tickets-cb56a"
API_KEY = "AIzaSyDKeyW89Ruf44_DHo2yWzBhsixvXe3gNj0"

DESCRIPTION = (
    "בדיקת חיבור אוטומטית מהבוט. בבקשה הוסיפו שורת הערה בתחילת קובץ "
    "README.md שאומרת: \"Tested by auto-bug-fixer\". "
    "זו תקלה בדיקה — אפשר לסגור את ה-PR או למזג, שניהם בסדר."
)

PAYLOAD: dict = {
    "fields": {
        "name": {"stringValue": "Test - Auto Bug Fixer"},
        "email": {"stringValue": "talya@talyaosher.com"},
        "phone": {"stringValue": ""},
        "type": {"stringValue": "bug"},
        "project": {"stringValue": "ארגמן"},
        "description": {"stringValue": DESCRIPTION},
        "status": {"stringValue": "open"},
        "images": {"arrayValue": {"values": []}},
        "createdAt": {
            "timestampValue": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        },
        "test_marker": {"stringValue": "auto-bug-fixer-smoke-test"},
    }
}

URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
    f"/databases/(default)/documents/tickets"
)


def main() -> int:
    resp = httpx.post(URL, params={"key": API_KEY}, json=PAYLOAD, timeout=15)
    if resp.status_code >= 400:
        print(f"FAIL {resp.status_code}: {resp.text}")
        return 1
    body = resp.json()
    full_name = body.get("name", "")
    doc_id = full_name.rsplit("/", 1)[-1] if full_name else "?"
    print(f"created doc id: {doc_id}")
    print(f"full path     : {full_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
