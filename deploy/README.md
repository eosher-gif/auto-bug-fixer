# Deployment

The system supports two runtimes. **Pick one — they do the same job.**

| Runtime | Cost | Credit card? | Polling latency | Setup time |
|---|---|---|---|---|
| **GitHub Actions (recommended, default)** | Free (2000 min/mo private, unlimited public) | **No** | ~15 min cron tick | 5 min |
| **VM + Docker** | Free on Oracle Cloud Always Free; ~€4/mo on Hetzner | Yes (CC for ID verification only) | Continuous (seconds) | 15 min |

Both runtimes consume the same `.env`-style configuration. You can start with GitHub Actions and migrate to a VM later without code changes.

---

## Path A — GitHub Actions (recommended, no credit card needed)

This is the simplest, cheapest, most boring way to run it. Two scheduled workflows do everything:

| Workflow | Schedule | What it does |
|---|---|---|
| `.github/workflows/bug-fixer.yml` | Every 15 min (`*/15 * * * *`) | Pulls new bugs from your DB, runs Claude, opens PRs, sends email |
| `.github/workflows/reindex.yml`   | Every day at 04:00 UTC | Re-indexes every repo in `repos.yaml`, commits `indexes/*.json` back to the repo |

### 1. Create the GitHub repo

- Create a new GitHub repo (any name, e.g. `auto-bug-fixer`)
- **Make it PUBLIC if possible** — that gives you unlimited Actions minutes. If it must be private, you have 2000 free minutes/month, which fits the default 15-minute polling schedule.

### 2. Push this code to it

```bash
cd /c/dev/nlastic/auto-bug-fixer
git init -b main
git add .
git commit -m "chore: initial commit"
git remote add origin https://github.com/<your-org>/<repo-name>.git
git push -u origin main
```

### 3. Edit `repos.yaml`

Open `repos.yaml` in your new repo (the example file is committed) and list every repository the bot should fix bugs in:

```yaml
repos:
  - url: https://github.com/your-org/some-service
    default_branch: main
    language: python                # optional
    test_command: pytest -q         # optional
    description: |                  # optional, fed to Claude
      What this service does and where bugs usually come from.
```

Commit + push.

### 4. Add Secrets and Variables

In your new repo: **Settings → Secrets and variables → Actions**

**Secrets** (sensitive — write only):

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key (`sk-ant-…`) |
| `DATABASE_URL` | Full SQLAlchemy URL of your bug DB |
| `BUG_FIXER_GITHUB_TOKEN` | Fine-grained PAT with `Contents: write` + `Pull requests: write` on every target repo |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_USERNAME` | e.g. your sender address |
| `SMTP_PASSWORD` | App password (Gmail) or SMTP password |

**Variables** (non-sensitive):

| Name | Example |
|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5-20250929` |
| `BUG_TABLE_NAME` | `customer_bugs` |
| `BUG_ID_COLUMN` | `id` |
| `BUG_TITLE_COLUMN` | `title` |
| `BUG_DESCRIPTION_COLUMN` | `description` |
| `BUG_STATUS_COLUMN` | `status` |
| `BUG_REPO_URL_COLUMN` | `repo_url` |
| `BUG_REPO_BRANCH_COLUMN` | `base_branch` |
| `BUG_REPORTER_EMAIL_COLUMN` | `reporter_email` |
| `BUG_STATUS_NEW` | `new` |
| `BUG_STATUS_PROCESSING` | `processing` |
| `BUG_STATUS_MR_OPENED` | `mr_opened` |
| `BUG_STATUS_FAILED` | `failed` |
| `MAX_BUGS_PER_RUN` | `3` |
| `SMTP_PORT` | `587` |
| `NOTIFY_FROM` | the From address |
| `NOTIFY_CC` | optional CC list |
| `GIT_COMMITTER_NAME` | `auto-bug-fixer[bot]` |
| `GIT_COMMITTER_EMAIL` | `auto-bug-fixer[bot]@users.noreply.github.com` |

### 5. Trigger the first run

- **Actions** tab → **reindex** workflow → **Run workflow** → wait ~5 min
  - This produces `indexes/<owner>__<name>.json` for every entry in `repos.yaml` and commits them.
- **Actions** tab → **bug-fixer** workflow → **Run workflow** (or wait for the cron)
  - Each run logs JSON to the Actions UI. Look for `processing_bug` / `pr_opened` / `email_sent`.

### 6. (Optional) Tighten the cron schedule

If your repo is **public** (unlimited minutes), you can poll more aggressively:

- Edit `.github/workflows/bug-fixer.yml`
- Change `cron: "*/15 * * * *"` → `cron: "*/5 * * * *"` (5-minute minimum)
- Commit + push

### Free-minute budget (private repos only)

| Schedule | Idle min/month | Bug-fix min/month (≈10 fixes/day) | Total |
|---|---|---|---|
| Every 5 min | ~4320 | ~600 | over budget |
| Every 10 min | ~2160 | ~600 | over budget |
| **Every 15 min** | ~720 | ~600 | **~1320 / 2000** |
| Every 30 min | ~360 | ~600 | ~960 / 2000 |

Make the repo public to remove this constraint entirely.

---

## Path B — VM + Docker (for sub-minute polling or always-on health endpoint)

Use this when you want continuous polling instead of 15-min ticks, or you want the `/health` HTTP endpoint for monitoring.

### B.1 Oracle Cloud Always Free

- Create account at https://www.oracle.com/cloud/free/ (CC required for ID verification, **never charged on free tier**)
- Console → Compute → Create instance
  - Image: Ubuntu 22.04
  - Shape: VM.Ampere.A1.Flex with 2 OCPU + 12 GB RAM
  - Add your SSH public key
  - Advanced options → Cloud-init: paste contents of `cloud-init.yaml` (replace `REPLACE_ME` first)
- Add ingress rule for TCP 8080 in the VCN security list
- SSH in, edit `.env` and `repos.yaml`, run `sudo bash /opt/auto-bug-fixer/deploy/install.sh`

### B.2 GCP e2-micro Always Free

- Create account at https://cloud.google.com/free
- Compute Engine → Create instance
  - Machine type: e2-micro
  - Region: us-west1, us-central1, or us-east1
  - Image: Ubuntu 22.04 LTS
  - Startup script: paste `cloud-init.yaml`
- Same finish.

### B.3 Hetzner CX22 (paid, ~€4/mo, simplest)

When you don't want to deal with free-tier capacity issues.

### After the VM is up
```bash
ssh ubuntu@<vm-ip>
sudo nano /opt/auto-bug-fixer/.env
sudo nano /opt/auto-bug-fixer/repos.yaml
sudo bash /opt/auto-bug-fixer/deploy/install.sh
curl http://localhost:8080/health
```

---

## Files in this directory

| File | Purpose |
|---|---|
| `install.sh` | Idempotent bootstrap for fresh Linux VM (Path B) |
| `cloud-init.yaml` | First-boot automation for any cloud-init capable provider (Path B) |
| `systemd/auto-bug-fixer.service` | Non-Docker run-as-systemd-service alternative (Path B) |
| `README.md` | This file |

The actual production workflows for Path A live in `.github/workflows/`.
