#!/usr/bin/env python3
"""
Script 6: 6_predict_today.py  [v2 — upgraded]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Loads trained MLB + NBA models, pulls today's games and odds, and
outputs a ranked edge report.

UPGRADES in v2:
  1. Team batting stats (OBP, OPS, runs/game) pulled live from MLB Stats API
  2. Pitcher ERA / K-rate pulled live from MLB Stats API (no DB required)
  3. Kelly Criterion (25% fractional) bet sizing on every pick
  4. Opening odds + model prob stored at pick time for CLV tracking
  5. Umpire run-tendency from umpscorecards.com
  6. Wind speed + temperature from wttr.in

Run this AFTER sports_betting_daily.py (which populates the odds cache).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import sqlite3
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB_PATH    = os.path.join(BASE_DIR, "sports_history.db")
CACHE_FILE = os.path.join(PARENT_DIR, "lines_cache.json")
MODEL_DIR  = os.path.join(BASE_DIR, "models")

# ── Config ────────────────────────────────────────────────────────────────────
EDGE_THRESHOLD  = 0.03   # Minimum edge to show a pick (3 pp)
KELLY_FRACTION  = 0.25   # 25% fractional Kelly — conservative, variance-reducing
MAX_KELLY_BET   = 0.05   # Cap at 5% of bankroll per bet
EASTERN         = ZoneInfo("America/New_York")
MLB_API         = "https://statsapi.mlb.com/api/v1"

# ── Park factors ──────────────────────────────────────────────────────────────
PARK_FACTORS = {
    "Colorado Rockies": 1.38, "Cincinnati Reds": 1.18, "Boston Red Sox": 1.14,
    "New York Yankees": 1.12, "Texas Rangers": 1.10, "Baltimore Orioles": 1.08,
    "Philadelphia Phillies": 1.07, "Chicago Cubs": 1.06, "Atlanta Braves": 1.05,
    "Toronto Blue Jays": 1.04, "Milwaukee Brewers": 1.03, "Minnesota Twins": 1.02,
    "Kansas City Royals": 1.01, "Detroit Tigers": 1.00, "Houston Astros": 1.00,
    "Cleveland Guardians": 0.99, "Cleveland Indians": 0.99,
    "St. Louis Cardinals": 0.98, "New York Mets": 0.97, "Pittsburgh Pirates": 0.96,
    "Chicago White Sox": 0.95, "Washington Nationals": 0.95,
    "Los Angeles Angels": 0.94, "Los Angeles Dodgers": 0.93,
    "Arizona Diamondbacks": 0.92, "Tampa Bay Rays": 0.91,
    "San Diego Padres": 0.90, "Oakland Athletics": 0.90, "Athletics": 0.90,
    "Seattle Mariners": 0.89, "Miami Marlins": 0.88, "San Francisco Giants": 0.86,
}

# ── Stadium city mapping for weather ──────────────────────────────────────────
STADIUM_CITIES = {
    "Arizona Diamondbacks": "Phoenix, AZ",
    "Atlanta Braves": "Cumberland, GA",
    "Baltimore Orioles": "Baltimore, MD",
    "Boston Red Sox": "Boston, MA",
    "Chicago Cubs": "Chicago, IL",
    "Chicago White Sox": "Chicago, IL",
    "Cincinnati Reds": "Cincinnati, OH",
    "Cleveland Guardians": "Cleveland, OH",
    "Colorado Rockies": "Denver, CO",
    "Detroit Tigers": "Detroit, MI",
    "Houston Astros": "Houston, TX",
    "Kansas City Royals": "Kansas City, MO",
    "Los Angeles Angels": "Anaheim, CA",
    "Los Angeles Dodgers": "Los Angeles, CA",
    "Miami Marlins": "Miami, FL",
    "Milwaukee Brewers": "Milwaukee, WI",
    "Minnesota Twins": "Minneapolis, MN",
    "New York Mets": "Flushing, NY",
    "New York Yankees": "Bronx, NY",
    "Athletics": "West Sacramento, CA",
    "Oakland Athletics": "West Sacramento, CA",
    "Philadelphia Phillies": "Philadelphia, PA",
    "Pittsburgh Pirates": "Pittsburgh, PA",
    "San Diego Padres": "San Diego, CA",
    "San Francisco Giants": "San Francisco, CA",
    "Seattle Mariners": "Seattle, WA",
    "St. Louis Cardinals": "St. Louis, MO",
    "Tampa Bay Rays": "St. Petersburg, FL",
    "Texas Rangers": "Arlington, TX",
    "Toronto Blue Jays": "Toronto, ON",
    "Washington Nationals": "Washington, DC",
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

# ── Kelly Criterion ───────────────────────────────────────────────────────────

def kelly_criterion(model_prob, odds_american, fraction=KELLY_FRACTION):
    """
    Fractional Kelly stake as % of bankroll.
    25% Kelly (default) cuts full Kelly by 75% — reduces variance significantly.
    Returns 0 when Kelly is negative (don't bet).
    """
    if odds_american >= 0:
        decimal_odds = odds_american / 100 + 1
    else:
        decimal_odds = 100 / abs(odds_american) + 1

    b = decimal_odds - 1   # net profit per unit if win
    p = model_prob
    q = 1 - p

    kelly = (b * p - q) / b
    return round(min(MAX_KELLY_BET, max(0.0, kelly * fraction)), 4)

# ── MLB Stats API: Team batting ───────────────────────────────────────────────

_batting_cache = {}

def fetch_team_batting_api(team_id, season):
    """
    Pull team OBP, OPS, AVG, SLG, runs/game from MLB Stats API.
    Cached per team-season so we only hit the API once per team per run.
    """
    if not team_id:
        return {}
    key = f"{team_id}_{season}"
    if key in _batting_cache:
        return _batting_cache[key]

    data = fetch_json(
        f"{MLB_API}/teams/{team_id}/stats"
        f"?stats=season&group=hitting&season={season}&sportId=1",
        timeout=10,
    )
    result = {}
    if data:
        splits = data.get("stats", [{}])[0].get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            gp   = max(float(stat.get("gamesPlayed", 1) or 1), 1)
            result = {
                "obp":          float(stat.get("obp",  0.320) or 0.320),
                "ops":          float(stat.get("ops",  0.720) or 0.720),
                "avg":          float(stat.get("avg",  0.250) or 0.250),
                "slg":          float(stat.get("slg",  0.400) or 0.400),
                "runs_per_game": float(stat.get("runs", 0) or 0) / gp,
            }

    # Defaults if API returns nothing
    if not result:
        result = {"obp": 0.320, "ops": 0.720, "avg": 0.250,
                  "slg": 0.400, "runs_per_game": 4.5}

    _batting_cache[key] = result
    return result

# ── MLB Stats API: Pitcher stats ──────────────────────────────────────────────

_pitcher_cache = {}

def fetch_pitcher_stats_api(pitcher_id, season):
    """
    Pull pitcher ERA, WHIP, K/9, BB/9 from MLB Stats API.
    Used as fallback when DB doesn't have Statcast/xFIP data.
    """
    if not pitcher_id:
        return {}
    key = f"{pitcher_id}_{season}"
    if key in _pitcher_cache:
        return _pitcher_cache[key]

    data = fetch_json(
        f"{MLB_API}/people/{pitcher_id}/stats"
        f"?stats=season&group=pitching&season={season}&sportId=1",
        timeout=10,
    )
    result = {}
    if data:
        splits = data.get("stats", [{}])[0].get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            def safe_float(val, default):
                try:
                    return float(str(val).replace("-.--", str(default))) or default
                except Exception:
                    return default
            result = {
                "era":     safe_float(stat.get("era"),            4.50),
                "whip":    safe_float(stat.get("whip"),           1.30),
                "k_per_9": safe_float(stat.get("strikeoutsPer9Inn"), 8.0),
                "bb_per_9":safe_float(stat.get("walksPer9Inn"),    3.0),
            }

    if not result:
        result = {"era": 4.50, "whip": 1.30, "k_per_9": 8.0, "bb_per_9": 3.0}

    _pitcher_cache[key] = result
    return result

# ── Umpire data ───────────────────────────────────────────────────────────────

def fetch_umpire(game_pk):
    """Return home plate umpire name from MLB boxscore API."""
    if not game_pk:
        return None
    data = fetch_json(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8)
    if not data:
        return None
    for official in data.get("officials", []):
        if "Home Plate" in official.get("officialType", ""):
            return official.get("official", {}).get("fullName")
    return None

_ump_cache = {}

def fetch_umpire_stats(ump_name):
    """
    Fetch umpire run-tendency from umpscorecards.com API.
    favor_home: positive = umpire calls more strikes for home team.
    run_factor: > 0 means more runs allowed than average.
    Returns (favor_home, run_factor) tuple.
    """
    if not ump_name:
        return 0.0, 0.0
    if ump_name in _ump_cache:
        return _ump_cache[ump_name]

    try:
        encoded = urllib.parse.quote(ump_name)
        data = fetch_json(
            f"https://umpscorecards.com/api/umpires/?name={encoded}",
            timeout=6,
        )
        if data and isinstance(data, list) and data:
            ump = data[0]
            favor = float(ump.get("favor_home", 0.0) or 0.0)
            runs  = float(ump.get("avg_favor_runs", 0.0) or 0.0)
            result = (favor, runs)
            _ump_cache[ump_name] = result
            return result
    except Exception:
        pass

    _ump_cache[ump_name] = (0.0, 0.0)
    return 0.0, 0.0

# ── Weather data ──────────────────────────────────────────────────────────────

_weather_cache = {}

def fetch_weather(home_team):
    """
    Fetch wind speed, wind direction, and temperature from wttr.in.
    Used for outdoor MLB stadiums. Returns defaults for domes.
    """
    DOMED_STADIUMS = {
        "Tampa Bay Rays", "Toronto Blue Jays", "Houston Astros",
        "Arizona Diamondbacks", "Seattle Mariners", "Milwaukee Brewers",
        "Minnesota Twins", "Miami Marlins",
    }
    if home_team in DOMED_STADIUMS:
        return {"wind_speed": 0.0, "wind_dir": 0.0, "temp_f": 72.0, "domed": True}

    if home_team in _weather_cache:
        return _weather_cache[home_team]

    city = STADIUM_CITIES.get(home_team, "")
    result = {"wind_speed": 0.0, "wind_dir": 0.0, "temp_f": 72.0, "domed": False}

    if city:
        try:
            encoded = urllib.parse.quote(city)
            data = fetch_json(f"https://wttr.in/{encoded}?format=j1", timeout=8)
            if data:
                current = data.get("current_condition", [{}])[0]
                result["wind_speed"] = float(current.get("windspeedMiles", 0) or 0)
                result["wind_dir"]   = float(current.get("winddirDegree", 0) or 0)
                result["temp_f"]     = float(current.get("temp_F", 72) or 72)
        except Exception:
            pass

    _weather_cache[home_team] = result
    return result

# ── Load models ───────────────────────────────────────────────────────────────

def load_model(sport):
    path = os.path.join(MODEL_DIR, f"{sport.lower()}_model.pkl")
    if not os.path.exists(path):
        return None
    return joblib.load(path)

def predict(bundle, feature_row):
    """Run ensemble prediction on a single-row dict. Returns home win prob."""
    xgb_m  = bundle["xgb"]
    lr_m   = bundle["lr"]
    scaler = bundle["scaler"]
    feats  = bundle["features"]

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
    date = date or today_str()
    matchup  = f"{away_team} @ {home_team}"
    home_key = f"{date}|{sport_label}|{matchup}|{home_team}"
    away_key = f"{date}|{sport_label}|{matchup}|{away_team}"
    return cache.get(home_key), cache.get(away_key)

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_mlb_rolling(conn, team, before_date, window=10):
    sql = """
        SELECT home_score, away_score,
               CASE WHEN home_team=? THEN 1 ELSE 0 END as is_home
        FROM mlb_games
        WHERE (home_team=? OR away_team=?)
          AND game_date < ?
          AND status = 'Final'
        ORDER BY game_date DESC LIMIT ?
    """
    rows = conn.execute(sql, (team, team, team, before_date, window)).fetchall()
    if not rows:
        return 0.5, 0.0
    wins, margins = [], []
    for hs, as_, is_home in rows:
        if is_home:
            wins.append(1 if hs > as_ else 0)
            margins.append(hs - as_)
        else:
            wins.append(1 if as_ > hs else 0)
            margins.append(as_ - hs)
    return round(np.mean(wins), 4), round(np.mean(margins), 3)

def get_nba_rolling(conn, team_name, before_date, window=10):
    sql = """
        SELECT pts, opp_pts, winner FROM nba_games
        WHERE team_name=? AND game_date < ?
        ORDER BY game_date DESC LIMIT ?
    """
    rows = conn.execute(sql, (team_name, before_date, window)).fetchall()
    if not rows:
        return 0.5, 0.0
    wins    = [r[2] for r in rows]
    margins = [r[0] - r[1] for r in rows]
    return round(np.mean(wins), 4), round(np.mean(margins), 3)

def get_nba_rest(conn, team_name, before_date):
    sql = "SELECT MAX(game_date) FROM nba_games WHERE team_name=? AND game_date < ?"
    row = conn.execute(sql, (team_name, before_date)).fetchone()
    if not row or not row[0]:
        return 3
    last  = datetime.strptime(row[0], "%Y-%m-%d").date()
    today = datetime.strptime(before_date, "%Y-%m-%d").date()
    return (today - last).days

def get_nba_ratings(conn, team_name):
    sql = """
        SELECT a.off_rating, a.def_rating, a.net_rating, a.pace
        FROM nba_team_advanced a
        JOIN nba_games g ON g.team_id = a.team_id AND g.season = a.season
        WHERE g.team_name = ?
        ORDER BY a.season DESC LIMIT 1
    """
    row = conn.execute(sql, (team_name,)).fetchone()
    if row:
        return {"off": row[0], "def": row[1], "net": row[2], "pace": row[3]}
    return {"off": 110.0, "def": 110.0, "net": 0.0, "pace": 100.0}

def get_mlb_starter_stats(conn, pitcher_name, season):
    sql = """
        SELECT xfip, fip, era, k_pct, bb_pct FROM mlb_pitcher_stats
        WHERE player_name=? AND season=? LIMIT 1
    """
    row = conn.execute(sql, (pitcher_name, season)).fetchone()
    if row:
        return {"xfip": row[0], "fip": row[1], "era": row[2],
                "k_pct": row[3], "bb_pct": row[4]}
    return {}

# ── Today's schedule ──────────────────────────────────────────────────────────

def get_mlb_today():
    """Fetch today's MLB games with probable starters AND team IDs."""
    today = today_str()
    data  = fetch_json(
        f"{MLB_API}/schedule?sportId=1&date={today}"
        f"&hydrate=probablePitcher,team&gameType=R"
    )
    if not data:
        return []

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") == "Final":
                continue
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            games.append({
                "home_team":       home["team"]["name"],
                "away_team":       away["team"]["name"],
                "home_team_id":    home["team"]["id"],
                "away_team_id":    away["team"]["id"],
                "home_starter":    home.get("probablePitcher", {}).get("fullName", "TBD"),
                "away_starter":    away.get("probablePitcher", {}).get("fullName", "TBD"),
                "home_starter_id": home.get("probablePitcher", {}).get("id"),
                "away_starter_id": away.get("probablePitcher", {}).get("id"),
                "game_pk":         game.get("gamePk"),
                "game_time":       game.get("gameDate", ""),
            })
    return games

def get_nba_today():
    try:
        from nba_api.stats.endpoints import scoreboard
        import time
        time.sleep(0.5)
        sb = scoreboard.Scoreboard()
        df = sb.get_data_frames()[0]
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
    today  = today_str()
    season = int(today[:4])

    # ── Rolling stats from DB ──────────────────────────────────────────
    if conn:
        home_win_l10, home_diff_l10 = get_mlb_rolling(conn, game["home_team"], today)
        away_win_l10, away_diff_l10 = get_mlb_rolling(conn, game["away_team"], today)
        home_sp_db = get_mlb_starter_stats(conn, game.get("home_starter", ""), season)
        away_sp_db = get_mlb_starter_stats(conn, game.get("away_starter", ""), season)
    else:
        home_win_l10 = away_win_l10 = 0.5
        home_diff_l10 = away_diff_l10 = 0.0
        home_sp_db = away_sp_db = {}

    # ── Team batting stats from MLB API (FIXES the NULL issue) ────────
    home_bat = fetch_team_batting_api(game.get("home_team_id"), season)
    away_bat = fetch_team_batting_api(game.get("away_team_id"), season)

    # ── Pitcher stats: DB preferred, MLB API as fallback ──────────────
    home_sp_api = fetch_pitcher_stats_api(game.get("home_starter_id"), season)
    away_sp_api = fetch_pitcher_stats_api(game.get("away_starter_id"), season)

    home_era   = home_sp_db.get("era")  or home_sp_api.get("era",   4.50)
    away_era   = away_sp_db.get("era")  or away_sp_api.get("era",   4.50)
    home_k9    = home_sp_db.get("k_pct") or home_sp_api.get("k_per_9", 8.0)
    away_k9    = away_sp_db.get("k_pct") or away_sp_api.get("k_per_9", 8.0)
    home_xfip  = home_sp_db.get("xfip")
    away_xfip  = away_sp_db.get("xfip")

    # ── Umpire ────────────────────────────────────────────────────────
    ump_name          = fetch_umpire(game.get("game_pk"))
    ump_favor, ump_runs = fetch_umpire_stats(ump_name)
    if ump_name:
        print(f"   Umpire: {ump_name} (favor={ump_favor:+.2f}, runs={ump_runs:+.2f})")

    # ── Weather ───────────────────────────────────────────────────────
    wx = fetch_weather(game["home_team"])
    wind_speed = wx["wind_speed"]
    temp_f     = wx["temp_f"]

    # ── Open line ─────────────────────────────────────────────────────
    open_hp, _ = no_vig(home_ml, away_ml)

    return {
        # Core market features (trained)
        "open_home_prob":    open_hp,
        "true_home_prob":    open_hp,
        "true_away_prob":    1 - open_hp,
        "ml_move_home":      0.0,
        "total_move":        0.0,
        "sharp_home":        0,
        "sharp_away":        0,
        # Park
        "park_factor":       PARK_FACTORS.get(game["home_team"], 1.0),
        # Rolling form (trained)
        "home_win_rate_l10": home_win_l10,
        "away_win_rate_l10": away_win_l10,
        "win_rate_diff":     home_win_l10 - away_win_l10,
        "home_run_diff_l10": home_diff_l10,
        "away_run_diff_l10": away_diff_l10,
        "run_diff_diff":     home_diff_l10 - away_diff_l10,
        # ── NEW: Pitcher stats (populated, not None) ──────────────────
        "home_sp_era":       home_era,
        "away_sp_era":       away_era,
        "era_diff":          home_era - away_era,
        "home_sp_k9":        home_k9,
        "away_sp_k9":        away_k9,
        "k9_diff":           home_k9 - away_k9,
        "home_sp_xfip":      home_xfip,
        "away_sp_xfip":      away_xfip,
        "xfip_diff":         (home_xfip - away_xfip)
                             if (home_xfip and away_xfip) else None,
        # ── NEW: Team batting stats (was None, now live) ──────────────
        "home_obp":          home_bat.get("obp", 0.320),
        "away_obp":          away_bat.get("obp", 0.320),
        "obp_diff":          home_bat.get("obp", 0.320) - away_bat.get("obp", 0.320),
        "home_ops":          home_bat.get("ops", 0.720),
        "away_ops":          away_bat.get("ops", 0.720),
        "ops_diff":          home_bat.get("ops", 0.720) - away_bat.get("ops", 0.720),
        "home_rpg":          home_bat.get("runs_per_game", 4.5),
        "away_rpg":          away_bat.get("runs_per_game", 4.5),
        "rpg_diff":          home_bat.get("runs_per_game", 4.5) - away_bat.get("runs_per_game", 4.5),
        # Use OPS as wRC+ proxy until Fangraphs integration
        "home_wrc_plus":     round(home_bat.get("ops", 0.720) * 140, 1),
        "away_wrc_plus":     round(away_bat.get("ops", 0.720) * 140, 1),
        "wrc_diff":          round((home_bat.get("ops", 0.720) - away_bat.get("ops", 0.720)) * 140, 1),
        # ── NEW: Umpire ───────────────────────────────────────────────
        "ump_favor_home":    ump_favor,
        "ump_run_factor":    ump_runs,
        # ── NEW: Weather ──────────────────────────────────────────────
        "wind_speed":        wind_speed,
        "temp_f":            temp_f,
        "cold_game":         int(temp_f < 50),  # cold suppresses offense
        "wind_boost":        int(wind_speed > 12),  # high wind = more variance
    }

def build_nba_features(game, home_ml, away_ml, conn):
    today = today_str()

    home_ratings   = get_nba_ratings(conn, game["home_team"])
    away_ratings   = get_nba_ratings(conn, game["away_team"])
    home_rest      = get_nba_rest(conn, game["home_team"], today)
    away_rest      = get_nba_rest(conn, game["away_team"], today)
    home_win_l10, home_margin_l10 = get_nba_rolling(conn, game["home_team"], today)
    away_win_l10, away_margin_l10 = get_nba_rolling(conn, game["away_team"], today)

    open_hp, _ = no_vig(home_ml, away_ml)

    return {
        "open_home_prob":    open_hp,
        "sharp_home":        0,
        "sharp_away":        0,
        "home_off_rating":   home_ratings["off"],
        "away_off_rating":   away_ratings["off"],
        "off_rating_diff":   home_ratings["off"] - away_ratings["off"],
        "home_def_rating":   home_ratings["def"],
        "away_def_rating":   away_ratings["def"],
        "def_rating_diff":   home_ratings["def"] - away_ratings["def"],
        "home_net_rating":   home_ratings["net"],
        "away_net_rating":   away_ratings["net"],
        "net_rating_diff":   home_ratings["net"] - away_ratings["net"],
        "home_pace":         home_ratings["pace"],
        "away_pace":         away_ratings["pace"],
        "pace_diff":         abs(home_ratings["pace"] - away_ratings["pace"]),
        "home_rest_days":    home_rest,
        "away_rest_days":    away_rest,
        "rest_diff":         home_rest - away_rest,
        "home_b2b":          int(home_rest <= 1),
        "away_b2b":          int(away_rest <= 1),
        "b2b_diff":          int(away_rest <= 1) - int(home_rest <= 1),
        "home_win_rate_l10": home_win_l10,
        "away_win_rate_l10": away_win_l10,
        "win_rate_diff":     home_win_l10 - away_win_l10,
        "home_margin_l10":   home_margin_l10,
        "away_margin_l10":   away_margin_l10,
        "margin_diff":       home_margin_l10 - away_margin_l10,
    }

# ── Output formatting ─────────────────────────────────────────────────────────

def edge_bar(edge):
    if edge >= 0.08: return "🔥🔥 STRONG EDGE"
    if edge >= 0.05: return "✅ SOLID EDGE"
    if edge >= 0.03: return "📈 EDGE"
    return "—"

def format_pick(sport, game, model_prob, market_prob, home_ml, away_ml, rec_side):
    home = game["home_team"]
    away = game["away_team"]
    edge = abs(model_prob - market_prob)
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

    # ── Kelly Criterion sizing ────────────────────────────────────────
    kelly = kelly_criterion(side_prob, bet_ml_int)
    # Recommended bet in dollars given $1,000 bankroll as example
    rec_units_at_1k = round(kelly * 10, 2)  # Kelly% * $1000 / $100 per unit

    return {
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
        # ── v2 additions ──────────────────────────────────────────────
        "kelly_fraction":      kelly,
        "kelly_pct":           f"{kelly:.1%}",
        "rec_units_per_1k":    rec_units_at_1k,
        "opening_ml":          bet_ml_int,          # stored for CLV calc later
        "opening_market_prob": round(side_mkt, 4),  # stored for CLV calc later
        # Legacy text fields (kept for email/print compatibility)
        "model_p":     f"{side_prob:.1%}",
        "market_p":    f"{side_mkt:.1%}",
        "edge_raw":    edge,
    }

def print_report(picks, today):
    print("\n" + "━"*58)
    print(f" 🤖 MODEL PICKS — {today}")
    print(f" Edge ≥{EDGE_THRESHOLD:.0%} | {KELLY_FRACTION:.0%} Kelly sizing")
    print("━"*58)

    if not picks:
        print(" No edges found today above threshold.")
        print("━"*58)
        return

    picks.sort(key=lambda p: p["edge_raw"], reverse=True)

    for p in picks:
        print(f"\n [{p['sport']}] {p['matchup']}")
        print(f"  BET:    {p['bet']}")
        print(f"  Model:  {p['model_p']} | Market: {p['market_p']} | Edge: {p['edge_pct']} {p['label']}")
        print(f"  Kelly:  {p['kelly_pct']} of bankroll "
              f"(≈ {p['rec_units_per_1k']} units per $1,000)")

    print(f"\n  {len(picks)} bet(s) today")
    print("━"*58)

def report_as_text(picks, today):
    lines = ["━"*52, f"MODEL PICKS — {today}", "━"*52]
    if not picks:
        lines.append("No edges found today.")
    else:
        picks.sort(key=lambda p: p["edge_raw"], reverse=True)
        for p in picks:
            lines.append(f"[{p['sport']}] {p['matchup']}")
            lines.append(f"  BET: {p['bet']}")
            lines.append(f"  Model {p['model_p']} vs Market {p['market_p']} "
                         f"| Edge {p['edge_pct']} {p['label']}")
            lines.append(f"  Kelly: {p['kelly_pct']} of bankroll")
            lines.append("")
    lines.append("━"*52)
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def run_predictions():
    today = today_str()
    print(f"Loading models and data for {today}...")

    mlb_bundle = load_model("mlb")
    nba_bundle = load_model("nba")

    if not mlb_bundle and not nba_bundle:
        print("[ERROR] No trained models found. Run 4_train_model.py first.")
        return [], []

    cache = load_cache()
    conn  = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None

    picks     = []
    all_games = []

    # ── MLB ──────────────────────────────────────────────────────────
    if mlb_bundle:
        print("\n[MLB] Fetching today's schedule + batting/pitcher stats...")
        mlb_games = get_mlb_today()
        print(f"  {len(mlb_games)} games today")

        for game in mlb_games:
            home_ml, away_ml = get_cached_odds(
                cache, "MLB", game["home_team"], game["away_team"], today
            )
            if home_ml is None or away_ml is None:
                print(f"  No odds cached: {game['away_team']} @ {game['home_team']}")
                continue

            print(f"  {game['away_team']:25} @ {game['home_team']:25}", end=" ")
            feats      = build_mlb_features(game, home_ml, away_ml, cache, conn)
            model_prob = predict(mlb_bundle, feats)
            mkt_hp, _  = no_vig(home_ml, away_ml)

            edge_home = model_prob - mkt_hp
            edge_away = -edge_home
            print(f"model={model_prob:.1%} market={mkt_hp:.1%} edge={edge_home:+.1%}")

            game_entry = {
                "sport":      "MLB",
                "matchup":    f"{game['away_team']} @ {game['home_team']}",
                "home_team":  game["home_team"],
                "away_team":  game["away_team"],
                "home_ml":    home_ml,
                "away_ml":    away_ml,
                "model_prob": round(model_prob, 4),
                "market_prob": round(mkt_hp, 4),
                "edge":       round(edge_home, 4),
                "has_pick":   False,
                # Extra context for Odds Board
                "home_starter": game.get("home_starter", "TBD"),
                "away_starter": game.get("away_starter", "TBD"),
                "wind_speed":   feats.get("wind_speed", 0),
                "temp_f":       feats.get("temp_f", 72),
                "home_obp":     feats.get("home_obp"),
                "away_obp":     feats.get("away_obp"),
                "home_era":     feats.get("home_sp_era"),
                "away_era":     feats.get("away_sp_era"),
            }
            all_games.append(game_entry)

            if edge_home > EDGE_THRESHOLD and home_ml >= -250:
                p = format_pick("MLB", game, model_prob, mkt_hp, home_ml, away_ml, "home")
                picks.append(p)
                game_entry["has_pick"] = True
            elif edge_away > EDGE_THRESHOLD and away_ml >= -250:
                p = format_pick("MLB", game, model_prob, mkt_hp, home_ml, away_ml, "away")
                picks.append(p)
                game_entry["has_pick"] = True

    # ── NBA ──────────────────────────────────────────────────────────
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

            if not conn:
                print(f"  No DB: skipping NBA feature build for {game['home_team']}")
                continue

            print(f"  {game['away_team']:25} @ {game['home_team']:25}", end=" ")
            feats      = build_nba_features(game, home_ml, away_ml, conn)
            model_prob = predict(nba_bundle, feats)
            mkt_hp, _  = no_vig(home_ml, away_ml)

            edge_home = model_prob - mkt_hp
            edge_away = -edge_home
            print(f"model={model_prob:.1%} market={mkt_hp:.1%} edge={edge_home:+.1%}")

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
                p = format_pick("NBA", game, model_prob, mkt_hp, home_ml, away_ml, "home")
                picks.append(p)
                game_entry["has_pick"] = True
            elif edge_away > EDGE_THRESHOLD and away_ml >= -250:
                p = format_pick("NBA", game, model_prob, mkt_hp, home_ml, away_ml, "away")
                picks.append(p)
                game_entry["has_pick"] = True

    if conn:
        conn.close()

    return picks, all_games


def main():
    today     = today_str()
    picks, all_games = run_predictions()
    print_report(picks, today)

    now_iso = datetime.now(EASTERN).isoformat(timespec="seconds")

    # ── picks_today.json (Streamlit reads this) ──────────────────────
    mlb_picks = [p for p in picks if p["sport"] == "MLB"]
    nba_picks = [p for p in picks if p["sport"] == "NBA"]
    payload = {
        "date":         today,
        "generated_at": now_iso,
        "picks":        sorted(picks, key=lambda p: p["edge"], reverse=True),
        "all_games":    all_games,
        "summary": {
            "total_picks": len(picks),
            "mlb_picks":   len(mlb_picks),
            "nba_picks":   len(nba_picks),
            "top_edge":    max((p["edge"] for p in picks), default=0),
            "top_kelly":   max((p["kelly_fraction"] for p in picks), default=0),
        },
    }
    json_path = os.path.join(BASE_DIR, "picks_today.json")
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nJSON saved → {json_path}")

    # ── picks_log.json (performance tracker — append new picks) ──────
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
                "date":                today,
                "sport":               pick["sport"],
                "matchup":             pick["matchup"],
                "bet_team":            pick["bet_team"],
                "ml":                  pick["bet_ml"],
                "edge":                pick["edge"],
                "edge_pct":            pick["edge_pct"],
                "model_prob":          pick["model_prob"],
                "opening_market_prob": pick["opening_market_prob"],
                "kelly_fraction":      pick["kelly_fraction"],
                "result":              None,
                "score":               None,
                "closing_ml":          None,  # filled by 7_mark_results.py
                "clv":                 None,  # filled by 7_mark_results.py
            })
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"Log saved → {log_path}")

    # ── picks_today.txt (plain text for email) ────────────────────────
    txt_path = os.path.join(BASE_DIR, "picks_today.txt")
    with open(txt_path, "w") as f:
        f.write(report_as_text(picks, today))
    print(f"Text saved → {txt_path}")


if __name__ == "__main__":
    main()
