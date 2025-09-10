#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ----- Config from environment -----
URL="${AREENA_URL:?Set AREENA_URL env var (episode URL or series:<ID>)}"
OUT="${OUT_NAME:-areena.mp4}"
URL="$(printf '%s' "$URL" | xargs)"  # trim

PY="python3"
command -v "$PY" >/dev/null || { echo "❌ python3 not found"; exit 1; }

# ----- Prepare yle-dl runner (binary or module; install if missing) -----
if command -v yle-dl >/dev/null 2>&1; then
  YLEDL="yle-dl"
elif "$PY" -c "import yle_dl" >/dev/null 2>&1; then
  YLEDL="$PY -m yle_dl"
else
  echo "ℹ️  yle-dl not found — installing now…"
  pip install --no-cache-dir yle-dl || { echo "❌ Failed to install yle-dl"; exit 1; }
  if command -v yle-dl >/dev/null 2>&1; then
    YLEDL="yle-dl"
  elif "$PY" -c "import yle_dl" >/dev/null 2>&1; then
    YLEDL="$PY -m yle_dl"
  else
    echo "❌ yle-dl still not available after install"
    exit 1
  fi
fi

# ----- ffmpeg sanity -----
command -v ffmpeg >/dev/null 2>&1 || { echo "❌ ffmpeg not found (apt.txt should install it)"; exit 1; }

echo "Using yle-dl via: $YLEDL"
echo "Using ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1)"

# ----- Resolve series:<ID> → latest episode URL (RSS → HTML fallback) -----
resolve_latest() {
  local sid="$1"
  "$PY" - <<'PY'
import os, re, sys, requests, xml.etree.ElementTree as ET
sid = sys.argv[1].strip()
session = requests.Session()
session.headers.update({"User-Agent":"Mozilla/5.0 (bot) yle-clip-bot"})

def try_rss(url):
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            if link:
                return link
    except Exception:
        pass
    return ""

# 1) downloadable-only feed
link = try_rss(f"https://feeds.yle.fi/areena/v1/series/{sid}.rss?downloadable=true")
# 2) plain feed
if not link:
    link = try_rss(f"https://feeds.yle.fi/areena/v1/series/{sid}.rss")

# 3) fallback: scrape series HTML page and pick the first episode link
if not link:
    try:
        r = session.get(f"https://areena.yle.fi/{sid}", timeout=20)
        r.raise_for_status()
        # Look for href="/1-76xxxxxx" (episodes)
        m = re.findall(rb'href="/(1-\d+)"', r.content)
        # Prefer ones that look like episodes (often 1-76... for programs)
        if m:
            # Deduplicate while preserving order
            seen=set()
            ordered=[]
            for b in m:
                s=b.decode("utf-8")
                if s not in seen:
                    seen.add(s); ordered.append(s)
            # Pick the first viable link
            if ordered:
                link = "https://areena.yle.fi/" + ordered[0]
    except Exception:
        pass

print(link or "", end="")
PY
}

if [[ "$URL" == series:* ]]; then
  SID="${URL#series:}"
  SID="$(printf '%s' "$SID" | xargs)"
  echo "Resolving latest episode for series ID: $SID"
  LATEST="$(resolve_latest "$SID" || true)"
  if [[ -z "${LATEST:-}" ]]; then
    echo "❌ Could not resolve latest episode for series $SID (RSS + HTML fallback failed)."
    echo "   Tip: set AREENA_URL to a specific episode URL to test."
    exit 1
  fi
  echo "Latest episode: $LATEST"
  URL="$LATEST"
fi

echo "Target episode URL: [$URL]"
echo "Output name:        [$OUT]"

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
    SLEEP_SECS="$("$PY" - <<PY
import datetime
dt = datetime.datetime.fromisoformat("$TS")
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
