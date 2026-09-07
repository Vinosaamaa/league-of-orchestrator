#!/bin/sh
set -eu

if [ "${HERDR_PLUGIN_EVENT:-}" != "startup" ]; then
  exit 0
fi

state_root=${LEAGUE_STATE_ROOT:-}
if [ -z "$state_root" ]; then
  pointer=${LEAGUE_WRITER_POINTER:-"${HOME:-/nonexistent}/.local/state/league-writer-pointer.json"}
  if [ ! -f "$pointer" ] || ! grep -q '"writer":"sqlite"' "$pointer"; then
    echo "league_restore_state_unavailable" >&2
    exit 1
  fi
  state_root=$(dirname -- "$pointer")/league
fi

case $state_root in
  /*) ;;
  *) echo "league_restore_state_root_invalid" >&2; exit 1 ;;
esac

league_command=${LEAGUE_COMMAND:-league}
exec "$league_command" --state-root "$state_root" runtime reconcile-restored-agent \
  --multiplexer-kind herdr --timeout-ms 30000 --poll-ms 100
