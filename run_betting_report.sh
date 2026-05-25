#!/bin/bash
# Daily betting report + model picks — emailed via Brevo
#
# Order of operations:
#   1. Mark yesterday's results (auto W/L in picks_log.json)
#   2. Fetch today's odds from ESPN (free, no key)
#   3. Run ML model → generate model picks
#   4. Pull full odds report (FanDuel / DraftKings / BetMGM)
#   5. Combine everything → send one email

DOCS=/Users/shaquilleregis/Documents
MODEL=$DOCS/model

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [1/4] Marking yesterday's results..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
/usr/bin/python3 $MODEL/7_mark_results.py > /tmp/_sr_results.txt 2>&1
cat /tmp/_sr_results.txt

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [2/4] Fetching ESPN odds..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
/usr/bin/python3 $MODEL/2_fetch_odds_espn.py > /dev/null 2>&1

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [3/4] Running model predictions..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
/usr/bin/python3 $MODEL/6_predict_today.py > /dev/null 2>&1

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " [4/4] Pulling full odds report..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
/usr/bin/python3 $DOCS/sports_betting_daily.py > /tmp/_sr_odds.txt 2>&1

echo ""
echo "Sending email..."
/usr/bin/python3 - <<'PYEOF'
import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
today   = datetime.now(EASTERN).strftime("%A, %B %-d")
MODEL   = "/Users/shaquilleregis/Documents/model"

API_KEY = "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"

def read(path, fallback=""):
    try:
        return open(path).read().strip()
    except Exception:
        return fallback

picks   = read(f"{MODEL}/picks_today.txt")
results = read("/tmp/_sr_results.txt")
odds    = read("/tmp/_sr_odds.txt")

div = "━" * 52

sections = []

# Model picks — goes first (most actionable)
if picks:
    sections.append(picks)

# Yesterday's results — only include if there's real content
if results and any(c in results for c in ["✅", "❌", "Resolved"]):
    sections.append(f"{div}\nYESTERDAY'S RESULTS\n{div}\n{results}")

# Full odds report
if odds:
    sections.append(odds)

output = "\n\n".join(sections)

html = (
    "<pre style='font-family:monospace;font-size:14px;line-height:1.6;"
    "background:#0f0f1a;color:#e8e8e8;padding:20px;border-radius:8px;'>"
    + output.replace("<", "&lt;").replace(">", "&gt;")
    + "</pre>"
)

payload = json.dumps({
    "sender":      {"name": "Betting Report", "email": "shaqr@nychvacpro.com"},
    "to":          [{"email": "shaqr@nychvacpro.com",         "name": "Shaquille"},
                    {"email": "shaquilleregis23@gmail.com",    "name": "Shaquille"}],
    "subject":     f"🎯 Picks + Report — {today}",
    "htmlContent": html,
}).encode()

req = urllib.request.Request(
    "https://api.brevo.com/v3/smtp/email",
    data=payload,
    headers={
        "api-key":      API_KEY,
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    urllib.request.urlopen(req)
    print("✅ Email sent.")
except Exception as e:
    print(f"❌ Email error: {e}")

# Cleanup
for f in ["/tmp/_sr_results.txt", "/tmp/_sr_odds.txt"]:
    try:
        os.remove(f)
    except Exception:
        pass
PYEOF
