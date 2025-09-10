#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ----- Config from environment -----
URL="${AREENA_URL:?Set AREENA_URL env var (episode URL or series:<ID>)}"
OUT="${OUT_NAME:-areena.mp4}"

# ----- Python binary on Render/local -----
PY="python3"
command -v "$PY" >/dev/null || { echo "❌ python3 not found"; exit 1; }

# ----- Tools sanity -----
command -v yle-dl >/dev/null || { echo "❌ yle-dl not found"; exit 1; }
command -v ffmpeg >/dev/null || { echo "❌ ffmpeg not found"; exit 1; }

echo "Using: $(yle-dl --version | head -n1), $(ffmpeg -version | head -n1)"

# ----- Resolve series:<ID> → latest episode URL via RSS -----
resolve_latest() {
  local sid="$1"
  local resolved
  resolved="$("$PY" - "$sid" <<'PY'
import sys, requests, xml.etree.ElementTree as ET
sid = sys.argv[1]
feed = f"https://feeds.yle.fi/areena/v1/series/{sid}.rss?downloadable=true"
try:
    r = requests.get(feed, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        pub  = (item.findtext("pubDate") or "").strip()
        if link: items.append((pub, link))
    if not items:
        print("", end="")
        sys.exit(0)
    latest = next((link for _,link in items if link), "")
    print(latest, end="")
except Exception:
    print("", end="")
PY
)"
  echo "$resolved"
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
  HINT="$(yle-dl -o /dev/null "$URL" 2>&1 || true)"
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
