#!/usr/bin/env bash
# Soundbar wake test (CLAUDE.md, known risk 1): does the Q990F wake when MA casts to it?
#
#   ./wake_test.sh <album uri> "<how the soundbar was put to sleep>"
#
# Put the soundbar in the state you want to test first. Worth trying, in this order:
#   "remote off, 2 min"            "auto power off, idle 25 min"
#   "overnight standby"            "on, TV/HDMI input selected"
# The script sends the play command, watches MA's view of the player for up to 60 s,
# asks whether you heard music, and appends a line to wake_results.tsv.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$here/ma.sh"
need_queue
uri=${1:?usage: wake_test.sh <album uri> "<note>"}
note=${2:?usage: wake_test.sh <album uri> "<note>"}
results=$here/wake_results.tsv

player_state() {  # prints "available powered playback_state", or "not-found"
  ma_call players/get "{\"player_id\": \"$QUEUE_ID\"}" 2>/dev/null | python3 -c '
import json, sys
p = json.load(sys.stdin)
print("not-found" if p is None else
      "%s %s %s" % (p["available"], p.get("powered"), p.get("playback_state")))
' 2>/dev/null || echo "error"
}

since() { awk -v a="$1" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }'; }

before=$(player_state)
echo "MA's view of the soundbar (available powered state): $before"
read -rp "Soundbar in the state you want to test? Press Enter to send the play command. "

start=$(date +%s.%N)
if response=$(MA_TIMEOUT=90 ma_call player_queues/play_media \
    "{\"queue_id\": \"$QUEUE_ID\", \"media\": \"$uri\"}" 2>&1); then
  api=ok
else
  api="failed: ${response//[$'\t\n']/ }"
fi
api_secs=$(since "$start")
echo "Play command: $api (${api_secs} s)"

playing_secs=never
for _ in $(seq 30); do
  if [[ $(player_state) == *" playing" ]]; then
    playing_secs=$(since "$start")
    break
  fi
  sleep 2
done
echo "MA reports playing after (s): $playing_secs"

read -rp "Did the soundbar switch on and play music by itself? [y/n] " heard
read -rp "Stop playback now? [Y/n] " stop
[[ $stop == [nN]* ]] || ma_call player_queues/stop "{\"queue_id\": \"$QUEUE_ID\"}" >/dev/null || true

if [[ ! -f $results ]]; then
  printf 'when\tnote\tbefore\tplay_command\tcommand_s\tplaying_after_s\theard\n' > "$results"
fi
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -Iseconds)" "$note" "$before" \
  "$api" "$api_secs" "$playing_secs" "$heard" >> "$results"
echo "Recorded in $results"
