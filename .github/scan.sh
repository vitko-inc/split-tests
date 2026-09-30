#!/usr/bin/env bash
# Fail if any file names an identifier from the private pattern list. The list comes from
# $IDENTIFIER_PATTERNS (one extended regex per line) or from the file given with -f; it is never
# stored in this repository. Matches are reported by file and line only.
# usage: scan.sh [-f patterns-file] [dir]
set -euo pipefail
patterns="${IDENTIFIER_PATTERNS:-}"
if [[ "${1:-}" == -f ]]; then patterns="$(cat "$2")"; shift 2; fi
dir="${1:-.}"
regex="$(printf '%s\n' "$patterns" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' | paste -sd'|' -)"
if [[ -z "$regex" ]]; then
  echo "identifier scan: no patterns given" >&2
  exit 2
fi
hits="$(grep -rniE --exclude-dir=.git --exclude=LICENSE "$regex" "$dir" | cut -d: -f1,2 || true)"
if [[ -n "$hits" ]]; then
  printf 'identifier scan: internal identifiers at:\n%s\n' "$hits" >&2
  exit 1
fi
echo "identifier scan: clean"
