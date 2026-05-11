"""Tests for BugRepository using an in-memory SQLite database."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from auto_bug_fixer.config import Settings
from auto_bug_fixer.db.repository import BugRepository, BugRepositoryError


def _make_settings(database_url: str, table: str = "customer_bugs") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="x",
        database_url=database_url,
        bug_table_name=table,
        github_token="x",
        smtp_host="smtp.example",
        smtp_username="u",
        smtp_password="p",
        notify_from="[email protected]",
    )


@pytest.fixture
def populated_db(tmp_path) -> Iterator[str]:
    db_path = tmp_path / "bugs.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE customer_bugs (
                    id            TEXT PRIMARY KEY,
                    title         TEXT,
                    description   TEXT,
                    status        TEXT,
                    repo_url      TEXT,
                    base_branch   TEXT,
                    reporter_email TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO customer_bugs VALUES
                  ('B1','t1','d1','new','https://github.com/a/b','main','[email protected]'),
                  ('B2','t2','d2','new','https://github.com/c/d','main',NULL),
                  ('B3','t3','d3','done','https://github.com/e/f','main','[email protected]')
                """
            )
        )
    yield url
    engine.dispose()


def test_fetch_pending_returns_only_new(populated_db: str) -> None:
    repo = BugRepository(_make_settings(populated_db))
    bugs = repo.fetch_pending(limit=10)
    ids = sorted(b.id for b in bugs)
    assert ids == ["B1", "B2"]


def test_fetch_pending_respects_limit(populated_db: str) -> None:
    repo = BugRepository(_make_settings(populated_db))
    assert len(repo.fetch_pending(limit=1)) == 1


def test_reporter_email_optional(populated_db: str) -> None:
    repo = BugRepository(_make_settings(populated_db))
    bugs = {b.id: b for b in repo.fetch_pending(limit=10)}
    assert bugs["B1"].reporter_email == "[email protected]"
    assert bugs["B2"].reporter_email is None


def test_mark_status_updates_row(populated_db: str) -> None:
    repo = BugRepository(_make_settings(populated_db))
    repo.mark_status("B1", "processing")
    engine = create_engine(populated_db, future=True)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status FROM customer_bugs WHERE id='B1'")).one()
    assert row[0] == "processing"
    assert len(repo.fetch_pending(limit=10)) == 1


def test_missing_table_raises(tmp_path) -> None:
    db = tmp_path / "empty.db"
    create_engine(f"sqlite:///{db.as_posix()}", future=True).dispose()
    with pytest.raises(BugRepositoryError, match="not found"):
        BugRepository(_make_settings(f"sqlite:///{db.as_posix()}"))


def test_missing_columns_raises(tmp_path) -> None:
    db = tmp_path / "bad.db"
    url = f"sqlite:///{db.as_posix()}"
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customer_bugs (id TEXT, title TEXT)"))
    with pytest.raises(BugRepositoryError, match="missing required columns"):
        BugRepository(_make_settings(url))
