#!/usr/bin/env python3
"""
Script 1: 1_collect_historical.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pulls 5 years of historical MLB + NBA data into a local SQLite database.
Run this ONCE to build the foundation. Takes 15–30 minutes.

What it collects:
  MLB → Game results, starting pitchers, team batting/pitching stats (FanGraphs)
  NBA → Game logs, team advanced stats (ORTG, DRTG, pace, eFG%)
  ODDS → Historical moneylines + O/U from The Odds API (optional, uses credits)

After this script: run 3_engineer_features.py to build the model dataset.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, date

# ── CONFIG ────────────────────────────────────────────────────────────────────

ODDS_API_KEY = "26dcaafb7d69e3ac46d339c733105f90"
MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

# How many years back to collect (5 = 2020 through current season)
YEARS_BACK = 5
START_YEAR = datetime.now().year - YEARS_BACK  # e.g., 2020

# Odds collection: set to True only if you have a paid Odds API plan.
# Each date × sport uses ~1 API credit. 5 years ≈ 2,100 credits total.
COLLECT_HISTORICAL_ODDS = False  # Flip to True if you're on a paid plan

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_history.db")

# ── DEPENDENCY CHECK ──────────────────────────────────────────────────────────

def check_dependencies():
    missing = []
    try:
        import pybaseball  # noqa: F401
    except ImportError:
        missing.append("pybaseball")
    try:
        import nba_api  # noqa: F401
    except ImportError:
        missing.append("nba_api")

    if missing:
        print("\n[SETUP] Missing libraries. Install them with:\n")
        print(f"  pip install {' '.join(missing)}\n")
        sys.exit(1)

# ── DATABASE SETUP ────────────────────────────────────────────────────────────

def init_db(conn):
    """Create all tables if they don't exist."""
    c = conn.cursor()

    # MLB: one row per game
    c.execute("""
        CREATE TABLE IF NOT EXISTS mlb_games (
            game_id       TEXT PRIMARY KEY,
            game_date     TEXT,
            home_team     TEXT,
            away_team     TEXT,
            home_score    INTEGER,
            away_score    INTEGER,
            winner        TEXT,   -- 'home' or 'away'
            home_starter  TEXT,
            away_starter  TEXT,
            status        TEXT,   -- 'Final', 'Postponed', etc.
            season        INTEGER
        )
    """)

    # MLB: pitcher-level season stats from FanGraphs (for feature engineering)
    c.execute("""
        CREATE TABLE IF NOT EXISTS mlb_pitcher_stats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            season        INTEGER,
            player_name   TEXT,
            team          TEXT,
            g             INTEGER,  -- games
            ip            REAL,     -- innings pitched
            era           REAL,
            fip           REAL,     -- fielding independent pitching
            xfip          REAL,     -- expected FIP (luck-adjusted)
            k_pct         REAL,     -- strikeout rate
            bb_pct        REAL,     -- walk rate
            hr_9          REAL,     -- home runs per 9 innings
            war           REAL,
            UNIQUE(season, player_name, team)
        )
    """)

    # MLB: team-level season batting stats from FanGraphs
    c.execute("""
        CREATE TABLE IF NOT EXISTS mlb_team_batting (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            season        INTEGER,
            team          TEXT,
            wrc_plus      REAL,  -- park/era-adjusted offense (100 = league avg)
            obp           REAL,
            slg           REAL,
            iso           REAL,  -- isolated power
            k_pct         REAL,
            bb_pct        REAL,
            babip         REAL,
            UNIQUE(season, team)
        )
    """)

    # NBA: one row per team per game
    c.execute("""
        CREATE TABLE IF NOT EXISTS nba_games (
            game_id       TEXT,
            game_date     TEXT,
            team_id       INTEGER,
            team_name     TEXT,
            opponent_id   INTEGER,
            opponent_name TEXT,
            is_home       INTEGER,  -- 1 = home, 0 = away
            pts           INTEGER,
            opp_pts       INTEGER,
            winner        INTEGER,  -- 1 = win, 0 = loss
            season        TEXT,     -- e.g., '2023-24'
            PRIMARY KEY (game_id, team_id)
        )
    """)

    # NBA: advanced team stats per game (from TeamDashboardByGeneralSplits)
    c.execute("""
        CREATE TABLE IF NOT EXISTS nba_team_advanced (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id       TEXT,
            team_id       INTEGER,
            season        TEXT,
            off_rating    REAL,  -- points per 100 possessions, offensive
            def_rating    REAL,  -- points per 100 possessions, defensive
            net_rating    REAL,
            pace          REAL,  -- possessions per 48 min
            efg_pct       REAL,  -- effective field goal %
            ts_pct        REAL,  -- true shooting %
            ast_pct       REAL,
            reb_pct       REAL,
            UNIQUE(game_id, team_id)
        )
    """)

    # Odds: historical moneylines + totals per game
    c.execute("""
        CREATE TABLE IF NOT EXISTS historical_odds (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sport         TEXT,    -- 'mlb' or 'nba'
            game_date     TEXT,
            home_team     TEXT,
            away_team     TEXT,
            book          TEXT,
            home_ml       INTEGER, -- American odds
            away_ml       INTEGER,
            total_line    REAL,    -- O/U line
            over_odds     INTEGER,
            under_odds    INTEGER,
            recorded_at   TEXT,    -- timestamp of the snapshot
            UNIQUE(sport, game_date, home_team, away_team, book)
        )
    """)

    conn.commit()
    print("[DB] Tables ready.")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def fetch_json(url, retries=3, delay=2):
    """Fetch a URL and return parsed JSON. Retries on failure."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt < retries - 1:
                print(f"  [retry {attempt+1}] {e}")
                time.sleep(delay)
            else:
                print(f"  [SKIP] Failed after {retries} attempts: {url[:80]}")
                return None

def progress(current, total, label=""):
    bar_len = 30
    filled = int(bar_len * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    pct = int(100 * current / total) if total > 0 else 0
    print(f"\r  [{bar}] {pct}% {label}", end="", flush=True)

# ── MLB GAME RESULTS (MLB Stats API — free) ───────────────────────────────────

MLB_SEASONS = {
    2020: ("2020-07-23", "2020-09-27"),  # shortened COVID season
    2021: ("2021-04-01", "2021-10-03"),
    2022: ("2022-04-07", "2022-10-05"),
    2023: ("2023-03-30", "2023-10-01"),
    2024: ("2024-03-20", "2024-09-29"),
    2025: ("2025-03-27", "2025-09-28"),
}

def collect_mlb_games(conn):
    """Pull all MLB game results for each season via the official MLB Stats API."""
    print("\n[MLB] Collecting game results...")
    c = conn.cursor()
    years = [y for y in range(START_YEAR, datetime.now().year + 1) if y in MLB_SEASONS]
    total_inserted = 0

    for year in years:
        start_date, end_date = MLB_SEASONS[year]
        url = (
            f"{MLB_STATS_API}/schedule"
            f"?sportId=1"
            f"&startDate={start_date}"
            f"&endDate={end_date}"
            f"&hydrate=probablePitcher,linescore"
            f"&gameType=R"   # Regular season only
        )
        print(f"\n  Fetching {year} schedule...", end="")
        data = fetch_json(url)
        if not data:
            continue

        dates = data.get("dates", [])
        inserted = 0
        for d in dates:
            for game in d.get("games", []):
                status = game.get("status", {}).get("abstractGameState", "")
                if status != "Final":
                    continue

                game_id    = str(game["gamePk"])
                game_date  = game["gameDate"][:10]
                home       = game["teams"]["home"]
                away       = game["teams"]["away"]
                home_team  = home["team"]["name"]
                away_team  = away["team"]["name"]
                home_score = home.get("score", 0)
                away_score = away.get("score", 0)
                winner     = "home" if home_score > away_score else "away"

                # Probable starters
                home_starter = home.get("probablePitcher", {}).get("fullName", "")
                away_starter = away.get("probablePitcher", {}).get("fullName", "")

                try:
                    c.execute("""
                        INSERT OR IGNORE INTO mlb_games
                        (game_id, game_date, home_team, away_team, home_score, away_score,
                         winner, home_starter, away_starter, status, season)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (game_id, game_date, home_team, away_team, home_score, away_score,
                          winner, home_starter, away_starter, status, year))
                    inserted += c.rowcount
                except Exception as e:
                    print(f"\n  [WARN] game {game_id}: {e}")

        conn.commit()
        total_inserted += inserted
        print(f" {inserted} games saved.")

    print(f"[MLB] Game results done. Total rows: {total_inserted}")

# ── MLB PITCHER STATS from FanGraphs (via pybaseball) ────────────────────────

def collect_mlb_pitcher_stats(conn):
    """
    Pull season-level pitcher stats (xFIP, FIP, K%, BB%) from FanGraphs.
    These are the core features for our model.
    """
    from pybaseball import pitching_stats
    print("\n[MLB] Collecting pitcher stats (FanGraphs)...")
    c = conn.cursor()

    end_year = datetime.now().year
    # pybaseball can be slow — it scrapes FanGraphs
    print(f"  Pulling {START_YEAR}–{end_year} pitcher stats (this may take 1-2 min)...")

    try:
        df = pitching_stats(START_YEAR, end_year, qual=10)  # min 10 innings pitched
    except Exception as e:
        print(f"  [ERROR] pybaseball failed: {e}")
        return

    # Normalize column names (pybaseball uses FanGraphs naming)
    col_map = {
        "Name":   "player_name",
        "Team":   "team",
        "Season": "season",
        "G":      "g",
        "IP":     "ip",
        "ERA":    "era",
        "FIP":    "fip",
        "xFIP":   "xfip",
        "K%":     "k_pct",
        "BB%":    "bb_pct",
        "HR/9":   "hr_9",
        "WAR":    "war",
    }

    df = df.rename(columns=col_map)
    keep = [c for c in col_map.values() if c in df.columns]
    df = df[keep].dropna(subset=["player_name", "season"])

    inserted = 0
    for _, row in df.iterrows():
        try:
            c.execute("""
                INSERT OR REPLACE INTO mlb_pitcher_stats
                (season, player_name, team, g, ip, era, fip, xfip, k_pct, bb_pct, hr_9, war)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(row.get("season", 0)),
                row.get("player_name", ""),
                row.get("team", ""),
                int(row.get("g", 0)),
                float(row.get("ip", 0)),
                float(row.get("era", 0)) if row.get("era") else None,
                float(row.get("fip", 0)) if row.get("fip") else None,
                float(row.get("xfip", 0)) if row.get("xfip") else None,
                float(row.get("k_pct", 0)) if row.get("k_pct") else None,
                float(row.get("bb_pct", 0)) if row.get("bb_pct") else None,
                float(row.get("hr_9", 0)) if row.get("hr_9") else None,
                float(row.get("war", 0)) if row.get("war") else None,
            ))
            inserted += c.rowcount
        except Exception as e:
            print(f"\n  [WARN] row skip: {e}")

    conn.commit()
    print(f"[MLB] Pitcher stats done. {inserted} pitcher-seasons saved.")

# ── MLB TEAM BATTING STATS from FanGraphs (via pybaseball) ───────────────────

def collect_mlb_team_batting(conn):
    """Pull team-level wRC+, OBP, and other batting stats from FanGraphs."""
    from pybaseball import team_batting
    print("\n[MLB] Collecting team batting stats (FanGraphs)...")
    c = conn.cursor()

    end_year = datetime.now().year
    inserted = 0

    for year in range(START_YEAR, end_year + 1):
        try:
            df = team_batting(year, year)
        except Exception as e:
            print(f"  [WARN] {year} team batting failed: {e}")
            continue

        col_map = {
            "Team":  "team",
            "wRC+":  "wrc_plus",
            "OBP":   "obp",
            "SLG":   "slg",
            "ISO":   "iso",
            "K%":    "k_pct",
            "BB%":   "bb_pct",
            "BABIP": "babip",
        }
        df = df.rename(columns=col_map)

        for _, row in df.iterrows():
            try:
                c.execute("""
                    INSERT OR REPLACE INTO mlb_team_batting
                    (season, team, wrc_plus, obp, slg, iso, k_pct, bb_pct, babip)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    year,
                    row.get("team", ""),
                    float(row.get("wrc_plus", 100)) if row.get("wrc_plus") else 100.0,
                    float(row.get("obp", 0)) if row.get("obp") else None,
                    float(row.get("slg", 0)) if row.get("slg") else None,
                    float(row.get("iso", 0)) if row.get("iso") else None,
                    float(row.get("k_pct", 0)) if row.get("k_pct") else None,
                    float(row.get("bb_pct", 0)) if row.get("bb_pct") else None,
                    float(row.get("babip", 0)) if row.get("babip") else None,
                ))
                inserted += c.rowcount
            except Exception as e:
                print(f"\n  [WARN] row skip: {e}")

        print(f"  {year}: {df.shape[0]} teams")

    conn.commit()
    print(f"[MLB] Team batting done. {inserted} team-seasons saved.")

# ── NBA GAME LOGS (nba_api — free) ───────────────────────────────────────────

# NBA seasons: nba_api uses the format '2023-24'
def get_nba_seasons():
    seasons = []
    end = datetime.now().year
    for y in range(START_YEAR, end + 1):
        seasons.append(f"{y}-{str(y+1)[-2:]}")
    return seasons

def collect_nba_games(conn):
    """Pull NBA team game logs for all seasons. Includes pts, opponent, home/away."""
    from nba_api.stats.endpoints import leaguegamelog
    print("\n[NBA] Collecting game logs...")
    c = conn.cursor()
    seasons = get_nba_seasons()
    total_inserted = 0

    for season in seasons:
        print(f"\n  Season {season}...", end="", flush=True)
        try:
            # Small delay to avoid rate limiting (nba_api is strict)
            time.sleep(1)
            logs = leaguegamelog.LeagueGameLog(
                season=season,
                season_type_all_star="Regular Season",
                player_or_team_abbreviation="T"  # team-level
            )
            df = logs.get_data_frames()[0]
        except Exception as e:
            print(f" [ERROR] {e}")
            continue

        inserted = 0
        # Each row is one team in one game. We process pairs.
        game_ids = df["GAME_ID"].unique()

        for gid in game_ids:
            pair = df[df["GAME_ID"] == gid]
            if len(pair) != 2:
                continue

            row0, row1 = pair.iloc[0], pair[pair.index != pair.index[0]].iloc[0]

            # Determine home/away from MATCHUP field (e.g., "BOS vs. NYK" = home, "BOS @ NYK" = away)
            def is_home(matchup):
                return "vs." in matchup

            for team_row, opp_row in [(row0, row1), (row1, row0)]:
                home = 1 if is_home(team_row["MATCHUP"]) else 0
                won  = 1 if team_row["WL"] == "W" else 0

                try:
                    c.execute("""
                        INSERT OR IGNORE INTO nba_games
                        (game_id, game_date, team_id, team_name, opponent_id, opponent_name,
                         is_home, pts, opp_pts, winner, season)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        str(team_row["GAME_ID"]),
                        team_row["GAME_DATE"],
                        int(team_row["TEAM_ID"]),
                        team_row["TEAM_NAME"],
                        int(opp_row["TEAM_ID"]),
                        opp_row["TEAM_NAME"],
                        home,
                        int(team_row["PTS"]),
                        int(opp_row["PTS"]),
                        won,
                        season,
                    ))
                    inserted += c.rowcount
                except Exception as e:
                    print(f"\n  [WARN] {gid}: {e}")

        conn.commit()
        total_inserted += inserted
        print(f" {inserted // 2} games saved.")  # divide by 2 since we store both team rows

    print(f"[NBA] Game logs done. Total game-team rows: {total_inserted}")

# ── NBA ADVANCED STATS (per-game rolling via TeamDashboard) ──────────────────

def collect_nba_advanced(conn):
    """
    Pull advanced per-game stats (ORTG, DRTG, pace, eFG%) by season.
    These are aggregated per team per season — we'll use rolling windows in feature engineering.
    """
    from nba_api.stats.endpoints import teamestimatedmetrics
    print("\n[NBA] Collecting advanced team metrics...")
    c = conn.cursor()
    seasons = get_nba_seasons()

    for season in seasons:
        print(f"\n  Season {season}...", end="", flush=True)
        try:
            time.sleep(1)
            metrics = teamestimatedmetrics.TeamEstimatedMetrics(season=season)
            df = metrics.get_data_frames()[0]
        except Exception as e:
            print(f" [ERROR] {e}")
            continue

        inserted = 0
        for _, row in df.iterrows():
            try:
                c.execute("""
                    INSERT OR IGNORE INTO nba_team_advanced
                    (game_id, team_id, season, off_rating, def_rating, net_rating, pace,
                     efg_pct, ts_pct, ast_pct, reb_pct)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    f"season_{season}_{int(row['TEAM_ID'])}",  # synthetic key for season totals
                    int(row["TEAM_ID"]),
                    season,
                    float(row.get("E_OFF_RATING", 0) or 0),
                    float(row.get("E_DEF_RATING", 0) or 0),
                    float(row.get("E_NET_RATING", 0) or 0),
                    float(row.get("E_PACE", 0) or 0),
                    None,  # eFG% not in this endpoint — added in feature engineering
                    None,
                    None,
                    None,
                ))
                inserted += c.rowcount
            except Exception as e:
                print(f"\n  [WARN] {e}")

        conn.commit()
        print(f" {inserted} teams.")

    print(f"[NBA] Advanced metrics done.")

# ── HISTORICAL ODDS (The Odds API — uses credits) ─────────────────────────────

SPORT_MAP = {
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
}

DATE_RANGES = {
    "mlb": [
        (f"{y}-04-01", f"{y}-10-15")
        for y in range(START_YEAR, datetime.now().year + 1)
    ],
    "nba": [
        (f"{y}-10-15", f"{y+1}-06-30")
        for y in range(START_YEAR, datetime.now().year + 1)
    ],
}

def date_range(start_str, end_str, step_days=7):
    """Generate weekly date snapshots between start and end."""
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.strptime(end_str, "%Y-%m-%d")
    cur   = start
    while cur <= end:
        yield cur.strftime("%Y-%m-%dT12:00:00Z")  # noon UTC snapshot
        cur += timedelta(days=step_days)

def collect_historical_odds(conn):
    """
    Pull historical odds snapshots. Requires a paid Odds API plan.
    Set COLLECT_HISTORICAL_ODDS = True in CONFIG to enable.

    Credit estimate: ~300 requests per sport per 5 years (weekly snapshots).
    At $79/month Starter plan (10k credits), this is well within budget.
    """
    if not COLLECT_HISTORICAL_ODDS:
        print("\n[ODDS] Skipped. Set COLLECT_HISTORICAL_ODDS = True to enable.")
        print("       Requires Odds API paid plan (~300 credits per sport).")
        return

    print("\n[ODDS] Collecting historical odds (weekly snapshots)...")
    c = conn.cursor()
    total = 0

    for sport, api_sport in SPORT_MAP.items():
        for start, end in DATE_RANGES[sport]:
            for snapshot_date in date_range(start, end, step_days=7):
                url = (
                    f"{ODDS_BASE}/historical/sports/{api_sport}/odds"
                    f"?apiKey={ODDS_API_KEY}"
                    f"&regions=us"
                    f"&markets=h2h,totals"
                    f"&oddsFormat=american"
                    f"&date={snapshot_date}"
                )
                data = fetch_json(url)
                if not data:
                    continue

                games = data.get("data", [])
                for game in games:
                    game_date  = game.get("commence_time", "")[:10]
                    home_team  = game.get("home_team", "")
                    away_team  = game.get("away_team", "")

                    for bookmaker in game.get("bookmakers", []):
                        book = bookmaker["key"]
                        if book not in ["fanduel", "draftkings", "betmgm", "betonlineag"]:
                            continue

                        home_ml = away_ml = None
                        total_line = over_odds = under_odds = None

                        for market in bookmaker.get("markets", []):
                            if market["key"] == "h2h":
                                for outcome in market["outcomes"]:
                                    if outcome["name"] == home_team:
                                        home_ml = outcome["price"]
                                    elif outcome["name"] == away_team:
                                        away_ml = outcome["price"]
                            elif market["key"] == "totals":
                                for outcome in market["outcomes"]:
                                    total_line = outcome.get("point", total_line)
                                    if outcome["name"] == "Over":
                                        over_odds = outcome["price"]
                                    elif outcome["name"] == "Under":
                                        under_odds = outcome["price"]

                        try:
                            c.execute("""
                                INSERT OR IGNORE INTO historical_odds
                                (sport, game_date, home_team, away_team, book,
                                 home_ml, away_ml, total_line, over_odds, under_odds, recorded_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            """, (sport, game_date, home_team, away_team, book,
                                  home_ml, away_ml, total_line, over_odds, under_odds, snapshot_date))
                            total += c.rowcount
                        except Exception:
                            pass

                conn.commit()
                time.sleep(0.3)  # Odds API rate limit

    print(f"[ODDS] Done. {total} odds rows saved.")

# ── SUMMARY ───────────────────────────────────────────────────────────────────

def print_summary(conn):
    c = conn.cursor()
    tables = ["mlb_games", "mlb_pitcher_stats", "mlb_team_batting",
              "nba_games", "nba_team_advanced", "historical_odds"]
    print("\n" + "━"*50)
    print("DATABASE SUMMARY")
    print("━"*50)
    for t in tables:
        c.execute(f"SELECT COUNT(*) FROM {t}")
        count = c.fetchone()[0]
        print(f"  {t:<28} {count:>8,} rows")
    print("━"*50)
    print(f"  Saved to: {DB_PATH}")
    print("\nNext step: run 3_engineer_features.py to build the model dataset.\n")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("━"*50)
    print("HISTORICAL DATA COLLECTOR")
    print(f"  Collecting {START_YEAR} → {datetime.now().year}")
    print(f"  Database: {DB_PATH}")
    print("━"*50)

    # Check libraries are installed
    check_dependencies()

    # Suppress noisy FanGraphs cache warnings from pybaseball
    import warnings
    warnings.filterwarnings("ignore")

    # Suppress verbose nba_api HTTP logging
    import logging
    logging.getLogger("nba_api").setLevel(logging.ERROR)

    conn = sqlite3.connect(DB_PATH)

    try:
        # 1. Create tables
        init_db(conn)

        # 2. MLB game results (MLB Stats API — fast, free)
        collect_mlb_games(conn)

        # 3. MLB pitcher stats — xFIP, FIP, K% (FanGraphs via pybaseball — slow)
        collect_mlb_pitcher_stats(conn)

        # 4. MLB team batting — wRC+, OBP (FanGraphs via pybaseball)
        collect_mlb_team_batting(conn)

        # 5. NBA game logs — scores, home/away, results (nba_api — free)
        collect_nba_games(conn)

        # 6. NBA advanced metrics — ORTG, DRTG, pace (nba_api — free)
        collect_nba_advanced(conn)

        # 7. Historical odds (optional — requires paid Odds API plan)
        collect_historical_odds(conn)

        # Print final row counts
        print_summary(conn)

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Progress saved to database. Re-run to continue.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
