#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ----- Config from environment -----
URL="${AREENA_URL:?Set AREENA_URL env var (episode URL or series:<ID>)}"
OUT="${OUT_NAME:-areena.mp4}"

PY="python3"
command -v "$PY" >/dev/null || { echo "❌ python3 not found"; exit 1; }

# ----- Prepare yle-dl runner (binary or module) -----
YLEDL=""
if command -v yle-dl >/dev/null 2>&1; then
  YLEDL="yle-dl"
else
  # Try module
  if "$PY" - <<'PY' >/dev/null 2>&1; then
import importlib; importlib.import_module("yle_dl")
PY
  then
    YLEDL="$PY -m yle_dl"
  else
    echo "ℹ️  yle-dl not on PATH and module missing — installing now…"
    pip install --no-cache-dir yle-dl || { echo "❌ Failed to install yle-dl"; exit 1; }
    if command -v yle-dl >/dev/null 2>&1; then
      YLEDL="yle-dl"
    elif "$PY" - <<'PY' >/dev/null 2>&1; then
import importlib; importlib.import_module("yle_dl")
PY
    then
      YLEDL="$PY -m yle_dl"
    else
      echo "❌ yle-dl still not available after install"
      exit 1
    fi
  fi
fi

# ----- ffmpeg sanity -----
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "❌ ffmpeg not found (Render should install from apt.txt)."
  exit 1
fi

echo "Using yle-dl via: $YLEDL"
echo "Using ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1)"

# ----- Resolve series:<ID> → latest episode URL via RSS -----
resolve_latest() {
  local sid="$1"
  "$PY" - "$sid" <<'PY'
import sys, requests, xml.etree.ElementTree as ET
sid = sys.argv[1]
feed = f"https://feeds.yle.fi/areena/v1/series/{sid}.rss?downloadable=true"
try:
    r = requests.get(feed, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        if link:
            print(link, end=""); break
except Exception:
    pass
PY
}

if [[ "$URL" == series:* ]]; then
  SID="${URL#series:}"
  echo "Resolving latest episode for series ID: $SID"
  LATEST="$(resolve_latest "$SID" || true)"
  if [[ -z "${LATEST:-}" ]]; then
    echo "❌ Could not resolve latest episode from RSS for series $SID."
    exit 1
  fi
  echo "Latest episode: $LATEST"
  URL="$LATEST"
fi

echo "Target episode URL: $URL"
echo "Output name:        $OUT"

# ----- Main loop: wait until on-demand, then run pipeline -----
while true; do
  if "$PY" yle_clip_bot.py "$URL" --out "$OUT"; then
    echo "✅ Done: $OUT"
    exit 0
  fi

  echo "ℹ️  Checking availability hint from yle-dl…"
  HINT="$($YLEDL -o /dev/null "$URL" 2>&1 || true)"
  TS="$(printf '%s\n' "$HINT" | sed -n 's/.*Becomes available on \([0-9T:+-]\{25,\}\).*/\1/p' | head -n1)"

  if [[ -n "${TS:-}" ]]; then
    echo "⏳ Not yet available. Release time: $TS"
    SLEEP_SECS="$("$PY" - "$TS" <<'PY'
import sys, datetime
dt = datetime.datetime.fromisoformat(sys.argv[1])
now = datetime.datetime.now(dt.tzinfo)
print(max(60, int((dt - now).total_seconds()) + 60))
PY
)"
    echo "💤 Sleeping ${SLEEP_SECS}s…"
    sleep "$SLEEP_SECS"
  else
    echo "🕒 No release hint; retrying in 10 minutes…"
    sleep 600
  fi
done
