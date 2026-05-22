#!/usr/bin/env bash
# loam-session-start.sh — SessionStart hook that surfaces the most recent
# compact-snapshot bridge entry to the new session.
#
# Wire it up in ~/.claude/settings.json under the SessionStart hook list.
# Output goes to stdout; Claude Code includes hook output in initial context.
#
# Env vars (optional):
#   LOAM_AGENT             — agent identity (default: hostname-short)
#   LOAM_BRIDGE_MAX_AGE_H  — skip bridges older than this many hours
#                            (default: 168 = one week)

set -euo pipefail

AGENT="${LOAM_AGENT:-$(hostname -s)}"
MAX_AGE_HOURS="${LOAM_BRIDGE_MAX_AGE_H:-168}"

if ! command -v loam-cli >/dev/null 2>&1; then
  # No loam installed; session-start works without us.
  exit 0
fi

# Compute the "since" cutoff — ISO-8601 with seconds, UTC.
CUTOFF=$(date -u -d "$MAX_AGE_HOURS hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
       || python3 -c "
import datetime
print((datetime.datetime.utcnow() - datetime.timedelta(hours=$MAX_AGE_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ'))
")

# Fetch the most recent compact-snapshot. Reinforce on read so the bridge
# gets a usage bump (signals "still relevant" if subsequent sessions resume
# the thread).
BRIDGE=$(loam-cli recent \
  --agent "$AGENT" \
  --type compact-snapshot \
  --since "$CUTOFF" \
  --limit 1 \
  2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data[0]['content'] if data else '')
" 2>/dev/null || true)

if [ -z "${BRIDGE// }" ]; then
  # No bridge entry — clean start, nothing to surface.
  exit 0
fi

# Emit it as a clear context block. The leading marker makes it
# distinguishable from regular content in the transcript.
cat <<EOF
// CONTEXT FROM PRIOR SESSION (session-loam compact-snapshot, agent=$AGENT)
$BRIDGE
// END PRIOR-SESSION CONTEXT
EOF
exit 0
