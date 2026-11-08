#!/usr/bin/env bash
# Example driver for a stock Gufo user service. Called by context_controller.py.
# Only the GUFO_CONTEXT_TOKENS line of GUFO_CONTEXT_ENV is rewritten; any other
# lines in that file are kept.
# Exit status: 0 switched, 1 failed and previous profile restored (or none to
# restore), 2 usage error, 3 failed and restoring the previous profile also failed.
set -euo pipefail

CONTEXT_ENV=${GUFO_CONTEXT_ENV:?set GUFO_CONTEXT_ENV to the service EnvironmentFile}
CONTEXT_UNIT=${GUFO_CONTEXT_UNIT:-gufo.service}

# The controller decides which sizes are allowed; this only rejects non-numbers.
is_tokens() {
  case "$1" in
    ''|0*|*[!0-9]*) return 1 ;;
    *) [ "${#1}" -le 9 ] ;;
  esac
}

read_context() {
  if [ ! -f "$CONTEXT_ENV" ]; then return 1; fi
  local line
  line=$(sed -n 's/^GUFO_CONTEXT_TOKENS=//p' "$CONTEXT_ENV" | head -1)
  if is_tokens "$line"; then printf '%s\n' "$line"; else return 1; fi
}

temp=""
trap 'if [ -n "$temp" ]; then rm -f "$temp"; fi' EXIT

write_context() {
  mkdir -p "$(dirname "$CONTEXT_ENV")"
  temp=$(mktemp "${CONTEXT_ENV}.XXXXXX")
  if [ -f "$CONTEXT_ENV" ]; then
    # grep exits 1 when no other lines exist; only 2 is a real error.
    grep -v '^GUFO_CONTEXT_TOKENS=' "$CONTEXT_ENV" > "$temp" || [ $? -eq 1 ]
    chmod --reference="$CONTEXT_ENV" "$temp"
  fi
  printf 'GUFO_CONTEXT_TOKENS=%s\n' "$1" >> "$temp"
  mv "$temp" "$CONTEXT_ENV"
  temp=""
}

case "${1:-}" in
  state)
    read_context
    ;;
  switch)
    value=${2:-}
    if ! is_tokens "$value"; then echo "invalid context size: $value" >&2; exit 2; fi
    old=$(read_context || true)
    write_context "$value"
    if ! systemctl --user restart "$CONTEXT_UNIT"; then
      if [ -z "$old" ]; then
        echo "restart at $value failed; no previous profile to restore" >&2
        exit 1
      fi
      write_context "$old"
      if systemctl --user restart "$CONTEXT_UNIT"; then
        echo "restart at $value failed; restored $old" >&2
        exit 1
      fi
      echo "restart at $value failed and restoring $old also failed; $CONTEXT_UNIT is down" >&2
      exit 3
    fi
    ;;
  *) echo "usage: $0 {state|switch N}" >&2; exit 2 ;;
esac
