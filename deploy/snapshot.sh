#!/usr/bin/env bash
# Snapshot / rollback the working tree WITHOUT git (commits are off-limits: gitleaks hook).
#   bash deploy/snapshot.sh save <label>     → .snapshots/<time>-<label>.tgz
#   bash deploy/snapshot.sh list
#   bash deploy/snapshot.sh restore <file>   → overwrite app/ web/ tests/ scripts/ deploy/ + docs from it
# Excludes secrets (.env*), the venv and runtime state.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .snapshots
case "${1:-}" in
  save)
    f=".snapshots/$(date +%m%d-%H%M%S)-${2:-snap}.tgz"
    tar czf "$f" --exclude='.env' --exclude='.env.*' --exclude='__pycache__' --exclude='*.pyc' \
      app web tests scripts deploy requirements.txt *.md .gitignore
    echo "saved $f ($(du -h "$f" | cut -f1))";;
  list) ls -1t .snapshots/;;
  restore)
    [ -f "${2:-}" ] || { echo "usage: restore .snapshots/<file>.tgz"; exit 1; }
    bash "$0" save before-restore >/dev/null
    tar xzf "$2"
    echo "restored $2 (previous state saved as before-restore)";;
  *) echo "usage: save <label> | list | restore <file>"; exit 1;;
esac
