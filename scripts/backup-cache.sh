#!/usr/bin/env bash
# Copies the expensive-to-rebuild local data caches (va-comparison.sqlite,
# ipeds.sqlite) to an external backup location, since both can exceed
# GitHub's 100MB per-file limit and .cache/ is intentionally excluded from
# git (see README). va-comparison.sqlite in particular holds data -- Serper
# website guesses, crawled apply links, the nationwide program catalog --
# that cost real API calls/time to build and, for the website guesses, may
# not be reproducible at all without a search API key. There is no other
# copy of this data anywhere; losing the file means losing the work.
#
# Set CACHE_BACKUP_DIR in .env (or the environment) to where backups should
# go, for example a OneDrive-synced folder for automatic off-machine copies.
# Run this manually after any full or partial rebuild of these caches, and
# before running any script that deletes/rebuilds them.
set -euo pipefail

WORKSPACE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "${WORKSPACE_DIR}/.env" ]]; then
  set -a
  source "${WORKSPACE_DIR}/.env"
  set +a
fi

if [[ -z "${CACHE_BACKUP_DIR:-}" ]]; then
  echo "CACHE_BACKUP_DIR is not set. Add it to .env, e.g.:" >&2
  echo "  CACHE_BACKUP_DIR=/c/Users/you/OneDrive/jarvet-backups" >&2
  exit 1
fi

mkdir -p "${CACHE_BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"

for name in va-comparison.sqlite ipeds.sqlite; do
  source_file="${WORKSPACE_DIR}/.cache/${name}"
  if [[ ! -f "${source_file}" ]]; then
    echo "Skipping ${name}: not present in .cache/."
    continue
  fi
  cp "${source_file}" "${CACHE_BACKUP_DIR}/${name%.sqlite}.${STAMP}.sqlite"
  cp "${source_file}" "${CACHE_BACKUP_DIR}/${name}"
  echo "Backed up ${name} ($(du -h "${source_file}" | cut -f1)) -> ${CACHE_BACKUP_DIR}/"
done
