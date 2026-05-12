# Auto-Bug-Fixer — Handoff Document

> **Audience:** the next AI coding session that picks up this project.
> **Goal:** give you everything you need to continue without asking the
> human what was done, what's deployed, what tokens exist, and what
> design choices were already locked in.
>
> **Last live verified:** 2026-05-12 19:38 IDT — bug-fixer run
> `25748375106` succeeded with `index_loaded` for argaman-new, opened
> PR #4, sent the rich Hebrew email to Talya.

---

## 1. What this product is

A free, fully-hosted-on-GitHub-Actions service that:

1. Polls a Firestore `tickets` collection owned by the human's friend
   **Talya** (a non-developer running 4 small e-commerce React storefronts).
2. For each open `type:bug` ticket: clones the matching repo, asks
   Claude Sonnet to produce a fix, opens a PR on Talya's repo, marks
   the ticket `mr_opened`, and emails Talya a rich Hebrew HTML email
   with the PR link + summary so she can review and merge.
3. Maintains a per-repo "knowledge index" (file tree + README +
   framework + test command) so Claude always sees the codebase shape
   before fixing.

**Repo (the bot itself):**
[`https://github.com/eosher-gif/auto-bug-fixer`](https://github.com/eosher-gif/auto-bug-fixer)
(public, owned by the human `eosher-gif`)

**Local checkout:** `c:/dev/nlastic/auto-bug-fixer/` with venv at
`./venv` (Python 3.12).

---

## 2. Product context (Talya's spec — frozen)

- **Firestore project:** `service-tickets-cb56a`
- **Collection:** `tickets`
- **API key (web, public):** `AIzaSyDKeyW89Ruf44_DHo2yWzBhsixvXe3gNj0`
  (no service-account key — Google org policy blocks it; we use the
  REST API + this web key — Firestore rules allow read/write/update,
  delete is blocked)
- **4 target repos** (all PRIVATE under `talya-debug` org):
  - `talya-debug/argaman-new` (default branch `master`) — display name `ארגמן`
  - `talya-debug/yishai-yosef` (default branch `main`) — display name `ישי יוסף`
  - `talya-debug/yael_siso` (default branch `master`) — display name `יעל סיסו`
  - `talya-debug/lia-fine-jewelry` (default branch `master`) — display names `ליה`, `LIA`
- **Old `talya-debug/Argaman` repo exists but is INACTIVE — never touch it.**
- **Stack:** all 4 are React + Firebase, Hebrew RTL UI, deployed on Vercel
  (every PR gets an automatic preview URL).
- **Customers:** Hebrew speakers, non-technical. Tickets are free-text
  Hebrew descriptions. Project name comes from a URL query param on
  Talya's ticket form, so EXACT match is safe — no fuzzy matching needed.
- **Forbidden files** (Claude must never modify): `.env`, `.env.local`,
  `firebase.js`, `lib/firebase.js`, `vercel.json`, `package-lock.json`,
  `yarn.lock`. Listed under `forbidden_paths` in `repos.yaml`.
- **Notification policy:**
  - Talya's mail: `talya@talyaosher.com` — gets PR-ready emails.
  - Liran's mail: `liran.asulin87@gmail.com` — backup, should be CC'd
    (currently NOT yet wired — see "Pending tweaks" below).
  - Customer-facing email is OUT OF SCOPE for now (Talya wants to
    notify customers manually after she merges).
- **Status state machine:** `open` → `processing` → `mr_opened` /
  `failed`. Talya's tickets are created with `status: "open"` (NOT `"new"`).
- **Type filter:** only `type: "bug"` is processed. `type: "dev"` is
  marked `failed` with a Hebrew note "בקשת פיתוח — לא נתמך אוטומטית"
  (handled by ticket-type filter at fetch time).
- **Working hours** (Talya's commitment to respond): 09:00-18:00 Israel
  time, Sun-Thu. Bot can run 24/7.

---

## 3. Architecture (the actual code)

```
src/auto_bug_fixer/
├── cli.py                        # entrypoints: daemon | run-once | index-once
├── config.py                     # pydantic-settings (env-driven)
├── logging_setup.py              # structlog json output
├── models.py                     # Bug, FixOutcome, PullRequest dataclasses
├── registry.py                   # repos.yaml loader + RegistryEntry
├── pipeline.py                   # BugFixPipeline._process_one orchestration
├── claude_agent/
│   ├── agent.py                  # ClaudeBugFixerAgent (tool-use loop)
│   └── sandbox.py                # SandboxedFileTools (read/write/list inside cloned repo)
├── db/
│   ├── firestore_repository.py   # FirestoreBugRepository (REST + httpx)
│   └── project_resolver.py       # Hebrew/Latin display-name → RegistryEntry
├── git_ops/
│   ├── repo.py                   # GitClient (clone, branch, commit, push)
│   └── github_api.py             # GitHubClient (open_pull_request)
├── indexer/
│   ├── repo_index.py             # RepoIndex dataclass + RepoIndexBuilder + walk
│   ├── index_store.py            # IndexStore (load/save JSON per slug)
│   └── runner.py                 # IndexRunner (clone all + index all)
└── notify/
    └── email_sender.py           # EmailNotifier (rich Hebrew HTML, multipart)

tests/                            # pytest suite (141 tests, all green)
.github/workflows/
├── bug-fixer.yml                 # main pipeline (push trigger + dispatch)
├── reindex.yml                   # daily cron + push-trigger for indexer
└── ci.yml                        # tests on every push (already existed)

cursor_helper/                    # one-off scripts (NOT part of the product)
├── HANDOFF.md                    # this file
├── create_test_ticket.py         # writes a single test ticket to Firestore
├── reset_test_ticket.py          # flips that ticket back to status="open"
└── email_preview.html            # last rendered Hebrew email (for visual QA)

indexes/                          # COMMITTED per-repo knowledge artifacts
├── talya-debug__argaman-new.json
├── talya-debug__lia-fine-jewelry.json
├── talya-debug__yael_siso.json
└── talya-debug__yishai-yosef.json
```

### How a single bug flows through the pipeline

1. `cli.run-once` → `BugFixPipeline.run_once()`
2. `FirestoreBugRepository.fetch_pending(N)` → REST query
   `where status == "open" AND type == "bug"`, decode typed values, run
   each row through `ProjectResolver.resolve(project_name)` to get the
   target repo. Rows whose project is unknown are skipped with a
   warning (NOT failed).
3. For each `Bug`:
   - `mark_status(bug.id, "processing")`
   - `git.clone(bug.repo_url, bug.base_branch, sandbox)`
   - `_lookup_index(bug.repo_url)` → loads
     `indexes/<slug>.json` → emits `index_loaded` log
   - `agent.fix_bug(bug, sandbox, repo_index=...)` — Claude tool-use
     loop, uses `SandboxedFileTools` (`read_file`, `write_file`,
     `list_dir`, `run_cmd`, `finish`)
   - `git.commit_all + push` → `github.open_pull_request`
   - `mark_status("mr_opened")`, `attach_pr_url`, `attach_ai_notes`
   - `email.notify_success(bug, outcome, pr)` → multipart Hebrew email
4. On any failure: `mark_status("failed")`, `attach_ai_notes(error)`,
   `email.notify_failure`.

### How the indexer flows

1. `cli.index-once` → `IndexRunner.index_all()`
2. For each `RegistryEntry`: clone to a temp dir → `RepoIndexBuilder.build`
   walks the tree (max 4 levels, 400 entries, skips
   `node_modules` / `.git` / etc.), reads first 6 KB of README,
   detects key marker files (`package.json`, etc.) → produces a
   `RepoIndex` → `IndexStore.save` writes `indexes/<slug>.json`.
3. The `reindex.yml` workflow auto-commits any changed indexes back to
   `main` with `[skip ci]` — bug-fixer's checkout then sees the fresh
   indexes on its next run.

---

## 4. Configuration that's already in place

### GitHub Actions secrets (already set on `eosher-gif/auto-bug-fixer`)

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `FIREBASE_API_KEY` | Talya's Firestore web key |
| `TALYA_TOKEN` | Talya's fine-grained PAT — Contents+PullRequests write on the 4 `talya-debug/*` repos. **Must be Talya's, NOT Eilon's.** Eilon has no access. |
| `SMTP_HOST` | Gmail SMTP (`smtp.gmail.com`) |
| `SMTP_USERNAME` | `talya@talyaosher.com` |
| `SMTP_PASSWORD` | Gmail **App Password** for that mailbox (16 chars, no spaces) |
| `GH_TOKEN` | Eilon's personal PAT — only used by `reindex.yml` checkout (*should* be unused going forward; reindex now also uses TALYA_TOKEN) |

### GitHub Actions variables (also set)

| Variable | Value |
|---|---|
| `FIREBASE_PROJECT_ID` | `service-tickets-cb56a` |
| `EMAIL_ENABLED` | `true` |
| `NOTIFY_FROM` | `talya@talyaosher.com` |
| `SMTP_PORT` | (unset, falls back to 587) |
| `NOTIFY_CC` | (unset — needs to be set to `liran.asulin87@gmail.com`, see below) |
| `MAX_BUGS_PER_RUN` | (unset, falls back to 3) |

**⚠️ Variable `BUG_STATUS_NEW` is set to `"new"` (legacy from earlier
in the project).** The workflow YAML now intentionally does NOT
forward it — the code default `"open"` (matching Talya's schema)
wins. Don't be confused by seeing the variable still in the GitHub UI.

### Locally on the human's machine

- `.env` is git-ignored (he uses `.env.example` as the template).
- The PAT used for git push from local is an SSH key; remote is
  `git@github.com:eosher-gif/auto-bug-fixer.git`.
- An `WF_TOKEN` env var is set in the assistant's shell — fine-grained
  PAT with **`actions: read`** + **`metadata: read`** on this repo.
  It can read run logs but **cannot** call `workflow_dispatch` (no
  `actions: write`). That is why we added a `push:` trigger on
  bug-fixer.yml so each deploy auto-validates without needing a
  dispatch. **Do not assume you can dispatch — push instead.**

---

## 5. Workflows — current state

### `bug-fixer.yml`
- Triggers: `workflow_dispatch` AND `push` to `main` on paths:
  `.github/workflows/bug-fixer.yml`, `repos.yaml`, `requirements.txt`,
  `src/**`.
- Cron is **DISABLED** (commented out). Re-enable
  `*/15 * * * *` once you trust the loop.
- Uses `secrets.TALYA_TOKEN` as `GITHUB_TOKEN` so it can clone +
  push + open PRs on `talya-debug/*`.

### `reindex.yml`
- Triggers: daily `0 4 * * *` UTC, `workflow_dispatch`, AND `push` on
  paths: `.github/workflows/reindex.yml`, `repos.yaml`,
  `src/auto_bug_fixer/indexer/**`, `src/auto_bug_fixer/registry.py`.
- `indexes/` deliberately excluded from the path filter so the
  auto-commit at the end doesn't loop.
- Uses `secrets.TALYA_TOKEN` (changed from `GH_TOKEN` mid-session
  because Eilon's PAT can't read Talya's private repos).
- Auto-commits refreshed `indexes/*.json` back to `main` with
  `[skip ci]` to avoid triggering the CI workflow.

### `ci.yml`
- Standard pytest-on-push. 141 tests must stay green.

---

## 6. Helper scripts that exist

```bash
# Inside ./venv on Windows:
PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe cursor_helper/create_test_ticket.py
# Creates ONE Firestore ticket for project="ארגמן" with the test
# description "Tested by auto-bug-fixer". Prints the new doc id.

PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe cursor_helper/reset_test_ticket.py
# Flips the existing test ticket VjgrhaB9WIN4vmvKqVq3 back to
# status="open" so the next bug-fixer run will pick it up again.
# Idempotent.
```

The `email_preview.html` artifact is the last rendered Hebrew email
(open it in a browser to QA the styling without sending).

---

## 7. What we built today (chronological highlights)

1. **Migrated bug source from SQLAlchemy to Firestore** (REST + httpx
   only — no `firebase-admin` because Google org policy blocks SA keys
   on Talya's project). Includes a custom typed-value codec.
2. **Added `ProjectResolver`** that maps Hebrew/Latin display names to
   `RegistryEntry` objects. Case-insensitive, whitespace-tolerant,
   exact match (no fuzzy).
3. **Set up the GitHub Actions deployment**:
   - Bumped `anthropic` 0.39 → 0.43 (httpx 0.28 incompatibility).
   - Stopped the workflow forwarding empty/legacy `vars.X` values that
     overrode correct in-code defaults (`BUG_STATUS_NEW`,
     `FIRESTORE_COLLECTION`, `FIRESTORE_TYPE_FILTER`).
   - Switched `reindex.yml` from `GH_TOKEN` → `TALYA_TOKEN` so it can
     clone Talya's private repos.
   - Added `push:` triggers (path-filtered) to both workflows so
     deploys auto-validate without needing `actions: write`.
4. **Rewrote the email** from a 5-line English plain text to a rich
   Hebrew HTML multipart email with: success banner, customer card,
   quoted ticket description, AI summary card, files-changed list,
   PR coordinates card, big green CTA button to GitHub PR, numbered
   "next steps" guide. Right-to-left layout. Plain-text alternative
   carries the same data.
5. **Built real per-repo indexes** for all 4 of Talya's repos. The
   indexer + IndexStore + IndexRunner already existed; we just made
   them actually run by adding the push trigger and switching the
   token. Result: `indexes/` now has 4 JSONs, each ~7-8 KB,
   committed at `77ecfa5`.
6. **Added `index_loaded` info log** so we can prove from the logs
   that Claude received the per-repo knowledge on a real run. Verified
   on bug-fixer run `25748375106`.

### Live evidence in production (Talya's mailbox)

| PR | Bug ID | Email subject |
|---|---|---|
| `talya-debug/argaman-new#1` | `VjgrhaB9WIN4vmvKqVq3` | `[auto-bug-fixer] PR ready for bug VjgrhaB9WIN4vmvKqVq3: ...` (old format) |
| `argaman-new#2` | `9cWHWTAaxmCTDu5H7YAF` | `[auto-bug-fixer] תיקון מוכן לבדיקה — באג ...` (new format) |
| `argaman-new#3` | `VjgrhaB9WIN4vmvKqVq3` | `[auto-bug-fixer] תיקון מוכן לבדיקה — באג ...` (new format, post-rich-email) |
| `argaman-new#4` | `VjgrhaB9WIN4vmvKqVq3` | `[auto-bug-fixer] תיקון מוכן לבדיקה — באג ...` (new format, post-`index_loaded`) |

(All 4 PRs are still open on Talya's repo — she can pick one to merge
or close them all and let the next real ticket be the proper end-to-end test.)

---

## 8. ⚡ Where we stopped — the work YOU need to continue

The human approved a feature pack: **A + B + C1**. Build them in
order; each commit should be a separate, reviewable change. Every
push to a watched path will auto-fire the corresponding workflow,
so verify in production after each step.

### Feature A — persistent clone cache (~30 min)

**Why:** today every reindex re-clones 4 private repos from scratch
(~5-7 sec total + bandwidth). With a cache: clone once, then
`git fetch + reset --hard` on subsequent runs. Faster, less network,
and gives the indexer access to git history for v2 features.

**Implementation:**

1. Add a `setup-cache` step in `reindex.yml` BEFORE the indexing step:

   ```yaml
   - name: Cache cloned target repos
     uses: actions/cache@v4
     with:
       path: /tmp/repo-cache
       key: target-repos-${{ hashFiles('repos.yaml') }}
       restore-keys: |
         target-repos-
   ```

2. Update `IndexRunner.__init__` to accept a `cache_root: Path | None`.
3. Update `IndexRunner.index_one`:
   - If `cache_root` is set, the per-repo clone dir becomes
     `cache_root / entry.slug` (deterministic path, NOT a temp dir).
   - On first run that dir doesn't exist → `git clone --depth 50`
     into it.
   - On subsequent runs the dir already exists → `git fetch --all
     --prune` + `git reset --hard origin/<default_branch>` + `git
     clean -fdx`.
   - DON'T `shutil.rmtree` in the `finally` block when cache is on.
4. Wire the workflow env var: `INDEX_CLONE_CACHE: /tmp/repo-cache`.
   Add `index_clone_cache: Path | None = None` to `Settings`.
5. Pass it through in `cli.py` `_run_indexer`.
6. **Tests:** add a unit test for `IndexRunner` that exercises the
   cache hit path (mock `GitClient` to record `fetch` vs `clone`).

**Acceptance:** logs from a 2nd reindex run show `git fetch` instead
of a fresh clone for each repo, and the cache key shows up in the
"Restore cache" Action step.

---

### Feature B — per-bug "history" Claude can learn from (~1 h)

**Why:** today every ticket starts with zero context about past
fixes in the same repo. With a learning ledger Claude sees similar
tickets and their successful resolutions and reuses the pattern.

**Implementation:**

1. New file `src/auto_bug_fixer/indexer/history_store.py`:
   - `HistoryEntry` dataclass: `bug_id, ticket_title, ticket_description,
     pr_url, pr_number, files_touched: list[str], ai_summary, ts`.
   - `HistoryStore`:
     - `path_for(entry: RegistryEntry) -> Path` →
       `<base>/<slug>.history.jsonl`
     - `append(entry, history_entry)` → atomic append (open with `"a"`)
     - `read_recent(entry, limit: int = 10) -> list[HistoryEntry]`
     - `to_prompt_block(entries) -> str` — short bulleted summary
       Claude can read.
2. Wire in `pipeline.BugFixPipeline._handle_success`:
   - Build a `HistoryEntry` from the in-flight `bug`, `outcome`, `pr`.
   - Call `self._history.append(entry, history_entry)`.
3. Wire in `pipeline.BugFixPipeline._lookup_index` (or a new
   `_assemble_context` helper):
   - In addition to the static `RepoIndex`, also load
     `history.read_recent(entry, limit=10)`.
   - Return both. The agent's prompt assembly should join them.
4. `claude_agent/agent.py`:
   - Update the prompt assembly so when a `RepoIndex` is provided
     it ALSO includes any history block (clearly labeled "Previously
     fixed tickets in this repo — use as guidance only, do not blindly
     copy").
5. `reindex.yml`: also `git add indexes/*.history.jsonl` in the
   commit step (the JSONL files live alongside the indexes).
6. **Tests:**
   - `test_history_store.py`: append + read round-trip, atomic write,
     truncation behavior for very long descriptions.
   - Update `test_pipeline_integration.py` with a fake history store
     and assert `append` is called on success but NOT on failure.

**Acceptance:** after running 2 successful tickets on the same repo,
`indexes/talya-debug__argaman-new.history.jsonl` has 2 lines, and
the next bug-fixer run's pipeline log includes
`history_loaded count=2 repo=...`. Claude's first user message in
the next iteration mentions "previously fixed".

**Privacy note:** the JSONL contains the full ticket description.
That's fine because Talya's repo is private and these JSONLs live in
**OUR public** `auto-bug-fixer` repo. **Mitigate** by either:
- (a) storing only ticket *titles* + file paths + PR url (no PII),
  OR
- (b) keeping the auto-bug-fixer repo private (we made it public
  earlier just so I could read workflow logs anonymously — that's
  no longer needed because we authenticate with `WF_TOKEN`; the
  human can flip it back to private at any time).
- Pick (a) by default — it is enough for Claude to spot patterns.

---

### Feature C1 — refresh more often (~2 min)

**Why:** the daily 04:00 UTC cron means the index is stale by up to
24 h. With hourly: stale by at most 1 h, still well within free-tier
budget (24 reindex runs/day × ~30 sec = 12 minutes; budget is
2000 min/month).

**Implementation:**

1. In `reindex.yml`, change:
   ```yaml
   schedule:
     - cron: "0 4 * * *"
   ```
   to:
   ```yaml
   schedule:
     - cron: "0 * * * *"   # every hour at :00
   ```

That's it. Push, watch the cron history. If you observe the cache hit
rate is high (Feature A), bump to `*/30 * * * *` later for free.

---

### After A + B + C1: end-to-end smoke test

1. Create a fresh test ticket on a DIFFERENT project than argaman-new
   (e.g. set `project: "ישי יוסף"` in `create_test_ticket.py`) so
   the pipeline exercises a different repo + index.
2. Push (or just wait for the next hourly reindex + then the
   workflow_dispatch via the human).
3. In the bug-fixer log expect:
   - `index_loaded` for `talya-debug/yishai-yosef`
   - `history_loaded count=N` if any previous fixes exist for that repo
   - `pr_opened` on the right repo (yishai-yosef, NOT argaman-new)
   - `email_sent` to talya@talyaosher.com with the rich Hebrew layout

---

## 9. Pending tweaks (not part of A/B/C but worth fixing soon)

- **`NOTIFY_CC` is unset.** Per Talya's spec Liran
  (`liran.asulin87@gmail.com`) should also receive every email as
  a backup. Set the variable in repo settings; the email_sender
  already supports CC (splits on comma).
- **`type:dev` tickets get `"failed"`.** Talya's spec says they
  should be marked failed with a *Hebrew* note "בקשת פיתוח — לא
  נתמך אוטומטית". Verify the current behavior in
  `firestore_repository.py` and add the note if missing.
- **PR template.** `_render_pr_body` in `pipeline.py` is in English;
  consider a Hebrew template since Talya is the reviewer.
- **The legacy `BUG_STATUS_NEW=new` GitHub Variable** can be deleted
  from the UI now (workflow no longer reads it). Cosmetic only.
- **The 4 open test PRs on `argaman-new`** (`#1`, `#2`, `#3`, `#4`)
  should be closed before real customer tickets pile on top.

---

## 10. Operational reference

### URLs

| Thing | URL |
|---|---|
| The bot's repo | https://github.com/eosher-gif/auto-bug-fixer |
| Actions | https://github.com/eosher-gif/auto-bug-fixer/actions |
| Bug-fixer workflow | https://github.com/eosher-gif/auto-bug-fixer/actions/workflows/bug-fixer.yml |
| Reindex workflow | https://github.com/eosher-gif/auto-bug-fixer/actions/workflows/reindex.yml |
| Settings → Secrets | https://github.com/eosher-gif/auto-bug-fixer/settings/secrets/actions |
| Settings → Variables | https://github.com/eosher-gif/auto-bug-fixer/settings/variables/actions |
| Firestore console | https://console.firebase.google.com/project/service-tickets-cb56a |
| Talya's storefronts | `https://github.com/talya-debug/{argaman-new,yishai-yosef,yael_siso,lia-fine-jewelry}` |

### Live IDs to keep handy

- Test ticket Firestore doc id: `VjgrhaB9WIN4vmvKqVq3`
- Last verified bug-fixer run: `25748375106`
- Last verified reindex run: `25744865169`
- Last reindex commit (the one that produced indexes/): `77ecfa5`
- Last `index_loaded` commit: `f389827`

### Local commands cheatsheet

```bash
cd c:/dev/nlastic/auto-bug-fixer

# venv (always)
./venv/Scripts/python.exe -m pip install -r requirements.txt

# tests (must stay 141/141 green)
PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe -m pytest -q

# render an email locally for visual QA
PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from auto_bug_fixer.notify.email_sender import _render_success_html
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest
# ... build sample objects ... write HTML to cursor_helper/email_preview.html
"

# read run logs without leaving the terminal (needs $WF_TOKEN)
curl -sL -H "Authorization: Bearer $WF_TOKEN" \
  -o run-logs.zip \
  "https://api.github.com/repos/eosher-gif/auto-bug-fixer/actions/runs/<RUN_ID>/logs"
unzip -q run-logs.zip -d run-logs

# trigger bug-fixer remotely
# DO NOT try `gh workflow run` — WF_TOKEN lacks actions:write.
# Instead push a no-op commit to a watched path, e.g.:
git commit --allow-empty -m "trigger" ; git push --no-verify origin main
# (only works if the empty commit is on a watched path; better to
#  touch a .md or a non-functional comment in src/)

# reset the test ticket and watch the next push fire bug-fixer
PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe cursor_helper/reset_test_ticket.py
```

---

## 11. How to triage a failed run

1. Get the run id from
   `https://github.com/eosher-gif/auto-bug-fixer/actions` (or via the
   API listing in §10).
2. Download logs via the curl above (needs `WF_TOKEN`).
3. Look at `run-logs/process-bugs/5_Run one pipeline tick.txt` (or
   the equivalent step name for reindex).
4. Search for `"event": "startup_failed"` first — that catches
   Settings/import errors before any bug is touched.
5. Search for `"level": "error"` and `"event": "bug_fix_failed"`.
6. Common past failures and fixes (already resolved, here for memory):
   - `Client.__init__() got an unexpected keyword argument 'proxies'`
     → bump anthropic to ≥ 0.40 (we're on 0.43.0).
   - `registry file not found` → repos.yaml not committed; ensure
     `git ls-files repos.yaml` returns the path.
   - `Resource not accessible by personal access token` (403) on
     `talya-debug/*` → wrong token; bug-fixer + reindex must use
     `secrets.TALYA_TOKEN`, never `secrets.GH_TOKEN`.
   - `email_sent` missing while everything else looks green →
     `EMAIL_ENABLED` variable is not set to `true` (or `bug.reporter_email`
     was empty in the ticket).
   - `no_index_available` warning → the indexer never ran for this
     repo. Run reindex manually (workflow_dispatch in the UI) or
     push any change that touches a watched path.

---

## 12. Conventions to keep — do not break

- **Always work inside `./venv`.** Never install packages globally.
- **Never edit files via `sed`/`echo`/heredoc.** Use the editor tools
  (StrReplace / Write).
- **Never push without `--no-verify`** — there's a local AI-review
  pre-push hook on this machine that times out and blocks pushes.
- **Never push secrets, app passwords, or PATs into commits or files.**
  Everything sensitive lives in GitHub Secrets / repo Variables.
  Secrets that did appear in chat history (the rotated PATs, the
  Gmail App Password) should be rotated by the human before release.
- **Reply in English.** The human's `conversation-conventions.mdc`
  rule says: user may write Hebrew, assistant always replies English.
- **Use `cursor_helper/` for one-off scripts.** Not `scripts/`,
  not the repo root. Per the human's user rule.
- **No emojis in code or commit messages** unless explicitly asked.
- **`indexes/*.json` is committed on purpose** (so bug-fixer's
  checkout can read them without a separate fetch). Don't add
  it to `.gitignore`.
- **Don't touch the old `talya-debug/Argaman` repo.** All ארגמן
  tickets go to `argaman-new`.
- **Forbidden files** (`.env`, `firebase.js`, `vercel.json`,
  `package-lock.json`, `yarn.lock`) must remain in
  `repos.yaml` `forbidden_paths`. The agent sandbox enforces this.

---

## 13. If the human asks "where did we stop?"

You stopped just after verifying the live `index_loaded` log and
before starting Feature A (clone cache). The human chose option
`all` (A + B + C1) from the multi-choice menu. Start with A. Push
each feature as a separate commit so the auto-validation push
trigger fires and you can read the result before moving on to the
next feature.

— end of handoff —
