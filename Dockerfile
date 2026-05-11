FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    WORKSPACE_DIR=/var/workspace \
    INDEX_DIR=/var/auto-bug-fixer/index \
    REPOS_FILE=/app/repos.yaml

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src

RUN useradd --create-home --uid 10001 fixer \
 && mkdir -p "$WORKSPACE_DIR" "$INDEX_DIR" \
 && chown -R fixer:fixer /app "$WORKSPACE_DIR" "$INDEX_DIR"

USER fixer

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "auto_bug_fixer", "daemon"]
