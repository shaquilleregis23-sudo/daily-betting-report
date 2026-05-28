#!/usr/bin/env python3
"""
generate_site.py — Daily Betting Cheatsheet Generator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads picks_today.json + picks_log.json and writes
docs/index.html — a mobile-friendly static cheatsheet.

Runs as part of GitHub Actions after 6_predict_today.py.
GitHub Pages serves the result automatically.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_DIR  = os.path.dirname(BASE_DIR)
DOCS_DIR  = os.path.join(REPO_DIR, "docs")
PICKS_F   = os.path.join(BASE_DIR, "picks_today.json")
LOG_F     = os.path.join(BASE_DIR, "picks_log.json")
OUT_F     = os.path.join(DOCS_DIR, "index.html")
EASTERN   = ZoneInfo("America/New_York")

os.makedirs(DOCS_DIR, exist_ok=True)

# ── Load data ─────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

picks_data = load_json(PICKS_F, {})
log        = load_json(LOG_F,   [])

picks     = picks_data.get("picks", [])
all_games = picks_data.get("all_games", [])
today     = picks_data.get("date", datetime.now(EASTERN).strftime("%Y-%m-%d"))
gen_at    = picks_data.get("generated_at", "")[:16].replace("T", " ")

now_et    = datetime.now(EASTERN).strftime("%b %d, %Y · %I:%M %p ET")
today_fmt = datetime.strptime(today, "%Y-%m-%d").strftime("%A, %B %-d, %Y")

# ── Performance from log ──────────────────────────────────────────────────────

resolved = [r for r in log if r.get("result") in ("W", "L")]
pending  = [r for r in log if r.get("result") not in ("W", "L")]

wins   = sum(1 for r in resolved if r["result"] == "W")
losses = len(resolved) - wins
wr     = wins / len(resolved) if resolved else 0

def pnl(r):
    ml = r.get("ml", -110)
    if r["result"] == "W":
        return 100 / abs(ml) if ml < 0 else ml / 100
    return -1.0

net_units = sum(pnl(r) for r in resolved)
roi       = net_units / len(resolved) * 100 if resolved else 0

# Yesterday's picks (most recent resolved)
yesterday_picks = sorted(resolved, key=lambda r: r["date"], reverse=True)
yesterday_date  = yesterday_picks[0]["date"] if yesterday_picks else None
yesterdays      = [r for r in yesterday_picks if r["date"] == yesterday_date] if yesterday_date else []

# ── HTML helpers ──────────────────────────────────────────────────────────────

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def grade_pill(grade):
    colors = {
        "A+": ("linear-gradient(135deg,#ff6b35,#ff4500)", "#fff"),
        "A":  ("linear-gradient(135deg,#4ecca3,#00b894)", "#111"),
        "B+": ("linear-gradient(135deg,#7ec8e3,#0984e3)", "#111"),
        "B":  ("linear-gradient(135deg,#636e72,#2d3436)", "#fff"),
    }
    bg, fg = colors.get(grade, ("#444", "#fff"))
    return (
        f'<span style="background:{bg};color:{fg};border-radius:6px;'
        f'padding:2px 9px;font-size:0.75em;font-weight:800;'
        f'letter-spacing:0.5px;vertical-align:middle">{esc(grade)}</span>'
    )

def edge_pill(edge_pct, label):
    if "STRONG" in label:
        bg = "linear-gradient(135deg,#ff6b35,#d63031)"
        icon = "🔥🔥"
    elif "SOLID" in label:
        bg = "linear-gradient(135deg,#4ecca3,#00b894)"
        icon = "✅"
    else:
        bg = "linear-gradient(135deg,#7ec8e3,#0984e3)"
        icon = "📈"
    return (
        f'<span style="background:{bg};color:#fff;border-radius:6px;'
        f'padding:3px 10px;font-size:0.82em;font-weight:700">'
        f'{icon} {esc(edge_pct)}</span>'
    )

def record_cell(w, l, wr_val):
    rec = f"{w}-{l}"
    if wr_val >= 0.6:
        return f'<span style="color:#4ecca3;font-weight:700">{rec}</span>'
    elif wr_val <= 0.3:
        return f'<span style="color:#ff6b35;font-weight:700">{rec}</span>'
    else:
        return f'<span style="color:#ffeaa7;font-weight:600">{rec}</span>'

def l10_table(home_l10, away_l10, bet_team):
    if not home_l10 or not away_l10:
        return ""
    rows_html = ""
    for s in [home_l10, away_l10]:
        is_pick = s["team"] == bet_team
        star    = "★ " if is_pick else "&nbsp;&nbsp;"
        wr_val  = s.get("wr", 0)
        rec     = record_cell(s["w"], s["l"], wr_val)
        bold    = "font-weight:700;color:#fff" if is_pick else "color:#ccc"
        rows_html += f"""
        <tr>
          <td style="padding:5px 8px;{bold}">{star}{esc(s['team'])}</td>
          <td style="padding:5px 8px;text-align:center">{rec}</td>
          <td style="padding:5px 8px;text-align:center;color:#e0e0e0">{s['rspg']}</td>
          <td style="padding:5px 8px;text-align:center;color:#e0e0e0">{s['rapg']}</td>
          <td style="padding:5px 8px;text-align:center;color:#aaa">{esc(s['home'])}</td>
          <td style="padding:5px 8px;text-align:center;color:#aaa">{esc(s['away'])}</td>
        </tr>"""
    return f"""
    <div style="margin-top:12px;overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:0.82em;
                    background:rgba(0,0,0,0.25);border-radius:8px;overflow:hidden">
        <thead>
          <tr style="background:rgba(255,255,255,0.06);color:#999;font-size:0.78em;text-transform:uppercase;letter-spacing:0.5px">
            <th style="padding:6px 8px;text-align:left">Team</th>
            <th style="padding:6px 8px">L10</th>
            <th style="padding:6px 8px">R/G</th>
            <th style="padding:6px 8px">RA/G</th>
            <th style="padding:6px 8px">Home</th>
            <th style="padding:6px 8px">Away</th>
          </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
      </table>
      <p style="font-size:0.72em;color:#666;margin:5px 0 0 2px">
        ★ model's pick &nbsp;·&nbsp; 🟢 ≥6-4 &nbsp;🟡 5-5 &nbsp;🔴 ≤4-6 &nbsp;·&nbsp; 2026 season L10
      </p>
    </div>"""

def pick_card(pick):
    edge  = pick.get("edge", 0)
    grade = pick.get("grade", "")
    label = pick.get("label", "")

    if edge >= 0.08:
        border = "#ff6b35"
        glow   = "rgba(255,107,53,0.15)"
    elif edge >= 0.05:
        border = "#4ecca3"
        glow   = "rgba(78,204,163,0.12)"
    else:
        border = "#7ec8e3"
        glow   = "rgba(126,200,227,0.10)"

    grade_html = f"&nbsp;{grade_pill(grade)}" if grade else ""
    edge_html  = edge_pill(pick.get("edge_pct",""), label)

    kelly     = pick.get("kelly_fraction", 0)
    kelly_pct = pick.get("kelly_pct", f"{kelly:.1%}") if kelly else None
    kelly_html = (
        f'<div style="margin-top:8px;font-size:0.82em;color:#aaa">'
        f'💰 Kelly: <strong style="color:{border}">{esc(kelly_pct)}</strong> of bankroll'
        f'</div>'
    ) if kelly_pct and kelly else ""

    conf      = pick.get("model_prob", 0.5)
    bar_pct   = max(0, min(100, (conf - 0.5) * 200))
    bar_html  = f"""
    <div style="margin-top:10px">
      <div style="display:flex;justify-content:space-between;font-size:0.75em;color:#888;margin-bottom:3px">
        <span>Model confidence</span><span>{conf:.1%}</span>
      </div>
      <div style="height:5px;background:rgba(255,255,255,0.1);border-radius:3px;overflow:hidden">
        <div style="height:100%;width:{bar_pct:.0f}%;background:{border};border-radius:3px;
                    transition:width 0.6s ease"></div>
      </div>
    </div>"""

    l10_html = l10_table(
        pick.get("home_l10"), pick.get("away_l10"), pick.get("bet_team","")
    )

    return f"""
  <div style="border-left:5px solid {border};background:linear-gradient(135deg,{glow},rgba(255,255,255,0.02));
              border-radius:0 12px 12px 0;padding:18px 20px;margin-bottom:20px;
              box-shadow:0 2px 12px rgba(0,0,0,0.3)">
    <div style="font-size:0.7em;font-weight:800;letter-spacing:2px;color:#888;text-transform:uppercase;margin-bottom:4px">
      {esc(pick.get('sport','MLB'))}
    </div>
    <div style="font-size:0.88em;color:#aaa;margin-bottom:6px">{esc(pick.get('matchup',''))}</div>
    <div style="font-size:1.3em;font-weight:800;color:#fff;margin-bottom:8px">
      {esc(pick.get('bet',''))}{grade_html}
    </div>
    <div style="margin-bottom:6px">{edge_html}</div>
    <div style="font-size:0.84em;color:#aaa;margin-top:6px">
      Model <strong style="color:#e0e0e0">{esc(pick.get('model_p',''))}</strong>
      &nbsp;·&nbsp;
      Market <strong style="color:#e0e0e0">{esc(pick.get('market_p',''))}</strong>
    </div>
    {kelly_html}
    {bar_html}
    {l10_html}
  </div>"""

def result_row(r):
    result = r.get("result", "?")
    icon   = "✅" if result == "W" else "❌"
    ml     = r.get("ml", 0)
    ml_str = f"+{ml}" if ml >= 0 else str(ml)
    units  = pnl(r)
    units_str = f"+{units:.2f}" if units > 0 else f"{units:.2f}"
    units_col = "#4ecca3" if units > 0 else "#ff6b35"
    return f"""
    <tr>
      <td style="padding:8px 10px">{icon} {esc(r.get('bet_team',''))}</td>
      <td style="padding:8px 10px;color:#aaa;font-size:0.85em">{esc(r.get('matchup',''))}</td>
      <td style="padding:8px 10px;text-align:center;color:#ccc">{ml_str}</td>
      <td style="padding:8px 10px;text-align:center;color:{units_col};font-weight:700">{units_str}u</td>
    </tr>"""

def odds_row(g):
    has_pick = g.get("has_pick", False)
    star     = "⭐" if has_pick else ""
    hml      = g.get("home_ml", 0)
    aml      = g.get("away_ml", 0)
    hml_str  = f"+{hml}" if hml >= 0 else str(hml)
    aml_str  = f"+{aml}" if aml >= 0 else str(aml)
    edge     = g.get("edge", 0)
    edge_dir = "H" if edge > 0 else "A"
    edge_str = f"{edge_dir} +{abs(edge):.1%}" if abs(edge) >= 0.01 else "—"
    bg       = "rgba(255,107,53,0.08)" if has_pick else "transparent"
    return f"""
    <tr style="background:{bg}">
      <td style="padding:7px 10px;font-size:0.8em">{star} {esc(g.get('matchup',''))}</td>
      <td style="padding:7px 10px;text-align:center;color:#aaa;font-size:0.82em">{aml_str}</td>
      <td style="padding:7px 10px;text-align:center;color:#aaa;font-size:0.82em">{hml_str}</td>
      <td style="padding:7px 10px;text-align:center;font-size:0.82em">{g.get('model_prob',0):.1%}</td>
      <td style="padding:7px 10px;text-align:center;font-size:0.82em;color:#ffeaa7">{edge_str}</td>
    </tr>"""

# ── Build HTML ────────────────────────────────────────────────────────────────

picks_html = "".join(pick_card(p) for p in picks) if picks else """
  <div style="text-align:center;padding:40px;color:#666;font-size:1.1em">
    No edges found today — model agrees with the market.
  </div>"""

# Yesterday's results block
if yesterdays:
    ydate_fmt = datetime.strptime(yesterday_date, "%Y-%m-%d").strftime("%B %-d")
    y_rows    = "".join(result_row(r) for r in yesterdays)
    yw = sum(1 for r in yesterdays if r["result"] == "W")
    yl = len(yesterdays) - yw
    yesterday_html = f"""
  <section style="margin-bottom:32px">
    <h2 style="font-size:1em;font-weight:800;letter-spacing:2px;text-transform:uppercase;
               color:#888;margin-bottom:14px">📋 {ydate_fmt} Results — {yw}W {yl}L</h2>
    <div style="overflow-x:auto;background:rgba(255,255,255,0.03);border-radius:10px">
      <table style="width:100%;border-collapse:collapse;font-size:0.88em">
        <thead>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#666;font-size:0.75em;text-transform:uppercase">
            <th style="padding:8px 10px;text-align:left">Bet</th>
            <th style="padding:8px 10px;text-align:left">Game</th>
            <th style="padding:8px 10px;text-align:center">ML</th>
            <th style="padding:8px 10px;text-align:center">Units</th>
          </tr>
        </thead>
        <tbody>{y_rows}</tbody>
      </table>
    </div>
  </section>"""
else:
    yesterday_html = ""

# Full odds board
odds_rows = "".join(odds_row(g) for g in all_games) if all_games else ""
odds_board = f"""
  <section style="margin-bottom:32px">
    <h2 style="font-size:1em;font-weight:800;letter-spacing:2px;text-transform:uppercase;
               color:#888;margin-bottom:14px">📋 Full Odds Board</h2>
    <div style="overflow-x:auto;background:rgba(255,255,255,0.03);border-radius:10px">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#666;font-size:0.72em;text-transform:uppercase;letter-spacing:0.5px">
            <th style="padding:8px 10px;text-align:left">Game</th>
            <th style="padding:8px 10px">Away ML</th>
            <th style="padding:8px 10px">Home ML</th>
            <th style="padding:8px 10px">Model</th>
            <th style="padding:8px 10px">Edge</th>
          </tr>
        </thead>
        <tbody>{odds_rows}</tbody>
      </table>
    </div>
    <p style="font-size:0.72em;color:#555;margin-top:6px">
      ⭐ = pick generated &nbsp;·&nbsp; Edge: H=home model edge, A=away model edge
    </p>
  </section>""" if odds_rows else ""

# Season stats bar
wr_pct   = f"{wr:.1%}"
net_str  = f"{net_units:+.2f}"
roi_str  = f"{roi:+.1f}%"
net_col  = "#4ecca3" if net_units >= 0 else "#ff6b35"

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="3600">
  <title>🎯 Picks — {today_fmt}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: #0a0a14;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100vh;
      padding: 0 0 60px 0;
    }}

    /* Hero header */
    .hero {{
      background: linear-gradient(135deg, #0d0d20 0%, #1a0a2e 50%, #0d0d20 100%);
      border-bottom: 1px solid rgba(255,255,255,0.06);
      padding: 24px 20px 20px;
      text-align: center;
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(12px);
    }}

    .hero h1 {{
      font-size: 1.6em;
      font-weight: 900;
      letter-spacing: -0.5px;
      background: linear-gradient(135deg, #ff6b35, #ffeaa7, #4ecca3);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}

    .hero .date {{
      font-size: 0.82em;
      color: #666;
      margin-top: 4px;
    }}

    /* Stats bar */
    .stats-bar {{
      display: flex;
      gap: 0;
      background: rgba(255,255,255,0.03);
      border-bottom: 1px solid rgba(255,255,255,0.06);
      overflow-x: auto;
      scrollbar-width: none;
    }}
    .stats-bar::-webkit-scrollbar {{ display: none; }}

    .stat-item {{
      flex: 1;
      min-width: 80px;
      text-align: center;
      padding: 12px 8px;
      border-right: 1px solid rgba(255,255,255,0.05);
    }}
    .stat-item:last-child {{ border-right: none; }}
    .stat-value {{ font-size: 1.1em; font-weight: 800; }}
    .stat-label {{ font-size: 0.65em; color: #555; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 1px; }}

    /* Main content */
    .container {{
      max-width: 680px;
      margin: 0 auto;
      padding: 24px 16px;
    }}

    section h2 {{
      font-size: 1em;
      font-weight: 800;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: #888;
      margin-bottom: 14px;
    }}

    /* Pick count badge */
    .pick-count {{
      display: inline-block;
      background: linear-gradient(135deg, #ff6b35, #d63031);
      color: #fff;
      border-radius: 20px;
      padding: 2px 10px;
      font-size: 0.72em;
      font-weight: 700;
      margin-left: 8px;
      vertical-align: middle;
    }}

    /* Updated tag */
    .updated {{
      font-size: 0.68em;
      color: #444;
      text-align: center;
      margin-top: -4px;
      padding-bottom: 16px;
    }}

    /* Divider */
    .divider {{
      border: none;
      border-top: 1px solid rgba(255,255,255,0.06);
      margin: 24px 0;
    }}

    /* Footer */
    footer {{
      text-align: center;
      font-size: 0.72em;
      color: #333;
      padding: 16px;
      margin-top: 32px;
      border-top: 1px solid rgba(255,255,255,0.04);
    }}

    @media (max-width: 400px) {{
      .hero h1 {{ font-size: 1.3em; }}
      .stat-value {{ font-size: 0.95em; }}
    }}
  </style>
</head>
<body>

  <!-- ── Header ── -->
  <div class="hero">
    <h1>🎯 Daily Picks</h1>
    <div class="date">{today_fmt}</div>
  </div>

  <!-- ── Season stats bar ── -->
  <div class="stats-bar">
    <div class="stat-item">
      <div class="stat-value" style="color:#4ecca3">{wr_pct}</div>
      <div class="stat-label">Win Rate</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" style="color:{net_col}">{net_str}u</div>
      <div class="stat-label">Net Units</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" style="color:{net_col}">{roi_str}</div>
      <div class="stat-label">ROI</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" style="color:#ffeaa7">{wins}W–{losses}L</div>
      <div class="stat-label">Record</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" style="color:#e0e0e0">{len(picks)}</div>
      <div class="stat-label">Today</div>
    </div>
  </div>

  <div class="container">

    <!-- ── Today's Picks ── -->
    <section style="margin-bottom:32px">
      <h2>🎯 Today's Picks
        <span class="pick-count">{len(picks)} bet{"s" if len(picks) != 1 else ""}</span>
      </h2>
      {picks_html}
    </section>

    <hr class="divider">

    <!-- ── Yesterday's Results ── -->
    {yesterday_html}

    <!-- ── Full Odds Board ── -->
    {odds_board}

  </div>

  <footer>
    Updated {gen_at} ET &nbsp;·&nbsp; Refreshes automatically every hour<br>
    Model picks powered by 6-layer MLB model (L10 · ERA · Bullpen · Weather · Rest · H/A splits)
  </footer>

</body>
</html>"""

with open(OUT_F, "w") as f:
    f.write(html)

print(f"✅  Site written to {OUT_F}")
print(f"    {len(picks)} picks · {wins}W-{losses}L season · {net_units:+.2f} units")
