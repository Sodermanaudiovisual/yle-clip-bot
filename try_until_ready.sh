#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

URL="https://areena.yle.fi/1-72069916"
OUT="rony_rex.mp4"

# Activate venv if present; else use python3
if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
  PY="python"
else
  PY="python3"
fi

# Sanity checks
command -v "$PY" >/dev/null || { echo "❌ Python not found"; exit 1; }
command -v yle-dl >/dev/null || { echo "❌ yle-dl not found"; exit 1; }
command -v ffmpeg >/dev/null || { echo "❌ ffmpeg not found"; exit 1; }

echo "Using Python: $("$PY" -V)"
echo "Using yle-dl: $(yle-dl --version 2>/dev/null | head -n1)"
echo "Using ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1)"

while true; do
  if "$PY" yle_clip_bot.py "$URL" --out "$OUT"; then
    echo "✅ Done: $OUT"
    exit 0
  fi

  echo "ℹ️  Checking availability hint from yle-dl…"
  HINT="$(yle-dl -o /dev/null "$URL" 2>&1 || true)"
  TS="$(printf '%s\n' "$HINT" | sed -n 's/.*Becomes available on \([0-9T:+-]\{25,\}\).*/\1/p' | head -n1)"

  if [[ -n "${TS:-}" ]]; then
    echo "⏳ Not yet available. Release time according to Areena: $TS"

    # Pass TS as argv[1] BEFORE the heredoc
    SLEEP_SECS="$("$PY" - "$TS" <<'PY'
import sys, datetime
ts = sys.argv[1]
dt = datetime.datetime.fromisoformat(ts)     # e.g. 2025-09-10T20:00:00+03:00
now = datetime.datetime.now(dt.tzinfo)
delta = (dt - now).total_seconds()
delta = max(60, int(delta) + 60)             # at least 60s, add 60s buffer
print(delta)
PY
)"
    echo "💤 Sleeping ${SLEEP_SECS}s until availability…"
    sleep "$SLEEP_SECS"
  else
    echo "🕒 No release hint found; retrying in 10 minutes…"
    sleep 600
  fi
done
