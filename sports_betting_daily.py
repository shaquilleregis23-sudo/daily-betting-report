#!/usr/bin/env python3
"""
Daily Sports Betting Report — MLB & NBA
Pulls moneylines, game totals (O/U), and player props from The Odds API.
Primary book: FanDuel (Fanatics proxy). Compares DraftKings and BetMGM for line shopping.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API_KEY = "26dcaafb7d69e3ac46d339c733105f90"
BASE = "https://api.the-odds-api.com/v4"
MLB_API = "https://statsapi.mlb.com/api/v1"
BOOKS = ["fanduel", "draftkings", "betmgm"]
ALL_BOOKS = ["fanduel", "draftkings", "betmgm", "betonlineag", "betrivers", "betus", "bovada", "lowvig", "mybookieag"]
PRIMARY = "fanduel"
EASTERN = ZoneInfo("America/New_York")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lines_cache.json")

# Park HR factors (above 1.0 = hitter friendly, below = pitcher friendly)
PARK_FACTORS = {
    "Colorado Rockies":       1.38,
    "Cincinnati Reds":        1.26,
    "Boston Red Sox":         1.22,
    "New York Yankees":       1.18,
    "Texas Rangers":          1.15,
    "Baltimore Orioles":      1.14,
    "Philadelphia Phillies":  1.12,
    "Chicago Cubs":           1.10,
    "Toronto Blue Jays":      1.08,
    "Atlanta Braves":         1.06,
    "Milwaukee Brewers":      1.05,
    "Minnesota Twins":        1.04,
    "Kansas City Royals":     1.03,
    "Detroit Tigers":         1.02,
    "Houston Astros":         1.01,
    "Cleveland Guardians":    1.00,
    "St. Louis Cardinals":    0.99,
    "New York Mets":          0.98,
    "Pittsburgh Pirates":     0.97,
    "Chicago White Sox":      0.96,
    "Washington Nationals":   0.95,
    "Los Angeles Angels":     0.95,
    "Los Angeles Dodgers":    0.94,
    "Arizona Diamondbacks":   0.93,
    "Tampa Bay Rays":         0.91,
    "San Diego Padres":       0.90,
    "Oakland Athletics":      0.90,
    "Athletics":              0.90,
    "Seattle Mariners":       0.89,
    "Miami Marlins":          0.88,
    "San Francisco Giants":   0.86,
}

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

# ── Line Movement Cache ───────────────────────────────────────────────────────

def load_lines_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE))
        except Exception:
            pass
    return {}

def save_lines_cache(cache):
    json.dump(cache, open(CACHE_FILE, "w"), indent=2)

def detect_line_moves(games, sport_label, cache):
    """
    Compare current FanDuel moneylines to cached opening lines.
    Returns list of significant moves (10+ pts) and updates cache.
    """
    moves = []
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    for game in games:
        for team, book_odds in game["sides"].items():
            if PRIMARY not in book_odds:
                continue
            current = book_odds[PRIMARY]
            key = f"{today}|{sport_label}|{game['matchup']}|{team}"
            if key not in cache:
                cache[key] = current  # store as opening line
            else:
                opening = cache[key]
                move = current - opening
                if abs(move) >= 10:
                    moves.append({
                        "sport": sport_label,
                        "matchup": game["matchup"],
                        "time": game["time"],
                        "team": team,
                        "opening": opening,
                        "current": current,
                        "move": move,
                    })
    return moves


# ── +EV Calculator ────────────────────────────────────────────────────────────

def no_vig_true_prob(all_odds_side_a, all_odds_side_b):
    """
    Given lists of American odds for both sides of a market across all books,
    calculate the no-vig true probability for side A.
    """
    probs_a = []
    for oa, ob in zip(all_odds_side_a, all_odds_side_b):
        imp_a = american_to_implied(oa) / 100
        imp_b = american_to_implied(ob) / 100
        total = imp_a + imp_b
        probs_a.append(imp_a / total)  # remove vig
    return sum(probs_a) / len(probs_a) if probs_a else None

def ev_pct(book_odds, true_prob):
    """Expected value as a percentage."""
    return ((true_prob * american_to_decimal(book_odds)) - 1) * 100

def get_ev_moneylines(sport_key, sport_label):
    """Pull moneylines from all books, find +EV plays vs consensus."""
    url = (f"{BASE}/sports/{sport_key}/odds/"
           f"?apiKey={API_KEY}&regions=us&markets=h2h"
           f"&oddsFormat=american")  # no bookmakers filter = all books
    games = fetch(url)
    today_games = [g for g in games if is_today(g["commence_time"])]

    ev_plays = []
    for game in today_games:
        teams = [game["away_team"], game["home_team"]]
        # Collect odds per team per book
        book_odds = {t: {} for t in teams}
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for o in mkt["outcomes"]:
                    book_odds[o["name"]][bm["key"]] = o["price"]

        # Need both sides from same books to remove vig
        shared_books = set(book_odds[teams[0]].keys()) & set(book_odds[teams[1]].keys())
        if len(shared_books) < 3:
            continue

        odds_a = [book_odds[teams[0]][b] for b in shared_books]
        odds_b = [book_odds[teams[1]][b] for b in shared_books]
        true_a = no_vig_true_prob(odds_a, odds_b)
        true_b = 1 - true_a if true_a else None
        if not true_a:
            continue

        for team, true_prob in [(teams[0], true_a), (teams[1], true_b)]:
            if PRIMARY not in book_odds[team]:
                continue
            fd_price = book_odds[team][PRIMARY]
            ev = ev_pct(fd_price, true_prob)
            if ev > 0:
                best_price = max(book_odds[team].values())
                best_book = [b for b, p in book_odds[team].items() if p == best_price][0]
                best_ev = ev_pct(best_price, true_prob)
                ev_plays.append({
                    "sport": sport_label,
                    "matchup": f"{game['away_team']} @ {game['home_team']}",
                    "time": game_time(game["commence_time"]),
                    "team": team,
                    "true_prob": true_prob,
                    "fanduel": fd_price,
                    "fd_ev": ev,
                    "best_price": best_price,
                    "best_book": best_book,
                    "best_ev": best_ev,
                    "books_used": len(shared_books),
                })

    ev_plays.sort(key=lambda x: -x["best_ev"])
    return ev_plays

def get_ev_totals(sport_key, sport_label):
    """Pull game totals from all books, find +EV plays vs consensus."""
    url = (f"{BASE}/sports/{sport_key}/odds/"
           f"?apiKey={API_KEY}&regions=us&markets=totals&oddsFormat=american")
    games = fetch(url)
    today_games = [g for g in games if is_today(g["commence_time"])]

    ev_plays = []
    for game in today_games:
        # Collect over/under odds per book per line
        lines = {}
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "totals":
                    continue
                for o in mkt["outcomes"]:
                    pt = o["point"]
                    side = o["name"]
                    key = f"{pt}|{side}"
                    if key not in lines:
                        lines[key] = {}
                    lines[key][bm["key"]] = o["price"]

        # Match over/under pairs at same point
        points = set(k.split("|")[0] for k in lines)
        for pt in points:
            over_key = f"{pt}|Over"
            under_key = f"{pt}|Under"
            if over_key not in lines or under_key not in lines:
                continue
            shared = set(lines[over_key].keys()) & set(lines[under_key].keys())
            if len(shared) < 3:
                continue
            odds_o = [lines[over_key][b] for b in shared]
            odds_u = [lines[under_key][b] for b in shared]
            true_o = no_vig_true_prob(odds_o, odds_u)
            true_u = 1 - true_o if true_o else None
            if not true_o:
                continue
            for side_key, side_label, true_p in [(over_key, "Over", true_o), (under_key, "Under", true_u)]:
                if PRIMARY not in lines[side_key]:
                    continue
                fd_price = lines[side_key][PRIMARY]
                ev = ev_pct(fd_price, true_p)
                if ev > 0:
                    best_price = max(lines[side_key].values())
                    best_book = [b for b, p in lines[side_key].items() if p == best_price][0]
                    best_ev = ev_pct(best_price, true_p)
                    ev_plays.append({
                        "sport": sport_label,
                        "matchup": f"{game['away_team']} @ {game['home_team']}",
                        "time": game_time(game["commence_time"]),
                        "side": side_label,
                        "point": float(pt),
                        "true_prob": true_p,
                        "fanduel": fd_price,
                        "fd_ev": ev,
                        "best_price": best_price,
                        "best_book": best_book,
                        "best_ev": best_ev,
                        "books_used": len(shared),
                    })

    ev_plays.sort(key=lambda x: -x["best_ev"])
    return ev_plays


# ── Confirmed Lineups ─────────────────────────────────────────────────────────

def get_confirmed_lineups():
    """Returns a set of player full names confirmed in today's MLB lineups."""
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    data = mlb_fetch(
        f"{MLB_API}/schedule?sportId=1&date={today}&hydrate=lineups,team"
    )
    confirmed = set()
    for date in data.get("dates", []):
        for game in date.get("games", []):
            for side in ["awayPlayers", "homePlayers"]:
                for p in game.get("lineups", {}).get(side, []):
                    confirmed.add(p["fullName"])
    return confirmed


# ── MLB Stats API ─────────────────────────────────────────────────────────────

_player_cache = {}

def mlb_fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {}

def get_today_probable_pitchers():
    """Returns {matchup_key: {away_pitcher_id, home_pitcher_id}} for today."""
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    data = mlb_fetch(f"{MLB_API}/schedule?sportId=1&date={today}&hydrate=probablePitcher,team")
    games = {}
    for date in data.get("dates", []):
        for game in date.get("games", []):
            away = game["teams"]["away"]["team"]["name"]
            home = game["teams"]["home"]["team"]["name"]
            away_p = game["teams"]["away"].get("probablePitcher", {})
            home_p = game["teams"]["home"].get("probablePitcher", {})
            games[f"{away}|{home}"] = {
                "away_pitcher_id": away_p.get("id"),
                "home_pitcher_id": home_p.get("id"),
                "home_team": home,
            }
    return games

def get_pitcher_hand(pitcher_id):
    """Returns 'L', 'R', or None."""
    if not pitcher_id:
        return None
    data = mlb_fetch(f"{MLB_API}/people/{pitcher_id}")
    people = data.get("people", [])
    if people:
        return people[0].get("pitchHand", {}).get("code")
    return None

def get_player_id(name):
    """Search MLB API for a player ID by name. Cached."""
    if name in _player_cache:
        return _player_cache[name]
    encoded = urllib.parse.quote(name)
    data = mlb_fetch(f"{MLB_API}/people/search?names={encoded}&sportIds=1")
    people = data.get("people", [])
    pid = people[0]["id"] if people else None
    _player_cache[name] = pid
    return pid

def get_hitter_profile(player_id):
    """Returns batting side, season HR/PA rate, last-14-game HRs."""
    if not player_id:
        return None
    # Batting side
    bio = mlb_fetch(f"{MLB_API}/people/{player_id}")
    people = bio.get("people", [])
    bat_side = people[0].get("batSide", {}).get("code", "R") if people else "R"

    # Season stats
    season_data = mlb_fetch(
        f"{MLB_API}/people/{player_id}/stats?stats=season&group=hitting&season=2026"
    )
    season_splits = season_data.get("stats", [{}])[0].get("splits", [])
    season = season_splits[0]["stat"] if season_splits else {}
    hr = int(season.get("homeRuns", 0))
    pa = int(season.get("plateAppearances", 1)) or 1
    hr_rate = hr / pa

    # Last 14 games
    recent_data = mlb_fetch(
        f"{MLB_API}/people/{player_id}/stats?stats=lastXGames&group=hitting&season=2026&limit=14"
    )
    recent_splits = recent_data.get("stats", [{}])[0].get("splits", [])
    recent = recent_splits[0]["stat"] if recent_splits else {}
    recent_hr = int(recent.get("homeRuns", 0))

    return {
        "bat_side": bat_side,
        "season_hr": hr,
        "season_pa": pa,
        "hr_rate": hr_rate,
        "recent_hr": recent_hr,
    }

def score_hr_candidate(candidate, pitchers, home_team):
    """
    Score a HR candidate using real analytics.
    Returns (score, reason_string) or (0, reason) if data unavailable.
    """
    player_id = get_player_id(candidate["player"])
    profile = get_hitter_profile(player_id)
    if not profile:
        return 0.0, "no MLB data"

    # Find opposing pitcher (is player on away or home team?)
    matchup_key = None
    for key in pitchers:
        away, home = key.split("|")
        matchup_name = candidate["matchup"]  # "Away @ Home"
        if home in matchup_name and away in matchup_name:
            matchup_key = key
            break

    opp_pitcher_id = None
    if matchup_key:
        away_name, home_name = matchup_key.split("|")
        # Determine if player bats for away or home team
        matchup_away, matchup_home = candidate["matchup"].split(" @ ")
        if home_name in matchup_home:
            opp_pitcher_id = pitchers[matchup_key]["away_pitcher_id"]
        else:
            opp_pitcher_id = pitchers[matchup_key]["home_pitcher_id"]

    pitcher_hand = get_pitcher_hand(opp_pitcher_id)

    # Platoon advantage: RHB vs LHP or LHB vs RHP
    platoon = False
    if pitcher_hand and profile["bat_side"] != "S":
        platoon = (profile["bat_side"] == "R" and pitcher_hand == "L") or \
                  (profile["bat_side"] == "L" and pitcher_hand == "R")

    # Ballpark factor for home team
    home_team_name = home_team or candidate["matchup"].split(" @ ")[-1]
    park_factor = PARK_FACTORS.get(home_team_name, 1.0)

    # Composite score
    score = (
        profile["hr_rate"] * 100         # HR/PA rate (main driver)
        + profile["recent_hr"] * 0.12    # recent form bonus
        + (0.18 if platoon else 0)        # platoon advantage
    ) * park_factor

    reasons = [
        f"{profile['season_hr']} HR / {profile['season_pa']} PA this season",
        f"{profile['recent_hr']} HR last 14G",
    ]
    if platoon and pitcher_hand:
        reasons.append(f"platoon edge ({profile['bat_side']}HB vs {pitcher_hand}HP)")
    if park_factor >= 1.10:
        reasons.append(f"HR-friendly park ({park_factor:.2f}x)")
    elif park_factor <= 0.90:
        reasons.append(f"pitcher-friendly park ({park_factor:.2f}x)")

    return score, " | ".join(reasons)


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

def build_hr_parlay(mlb_props, confirmed_lineups=None):
    """
    Pick 3-4 HR parlay legs using real analytics:
    - HR/PA rate (season), recent form (last 14G), platoon matchup, ballpark factor
    - One player per game, odds filtered to +150–+550
    - Only confirmed starters if lineup data available
    """
    pitchers = get_today_probable_pitchers()

    # Build candidate pool
    raw_candidates = []
    for game in mlb_props:
        for prop in game["props"]:
            if prop["market"] != "batter_home_runs":
                continue
            if prop["side"] != "Over" or prop["point"] != 0.5:
                continue
            if not prop["odds"]:
                continue
            best_price = max(prop["odds"].values())
            best_book = [b for b, o in prop["odds"].items() if o == best_price][0]
            fd_odds = prop["odds"].get(PRIMARY, best_price)
            if best_price < 150 or best_price > 550:
                continue
            # Skip players not in confirmed lineup (if lineup data available)
            if confirmed_lineups and prop["player"] not in confirmed_lineups:
                continue
            raw_candidates.append({
                "player": prop["player"],
                "matchup": game["matchup"],
                "time": game["time"],
                "fanduel": fd_odds,
                "best_price": best_price,
                "best_book": best_book,
                "decimal": american_to_decimal(best_price),
            })

    # Score each candidate
    scored = []
    for c in raw_candidates:
        home_team = c["matchup"].split(" @ ")[-1]
        score, reason = score_hr_candidate(c, pitchers, home_team)
        scored.append({**c, "score": score, "reason": reason})

    # Best-scoring player per game
    best_per_game = {}
    for c in scored:
        g = c["matchup"]
        if g not in best_per_game or c["score"] > best_per_game[g]["score"]:
            best_per_game[g] = c

    pool = sorted(best_per_game.values(), key=lambda x: -x["score"])
    legs = pool[:4] if len(pool) >= 4 else pool[:3]

    if not legs:
        return None

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

def print_hr_parlay(mlb_props, confirmed_lineups=None):
    parlay = build_hr_parlay(mlb_props, confirmed_lineups)
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
        if leg.get("reason"):
            print(f"         WHY: {leg['reason']}")
    lineup_status = "✅ All legs confirmed in starting lineup" if confirmed_lineups else "⚠️  Lineups not yet confirmed — verify before placing"
    print(f"\n  {lineup_status}")
    print(f"  PARLAY ODDS: {format_odds(parlay['combined_american'])}  "
          f"({parlay['combined_decimal']}x)")
    print(f"  $100 wins ${round((parlay['combined_decimal'] - 1) * 100)}")
    print(divider + "\n")


def print_ev_section(ev_ml_nba, ev_ml_mlb, ev_tot_nba, ev_tot_mlb):
    divider = "═" * 70
    all_plays = ev_ml_nba + ev_ml_mlb + ev_tot_nba + ev_tot_mlb
    all_plays.sort(key=lambda x: -x["best_ev"])

    print(f"\n{divider}")
    print(f"  ⚡  +EV PLAYS  (Edge vs Consensus Line)")
    print(f"  How to read: EV% = how much you gain per $100 bet on average")
    print(divider)

    if not all_plays:
        print("  No +EV plays found today.\n")
        return

    for p in all_plays[:12]:
        if "team" in p:  # moneyline
            label = f"{p['team']}  ML"
        else:  # total
            label = f"{p['side']} {p['point']}  Total"

        fd_tag = f"  FD: {format_odds(p['fanduel'])} (EV: +{p['fd_ev']:.1f}%)" if p["fd_ev"] > 0 else ""
        best_tag = ""
        if p["best_book"] != PRIMARY:
            best_tag = f"  |  Best: {format_odds(p['best_price'])} {p['best_book'].upper()} (EV: +{p['best_ev']:.1f}%)"

        true_pct = p["true_prob"] * 100
        print(f"  [{p['sport']}]  {p['matchup']}  {p['time']}")
        print(f"  {label}")
        print(f"    True prob: {true_pct:.1f}%  |  {p['books_used']} books{fd_tag}{best_tag}")
    print()


def print_line_moves(moves):
    if not moves:
        return
    divider = "═" * 70
    print(f"\n{divider}")
    print(f"  📈  LINE MOVEMENT  (Sharp Money Signals)")
    print(f"  Lines moved 10+ pts from open — sharp bettors likely involved")
    print(divider)
    for m in sorted(moves, key=lambda x: -abs(x["move"])):
        direction = "↑" if m["move"] > 0 else "↓"
        sharp_side = "sharps LIKE this team" if m["move"] > 0 else "sharps AGAINST this team"
        print(f"  [{m['sport']}]  {m['matchup']}  {m['time']}")
        print(f"  {m['team']}:  Open {format_odds(m['opening'])}  →  Now {format_odds(m['current'])}  "
              f"({direction}{abs(m['move'])} pts)  —  {sharp_side}")
    print()


def print_report(nba_lines, mlb_lines, nba_totals, mlb_totals, nba_props, mlb_props,
                 mlb_hr_props=None, ev_ml_nba=None, ev_ml_mlb=None,
                 ev_tot_nba=None, ev_tot_mlb=None, moves=None, confirmed_lineups=None):
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

    print_hr_parlay(mlb_hr_props or mlb_props, confirmed_lineups)

    # +EV plays
    print_ev_section(
        ev_ml_nba  or [],
        ev_ml_mlb  or [],
        ev_tot_nba or [],
        ev_tot_mlb or [],
    )

    # Line movement
    print_line_moves(moves or [])

    print(divider)
    print("  Odds current as of pull time. Verify on FanDuel/Fanatics before placing.")
    print(divider + "\n")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Pulling odds data...")

    # Core odds
    nba_lines = get_moneylines("basketball_nba", "NBA")
    mlb_lines = get_moneylines("baseball_mlb", "MLB")
    nba_totals = get_totals("basketball_nba")
    mlb_totals = get_totals("baseball_mlb")

    # Player props
    nba_prop_markets = ["player_points", "player_rebounds", "player_assists", "player_threes"]
    mlb_prop_markets = ["batter_hits", "pitcher_strikeouts", "batter_home_runs", "batter_total_bases"]
    nba_props = get_player_props("basketball_nba", nba_prop_markets)
    mlb_props = get_player_props("baseball_mlb", mlb_prop_markets)
    mlb_hr_props = get_player_props("baseball_mlb", ["batter_home_runs"], upcoming_only=True, all_books=True)

    # Phase 1: +EV
    print("Calculating +EV plays...")
    ev_ml_nba  = get_ev_moneylines("basketball_nba", "NBA")
    ev_ml_mlb  = get_ev_moneylines("baseball_mlb",   "MLB")
    ev_tot_nba = get_ev_totals("basketball_nba",     "NBA")
    ev_tot_mlb = get_ev_totals("baseball_mlb",       "MLB")

    # Phase 1: Line movement
    cache = load_lines_cache()
    moves = []
    moves += detect_line_moves(nba_lines, "NBA", cache)
    moves += detect_line_moves(mlb_lines, "MLB", cache)
    save_lines_cache(cache)

    # Phase 1: Confirmed lineups for HR parlay
    print("Checking confirmed lineups...")
    confirmed_lineups = get_confirmed_lineups()

    print_report(nba_lines, mlb_lines, nba_totals, mlb_totals, nba_props, mlb_props,
                 mlb_hr_props, ev_ml_nba, ev_ml_mlb, ev_tot_nba, ev_tot_mlb, moves, confirmed_lineups)

if __name__ == "__main__":
    main()
