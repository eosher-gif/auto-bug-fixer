#!/usr/bin/env bash
# Idempotent bootstrap for a fresh Linux VM (Ubuntu 22.04 / 24.04 / Oracle Linux).
#
# Installs Docker + Docker Compose, clones (or pulls) the auto-bug-fixer repo,
# checks for a populated .env + repos.yaml, then starts the daemon via
# docker compose. Safe to re-run: each step is a no-op if already done.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<you>/auto-bug-fixer/main/deploy/install.sh | sudo bash
#   # (or clone the repo first and run it locally)
#
# Required env (or interactive prompt):
#   REPO_URL        Git URL of the auto-bug-fixer repo (defaults to upstream)
#   INSTALL_DIR     Target directory (default: /opt/auto-bug-fixer)
#   BRANCH          Git branch to deploy (default: main)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/REPLACE_ME/auto-bug-fixer.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/auto-bug-fixer}"
BRANCH="${BRANCH:-main}"

log() { printf '[install] %s\n' "$*" >&2; }
fail() { printf '[install][error] %s\n' "$*" >&2; exit 1; }

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    fail "must be run as root (use sudo)"
  fi
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    echo "${ID}"
  else
    echo "unknown"
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "docker + compose already installed"
    return
  fi
  log "installing docker via the official convenience script"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
}

ensure_repo() {
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "repo already cloned at ${INSTALL_DIR}, pulling latest"
    git -C "${INSTALL_DIR}" fetch --depth 1 origin "${BRANCH}"
    git -C "${INSTALL_DIR}" reset --hard "origin/${BRANCH}"
    return
  fi
  log "cloning ${REPO_URL} -> ${INSTALL_DIR}"
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
}

ensure_config_files() {
  if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
    log "created ${INSTALL_DIR}/.env from .env.example - FILL IT IN before continuing"
    log "edit it now, then re-run this script:"
    log "  sudo nano ${INSTALL_DIR}/.env"
    exit 0
  fi
  if [[ ! -f "${INSTALL_DIR}/repos.yaml" ]]; then
    cp "${INSTALL_DIR}/repos.yaml.example" "${INSTALL_DIR}/repos.yaml"
    log "created ${INSTALL_DIR}/repos.yaml from repos.yaml.example - FILL IT IN before continuing"
    log "edit it now, then re-run this script:"
    log "  sudo nano ${INSTALL_DIR}/repos.yaml"
    exit 0
  fi
  if grep -q 'sk-ant-...' "${INSTALL_DIR}/.env"; then
    fail "${INSTALL_DIR}/.env still contains placeholder values - fill in real secrets first"
  fi
  if grep -q 'REPLACE_ME\|acme/widgets' "${INSTALL_DIR}/repos.yaml"; then
    fail "${INSTALL_DIR}/repos.yaml still has example entries - replace with real repos first"
  fi
}

start_stack() {
  log "starting docker compose stack"
  cd "${INSTALL_DIR}"
  docker compose pull --quiet || true
  docker compose up -d --build
  docker compose ps
  log "tailing the last 50 log lines"
  docker compose logs --tail=50
  log "done. follow logs with: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f"
  log "health: curl http://localhost:8080/health"
}

main() {
  require_root
  log "OS: $(detect_os)"
  install_docker
  ensure_repo
  ensure_config_files
  start_stack
}

main "$@"
