#!/usr/bin/env python3
"""
Daily Sports Betting Report — MLB & NBA
Pulls moneylines, game totals (O/U), and player props from The Odds API.
Primary book: FanDuel (Fanatics proxy). Compares DraftKings and BetMGM for line shopping.
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API_KEY = "26dcaafb7d69e3ac46d339c733105f90"
BASE = "https://api.the-odds-api.com/v4"
BOOKS = ["fanduel", "draftkings", "betmgm"]
PRIMARY = "fanduel"
EASTERN = ZoneInfo("America/New_York")

# ── Helpers ──────────────────────────────────────────────────────────────────

def fetch(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())

def american_to_implied(odds):
    """Convert American odds to implied probability %."""
    if odds > 0:
        return 100 / (odds + 100) * 100
    else:
        return abs(odds) / (abs(odds) + 100) * 100

def format_odds(odds):
    return f"+{odds}" if odds > 0 else str(odds)

def game_time(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
    return dt.strftime("%-I:%M %p ET")

def is_today(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
    now = datetime.now(EASTERN)
    return dt.date() == now.date()

def is_upcoming(iso, hours=24):
    """Games that haven't started yet within the next N hours."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
    now = datetime.now(EASTERN)
    from datetime import timedelta
    return now <= dt <= now + timedelta(hours=hours)

def value_tag(fanduel_odds, best_odds):
    """Flag if another book has better odds than FanDuel."""
    if best_odds > fanduel_odds:
        diff = best_odds - fanduel_odds
        return f" ← +{diff} better elsewhere" if diff >= 5 else ""
    return ""

def best_odds_across_books(bookmakers, market_key, outcome_name, point=None, description=None):
    """Find the best price for a specific outcome across all books."""
    best = None
    best_book = None
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market["key"] != market_key:
                continue
            for o in market["outcomes"]:
                name_match = o["name"] == outcome_name
                desc_match = description is None or o.get("description") == description
                point_match = point is None or o.get("point") == point
                if name_match and desc_match and point_match:
                    if best is None or o["price"] > best:
                        best = o["price"]
                        best_book = bm["title"]
    return best, best_book

# ── Sections ─────────────────────────────────────────────────────────────────

def get_moneylines(sport_key, sport_label):
    url = (f"{BASE}/sports/{sport_key}/odds/"
           f"?apiKey={API_KEY}&regions=us&markets=h2h"
           f"&oddsFormat=american&bookmakers={','.join(BOOKS)}")
    games = fetch(url)
    today = [g for g in games if is_today(g["commence_time"])]

    lines = []
    for game in today:
        home = game["home_team"]
        away = game["away_team"]
        time = game_time(game["commence_time"])
        row = {"matchup": f"{away} @ {home}", "time": time, "sides": {}}

        for bm in game["bookmakers"]:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for o in market["outcomes"]:
                    team = o["name"]
                    if team not in row["sides"]:
                        row["sides"][team] = {}
                    row["sides"][team][bm["key"]] = o["price"]

        lines.append(row)
    return lines

def get_totals(sport_key):
    url = (f"{BASE}/sports/{sport_key}/odds/"
           f"?apiKey={API_KEY}&regions=us&markets=totals"
           f"&oddsFormat=american&bookmakers={','.join(BOOKS)}")
    games = fetch(url)
    today = [g for g in games if is_today(g["commence_time"])]

    totals = []
    for game in today:
        home = game["home_team"]
        away = game["away_team"]
        time = game_time(game["commence_time"])
        row = {"matchup": f"{away} @ {home}", "time": time, "books": {}}

        for bm in game["bookmakers"]:
            for market in bm.get("markets", []):
                if market["key"] != "totals":
                    continue
                for o in market["outcomes"]:
                    side = o["name"]  # Over / Under
                    key = f"{side}_{o['point']}"
                    if key not in row["books"]:
                        row["books"][key] = {"point": o["point"], "side": side, "odds": {}}
                    row["books"][key]["odds"][bm["key"]] = o["price"]

        totals.append(row)
    return totals

def get_player_props(sport_key, prop_markets, upcoming_only=False, all_books=False):
    url = f"{BASE}/sports/{sport_key}/events?apiKey={API_KEY}"
    events = fetch(url)
    if upcoming_only:
        today_events = [e for e in events if is_upcoming(e["commence_time"])]
    else:
        today_events = [e for e in events if is_today(e["commence_time"])]

    all_props = []
    for event in today_events:
        matchup = f"{event['away_team']} @ {event['home_team']}"
        time = game_time(event["commence_time"])
        markets_param = ",".join(prop_markets)
        books_param = "" if all_books else f"&bookmakers={','.join(BOOKS)}"
        url2 = (f"{BASE}/sports/{sport_key}/events/{event['id']}/odds"
                f"?apiKey={API_KEY}&regions=us&markets={markets_param}"
                f"&oddsFormat=american{books_param}")
        try:
            data = fetch(url2)
        except Exception:
            continue

        # Collect props: player → market → point → {book: odds}
        props = {}
        for bm in data.get("bookmakers", []):
            for market in bm.get("markets", []):
                mkey = market["key"]
                for o in market["outcomes"]:
                    player = o.get("description") or o["name"]
                    side = o["name"]   # Over / Under
                    point = o.get("point")
                    prop_id = f"{player}|{mkey}|{point}|{side}"
                    if prop_id not in props:
                        props[prop_id] = {
                            "player": player,
                            "market": mkey,
                            "point": point,
                            "side": side,
                            "odds": {}
                        }
                    props[prop_id]["odds"][bm["key"]] = o["price"]

        all_props.append({"matchup": matchup, "time": time, "props": list(props.values())})
    return all_props

# ── Value Finders ─────────────────────────────────────────────────────────────

def best_moneyline_value(lines):
    """Find best moneyline value: underdogs with +EV, best available price."""
    picks = []
    for game in lines:
        for team, book_odds in game["sides"].items():
            if PRIMARY not in book_odds:
                continue
            fd_odds = book_odds[PRIMARY]
            best_price = max(book_odds.values())
            best_book = [b for b, o in book_odds.items() if o == best_price][0]
            implied = american_to_implied(fd_odds)
            picks.append({
                "matchup": game["matchup"],
                "time": game["time"],
                "team": team,
                "fanduel": fd_odds,
                "best_price": best_price,
                "best_book": best_book,
                "implied_pct": implied,
            })
    # Sort: underdogs first (positive odds), then by implied prob ascending (most value)
    picks.sort(key=lambda x: (-1 if x["fanduel"] > 0 else 1, x["implied_pct"]))
    return picks

def best_totals_value(totals):
    """Find O/U with line discrepancies or best juice across books."""
    picks = []
    for game in totals:
        for key, data in game["books"].items():
            if PRIMARY not in data["odds"]:
                continue
            fd_odds = data["odds"][PRIMARY]
            best_price = max(data["odds"].values())
            best_book = [b for b, o in data["odds"].items() if o == best_price][0]
            # Find if any book has a different point (line discrepancy)
            picks.append({
                "matchup": game["matchup"],
                "time": game["time"],
                "side": data["side"],
                "point": data["point"],
                "fanduel": fd_odds,
                "best_price": best_price,
                "best_book": best_book,
            })
    # Sort: best juice (highest odds = least vig) first
    picks.sort(key=lambda x: -x["best_price"])
    return picks

def american_to_decimal(odds):
    if odds > 0:
        return (odds / 100) + 1
    else:
        return (100 / abs(odds)) + 1

def build_hr_parlay(mlb_props):
    """
    Pick 3-4 best-value HR parlay legs from today's MLB games.
    Strategy: target Over 0.5 HR props, best odds on FanDuel, one player per game,
    exclude extreme chalk (<-200) and extreme longshots (>+500).
    Prefer players in +100 to +300 range — realistic HR threats with value.
    """
    candidates = []
    seen_games = set()

    for game in mlb_props:
        for prop in game["props"]:
            if prop["market"] != "batter_home_runs":
                continue
            if prop["side"] != "Over":
                continue
            if prop["point"] != 0.5:
                continue
            if not prop["odds"]:
                continue
            best_price = max(prop["odds"].values())
            best_book = [b for b, o in prop["odds"].items() if o == best_price][0]
            fd_odds = prop["odds"].get(PRIMARY, best_price)
            # Target realistic power hitter range: +150 to +500
            if best_price < 150 or best_price > 500:
                continue
            candidates.append({
                "player": prop["player"],
                "matchup": game["matchup"],
                "time": game["time"],
                "fanduel": fd_odds,
                "best_price": best_price,
                "best_book": best_book,
                "decimal": american_to_decimal(best_price),
            })

    # One player per game, best odds
    best_per_game = {}
    for c in candidates:
        g = c["matchup"]
        if g not in best_per_game or c["best_price"] > best_per_game[g]["best_price"]:
            best_per_game[g] = c

    pool = sorted(best_per_game.values(), key=lambda x: -x["best_price"])

    # Pick 3 or 4 legs — 4 if we have enough options, else 3
    legs = pool[:4] if len(pool) >= 4 else pool[:3]

    if not legs:
        return None

    # Calculate combined parlay odds
    combined_decimal = 1.0
    for leg in legs:
        combined_decimal *= leg["decimal"]
    combined_american = round((combined_decimal - 1) * 100) if combined_decimal >= 2 else round(-100 / (combined_decimal - 1))

    return {"legs": legs, "combined_american": combined_american, "combined_decimal": round(combined_decimal, 2)}


def best_props_value(all_props):
    """Find player props with best price on FanDuel or better elsewhere."""
    picks = []
    for game in all_props:
        for prop in game["props"]:
            if PRIMARY not in prop["odds"]:
                continue
            fd_odds = prop["odds"][PRIMARY]
            best_price = max(prop["odds"].values())
            best_book = [b for b, o in prop["odds"].items() if o == best_price][0]
            market_label = (prop["market"]
                            .replace("player_", "")
                            .replace("batter_", "")
                            .replace("pitcher_", "P ")
                            .replace("_", " ")
                            .title())
            picks.append({
                "matchup": game["matchup"],
                "time": game["time"],
                "player": prop["player"],
                "market": market_label,
                "side": prop["side"],
                "point": prop["point"],
                "fanduel": fd_odds,
                "best_price": best_price,
                "best_book": best_book,
            })
    # Sort: best available price descending
    picks.sort(key=lambda x: -x["best_price"])
    return picks

# ── Formatter ─────────────────────────────────────────────────────────────────

def print_hr_parlay(mlb_props):
    parlay = build_hr_parlay(mlb_props)
    divider = "═" * 70
    print(f"\n{divider}")
    if not parlay:
        print("  ⚾  TODAY'S HR PARLAY")
        print(divider)
        print("  Not enough HR props available today for a parlay.\n")
        return
    print(f"  ⚾  TODAY'S HR PARLAY  ({len(parlay['legs'])}-LEG)")
    print(divider)
    for i, leg in enumerate(parlay["legs"], 1):
        tag = ""
        if leg["best_book"].lower() != PRIMARY and leg["best_price"] > leg["fanduel"]:
            diff = leg["best_price"] - leg["fanduel"]
            tag = f"  ← +{diff} on {leg['best_book'].upper()}"
        print(f"  LEG {i}: {leg['player']}  To Hit HR  (Over 0.5)")
        print(f"         FD: {format_odds(leg['fanduel'])}  "
              f"Best: {format_odds(leg['best_price'])} ({leg['best_book'].upper()}){tag}")
        print(f"         {leg['matchup']}  {leg['time']}")
    print(f"\n  PARLAY ODDS: {format_odds(parlay['combined_american'])}  "
          f"({parlay['combined_decimal']}x)")
    print(f"  $100 wins ${round((parlay['combined_decimal'] - 1) * 100)}")
    print(divider + "\n")


def print_report(nba_lines, mlb_lines, nba_totals, mlb_totals, nba_props, mlb_props, mlb_hr_props=None):
    now = datetime.now(EASTERN).strftime("%A, %B %-d %Y — %-I:%M %p ET")
    divider = "═" * 70

    print(f"\n{divider}")
    print(f"  DAILY BETTING REPORT  |  {now}")
    print(f"  NBA + MLB  |  FanDuel (Fanatics) Primary  |  DK + MGM Comparison")
    print(divider)

    # ── MONEYLINES ──
    print("\n★  MONEYLINES\n")
    for sport, lines in [("NBA", nba_lines), ("MLB", mlb_lines)]:
        picks = best_moneyline_value(lines)
        if not picks:
            print(f"  {sport}: No games today.\n")
            continue
        print(f"  ── {sport} ──")
        seen = set()
        for p in picks:
            key = f"{p['matchup']}|{p['team']}"
            if key in seen:
                continue
            seen.add(key)
            tag = ""
            if p["best_book"] != PRIMARY and p["best_price"] > p["fanduel"]:
                diff = p["best_price"] - p["fanduel"]
                tag = f"  ← +{diff} on {p['best_book'].upper()}"
            dog = " [DOG]" if p["fanduel"] > 0 else ""
            print(f"  {p['matchup']}  {p['time']}")
            print(f"    {p['team']}{dog}  FD: {format_odds(p['fanduel'])}  "
                  f"Best: {format_odds(p['best_price'])} ({p['best_book'].upper()}){tag}")
        print()

    # ── GAME TOTALS ──
    print("★  GAME TOTALS (O/U)\n")
    for sport, totals in [("NBA", nba_totals), ("MLB", mlb_totals)]:
        picks = best_totals_value(totals)
        if not picks:
            print(f"  {sport}: No games today.\n")
            continue
        print(f"  ── {sport} ──")
        seen_games = {}
        for p in picks:
            if p["matchup"] not in seen_games:
                seen_games[p["matchup"]] = []
            seen_games[p["matchup"]].append(p)

        for matchup, rows in seen_games.items():
            over = next((r for r in rows if r["side"] == "Over"), None)
            under = next((r for r in rows if r["side"] == "Under"), None)
            if over and under:
                print(f"  {matchup}  {over['time']}")
                tag_o = ""
                if over["best_book"] != PRIMARY and over["best_price"] > over["fanduel"]:
                    tag_o = f"  ← {format_odds(over['best_price'])} on {over['best_book'].upper()}"
                tag_u = ""
                if under["best_book"] != PRIMARY and under["best_price"] > under["fanduel"]:
                    tag_u = f"  ← {format_odds(under['best_price'])} on {under['best_book'].upper()}"
                print(f"    O {over['point']}  FD: {format_odds(over['fanduel'])}{tag_o}")
                print(f"    U {under['point']}  FD: {format_odds(under['fanduel'])}{tag_u}")
        print()

    # ── PLAYER PROPS ──
    print("★  PLAYER PROPS\n")
    for sport, props in [("NBA", nba_props), ("MLB", mlb_props)]:
        picks = best_props_value(props)
        if not picks:
            print(f"  {sport}: No props available today.\n")
            continue
        print(f"  ── {sport} — Top Props by Best Available Price ──")
        shown = 0
        seen_props = set()
        for p in picks:
            prop_key = f"{p['player']}|{p['market']}|{p['side']}|{p['point']}"
            if prop_key in seen_props:
                continue
            seen_props.add(prop_key)
            tag = ""
            if p["best_book"] != PRIMARY and p["best_price"] > p["fanduel"]:
                diff = p["best_price"] - p["fanduel"]
                tag = f"  ← +{diff} on {p['best_book'].upper()}"
            print(f"  {p['player']}  {p['market']}  {p['side']} {p['point']}")
            print(f"    FD: {format_odds(p['fanduel'])}  "
                  f"Best: {format_odds(p['best_price'])} ({p['best_book'].upper()}){tag}"
                  f"  [{p['matchup']}]")
            shown += 1
            if shown >= 20:
                remaining = len([x for x in picks if f"{x['player']}|{x['market']}|{x['side']}|{x['point']}" not in seen_props])
                if remaining:
                    print(f"  ... and {remaining} more props available")
                break
        print()

    print_hr_parlay(mlb_hr_props or mlb_props)

    print(divider)
    print("  Odds current as of pull time. Verify on FanDuel before placing.")
    print(f"  Note: Fanatics lines mirror FanDuel (same platform).")
    print(divider + "\n")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Pulling odds data...")

    nba_lines = get_moneylines("basketball_nba", "NBA")
    mlb_lines = get_moneylines("baseball_mlb", "MLB")
    nba_totals = get_totals("basketball_nba")
    mlb_totals = get_totals("baseball_mlb")

    nba_prop_markets = [
        "player_points", "player_rebounds", "player_assists", "player_threes"
    ]
    mlb_prop_markets = [
        "batter_hits", "pitcher_strikeouts", "batter_home_runs", "batter_total_bases"
    ]

    nba_props = get_player_props("basketball_nba", nba_prop_markets)
    mlb_props = get_player_props("baseball_mlb", mlb_prop_markets)
    # HR parlay uses upcoming games + all books (FD/DK/MGM don't always post HR props early)
    mlb_hr_props = get_player_props("baseball_mlb", ["batter_home_runs"], upcoming_only=True, all_books=True)

    print_report(nba_lines, mlb_lines, nba_totals, mlb_totals, nba_props, mlb_props, mlb_hr_props)

if __name__ == "__main__":
    main()
