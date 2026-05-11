"""Tests for GitHubClient using httpx MockTransport (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from auto_bug_fixer.git_ops.github_api import GitHubAPIError, GitHubClient
from auto_bug_fixer.git_ops.repo import RepoCoordinates


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    real_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return captured


def test_open_pull_request_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"number": 42, "html_url": "https://github.com/a/b/pull/42"},
        )

    captured = _patch_client(monkeypatch, handler)

    client = GitHubClient(token="tkn", api_url="https://api.github.com")
    pr = client.open_pull_request(
        RepoCoordinates(owner="a", name="b"),
        title="t",
        body="body",
        head_branch="h",
        base_branch="main",
    )

    assert pr.number == 42
    assert pr.url == "https://github.com/a/b/pull/42"
    assert pr.branch == "h"

    sent = captured[0]
    assert sent.method == "POST"
    assert sent.url.path == "/repos/a/b/pulls"
    assert sent.headers["Authorization"] == "Bearer tkn"
    assert json.loads(sent.content)["title"] == "t"


def test_open_pull_request_raises_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"message":"validation failed"}')

    _patch_client(monkeypatch, handler)
    client = GitHubClient(token="tkn", api_url="https://api.github.com")
    with pytest.raises(GitHubAPIError, match="HTTP 422"):
        client.open_pull_request(
            RepoCoordinates(owner="a", name="b"),
            title="t",
            body="b",
            head_branch="h",
            base_branch="main",
        )


def test_open_pull_request_retries_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise httpx.ConnectError("simulated")
        return httpx.Response(
            201, json={"number": 7, "html_url": "https://github.com/a/b/pull/7"}
        )

    _patch_client(monkeypatch, handler)
    client = GitHubClient(token="tkn", api_url="https://api.github.com")
    pr = client.open_pull_request(
        RepoCoordinates(owner="a", name="b"),
        title="t",
        body="b",
        head_branch="h",
        base_branch="main",
    )
    assert pr.number == 7
    assert call_count["n"] == 3
