# Handoff: auto-bug-fixer — context for the next Claude session

**You are taking over a working production system.** Read this top-to-bottom
before touching anything. Everything below is verified true as of the last
commit on `main`.

---

## 1. What the project is

A fully-automated pipeline that:

1. **Polls** a Firebase Firestore `tickets` collection (Talya's customer
   ticket form at `https://tickets.talyaosher.com/?project=...`).
2. **Routes** each ticket to one of four target React storefronts
   (`talya-debug/argaman-new`, `yishai-yosef`, `yael_siso`,
   `lia-fine-jewelry`) by mapping the Hebrew `project` field to a repo
   via `repos.yaml`.
3. **Asks Claude** (`claude-sonnet-4-5-20250929`, anthropic SDK v0.43.0)
   to fix the bug inside a sandboxed clone of the target repo, using a
   pre-built per-repo "knowledge" index as system context.
4. **Opens a PR** against the target repo via the GitHub API (using
   Talya's PAT, not Eilon's).
5. **Updates** the Firestore ticket: `status=mr_opened`, `pr_url=...`,
   `ai_notes=...`.
6. **Sends a rich Hebrew HTML email** to the customer email on the
   ticket (`bug.reporter_email`) with the PR link, files changed, AI
   summary, and review steps.

The whole thing runs on **GitHub Actions free tier** — no servers, no
hosting bill, no always-on process.

Repo: <https://github.com/eosher-gif/auto-bug-fixer> (public).

---

## 2. Architecture map (where things live)

```
src/auto_bug_fixer/
├── cli.py                       # `daemon` / `run-once` / `index-once` entry points
├── config.py                    # Pydantic Settings — all env vars live here
├── models.py                    # Bug / FixOutcome / PullRequest dataclasses
├── pipeline.py                  # End-to-end orchestration (run_once + _process_one)
├── registry.py                  # Loader for repos.yaml -> RepoRegistry
├── logging_setup.py             # structlog
├── claude_agent/
│   └── agent.py                 # Tool-use loop (read_file / write_file / list_dir / run_cmd / finish)
├── db/
│   ├── firestore_repository.py  # Bug source — Firestore REST API (httpx, no admin SDK)
│   └── project_resolver.py      # Maps free-text Hebrew project name -> RegistryEntry
├── git_ops/
│   ├── repo.py                  # GitClient: clone / branch / commit / push (subprocess git)
│   └── github_api.py            # GitHubClient: open_pull_request via REST
├── indexer/
│   ├── repo_index.py            # RepoIndex dataclass + RepoIndexBuilder + to_prompt_block()
│   ├── runner.py                # IndexRunner: clone every repo in registry, build index, save
│   └── index_store.py           # IndexStore: read/write JSON files keyed by entry.slug
└── notify/
    └── email_sender.py          # EmailNotifier: SMTP, multipart/alternative, RICH HEBREW HTML

repos.yaml                       # Live registry (4 entries, all of Talya's repos)
indexes/                         # Committed knowledge artifacts: 1 .json per repo
.github/workflows/
├── bug-fixer.yml                # Main pipeline; cron commented OUT
├── reindex.yml                  # Daily at 04:00 UTC + push trigger on registry/indexer change
└── ci.yml                       # pytest on every push
cursor_helper/                   # Off-tree helper scripts (not shipped)
└── reset_test_ticket.py         # Resets Firestore test ticket back to status=open
└── create_test_ticket.py        # Creates a brand-new Hebrew test ticket
```

---

## 3. What we did this session (chronological, recent first)

| # | Commit | What |
|---|---|---|
| 8 | `f389827` | `feat(pipeline): log 'index_loaded' on success` — positive log line so we can verify the index actually reaches the agent. |
| 7 | `b19f808` | `ci(reindex): trigger on push to repos.yaml / indexer / workflow` — reindex now also fires on demand, not only on cron. |
| 6 | `c739707` | `feat(notify): rich Hebrew HTML email with full bug + PR context` — replaces the 5-line English email with a multipart RTL Hebrew page (banner, customer card, original ticket quote, AI summary, files, big CTA, next steps). |
| 5 | `df773ba` | `ci(bug-fixer): trigger on push to main so deploys auto-validate` — added `push:` trigger scoped to bug-fixer.yml + repos.yaml + requirements.txt + src/** (because we have no PAT with `actions:write` to call workflow_dispatch). |
| 4 | `e06f526` | `fix(workflows): unblock bug-fixer run on Firestore + httpx 0.28` — bumped `anthropic 0.39.0 -> 0.43.0` (proxies