#!/usr/bin/env python3
"""
Script 2: 2_fetch_odds_espn.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Free daily odds collector — no API key, no paid plan, no quota.

Source: ESPN's internal odds API (powers ESPN.com)
  Provider: DraftKings (sharp book, fast lines)
  Sports:   MLB + NBA
  Data:     Moneylines, spread, O/U, over/under juice

Two outputs:
  1. lines_cache.json  — same format as sports_betting_daily.py uses
                         so 6_predict_today.py works immediately after
  2. sports_history.db → historical_odds table (builds ongoing dataset)

Run this BEFORE 6_predict_today.py each morning.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB_PATH    = os.path.join(BASE_DIR,   "sports_history.db")
CACHE_FILE = os.path.join(PARENT_DIR, "lines_cache.json")
EASTERN    = ZoneInfo("America/New_York")

# ── ESPN API endpoints ────────────────────────────────────────────────────────
ESPN_SCOREBOARD = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
}
ESPN_ODDS_TPL = (
    "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}"
    "/events/{event_id}/competitions/{comp_id}/odds"
)
SPORT_LEAGUE = {
    "MLB": ("baseball", "mlb"),
    "NBA": ("basketball", "nba"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def today_str():
    return datetime.now(EASTERN).strftime("%Y-%m-%d")

def fmt_ml(ml):
    if ml is None:
        return "N/A"
    return f"+{int(ml)}" if ml >= 0 else str(int(ml))

def fetch(url, retries=3):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5)
            else:
                return None

def american_to_prob(ml):
    ml = float(ml)
    return 100 / (ml + 100) if ml >= 0 else abs(ml) / (abs(ml) + 100)

def no_vig(home_ml, away_ml):
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    t  = ph + pa
    return round(ph / t, 4), round(pa / t, 4)

# ── ESPN odds fetcher ─────────────────────────────────────────────────────────

def get_provider(items, preferred="DraftKings"):
    """
    Pick the best odds provider from ESPN's list.
    Prefer DraftKings static (not 'DraftKings - Live Odds').
    Fall back to first available provider.
    """
    static = [i for i in items if i.get("provider", {}).get("name") == preferred]
    if static:
        return static[0]
    # Accept any non-live provider
    non_live = [i for i in items
                if "live" not in i.get("provider", {}).get("name", "").lower()]
    if non_live:
        return non_live[0]
    return items[0] if items else None

def fetch_game_odds(sport_key, event_id, comp_id):
    """Fetch odds for a single game from ESPN's odds API."""
    sport, league = SPORT_LEAGUE[sport_key]
    url = ESPN_ODDS_TPL.format(
        sport=sport, league=league,
        event_id=event_id, comp_id=comp_id,
    )
    data = fetch(url)
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    return get_provider(items)

def parse_odds_item(item, home_team, away_team):
    """Extract structured odds dict from an ESPN odds item."""
    home_node = item.get("homeTeamOdds", {})
    away_node = item.get("awayTeamOdds", {})
    provider  = item.get("provider", {}).get("name", "unknown")

    home_ml = home_node.get("moneyLine")
    away_ml = away_node.get("moneyLine")
    spread  = item.get("spread")        # from home team's perspective (neg = home fav)
    ou      = item.get("overUnder")
    over_j  = item.get("overOdds")
    under_j = item.get("underOdds")

    # Validate — must have moneylines to be useful
    if home_ml is None or away_ml is None:
        return None

    hp, ap = no_vig(home_ml, away_ml)

    return {
        "home_team":   home_team,
        "away_team":   away_team,
        "home_ml":     int(home_ml),
        "away_ml":     int(away_ml),
        "spread":      spread,
        "total_line":  ou,
        "over_odds":   over_j,
        "under_odds":  under_j,
        "home_prob":   hp,
        "away_prob":   ap,
        "provider":    provider,
    }

# ── Today's games ─────────────────────────────────────────────────────────────

def get_today_games(sport_key):
    """
    Return list of today's games with ESPN event IDs.
    Each entry: {event_id, comp_id, home_team, away_team, game_time, status}
    """
    data = fetch(ESPN_SCOREBOARD[sport_key])
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]

        # Skip finished games (no need to bet)
        state = comp.get("status", {}).get("type", {}).get("state", "")
        if state in ("post",):
            continue

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c["homeAway"] == "home"), None)
        away = next((c for c in competitors if c["homeAway"] == "away"), None)
        if not home or not away:
            continue

        games.append({
            "event_id":  event["id"],
            "comp_id":   comp["id"],
            "home_team": home["team"]["displayName"],
            "away_team": away["team"]["displayName"],
            "game_time": event.get("date", ""),
            "status":    state,
        })

    return games

# ── Cache writer ──────────────────────────────────────────────────────────────

def update_cache(cache, sport_label, odds, game_date):
    """
    Write odds to lines_cache.json in the format sports_betting_daily.py expects:
    Key: '{date}|{sport}|{away} @ {home}|{team}' → moneyline (int)
    """
    matchup   = f"{odds['away_team']} @ {odds['home_team']}"
    home_key  = f"{game_date}|{sport_label}|{matchup}|{odds['home_team']}"
    away_key  = f"{game_date}|{sport_label}|{matchup}|{odds['away_team']}"

    # Only write if key doesn't exist (preserve opening line once set)
    if home_key not in cache:
        cache[home_key] = odds["home_ml"]
    if away_key not in cache:
        cache[away_key] = odds["away_ml"]

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

# ── DB writer ─────────────────────────────────────────────────────────────────

def ensure_db_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_odds (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sport        TEXT,
            game_date    TEXT,
            home_team    TEXT,
            away_team    TEXT,
            book         TEXT,
            home_ml      INTEGER,
            away_ml      INTEGER,
            total_line   REAL,
            over_odds    INTEGER,
            under_odds   INTEGER,
            spread       REAL,
            open_home_ml INTEGER,
            open_away_ml INTEGER,
            open_total   REAL,
            open_spread  REAL,
            recorded_at  TEXT,
            UNIQUE(sport, game_date, home_team, away_team, book)
        )
    """)
    conn.commit()

def save_to_db(conn, sport_label, odds, game_date):
    now = datetime.now(EASTERN).isoformat()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO historical_odds
            (sport, game_date, home_team, away_team, book,
             home_ml, away_ml, total_line, over_odds, under_odds,
             spread, recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sport_label.lower(),
            game_date,
            odds["home_team"],
            odds["away_team"],
            odds["provider"].lower().replace(" ", "_"),
            odds["home_ml"],
            odds["away_ml"],
            odds["total_line"],
            int(odds["over_odds"])  if odds["over_odds"]  else None,
            int(odds["under_odds"]) if odds["under_odds"] else None,
            odds["spread"],
            now,
        ))
        conn.commit()
    except Exception as e:
        print(f"  [DB WARN] {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def collect_sport(sport_key, cache, conn, today):
    print(f"\n[{sport_key}] Fetching today's games...")
    games = get_today_games(sport_key)
    print(f"  {len(games)} games found")

    collected = []
    for game in games:
        time.sleep(0.4)  # polite rate limiting

        item = fetch_game_odds(sport_key, game["event_id"], game["comp_id"])
        if not item:
            print(f"  No odds: {game['away_team']} @ {game['home_team']}")
            continue

        odds = parse_odds_item(item, game["home_team"], game["away_team"])
        if not odds:
            print(f"  Incomplete odds: {game['away_team']} @ {game['home_team']}")
            continue

        # Write to cache and DB
        update_cache(cache, sport_key, odds, today)
        if conn:
            save_to_db(conn, sport_key, odds, today)

        collected.append(odds)

        # Print what we got
        ou_str  = f"O/U {odds['total_line']}" if odds["total_line"] else "no total"
        spd_str = f"spread {odds['spread']:+.1f}" if odds["spread"] is not None else ""
        print(
            f"  {odds['away_team']:25} @ {odds['home_team']:25}  "
            f"away {fmt_ml(odds['away_ml']):5}  home {fmt_ml(odds['home_ml']):5}  "
            f"{ou_str}  {spd_str}"
        )

    return collected


def main():
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    print("━"*60)
    print(f" ESPN ODDS COLLECTOR — {today}")
    print(f" Source: ESPN (DraftKings lines, free, no key needed)")
    print("━"*60)

    # Load existing cache (preserve opening lines already stored)
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.load(open(CACHE_FILE))
        except Exception:
            pass
    cache_before = len(cache)

    # Open DB if it exists
    conn = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None
    if conn:
        ensure_db_table(conn)

    # Collect odds
    mlb_odds = collect_sport("MLB", cache, conn, today)
    nba_odds = collect_sport("NBA", cache, conn, today)

    # Save updated cache
    save_cache(cache)

    if conn:
        conn.close()

    # Summary
    new_entries = len(cache) - cache_before
    print("\n" + "━"*60)
    print(f" DONE")
    print(f"  MLB: {len(mlb_odds)} games with odds")
    print(f"  NBA: {len(nba_odds)} games with odds")
    print(f"  Cache: {new_entries} new entries added → {CACHE_FILE}")
    print(f"  DB:    odds saved to historical_odds table")
    print()
    print(" Next: python3 model/6_predict_today.py")
    print("━"*60)


if __name__ == "__main__":
    main()
