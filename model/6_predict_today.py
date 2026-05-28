#!/usr/bin/env python3
"""
Script 6: 6_predict_today.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Loads trained MLB + NBA models, pulls today's games and odds, and
outputs a ranked edge report ready to paste into the daily email.

Data flow (zero extra API calls):
  Lines:     lines_cache.json  ← written by sports_betting_daily.py
  Schedule:  MLB Stats API (free) + nba_api (free)
  History:   sports_history.db  ← built by 1_collect_historical.py

Run this AFTER sports_betting_daily.py (which populates the cache).
Or run it standalone — it falls back gracefully if cache is partial.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import sqlite3
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR  = os.path.dirname(BASE_DIR)
DB_PATH     = os.path.join(BASE_DIR,   "sports_history.db")
CACHE_FILE  = os.path.join(PARENT_DIR, "lines_cache.json")
MODEL_DIR   = os.path.join(BASE_DIR,   "models")

# ── Config ────────────────────────────────────────────────────────────────────
EDGE_THRESHOLD = 0.03    # Minimum edge to show a pick (3 pp)
EASTERN        = ZoneInfo("America/New_York")
MLB_API        = "https://statsapi.mlb.com/api/v1"

PARK_FACTORS = {
    "Colorado Rockies":1.38,"Cincinnati Reds":1.18,"Boston Red Sox":1.14,
    "New York Yankees":1.12,"Texas Rangers":1.10,"Baltimore Orioles":1.08,
    "Philadelphia Phillies":1.07,"Chicago Cubs":1.06,"Atlanta Braves":1.05,
    "Toronto Blue Jays":1.04,"Milwaukee Brewers":1.03,"Minnesota Twins":1.02,
    "Kansas City Royals":1.01,"Detroit Tigers":1.00,"Houston Astros":1.00,
    "Cleveland Guardians":0.99,"Cleveland Indians":0.99,
    "St. Louis Cardinals":0.98,"New York Mets":0.97,"Pittsburgh Pirates":0.96,
    "Chicago White Sox":0.95,"Washington Nationals":0.95,
    "Los Angeles Angels":0.94,"Los Angeles Dodgers":0.93,
    "Arizona Diamondbacks":0.92,"Tampa Bay Rays":0.91,
    "San Diego Padres":0.90,"Oakland Athletics":0.90,"Athletics":0.90,
    "Seattle Mariners":0.89,"Miami Marlins":0.88,"San Francisco Giants":0.86,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def today_str():
    return datetime.now(EASTERN).strftime("%Y-%m-%d")

def fmt_odds(ml):
    return f"+{int(ml)}" if ml >= 0 else str(int(ml))

def american_to_prob(ml):
    ml = float(ml)
    return 100 / (ml + 100) if ml >= 0 else abs(ml) / (abs(ml) + 100)

def no_vig(home_ml, away_ml):
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    t  = ph + pa
    return round(ph / t, 4), round(pa / t, 4)

def fetch_json(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

# ── Load models ───────────────────────────────────────────────────────────────

def load_model(sport):
    path = os.path.join(MODEL_DIR, f"{sport.lower()}_model.pkl")
    if not os.path.exists(path):
        return None
    return joblib.load(path)

def predict(bundle, feature_row):
    """Run ensemble prediction on a single-row dict. Returns home win prob."""
    xgb_m   = bundle["xgb"]
    lr_m    = bundle["lr"]
    scaler  = bundle["scaler"]
    feats   = bundle["features"]

    X = pd.DataFrame([feature_row])[feats].fillna(0.5)
    p_xgb = xgb_m.predict_proba(X)[0][1]
    p_lr  = lr_m.predict_proba(scaler.transform(X))[0][1]
    return round(0.65 * p_xgb + 0.35 * p_lr, 4)

# ── Lines cache ───────────────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE))
        except Exception:
            pass
    return {}

def get_cached_odds(cache, sport_label, home_team, away_team, date=None):
    """
    Extract home_ml and away_ml from lines_cache.json.
    Cache key format: '{date}|{sport}|{away} @ {home}|{team}'
    """
    date = date or today_str()
    matchup = f"{away_team} @ {home_team}"
    home_key = f"{date}|{sport_label}|{matchup}|{home_team}"
    away_key = f"{date}|{sport_label}|{matchup}|{away_team}"
    home_ml  = cache.get(home_key)
    away_ml  = cache.get(away_key)
    return home_ml, away_ml

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_mlb_rolling(conn, team, before_date, window=10):
    """Last-N games win rate and run differential for an MLB team."""
    sql = """
        SELECT home_score, away_score,
               CASE WHEN home_team=? THEN 1 ELSE 0 END as is_home
        FROM mlb_games
        WHERE (home_team=? OR away_team=?)
          AND game_date < ?
          AND status = 'Final'
        ORDER BY game_date DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (team, team, team, before_date, window)).fetchall()
    if not rows:
        return 0.5, 0.0

    wins, margins = [], []
    for home_score, away_score, is_home in rows:
        if is_home:
            wins.append(1 if home_score > away_score else 0)
            margins.append(home_score - away_score)
        else:
            wins.append(1 if away_score > home_score else 0)
            margins.append(away_score - home_score)
    return round(np.mean(wins), 4), round(np.mean(margins), 3)


def get_l10_full(conn, team, before_date, window=10):
    """Full L10 breakdown: record, R/G, RA/G, home split, away split, last game."""
    sql = """
        SELECT home_team, away_team, home_score, away_score, game_date
        FROM mlb_games
        WHERE (home_team=? OR away_team=?)
          AND game_date < ?
          AND status = 'Final'
        ORDER BY game_date DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (team, team, before_date, window)).fetchall()
    if not rows:
        return {"team": team, "w": 0, "l": 0, "n": 0, "wr": 0.5,
                "rspg": 0.0, "rapg": 0.0, "home": "0-0", "away": "0-0",
                "last_game": "N/A"}

    wins = losses = hw = hl = aw = al = 0
    rs = ra = 0
    last_game = rows[0][4]

    for ht, at, hs, as_, _ in rows:
        is_home = (ht == team)
        won = (hs > as_) if is_home else (as_ > hs)
        if won:
            wins += 1
            if is_home: hw += 1
            else:        aw += 1
        else:
            losses += 1
            if is_home: hl += 1
            else:        al += 1
        if is_home:
            rs += hs; ra += as_
        else:
            rs += as_; ra += hs

    n = wins + losses
    return {
        "team":      team,
        "w":         wins,
        "l":         losses,
        "n":         n,
        "wr":        round(wins / n, 3) if n else 0.5,
        "rspg":      round(rs / n, 1)  if n else 0.0,
        "rapg":      round(ra / n, 1)  if n else 0.0,
        "home":      f"{hw}-{hl}",
        "away":      f"{aw}-{al}",
        "last_game": last_game,
    }

def get_nba_rolling(conn, team_name, before_date, window=10):
    """Last-N games win rate and scoring margin for an NBA team."""
    sql = """
        SELECT pts, opp_pts, winner
        FROM nba_games
        WHERE team_name=? AND game_date < ?
        ORDER BY game_date DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (team_name, before_date, window)).fetchall()
    if not rows:
        return 0.5, 0.0
    wins    = [r[2] for r in rows]
    margins = [r[0] - r[1] for r in rows]
    return round(np.mean(wins), 4), round(np.mean(margins), 3)

def get_nba_rest(conn, team_name, before_date):
    """Days of rest since last game."""
    sql = """
        SELECT MAX(game_date) FROM nba_games
        WHERE team_name=? AND game_date < ?
    """
    row = conn.execute(sql, (team_name, before_date)).fetchone()
    if not row or not row[0]:
        return 3
    last = datetime.strptime(row[0], "%Y-%m-%d").date()
    today = datetime.strptime(before_date, "%Y-%m-%d").date()
    return (today - last).days

def get_nba_ratings(conn, team_name):
    """Season-level ORTG, DRTG, net rating, pace (most recent season)."""
    sql = """
        SELECT a.off_rating, a.def_rating, a.net_rating, a.pace
        FROM nba_team_advanced a
        JOIN nba_games g ON g.team_id = a.team_id AND g.season = a.season
        WHERE g.team_name = ?
        ORDER BY a.season DESC
        LIMIT 1
    """
    row = conn.execute(sql, (team_name,)).fetchone()
    if row:
        return {"off": row[0], "def": row[1], "net": row[2], "pace": row[3]}
    return {"off": 110.0, "def": 110.0, "net": 0.0, "pace": 100.0}

def get_mlb_starter_stats(conn, pitcher_name, season):
    """xFIP, ERA, K% for a starting pitcher this season."""
    sql = """
        SELECT xfip, fip, era, k_pct, bb_pct
        FROM mlb_pitcher_stats
        WHERE player_name=? AND season=?
        LIMIT 1
    """
    row = conn.execute(sql, (pitcher_name, season)).fetchone()
    if row:
        return {"xfip": row[0], "fip": row[1], "era": row[2],
                "k_pct": row[3], "bb_pct": row[4]}
    return {"xfip": None, "fip": None, "era": None, "k_pct": None, "bb_pct": None}

# ── Today's schedule ──────────────────────────────────────────────────────────

def get_mlb_today():
    """Fetch today's MLB games with probable starters from MLB Stats API."""
    today = today_str()
    url   = (f"{MLB_API}/schedule?sportId=1&date={today}"
             f"&hydrate=probablePitcher&gameType=R")
    data  = fetch_json(url)
    if not data:
        return []

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            status = game.get("status", {}).get("abstractGameState", "")
            if status == "Final":
                continue  # skip already-played games
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            games.append({
                "home_team":    home["team"]["name"],
                "away_team":    away["team"]["name"],
                "home_starter": home.get("probablePitcher", {}).get("fullName", "TBD"),
                "away_starter": away.get("probablePitcher", {}).get("fullName", "TBD"),
                "game_time":    game.get("gameDate", ""),
            })
    return games

def get_nba_today():
    """Fetch today's NBA games via nba_api."""
    try:
        from nba_api.stats.endpoints import scoreboard
        import time
        time.sleep(0.5)
        sb  = scoreboard.Scoreboard()
        df  = sb.get_data_frames()[0]
        games = []
        for _, row in df.iterrows():
            games.append({
                "home_team": row.get("HOME_TEAM_NAME", ""),
                "away_team": row.get("VISITOR_TEAM_NAME", ""),
            })
        return [g for g in games if g["home_team"] and g["away_team"]]
    except Exception:
        return []

# ── Feature builders ──────────────────────────────────────────────────────────

def build_mlb_features(game, home_ml, away_ml, cache, conn):
    today = today_str()
    season = int(today[:4])

    home_win_l10, home_diff_l10 = get_mlb_rolling(conn, game["home_team"], today)
    away_win_l10, away_diff_l10 = get_mlb_rolling(conn, game["away_team"], today)

    open_hp, _ = no_vig(home_ml, away_ml)

    # Line movement from cache (opening vs current — 0 if same)
    open_home_ml, open_away_ml = home_ml, away_ml  # cache IS the opening line
    ml_move = home_ml - open_home_ml  # will be 0 if same

    home_sp = get_mlb_starter_stats(conn, game.get("home_starter",""), season)
    away_sp = get_mlb_starter_stats(conn, game.get("away_starter",""), season)

    return {
        "open_home_prob":   open_hp,
        "ml_move_home":     ml_move,
        "total_move":       0.0,
        "sharp_home":       0,
        "sharp_away":       0,
        "park_factor":      PARK_FACTORS.get(game["home_team"], 1.0),
        "home_win_rate_l10": home_win_l10,
        "away_win_rate_l10": away_win_l10,
        "win_rate_diff":    home_win_l10 - away_win_l10,
        "home_run_diff_l10": home_diff_l10,
        "away_run_diff_l10": away_diff_l10,
        "run_diff_diff":    home_diff_l10 - away_diff_l10,
        "home_sp_xfip":     home_sp["xfip"],
        "away_sp_xfip":     away_sp["xfip"],
        "xfip_diff":        (home_sp["xfip"] - away_sp["xfip"])
                             if home_sp["xfip"] and away_sp["xfip"] else None,
        "home_sp_era":      home_sp["era"],
        "away_sp_era":      away_sp["era"],
        "home_sp_k_pct":    home_sp["k_pct"],
        "away_sp_k_pct":    away_sp["k_pct"],
        "home_wrc_plus":    None,
        "away_wrc_plus":    None,
        "wrc_diff":         None,
        "home_obp":         None,
        "away_obp":         None,
    }

def build_nba_features(game, home_ml, away_ml, conn):
    today = today_str()

    home_ratings = get_nba_ratings(conn, game["home_team"])
    away_ratings = get_nba_ratings(conn, game["away_team"])
    home_rest    = get_nba_rest(conn, game["home_team"], today)
    away_rest    = get_nba_rest(conn, game["away_team"], today)
    home_win_l10, home_margin_l10 = get_nba_rolling(conn, game["home_team"], today)
    away_win_l10, away_margin_l10 = get_nba_rolling(conn, game["away_team"], today)

    open_hp, _ = no_vig(home_ml, away_ml)

    return {
        "open_home_prob":   open_hp,
        "sharp_home":       0,
        "sharp_away":       0,
        "home_off_rating":  home_ratings["off"],
        "away_off_rating":  away_ratings["off"],
        "off_rating_diff":  home_ratings["off"] - away_ratings["off"],
        "home_def_rating":  home_ratings["def"],
        "away_def_rating":  away_ratings["def"],
        "def_rating_diff":  home_ratings["def"] - away_ratings["def"],
        "home_net_rating":  home_ratings["net"],
        "away_net_rating":  away_ratings["net"],
        "net_rating_diff":  home_ratings["net"] - away_ratings["net"],
        "home_pace":        home_ratings["pace"],
        "away_pace":        away_ratings["pace"],
        "pace_diff":        abs(home_ratings["pace"] - away_ratings["pace"]),
        "home_rest_days":   home_rest,
        "away_rest_days":   away_rest,
        "rest_diff":        home_rest - away_rest,
        "home_b2b":         int(home_rest <= 1),
        "away_b2b":         int(away_rest <= 1),
        "b2b_diff":         int(away_rest <= 1) - int(home_rest <= 1),
        "home_win_rate_l10": home_win_l10,
        "away_win_rate_l10": away_win_l10,
        "win_rate_diff":    home_win_l10 - away_win_l10,
        "home_margin_l10":  home_margin_l10,
        "away_margin_l10":  away_margin_l10,
        "margin_diff":      home_margin_l10 - away_margin_l10,
    }

# ── Output formatting ─────────────────────────────────────────────────────────

def edge_bar(edge):
    """Visual edge indicator."""
    if edge >= 0.08:  return "🔥🔥 STRONG EDGE"
    if edge >= 0.05:  return "✅  SOLID EDGE"
    if edge >= 0.03:  return "📈 EDGE"
    return "—"

def format_pick(sport, game, model_prob, market_prob, home_ml, away_ml, rec_side,
                home_l10=None, away_l10=None):
    home  = game["home_team"]
    away  = game["away_team"]
    edge  = abs(model_prob - market_prob)
    label = edge_bar(edge)

    if rec_side == "home":
        bet_team   = home
        bet_ml_int = home_ml
        side_prob  = model_prob
        side_mkt   = market_prob
    else:
        bet_team   = away
        bet_ml_int = away_ml
        side_prob  = 1 - model_prob
        side_mkt   = 1 - market_prob

    pick = {
        "sport":       sport,
        "matchup":     f"{away} @ {home}",
        "home_team":   home,
        "away_team":   away,
        "bet_team":    bet_team,
        "bet_ml":      bet_ml_int,
        "bet":         f"{bet_team} ML ({fmt_odds(bet_ml_int)})",
        "side":        rec_side,
        "model_prob":  round(side_prob, 4),
        "market_prob": round(side_mkt, 4),
        "edge":        round(edge, 4),
        "edge_pct":    f"+{edge:.1%}",
        "label":       label,
        # Legacy text fields kept for print_report / email
        "model_p":     f"{side_prob:.1%}",
        "market_p":    f"{side_mkt:.1%}",
        "edge_raw":    edge,
    }

    # Attach L10 stats for both teams (used in dashboard + email)
    if home_l10:
        pick["home_l10"] = home_l10
    if away_l10:
        pick["away_l10"] = away_l10

    # Grade based on L10 comparison
    if home_l10 and away_l10:
        pick["grade"] = _pick_grade(pick, home_l10, away_l10)

    return pick


def _pick_grade(pick, home_l10, away_l10):
    """A+/A/B+/B based on L10 form of bet team vs opponent."""
    bet_l10  = home_l10 if pick["side"] == "home" else away_l10
    opp_l10  = away_l10 if pick["side"] == "home" else home_l10
    wr_diff  = bet_l10["wr"] - opp_l10["wr"]
    opp_cold = opp_l10["w"] <= 2  # opponent ≤2-8 in L10

    if opp_cold and wr_diff >= 0:
        return "A+"
    if wr_diff >= 0.2:
        return "A"
    if wr_diff >= 0.0:
        return "B+"
    return "B"

def print_report(picks, today):
    print("\n" + "━"*58)
    print(f"  🤖 MODEL PICKS — {today}")
    print(f"  Edge threshold: {EDGE_THRESHOLD:.0%}   Flat 1-unit bets")
    print("━"*58)

    if not picks:
        print("  No edges found today above threshold.")
        print("  All games: model agrees with market.")
        print("━"*58)
        return

    # Sort by edge descending
    picks.sort(key=lambda p: p["edge_raw"], reverse=True)

    for p in picks:
        print(f"\n  [{p['sport']}]  {p['matchup']}")
        print(f"  BET:    {p['bet']}")
        print(f"  Model:  {p['model_p']}  |  Market: {p['market_p']}  |  Edge: {p['edge']}  {p['label']}")

    print(f"\n  {len(picks)} bet(s) today  ·  1 unit each")
    print("━"*58)

def report_as_text(picks, today):
    """Plain-text version for email insertion — includes L10 breakdown."""
    div = "━" * 52
    lines = [div, f"MODEL PICKS — {today}", div]

    if not picks:
        lines.append("No edges found today (model agrees with market on all games).")
    else:
        picks.sort(key=lambda p: p["edge_raw"], reverse=True)
        for p in picks:
            grade = p.get("grade", "")
            grade_str = f"  [{grade}]" if grade else ""
            lines.append(f"\n[{p['sport']}] {p['matchup']}")
            lines.append(f"  BET: {p['bet']}{grade_str}")
            lines.append(f"  Model {p['model_p']} vs Market {p['market_p']} | Edge {p['edge_pct']} {p['label']}")

            # L10 table
            hl = p.get("home_l10")
            al = p.get("away_l10")
            if hl and al:
                lines.append(f"  {'─'*48}")
                lines.append(f"  {'TEAM':<26} {'L10':>5}  {'R/G':>5}  {'RA/G':>5}  {'Home':>5}  {'Away':>5}")
                for s, marker in [(hl, "H"), (al, "A")]:
                    star = "★ " if s["team"] == p["bet_team"] else "  "
                    rec  = f"{s['w']}-{s['l']}"
                    lines.append(
                        f"  {star}{s['team']:<24} {rec:>5}  {s['rspg']:>5.1f}  "
                        f"{s['rapg']:>5.1f}  {s['home']:>5}  {s['away']:>5}"
                    )
                lines.append(f"  {'─'*48}")

    lines.append(div)
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def run_predictions():
    today = today_str()
    print(f"Loading models and data for {today}...")

    # Load models
    mlb_bundle = load_model("mlb")
    nba_bundle = load_model("nba")

    if not mlb_bundle and not nba_bundle:
        print("[ERROR] No trained models found. Run 4_train_model.py first.")
        return [], []

    # Load odds cache and DB
    cache = load_cache()
    conn  = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None

    picks     = []
    all_games = []  # every game with odds (for odds board)

    # ── MLB ──
    if mlb_bundle:
        print("\n[MLB] Fetching today's schedule...")
        mlb_games = get_mlb_today()
        print(f"  {len(mlb_games)} games today")

        for game in mlb_games:
            home_ml, away_ml = get_cached_odds(
                cache, "MLB", game["home_team"], game["away_team"], today
            )
            if home_ml is None or away_ml is None:
                print(f"  No odds cached: {game['away_team']} @ {game['home_team']}")
                continue

            feats      = build_mlb_features(game, home_ml, away_ml, cache, conn)
            model_prob = predict(mlb_bundle, feats)
            mkt_hp, _  = no_vig(home_ml, away_ml)

            edge_home = model_prob - mkt_hp
            edge_away = -edge_home

            print(f"  {game['away_team']:25} @ {game['home_team']:25}  "
                  f"model={model_prob:.1%}  market={mkt_hp:.1%}  "
                  f"edge={edge_home:+.1%}")

            game_entry = {
                "sport":       "MLB",
                "matchup":     f"{game['away_team']} @ {game['home_team']}",
                "home_team":   game["home_team"],
                "away_team":   game["away_team"],
                "home_ml":     home_ml,
                "away_ml":     away_ml,
                "model_prob":  round(model_prob, 4),
                "market_prob": round(mkt_hp, 4),
                "edge":        round(edge_home, 4),
                "has_pick":    False,
            }
            all_games.append(game_entry)

            if edge_home > EDGE_THRESHOLD and home_ml >= -250:
                home_l10 = get_l10_full(conn, game["home_team"], today) if conn else None
                away_l10 = get_l10_full(conn, game["away_team"], today) if conn else None
                picks.append(format_pick("MLB", game, model_prob, mkt_hp,
                                         home_ml, away_ml, "home",
                                         home_l10=home_l10, away_l10=away_l10))
                game_entry["has_pick"] = True
            elif edge_away > EDGE_THRESHOLD and away_ml >= -250:
                home_l10 = get_l10_full(conn, game["home_team"], today) if conn else None
                away_l10 = get_l10_full(conn, game["away_team"], today) if conn else None
                picks.append(format_pick("MLB", game, model_prob, mkt_hp,
                                         home_ml, away_ml, "away",
                                         home_l10=home_l10, away_l10=away_l10))
                game_entry["has_pick"] = True

    # ── NBA ──
    if nba_bundle:
        print("\n[NBA] Fetching today's schedule...")
        nba_games = get_nba_today()
        print(f"  {len(nba_games)} games today")

        for game in nba_games:
            home_ml, away_ml = get_cached_odds(
                cache, "NBA", game["home_team"], game["away_team"], today
            )
            if home_ml is None or away_ml is None:
                print(f"  No odds cached: {game['away_team']} @ {game['home_team']}")
                continue

            feats      = build_nba_features(game, home_ml, away_ml, conn)
            model_prob = predict(nba_bundle, feats)
            mkt_hp, _  = no_vig(home_ml, away_ml)

            edge_home = model_prob - mkt_hp
            edge_away = -edge_home

            print(f"  {game['away_team']:25} @ {game['home_team']:25}  "
                  f"model={model_prob:.1%}  market={mkt_hp:.1%}  "
                  f"edge={edge_home:+.1%}")

            game_entry = {
                "sport":       "NBA",
                "matchup":     f"{game['away_team']} @ {game['home_team']}",
                "home_team":   game["home_team"],
                "away_team":   game["away_team"],
                "home_ml":     home_ml,
                "away_ml":     away_ml,
                "model_prob":  round(model_prob, 4),
                "market_prob": round(mkt_hp, 4),
                "edge":        round(edge_home, 4),
                "has_pick":    False,
            }
            all_games.append(game_entry)

            if edge_home > EDGE_THRESHOLD and home_ml >= -250:
                picks.append(format_pick("NBA", game, model_prob, mkt_hp,
                                         home_ml, away_ml, "home"))
                game_entry["has_pick"] = True
            elif edge_away > EDGE_THRESHOLD and away_ml >= -250:
                picks.append(format_pick("NBA", game, model_prob, mkt_hp,
                                         home_ml, away_ml, "away"))
                game_entry["has_pick"] = True

    if conn:
        conn.close()

    return picks, all_games


def main():
    today     = today_str()
    picks, all_games = run_predictions()
    print_report(picks, today)

    now_iso = datetime.now(EASTERN).isoformat(timespec="seconds")

    # ── picks_today.json  (Streamlit app reads this) ───────────────────────────
    mlb_picks = [p for p in picks if p["sport"] == "MLB"]
    nba_picks = [p for p in picks if p["sport"] == "NBA"]
    payload = {
        "date":          today,
        "generated_at":  now_iso,
        "picks":         sorted(picks, key=lambda p: p["edge"], reverse=True),
        "all_games":     all_games,
        "summary": {
            "total_picks": len(picks),
            "mlb_picks":   len(mlb_picks),
            "nba_picks":   len(nba_picks),
        },
    }
    json_path = os.path.join(BASE_DIR, "picks_today.json")
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nJSON  saved to {json_path}")

    # ── picks_log.json  (performance tracker — append new picks, keep history) ─
    log_path = os.path.join(BASE_DIR, "picks_log.json")
    log = []
    if os.path.exists(log_path):
        try:
            log = json.load(open(log_path))
        except Exception:
            pass

    existing_keys = {(e["date"], e["matchup"], e["bet_team"]) for e in log}
    for pick in picks:
        key = (today, pick["matchup"], pick["bet_team"])
        if key not in existing_keys:
            log.append({
                "date":      today,
                "sport":     pick["sport"],
                "matchup":   pick["matchup"],
                "bet_team":  pick["bet_team"],
                "ml":        pick["bet_ml"],
                "edge":      pick["edge"],
                "edge_pct":  pick["edge_pct"],
                "result":    None,
            })
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"Log   saved to {log_path}")

    # ── picks_today.txt  (plain-text for email) ────────────────────────────────
    txt_path = os.path.join(BASE_DIR, "picks_today.txt")
    with open(txt_path, "w") as f:
        f.write(report_as_text(picks, today))
    print(f"Text  saved to {txt_path}")


if __name__ == "__main__":
    main()
