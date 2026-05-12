"""Firestore-backed bug repository implemented on top of the REST API.

Why REST and not the Admin SDK?
- Talya's GCP project (``service-tickets-cb56a``) has an org policy that
  blocks creating service-account keys, so ``firebase-admin`` cannot
  authenticate from CI.
- Firestore Security Rules on the project allow read/write/update for
  anyone holding the project's web API key (delete is blocked). That is
  enough for our pipeline.
- REST keeps the dependency surface tiny — just ``httpx``, which we
  already use for GitHub.

If the security rules are tightened later, the cleanest upgrade is
Workload Identity Federation: auth in the workflow with
``google-github-actions/auth`` and reuse this same REST client with the
short-lived OAuth token in the ``Authorization`` header.

Source schema (one document per ticket in collection ``tickets``)::

    {
      "name":        "ישראל ישראלי",
      "email":       "israel@example.com",
      "phone":       "050-1234567",
      "type":        "bug",                 # or "dev"
      "project":     "ישי יוסף",            # set from URL query param
      "description": "הכפתור של שמירה ...",
      "status":      "open",                # state machine: open -> processing
                                            #              -> mr_opened / failed
      "images":      ["https://..."],       # Firebase Storage URLs
      "createdAt":   <Firestore Timestamp>,
    }
"""
from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

import httpx

from auto_bug_fixer.config import Settings
from auto_bug_fixer.db.project_resolver import ProjectResolver, UnknownProjectError
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug

log = get_logger(__name__)

_TITLE_FROM_DESCRIPTION_MAX = 80


class BugRepositoryError(RuntimeError):
    """Raised when Firestore is unreachable, returns an error, or has bad data."""


class _HttpClientLike(Protocol):
    """Minimal subset of ``httpx.Client`` we depend on (helps test injection)."""

    def request(
        self, method: str, url: str, *, params: Any = None, json: Any = None
    ) -> httpx.Response: ...


class FirestoreBugRepository:
    """Reads pending tickets and writes status / metadata back via REST."""

    def __init__(
        self,
        settings: Settings,
        project_resolver: ProjectResolver,
        *,
        http_client: _HttpClientLike | None = None,
    ) -> None:
        """Initialize the repository.

        Args:
            settings: Application settings — needs project id, api key,
                collection name, status state-machine and the field-name
                map.
            project_resolver: Maps the free-text ``project`` field on a
                ticket to a registered repo. Tickets pointing at an
                unknown project are skipped (and logged), not failed.
            http_client: Optional injected httpx-style client. Tests pass
                an ``httpx.Client`` wired to ``httpx.MockTransport``;
                production code lets the repository own its own client.
        """
        self._settings = settings
        self._resolver = project_resolver
        self._http: _HttpClientLike = http_client or httpx.Client(
            timeout=settings.firestore_request_timeout_seconds
        )

    def fetch_pending(self, limit: int) -> list[Bug]:
        """Return up to ``limit`` open tickets that resolve to a known repo.

        Server-side filters: ``status == bug_status_new`` AND (when
        ``firestore_type_filter`` is non-empty) ``type == filter``.
        Client-side filter: project-name resolution.
        """
        s = self._settings
        body = self._build_query(limit)
        url = f"{self._documents_url()}:runQuery"
        response_items = self._post(url, body)

        bugs: list[Bug] = []
        seen = 0
        for item in response_items:
            doc = item.get("document")
            if not doc:
                continue
            seen += 1
            bug = self._doc_to_bug(doc)
            if bug is not None:
                bugs.append(bug)
        log.info(
            "fetched_pending_bugs",
            fetched=seen,
            usable=len(bugs),
            collection=s.firestore_collection,
        )
        return bugs

    def mark_status(self, bug_id: str, new_status: str) -> None:
        """Update the ``status`` field on document ``bug_id``."""
        self._update_string_fields(
            bug_id, {self._settings.firestore_status_field: new_status}
        )
        log.info("bug_status_updated", bug_id=bug_id, new_status=new_status)

    def attach_pr_url(self, bug_id: str, pr_url: str) -> None:
        """Persist the URL of the opened pull request."""
        self._update_string_fields(
            bug_id, {self._settings.firestore_pr_url_field: pr_url}
        )

    def attach_ai_notes(self, bug_id: str, notes: str) -> None:
        """Persist a short human-readable summary of what Claude did."""
        self._update_string_fields(
            bug_id, {self._settings.firestore_ai_notes_field: notes}
        )

    # ------------------------------------------------------------------ #
    # Internals — URL building, request, encode/decode
    # ------------------------------------------------------------------ #

    def _documents_url(self) -> str:
        return (
            f"{self._settings.firestore_base_url}/projects/"
            f"{self._settings.firebase_project_id}/databases/(default)/documents"
        )

    def _document_url(self, doc_id: str) -> str:
        return (
            f"{self._documents_url()}/"
            f"{quote(self._settings.firestore_collection, safe='')}/"
            f"{quote(doc_id, safe='')}"
        )

    def _api_key_param(self) -> dict[str, str]:
        return {"key": self._settings.firebase_api_key.get_secret_value()}

    def _post(self, url: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = self._http.request(
                "POST", url, params=self._api_key_param(), json=body
            )
        except httpx.RequestError as exc:
            raise BugRepositoryError(f"Firestore POST failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BugRepositoryError(
                f"Firestore POST {resp.status_code}: {resp.text}"
            )
        payload = resp.json()
        if isinstance(payload, list):
            return payload
        raise BugRepositoryError(
            f"unexpected runQuery payload: {type(payload).__name__}"
        )

    def _update_string_fields(self, doc_id: str, fields: dict[str, str]) -> None:
        params = {**self._api_key_param()}
        # PATCH with updateMask so we only touch the fields we set —
        # everything else (description, name, email, ...) is preserved.
        params_list = [("key", params["key"])]
        for field in fields:
            params_list.append(("updateMask.fieldPaths", field))
        body = {
            "fields": {field: _encode_value(value) for field, value in fields.items()}
        }
        try:
            resp = self._http.request(
                "PATCH",
                self._document_url(doc_id),
                params=params_list,
                json=body,
            )
        except httpx.RequestError as exc:
            raise BugRepositoryError(
                f"Firestore PATCH for {doc_id} failed: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise BugRepositoryError(
                f"Firestore PATCH {resp.status_code} for {doc_id}: {resp.text}"
            )

    def _build_query(self, limit: int) -> dict[str, Any]:
        s = self._settings
        filters: list[dict[str, Any]] = [
            _equality_filter(s.firestore_status_field, s.bug_status_new)
        ]
        if s.firestore_type_filter:
            filters.append(
                _equality_filter(s.firestore_type_field, s.firestore_type_filter)
            )
        where: dict[str, Any]
        if len(filters) == 1:
            where = filters[0]
        else:
            where = {"compositeFilter": {"op": "AND", "filters": filters}}
        return {
            "structuredQuery": {
                "from": [{"collectionId": s.firestore_collection}],
                "where": where,
                "limit": limit,
            }
        }

    def _doc_to_bug(self, doc: dict[str, Any]) -> Bug | None:
        s = self._settings
        doc_id = _doc_id_from_name(doc.get("name", ""))
        if not doc_id:
            log.warning("ticket_skipped_no_id", name=doc.get("name"))
            return None
        data = _decode_fields(doc.get("fields") or {})

        project_name = data.get(s.firestore_project_field)
        try:
            entry = self._resolver.resolve(
                project_name if isinstance(project_name, str) else None
            )
        except UnknownProjectError as exc:
            log.warning(
                "ticket_skipped_unknown_project",
                bug_id=doc_id,
                project=project_name,
                reason=str(exc),
            )
            return None

        description_raw = data.get(s.firestore_description_field) or ""
        description = str(description_raw).strip()
        if not description:
            log.warning("ticket_skipped_no_description", bug_id=doc_id)
            return None

        return Bug(
            id=doc_id,
            title=_synthesize_title(description),
            description=description,
            repo_url=entry.url,
            base_branch=entry.default_branch,
            reporter_email=_optional_str(data.get(s.firestore_email_field)),
            ticket_type=str(data.get(s.firestore_type_field) or "bug"),
            customer_name=_optional_str(data.get(s.firestore_customer_name_field)),
            project_name=_optional_str(project_name),
            image_urls=_string_tuple(data.get(s.firestore_images_field)),
        )


# ---------------------------------------------------------------------- #
# Firestore typed-value codec.
#
# Firestore REST wraps every value in a one-key dict that names the type:
#   {"stringValue": "..."}    {"integerValue": "42"}
#   {"timestampValue": "..."} {"arrayValue": {"values": [...]}}
#   {"mapValue": {"fields": {...}}}
# We only need to *decode* into plain Python and *encode* strings.
# ---------------------------------------------------------------------- #


def _decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {name: _decode_value(typed) for name, typed in fields.items()}


def _decode_value(typed: dict[str, Any]) -> Any:
    if not isinstance(typed, dict) or not typed:
        return None
    key, value = next(iter(typed.items()))
    if key == "nullValue":
        return None
    if key == "stringValue":
        return value
    if key == "booleanValue":
        return bool(value)
    if key == "integerValue":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if key == "doubleValue":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if key == "timestampValue":
        return value  # ISO-8601 string; pipeline does not parse dates
    if key == "arrayValue":
        return [_decode_value(v) for v in (value or {}).get("values", [])]
    if key == "mapValue":
        return _decode_fields((value or {}).get("fields") or {})
    if key == "referenceValue":
        return value
    if key == "geoPointValue":
        return value
    if key == "bytesValue":
        return value
    return value


def _encode_value(value: Any) -> dict[str, Any]:
    """We only ever write strings back, but be safe about the int/bool case."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _equality_filter(field_path: str, value: str) -> dict[str, Any]:
    return {
        "fieldFilter": {
            "field": {"fieldPath": field_path},
            "op": "EQUAL",
            "value": {"stringValue": value},
        }
    }


def _doc_id_from_name(name: str) -> str:
    """Firestore returns ``projects/.../documents/tickets/<id>``; keep the tail."""
    if not name:
        return ""
    return name.rsplit("/", 1)[-1]


def _synthesize_title(description: str) -> str:
    first_line = description.splitlines()[0].strip()
    if len(first_line) <= _TITLE_FROM_DESCRIPTION_MAX:
        return first_line
    return first_line[: _TITLE_FROM_DESCRIPTION_MAX - 1].rstrip() + "…"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
