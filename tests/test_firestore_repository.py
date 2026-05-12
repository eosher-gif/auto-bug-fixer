"""Tests for the REST-based FirestoreBugRepository.

We never hit the network — every test wires an ``httpx.Client`` to a
``MockTransport`` that pattern-matches on URL + method and returns the
exact JSON shape Firestore would. That keeps the tests honest about the
encode/decode work the repository does.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from auto_bug_fixer.config import Settings
from auto_bug_fixer.db.firestore_repository import (
    BugRepositoryError,
    FirestoreBugRepository,
)
from auto_bug_fixer.db.project_resolver import ProjectResolver
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry

_PROJECT_ID = "service-tickets-cb56a"
_API_KEY = "AIzaSyDKey-test"

_ARGAMAN = RegistryEntry(
    url="https://github.com/talya-debug/argaman-new",
    default_branch="master",
    language="javascript",
    test_command=None,
    description=None,
    framework="react",
    forbidden_paths=(),
    display_names=("ארגמן", "Argaman"),
)
_YOSEF = RegistryEntry(
    url="https://github.com/talya-debug/yishai-yosef",
    default_branch="main",
    language="javascript",
    test_command=None,
    description=None,
    framework="react",
    forbidden_paths=(),
    display_names=("ישי יוסף",),
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        anthropic_api_key="x",
        firebase_project_id=_PROJECT_ID,
        firebase_api_key=_API_KEY,
        github_token="t",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type, call-arg]


def _resolver() -> ProjectResolver:
    return ProjectResolver(RepoRegistry(entries=(_ARGAMAN, _YOSEF)))


# ---------------------------------------------------------------------- #
# Firestore typed-value helpers (test-side: build the raw REST payloads)
# ---------------------------------------------------------------------- #


def _doc(
    *,
    doc_id: str,
    project: str = "ארגמן",
    description: str = "הכפתור לא עובד",
    status: str = "open",
    ticket_type: str = "bug",
    email: str | None = "customer@example.com",
    name: str = "Customer",
    images: list[str] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "project": {"stringValue": project},
        "description": {"stringValue": description},
        "status": {"stringValue": status},
        "type": {"stringValue": ticket_type},
        "name": {"stringValue": name},
    }
    if email is None:
        fields["email"] = {"nullValue": None}
    else:
        fields["email"] = {"stringValue": email}
    if images is not None:
        fields["images"] = {
            "arrayValue": {"values": [{"stringValue": u} for u in images]}
        }
    return {
        "name": (
            f"projects/{_PROJECT_ID}/databases/(default)/"
            f"documents/tickets/{doc_id}"
        ),
        "fields": fields,
    }


def _run_query_response(*docs: dict[str, Any]) -> list[dict[str, Any]]:
    """Firestore wraps each hit in `{"document": ...}` and may include a tail."""
    return [{"document": d} for d in docs]


# ---------------------------------------------------------------------- #
# Mock transport — captures requests + serves canned responses
# ---------------------------------------------------------------------- #


class _Recorder:
    """Captures every outgoing request so tests can assert on them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)
        try:
            self.bodies.append(request.read())
        except Exception:  # noqa: BLE001
            self.bodies.append(None)


def _client_returning(payload: Any, *, status: int = 200) -> tuple[
    httpx.Client, _Recorder
]:
    """Build an httpx Client whose every request returns the same JSON."""
    rec = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), rec


def _client_routing(handler) -> tuple[httpx.Client, _Recorder]:
    """Lower-level variant: route by method/URL via a custom handler."""
    rec = _Recorder()

    def wrap(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrap)), rec


# ---------------------------------------------------------------------- #
# Tests — fetch_pending
# ---------------------------------------------------------------------- #


def test_fetch_pending_decodes_one_document_into_a_bug() -> None:
    payload = _run_query_response(_doc(doc_id="A", project="ארגמן"))
    client, rec = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)

    bugs = repo.fetch_pending(limit=10)

    assert len(bugs) == 1
    bug = bugs[0]
    assert bug.id == "A"
    assert bug.repo_url == _ARGAMAN.url
    assert bug.base_branch == _ARGAMAN.default_branch
    assert bug.description == "הכפתור לא עובד"
    assert bug.project_name == "ארגמן"

    # exactly one POST to runQuery, with the api key in the query string
    assert len(rec.requests) == 1
    parsed = urlparse(str(rec.requests[0].url))
    assert parsed.path.endswith(":runQuery")
    assert parse_qs(parsed.query)["key"] == [_API_KEY]


def test_fetch_pending_sends_correct_structured_query_body() -> None:
    payload = _run_query_response()
    client, rec = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)

    repo.fetch_pending(limit=5)

    import json as _json

    body = _json.loads(rec.requests[0].read())
    sq = body["structuredQuery"]
    assert sq["from"] == [{"collectionId": "tickets"}]
    assert sq["limit"] == 5
    composite = sq["where"]["compositeFilter"]
    assert composite["op"] == "AND"
    fields = sorted(
        f["fieldFilter"]["field"]["fieldPath"] for f in composite["filters"]
    )
    assert fields == ["status", "type"]
    values = sorted(
        f["fieldFilter"]["value"]["stringValue"] for f in composite["filters"]
    )
    assert values == ["bug", "open"]


def test_fetch_pending_skips_tickets_with_unknown_project() -> None:
    payload = _run_query_response(
        _doc(doc_id="A", project="ארגמן"),
        _doc(doc_id="B", project="פרויקט שלא קיים"),
    )
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)

    bugs = repo.fetch_pending(limit=10)
    assert [b.id for b in bugs] == ["A"]


def test_fetch_pending_skips_tickets_with_blank_description() -> None:
    payload = _run_query_response(_doc(doc_id="A", description="   "))
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    assert repo.fetch_pending(limit=10) == []


def test_fetch_pending_handles_empty_response() -> None:
    client, _ = _client_returning([])
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    assert repo.fetch_pending(limit=10) == []


def test_fetch_pending_handles_runquery_terminator_items() -> None:
    """Firestore can include an item with no `document` key (the read-time
    terminator). The repository must skip those gracefully."""
    payload = _run_query_response(_doc(doc_id="A")) + [{"readTime": "2026-01-01T00:00:00Z"}]
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    assert [b.id for b in repo.fetch_pending(limit=10)] == ["A"]


def test_fetch_pending_drops_type_filter_when_empty() -> None:
    settings = _settings(firestore_type_filter="")
    payload = _run_query_response(_doc(doc_id="A", ticket_type="dev"))
    client, rec = _client_returning(payload)
    repo = FirestoreBugRepository(settings, _resolver(), http_client=client)
    repo.fetch_pending(limit=10)
    body = rec.requests[0].read().decode("utf-8")
    assert "compositeFilter" not in body  # only one filter -> no AND wrapper
    assert '"type"' not in body or '"bug"' not in body


def test_fetch_pending_synthesizes_truncated_title_for_long_description() -> None:
    long = "א" * 200
    payload = _run_query_response(_doc(doc_id="A", description=long))
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    bug = repo.fetch_pending(limit=10)[0]
    assert bug.title.endswith("…")
    assert len(bug.title) <= 80


def test_fetch_pending_carries_metadata_through() -> None:
    payload = _run_query_response(
        _doc(
            doc_id="A",
            project="ישי יוסף",
            name="משה כהן",
            images=["https://firebasestorage.googleapis.com/img1.png"],
        )
    )
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    bug = repo.fetch_pending(limit=10)[0]
    assert bug.customer_name == "משה כהן"
    assert bug.project_name == "ישי יוסף"
    assert bug.image_urls == ("https://firebasestorage.googleapis.com/img1.png",)


def test_fetch_pending_handles_null_email() -> None:
    payload = _run_query_response(_doc(doc_id="A", email=None))
    client, _ = _client_returning(payload)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    bug = repo.fetch_pending(limit=10)[0]
    assert bug.reporter_email is None


# ---------------------------------------------------------------------- #
# Tests — write paths
# ---------------------------------------------------------------------- #


def test_mark_status_sends_patch_with_update_mask() -> None:
    client, rec = _client_returning({}, status=200)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)

    repo.mark_status("A", "processing")

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "PATCH"
    parsed = urlparse(str(req.url))
    assert parsed.path.endswith("/tickets/A")
    qs = parse_qs(parsed.query)
    assert qs["key"] == [_API_KEY]
    assert qs["updateMask.fieldPaths"] == ["status"]
    import json as _json

    body = _json.loads(req.read())
    assert body == {"fields": {"status": {"stringValue": "processing"}}}


def test_attach_pr_url_writes_pr_url_field_only() -> None:
    client, rec = _client_returning({}, status=200)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    repo.attach_pr_url("A", "https://github.com/x/y/pull/1")
    qs = parse_qs(urlparse(str(rec.requests[0].url)).query)
    assert qs["updateMask.fieldPaths"] == ["pr_url"]


def test_attach_ai_notes_writes_ai_notes_field_only() -> None:
    client, rec = _client_returning({}, status=200)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    repo.attach_ai_notes("A", "claude fixed the saving button")
    qs = parse_qs(urlparse(str(rec.requests[0].url)).query)
    assert qs["updateMask.fieldPaths"] == ["ai_notes"]
    body = rec.requests[0].read().decode("utf-8")
    assert "saving button" in body  # raw substring is enough


def test_drops_type_filter_query_has_a_single_field_filter() -> None:
    """When firestore_type_filter is empty we send a flat fieldFilter, not a composite."""
    settings = _settings(firestore_type_filter="")
    client, rec = _client_returning(_run_query_response())
    repo = FirestoreBugRepository(settings, _resolver(), http_client=client)
    repo.fetch_pending(limit=1)
    import json as _json

    body = _json.loads(rec.requests[0].read())
    where = body["structuredQuery"]["where"]
    assert "fieldFilter" in where
    assert where["fieldFilter"]["field"]["fieldPath"] == "status"


def test_doc_id_with_special_characters_is_url_encoded() -> None:
    client, rec = _client_returning({}, status=200)
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    repo.mark_status("with/slash", "failed")
    parsed = urlparse(str(rec.requests[0].url))
    # the slash must be encoded so the document id stays one path segment
    assert "/tickets/with%2Fslash" in parsed.path


# ---------------------------------------------------------------------- #
# Tests — error handling
# ---------------------------------------------------------------------- #


def test_runquery_4xx_response_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    with pytest.raises(BugRepositoryError, match="403"):
        repo.fetch_pending(limit=1)


def test_patch_4xx_response_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PATCH":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    with pytest.raises(BugRepositoryError, match="500"):
        repo.mark_status("A", "processing")


def test_network_error_is_wrapped() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    with pytest.raises(BugRepositoryError, match="POST failed"):
        repo.fetch_pending(limit=1)


def test_unexpected_payload_shape_raises() -> None:
    client, _ = _client_returning({"not": "a list"})
    repo = FirestoreBugRepository(_settings(), _resolver(), http_client=client)
    with pytest.raises(BugRepositoryError, match="unexpected runQuery payload"):
        repo.fetch_pending(limit=1)
