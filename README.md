# auto-bug-fixer

Always-on daemon that:

1. Polls a customer-bug database for new bugs.
2. Looks up a **pre-built repo index** so Claude starts with full knowledge of
   the target codebase (structure, language, test command, README, key files).
3. Runs Claude inside a sandboxed clone of the repo to investigate and fix.
4. Opens a GitHub pull request with the patch.
5. Emails the reporter for confirmation.
6. Re-indexes every repo periodically so the knowledge stays fresh.
7. Exposes an HTTP `/health` + `/ready` endpoint for liveness monitoring.

```
                          +-----------------+
                          | repos.yaml      |
                          | (registry)      |
                          +--------+--------+
                                   |
                                   v
                  +-------- IndexRunner -----------+
                  | clone -> RepoIndexBuilder ->   |
                  | IndexStore (JSON per repo)     |
                  +--------------------------------+
                                   |
+------------+   +-----------------v------------+   +-----------------+
| Claude API |<--+   BugFixPipeline.run_once    +-->|  Git CLI + PR   |
+------------+   +---+--------------------+-----+   +--------+--------+
                     ^                    |                  |
                     |                    v                  v
                +----+----+       +-------+--------+   +-----+--------+
                | repo    |       |  SMTP mail     |   |  GitHub PR   |
                | index   |       +----------------+   +--------------+
                +---------+

           BugFixDaemon ticks pipeline + reindexes + updates HealthState
           HealthServer (/health, /ready) reads HealthState
```

## CLI

```bash
python -m auto_bug_fixer daemon       # default: long-running loop
python -m auto_bug_fixer index-once   # build index for every repo, then exit
python -m auto_bug_fixer run-once     # process one batch of bugs, then exit
```

## Repository registry — `repos.yaml`

Every repo the system is responsible for must appear in this file. The daemon
indexes them on startup (if `INDEX_ON_STARTUP=true`) and again every
`REINDEX_INTERVAL_HOURS`.

```yaml
repos:
  - url: https://github.com/acme/widgets
    default_branch: main
    language: python              # optional override; auto-detected if omitted
    test_command: pytest -q       # optional override
    description: |                # optional, fed straight to Claude
      Customer-facing widgets backend.
```

The index for each repo is a small JSON file (a few KB) containing:

- Detected language + suggested test command
- A depth-limited directory tree
- Top-level "key files" (`README.md`, `pyproject.toml`, `package.json`, etc.)
- README excerpt (first ~6 KB)
- Your `description` field

When a bug arrives, the matching index is **injected verbatim into Claude's
first message**, so it knows the layout before reading anything.

## Status state-machine

```
new -> processing -> mr_opened
                 \-> failed
```

Names are configurable (`BUG_STATUS_*`). Status transitions are the only state
the daemon mutates in your DB.

## Repository layout

```
src/auto_bug_fixer/
  cli.py                    # argparse entry: daemon | index-once | run-once
  __main__.py               # `python -m auto_bug_fixer ...`
  config.py                 # pydantic-settings, all env vars
  logging_setup.py          # structlog JSON logs
  models.py                 # Bug, FixOutcome, PullRequest dataclasses
  daemon.py                 # always-on loop + signal handling + reindex
  pipeline.py               # one-tick orchestration
  health.py                 # /health + /ready HTTP server (stdlib only)
  registry.py               # repos.yaml loader/validator
  db/repository.py          # schema-agnostic SQLAlchemy reads + status updates
  claude_agent/
    tools.py                # sandboxed list_dir/read_file/write_file/run_cmd
    agent.py                # Anthropic tool-use loop, finishes when Claude calls `finish`
  git_ops/
    repo.py                 # git clone/branch/commit/push
    github_api.py           # GitHub REST: open PR
  indexer/
    repo_index.py           # walks a repo, builds compact index
    index_store.py          # JSON persistence
    runner.py               # iterates registry, indexes each entry
  notify/email_sender.py    # SMTP success/failure mail

tests/                      # 60+ tests, ~80% coverage gate in CI
.github/workflows/
  ci.yml                    # runs full pytest + coverage on every push
  poll.yml                  # MANUAL one-shot only (no cron)
Dockerfile                  # slim Python 3.12, non-root, tini PID1
docker-compose.yml          # restart: unless-stopped, persistent workspace
fly.toml                    # Fly.io deployment manifest
pytest.ini                  # strict markers, coverage configured
requirements.txt            # runtime deps
requirements-dev.txt        # adds pytest + aiosmtpd + respx
```

## Health endpoint

The daemon starts an HTTP server on `HEALTH_PORT` (default `8080`):

| Path     | 200 when                                     | 503 when                                |
| -------- | -------------------------------------------- | --------------------------------------- |
| `/ready` | At least one tick has completed              | Daemon hasn't ticked yet                |
| `/health`| Last successful tick is within `HEALTH_STALE_AFTER_SECONDS` | DB/Claude/GitHub broken or stalled |

Body example:

```json
{
  "healthy": true,
  "uptime_seconds": 3712.4,
  "last_tick_at": 1736591102.1,
  "last_success_at": 1736591102.1,
  "last_error": null,
  "last_handled_count": 2,
  "total_ticks": 124,
  "total_handled": 47
}
```

## Test suite

Run locally:

```bash
python -m venv venv
source venv/Scripts/activate
pip install -r requirements-dev.txt
pytest                                          # ~60 tests
pytest --cov=auto_bug_fixer --cov-report=term   # with coverage
```

The CI workflow enforces **>=80% coverage** on every push.

What is covered:

| Module / behavior            | Test file                       |
| ---------------------------- | ------------------------------- |
| Sandboxed Claude tools       | `test_sandbox_tools.py`         |
| Daemon loop + backoff + stress | `test_daemon.py`              |
| `repos.yaml` parser          | `test_registry.py`              |
| Repo indexer (synth tree)    | `test_indexer.py`               |
| Index store (roundtrip + corruption) | `test_index_store.py`   |
| IndexRunner orchestration    | `test_indexer_runner.py`        |
| `BugRepository` (sqlite)     | `test_repository.py`            |
| `EmailNotifier` (in-process SMTP) | `test_email_sender.py`     |
| `GitHubClient` (httpx mock)  | `test_github_api.py`            |
| Full pipeline integration    | `test_pipeline_integration.py`  |
| `/health` + `/ready` server  | `test_health.py`                |
| `Settings` env loading       | `test_config.py`                |

## Deployment

The default runtime is **GitHub Actions** — completely free, no credit card, no infra.

| Workflow | When | What |
|---|---|---|
| `.github/workflows/bug-fixer.yml` | every 15 min | poll bug DB, run Claude, open PRs, email |
| `.github/workflows/reindex.yml` | daily 04:00 UTC | regenerate `indexes/*.json`, commit to repo |

Steps:
1. Push this code to a new GitHub repo
2. Edit `repos.yaml` with your real repo list
3. Add Secrets + Variables in **Settings → Secrets and variables → Actions**
4. Manually trigger `reindex` once (Actions tab → Run workflow)
5. Manually trigger `bug-fixer` once to verify (or wait for the cron)

Full walkthrough with the exact list of secrets/variables: **[`deploy/README.md`](deploy/README.md)**.

For sub-minute polling or the `/health` HTTP endpoint, the same code runs as
a Docker daemon on a free VM (Oracle Cloud Always Free / GCP e2-micro) — see
the same deploy guide.

## Required env vars / secrets

See `.env.example` for the full list with comments. The bare-minimum required
fields are listed in the project chat as **Sections A–E**.

## Safety boundaries

- `SandboxedFileTools` rejects `..` and absolute paths; covered by tests.
- `run_cmd` only allows a small allowlist of read-only / test prefixes.
- The GitHub token is injected at push time only; never written to disk or logs.
- If the agent finishes without writing files, the bug is auto-marked `failed`.
- Pipeline errors mark the bug `failed` and notify the reporter.
- Daemon never hot-loops on crashes — three-tier exponential backoff.
