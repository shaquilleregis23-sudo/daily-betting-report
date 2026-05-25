#!/usr/bin/env python3
"""
Script 7: 7_mark_results.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto-marks W/L in picks_log.json by looking up final scores from
free public APIs — no API key required.

Sources:
  MLB: MLB Stats API (statsapi.mlb.com) — same source used for schedules
  NBA: nba_api scoreboardv2 endpoint

Run this BEFORE 6_predict_today.py each morning so the performance
tracker is always up to date before new picks are added.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "picks_log.json")
MLB_API  = "https://statsapi.mlb.com/api/v1"
EASTERN  = ZoneInfo("America/New_York")


def fetch_json(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── MLB final scores ───────────────────────────────────────────────────────────

def get_mlb_results(date_str):
    """
    Returns {(away_name, home_name): {winner, away_score, home_score}}
    Team names match MLB Stats API exactly (same source as 6_predict_today.py).
    """
    data = fetch_json(f"{MLB_API}/schedule?sportId=1&date={date_str}")
    if not data:
        return {}

    results = {}
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            home       = game["teams"]["home"]
            away       = game["teams"]["away"]
            home_name  = home["team"]["name"]
            away_name  = away["team"]["name"]
            home_score = home.get("score", 0) or 0
            away_score = away.get("score", 0) or 0
            winner     = home_name if home_score > away_score else away_name
            results[(away_name, home_name)] = {
                "winner":     winner,
                "away_score": away_score,
                "home_score": home_score,
            }
    return results


# ── NBA final scores ───────────────────────────────────────────────────────────

def get_nba_results(date_str):
    """
    Returns {(away_name, home_name): {winner, away_score, home_score}}
    Team names match nba_api scoreboard format (same source as 6_predict_today.py).
    """
    try:
        from nba_api.stats.endpoints import scoreboardv2
        time.sleep(0.6)
        sb        = scoreboardv2.ScoreboardV2(game_date=date_str)
        header_df = sb.get_data_frames()[0]   # GameHeader
        line_df   = sb.get_data_frames()[1]   # LineScore

        results = {}
        for _, game in header_df.iterrows():
            if game.get("GAME_STATUS_ID") != 3:   # 3 = Final
                continue
            game_id  = game["GAME_ID"]
            home_id  = game["HOME_TEAM_ID"]
            away_id  = game["VISITOR_TEAM_ID"]

            game_lines = line_df[line_df["GAME_ID"] == game_id]
            home_rows  = game_lines[game_lines["TEAM_ID"] == home_id]
            away_rows  = game_lines[game_lines["TEAM_ID"] == away_id]
            if home_rows.empty or away_rows.empty:
                continue

            hr = home_rows.iloc[0]
            ar = away_rows.iloc[0]

            home_name = f"{hr['TEAM_CITY_NAME']} {hr['TEAM_NICKNAME']}"
            away_name = f"{ar['TEAM_CITY_NAME']} {ar['TEAM_NICKNAME']}"
            home_pts  = int(hr["PTS"]) if hr["PTS"] else 0
            away_pts  = int(ar["PTS"]) if ar["PTS"] else 0
            winner    = home_name if home_pts > away_pts else away_name

            results[(away_name, home_name)] = {
                "winner":     winner,
                "away_score": away_pts,
                "home_score": home_pts,
            }
        return results
    except Exception as e:
        print(f"  [NBA] Result lookup failed: {e}")
        return {}


# ── Matching helpers ───────────────────────────────────────────────────────────

def find_game(results, matchup):
    """
    Find the result entry for 'Away @ Home' matchup.
    Tries exact match first, then nickname (last word) fallback.
    """
    parts = matchup.split(" @ ")
    if len(parts) != 2:
        return None
    away_t, home_t = parts[0].strip(), parts[1].strip()

    # Exact
    if (away_t, home_t) in results:
        return results[(away_t, home_t)]

    # Nickname fallback (last word of team name = city nickname, e.g. "Mets", "Sox")
    away_nick = away_t.split()[-1].lower()
    home_nick = home_t.split()[-1].lower()
    for (away_r, home_r), result in results.items():
        if (away_r.split()[-1].lower() == away_nick and
                home_r.split()[-1].lower() == home_nick):
            return result
    return None


def did_win(bet_team, winner):
    """
    Returns True if bet_team won.
    Uses word-level matching to handle minor name differences.
    """
    if bet_team == winner:
        return True
    # Nickname match (last word)
    if bet_team.split()[-1].lower() == winner.split()[-1].lower():
        return True
    # Multi-word overlap (≥2 shared words)
    b = set(bet_team.lower().split())
    w = set(winner.lower().split())
    return len(b & w) >= 2


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(LOG_FILE):
        print("No picks_log.json found — nothing to mark.")
        return

    with open(LOG_FILE) as f:
        log = json.load(f)

    pending = [e for e in log if e.get("result") not in ("W", "L")]
    if not pending:
        print("All bets already resolved. ✅")
        return

    today = datetime.now(EASTERN).strftime("%Y-%m-%d")

    # Group pending by date
    by_date: dict[str, list] = {}
    for entry in pending:
        d = entry.get("date", "")
        by_date.setdefault(d, []).append(entry)

    resolved = 0
    no_data  = 0

    for date_str in sorted(by_date):
        if date_str >= today:
            print(f"[{date_str}] Skipping — game may not be final yet.")
            continue

        entries     = by_date[date_str]
        mlb_entries = [e for e in entries if e.get("sport") == "MLB"]
        nba_entries = [e for e in entries if e.get("sport") == "NBA"]

        mlb_results = get_mlb_results(date_str) if mlb_entries else {}
        nba_results = get_nba_results(date_str) if nba_entries else {}

        print(f"\n[{date_str}]  MLB games found: {len(mlb_results)}  NBA games found: {len(nba_results)}")

        for entry in entries:
            sport   = entry.get("sport", "")
            results = mlb_results if sport == "MLB" else nba_results
            game    = find_game(results, entry["matchup"])

            if not game:
                print(f"  ❓ No final score found: [{sport}] {entry['matchup']}")
                no_data += 1
                continue

            verdict      = "W" if did_win(entry["bet_team"], game["winner"]) else "L"
            score_str    = f"{game['away_score']}-{game['home_score']}"
            entry["result"] = verdict
            entry["score"]  = score_str
            resolved += 1

            icon = "✅" if verdict == "W" else "❌"
            print(f"  {icon} {verdict}  {entry['bet_team']}  |  {entry['matchup']}  ({score_str})")

    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

    print(f"\n{'─'*40}")
    print(f"  Resolved: {resolved}  |  Not found: {no_data}")
    if resolved:
        wins   = sum(1 for e in log if e.get("result") == "W")
        losses = sum(1 for e in log if e.get("result") == "L")
        print(f"  All-time: {wins}W – {losses}L")
    print(f"{'─'*40}")


if __name__ == "__main__":
    main()
