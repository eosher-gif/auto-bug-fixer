"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: SecretStr
    anthropic_model: str = "claude-sonnet-4-5-20250929"
    claude_max_tool_iterations: int = Field(default=25, ge=1, le=200)
    claude_max_output_tokens: int = Field(default=8192, ge=512, le=64000)

    # ---------- Firestore (bug source) ----------
    # We talk to Firestore via the public REST API + the project's web API
    # key. Service-account keys are blocked by org policy on Talya's GCP
    # project, so the Admin SDK is unusable. Firestore Security Rules on
    # this project allow read/write/update with just the API key.
    #
    # Both fields are required.
    firebase_project_id: str = ""
    firebase_api_key: SecretStr = SecretStr("")

    # Override only for testing against the Firestore emulator.
    firestore_base_url: str = "https://firestore.googleapis.com/v1"

    # HTTP timeouts for every Firestore call.
    firestore_request_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)

    # Firestore collection containing tickets / bugs.
    firestore_collection: str = "tickets"

    # Field names inside each Firestore document.
    firestore_status_field: str = "status"
    firestore_type_field: str = "type"
    firestore_description_field: str = "description"
    firestore_email_field: str = "email"
    firestore_project_field: str = "project"
    firestore_customer_name_field: str = "name"
    firestore_images_field: str = "images"
    firestore_pr_url_field: str = "pr_url"
    firestore_ai_notes_field: str = "ai_notes"

    # Status state machine (values written back into the status field).
    bug_status_new: str = "open"
    bug_status_processing: str = "processing"
    bug_status_mr_opened: str = "mr_opened"
    bug_status_failed: str = "failed"

    # Only tickets whose `type` field matches this are picked up. Set to
    # empty string to disable the filter and process every ticket.
    firestore_type_filter: str = "bug"

    # Cap a single tick to this many tickets so a flood does not exhaust
    # the Anthropic budget in one go.
    max_bugs_per_run: int = Field(default=3, ge=1, le=50)

    # Daemon loop
    poll_interval_seconds: int = Field(default=30, ge=1, le=3600)
    idle_backoff_seconds: int = Field(default=60, ge=1, le=3600)
    error_backoff_seconds: int = Field(default=120, ge=1, le=3600)

    # GitHub
    github_token: SecretStr
    github_api_url: str = "https://api.github.com"
    git_committer_name: str = "auto-bug-fixer"
    git_committer_email: str = "[email protected]"

    # SMTP
    # Email is OPTIONAL. When email_enabled=False the system runs the full
    # bug -> Claude -> PR pipeline but skips sending the confirmation email,
    # so all SMTP_* fields below are unused and can be left blank.
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    notify_from: str = ""
    notify_cc: str = ""

    # Sandbox
    workspace_dir: Path = Path("/tmp/auto-bug-fixer")
    git_operation_timeout_seconds: int = Field(default=300, ge=10, le=3600)

    # Repo registry + indexing
    repos_file: Path = Path("repos.yaml")
    # Default points inside the repo so it works in GitHub Actions (the indexes
    # are committed back to the repo by the reindex workflow). On a VM you can
    # override to /var/auto-bug-fixer/index.
    index_dir: Path = Path("indexes")
    reindex_interval_hours: int = Field(default=24, ge=1, le=720)
    index_on_startup: bool = True

    # Health endpoint
    health_enabled: bool = True
    health_host: str = "0.0.0.0"
    health_port: int = Field(default=8080, ge=1, le=65535)
    health_stale_after_seconds: int = Field(default=900, ge=10, le=86400)

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    @model_validator(mode="after")
    def _require_firebase_credentials(self) -> "Settings":
        """Require both FIREBASE_PROJECT_ID and FIREBASE_API_KEY."""
        missing: list[str] = []
        if not self.firebase_project_id.strip():
            missing.append("FIREBASE_PROJECT_ID")
        if not self.firebase_api_key.get_secret_value().strip():
            missing.append("FIREBASE_API_KEY")
        if missing:
            raise ValueError(
                "Missing required Firebase config: " + ", ".join(missing)
            )
        return self

    @model_validator(mode="after")
    def _require_smtp_when_email_enabled(self) -> "Settings":
        """Fail fast at startup if email is on but SMTP fields are blank.

        Without this check, an EMAIL_ENABLED=true deployment would parse
        Settings successfully and only fail later inside _send() with an
        opaque EmailDeliveryError on the first bug.
        """
        if not self.email_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("smtp_host", self.smtp_host),
                ("smtp_username", self.smtp_username),
                ("smtp_password", self.smtp_password.get_secret_value()),
                ("notify_from", self.notify_from),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "EMAIL_ENABLED=true requires non-empty: " + ", ".join(missing)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]
