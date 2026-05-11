"""Schema-agnostic repository for the customer-bug table.

The actual table/column names are loaded from configuration so the same code
works against any existing bug database without ORM model changes.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Engine, MetaData, Table, create_engine, select, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import NoSuchTableError

from auto_bug_fixer.config import Settings
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug

log = get_logger(__name__)


class BugRepositoryError(RuntimeError):
    """Raised when the bug table cannot be accessed or mapped correctly."""


class BugRepository:
    """Reads pending bugs and updates their status using runtime-discovered columns."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the repository and reflect the configured table.

        Args:
            settings: Application settings holding DB URL and column mapping.
        """
        self._settings = settings
        self._engine: Engine = create_engine(settings.database_url, future=True)
        self._table = self._reflect_table()
        self._validate_columns()

    def _reflect_table(self) -> Table:
        metadata = MetaData()
        try:
            return Table(
                self._settings.bug_table_name,
                metadata,
                autoload_with=self._engine,
            )
        except NoSuchTableError as exc:
            raise BugRepositoryError(
                f"Bug table '{self._settings.bug_table_name}' not found"
            ) from exc

    def _validate_columns(self) -> None:
        required = {
            self._settings.bug_id_column,
            self._settings.bug_title_column,
            self._settings.bug_description_column,
            self._settings.bug_status_column,
            self._settings.bug_repo_url_column,
            self._settings.bug_repo_branch_column,
            self._settings.bug_reporter_email_column,
        }
        present = {c.name for c in self._table.columns}
        missing = required - present
        if missing:
            raise BugRepositoryError(
                f"Bug table missing required columns: {sorted(missing)}"
            )

    def fetch_pending(self, limit: int) -> list[Bug]:
        """Fetch up to ``limit`` bugs whose status equals ``BUG_STATUS_NEW``."""
        s = self._settings
        status_col = self._table.c[s.bug_status_column]
        stmt = (
            select(self._table)
            .where(status_col == s.bug_status_new)
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows: Sequence[Row] = conn.execute(stmt).all()
        bugs = [self._row_to_bug(row) for row in rows]
        log.info("fetched_pending_bugs", count=len(bugs))
        return bugs

    def _row_to_bug(self, row: Row) -> Bug:
        s = self._settings
        mapping = row._mapping
        reporter_email_value = mapping[s.bug_reporter_email_column]
        return Bug(
            id=str(mapping[s.bug_id_column]),
            title=str(mapping[s.bug_title_column] or "").strip(),
            description=str(mapping[s.bug_description_column] or "").strip(),
            repo_url=str(mapping[s.bug_repo_url_column]).strip(),
            base_branch=str(mapping[s.bug_repo_branch_column] or "main").strip(),
            reporter_email=(
                str(reporter_email_value).strip() if reporter_email_value else None
            ),
        )

    def mark_status(self, bug_id: str, new_status: str) -> None:
        """Update the status column for ``bug_id``."""
        s = self._settings
        stmt = (
            update(self._table)
            .where(self._table.c[s.bug_id_column] == bug_id)
            .values({s.bug_status_column: new_status})
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        log.info(
            "bug_status_updated",
            bug_id=bug_id,
            new_status=new_status,
            rowcount=result.rowcount,
        )
