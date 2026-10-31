#!/usr/bin/env bash
# Small client for the Music Assistant HTTP API. Runs on the Pi or the laptop.
#
#   ./ma.sh players                    list players: name, player_id, provider, state
#   ./ma.sh state                      state of the soundbar ($QUEUE_ID)
#   ./ma.sh play <uri>                 play <uri> on the soundbar, e.g. spotify://album/<id>
#   ./ma.sh stop                       stop the soundbar
#   ./ma.sh call <command> [<json>]    any API command; <json> is its args object
#
# Reads MA_URL, MA_TOKEN and QUEUE_ID from ma.env next to this script.
# Prints the response body; exits non-zero on an HTTP error or timeout.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -f $here/ma.env ]]; then
  # shellcheck source=/dev/null
  source "$here/ma.env"
fi
: "${MA_URL:?set MA_URL in $here/ma.env}" "${MA_TOKEN:?set MA_TOKEN in $here/ma.env}"

ma_call() {  # ma_call <command> [<args json>]
  local args=${2-} out
  [[ -n $args ]] || args='{}'
  if ! out=$(curl -sS --fail-with-body --max-time "${MA_TIMEOUT:-30}" \
      -H "Authorization: Bearer $MA_TOKEN" \
      -H 'Content-Type: application/json' \
      -d "{\"message_id\": \"1\", \"command\": \"$1\", \"args\": $args}" \
      "$MA_URL/api"); then
    echo "MA says: ${out:-nothing}" >&2
    return 1
  fi
  printf '%s\n' "$out"
}

need_queue() {
  : "${QUEUE_ID:?set QUEUE_ID in $here/ma.env (find it with ./ma.sh players)}"
}

# Prints one player per line from a JSON player or list of players on stdin.
print_players() {
  python3 -c '
import json, sys
players = json.load(sys.stdin)
if players is None:
    sys.exit("MA does not know that player_id")
for p in players if isinstance(players, list) else [players]:
    print("%-28s %-40s %-14s %-11s powered=%-5s %s" % (
        p["name"], p["player_id"], p["provider"],
        "available" if p["available"] else "UNAVAILABLE",
        p.get("powered"), p.get("playback_state")))
'
}

# Only run the command line when executed, so wake_test.sh can source the functions.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  case ${1-} in
    players) out=$(ma_call players/all); print_players <<<"$out" ;;
    state)
      need_queue
      out=$(ma_call players/get "{\"player_id\": \"$QUEUE_ID\"}")
      print_players <<<"$out"
      ;;
    play)
      need_queue
      ma_call player_queues/play_media \
        "{\"queue_id\": \"$QUEUE_ID\", \"media\": \"${2:?usage: ma.sh play <uri>}\"}"
      ;;
    stop) need_queue; ma_call player_queues/stop "{\"queue_id\": \"$QUEUE_ID\"}" ;;
    call) ma_call "${2:?usage: ma.sh call <command> [<json args>]}" "${3-}" ;;
    *) sed -n '2,11s/^# \{0,1\}//p' "$0"; exit 2 ;;
  esac
fi
