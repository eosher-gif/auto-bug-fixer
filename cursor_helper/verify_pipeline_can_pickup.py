"""End-to-end live preview of what the workflow will process.

Mirrors what the bug-fixer GitHub Action will do at startup:
- load Settings from env vars (same names as the workflow)
- load repos.yaml (same file as the workflow)
- ask the FirestoreBugRepository for pending bugs
- print exactly what would be sent to Claude

This is READ ONLY — no Firestore writes, no git, no Claude.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# Stand-in env vars so Settings() validates. Real workflow secrets stay
# in GitHub; nothing sensitive is needed for a read-only preview.
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("GITHUB_TOKEN", "fake")
os.environ.setdefault("FIREBASE_PROJECT_ID", "service-tickets-cb56a")
os.environ.setdefault(
    "FIREBASE_API_KEY", "AIzaSyDKeyW89Ruf44_DHo2yWzBhsixvXe3gNj0"
)
os.environ.setdefault("REPOS_FILE", str(REPO_ROOT / "repos.yaml"))

from auto_bug_fixer.config import Settings  # noqa: E402
from auto_bug_fixer.db.firestore_repository import FirestoreBugRepository  # noqa: E402
from auto_bug_fixer.db.project_resolver import ProjectResolver  # noqa: E402
from auto_bug_fixer.registry import load_registry  # noqa: E402


def main() -> int:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    registry = load_registry(s.repos_file)
    repo = FirestoreBugRepository(s, ProjectResolver(registry))
    bugs = repo.fetch_pending(limit=s.max_bugs_per_run)

    print(f"\nWorkflow would process {len(bugs)} ticket(s):")
    if not bugs:
        print("  (nothing) — workflow would log idle and exit 0")
        return 0
    for b in bugs:
        print("---")
        print(f"  ticket id     : {b.id}")
        print(f"  type          : {b.ticket_type}")
        print(f"  project (raw) : {b.project_name}")
        print(f"  -> repo       : {b.repo_url}")
        print(f"  -> branch     : {b.base_branch}")
        print(f"  reporter mail : {b.reporter_email}")
        print(f"  customer name : {b.customer_name}")
        print(f"  title (synth) : {b.title}")
        print("  description   :")
        for line in b.description.splitlines():
            print(f"    | {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
