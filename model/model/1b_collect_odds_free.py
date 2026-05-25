#!/usr/bin/env python3
"""
Script 1b: 1b_collect_odds_free.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Free historical odds scraper — no API key, no paid plan needed.

Source: sportsbookreviewsonline.com (SBR)
  MLB  2020–2021 → Excel (.xlsx) files (Pinnacle / consensus lines)
  NBA  2020-21, 2021-22, 2022-23 → HTML tables embedded in their site

What's in the data (Pinnacle opening + closing lines):
  MLB  → Opening ML, Closing ML, Opening O/U, Closing O/U, Run Line
  NBA  → Opening ML, Closing ML, Opening Spread, Closing Spread, Opening Total

Adds rows to the historical_odds table in sports_history.db.
Run AFTER 1_collect_historical.py.

Runtime: ~2 minutes (mostly download time).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import io
import os
import re
import sqlite3
import time
import urllib.request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_history.db")
SBR_BASE = "https://www.sportsbookreviewsonline.com"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ── MLB xlsx files available on SBR (2010–2021) ───────────────────────────────
# We only download the years that overlap with our model window (2020+).
MLB_XLSX = {
    2020: "/wp-content/uploads/sportsbookreviewsonline_com_737/mlb-odds-2020.xlsx",
    2021: "/wp-content/uploads/sportsbookreviewsonline_com_737/mlb-odds-2021.xlsx",
}

# ── NBA HTML article pages with embedded odds tables ──────────────────────────
# Season label → (URL slug, year the season started)
NBA_PAGES = {
    "2020-21": ("/scoresoddsarchives/nba-odds-2020-21/", 2020),
    "2021-22": ("/scoresoddsarchives/nba-odds-2021-22/", 2021),
    "2022-23": ("/scoresoddsarchives/nba-odds-2022-23/", 2022),
}

# ── MLB 3-letter team codes → full names (matches mlb_games table) ────────────
MLB_TEAM_MAP = {
    "ANA": "Los Angeles Angels",   "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",       "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",       "CHC": "Chicago Cubs",
    "CIN": "Cincinnati Reds",      "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",     "CWS": "Chicago White Sox",
    "CUB": "Chicago Cubs",         "DET": "Detroit Tigers",
    "HOU": "Houston Astros",       "KAN": "Kansas City Royals",
    "LAA": "Los Angeles Angels",   "LAD": "Los Angeles Dodgers",
    "LAN": "Los Angeles Dodgers",  "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",    "MIN": "Minnesota Twins",
    "MNT": "Minnesota Twins",      "NYM": "New York Mets",
    "NYY": "New York Yankees",     "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies","PIT": "Pittsburgh Pirates",
    "SDG": "San Diego Padres",     "SEA": "Seattle Mariners",
    "SFG": "San Francisco Giants", "STL": "St. Louis Cardinals",
    "TAM": "Tampa Bay Rays",       "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",    "WAS": "Washington Nationals",
    "WSH": "Washington Nationals",
    # alternate codes SBR uses
    "CHW": "Chicago White Sox",    "KCR": "Kansas City Royals",
    "TBR": "Tampa Bay Rays",       "SDP": "San Diego Padres",
    "SFN": "San Francisco Giants", "NYA": "New York Yankees",
    "NYN": "New York Mets",        "LAN": "Los Angeles Dodgers",
    "SDN": "San Diego Padres",
}

# ── NBA short names → full names (matches nba_games table) ───────────────────
NBA_TEAM_MAP = {
    "Atlanta":        "Atlanta Hawks",
    "Boston":         "Boston Celtics",
    "Brooklyn":       "Brooklyn Nets",
    "Charlotte":      "Charlotte Hornets",
    "Chicago":        "Chicago Bulls",
    "Cleveland":      "Cleveland Cavaliers",
    "Dallas":         "Dallas Mavericks",
    "Denver":         "Denver Nuggets",
    "Detroit":        "Detroit Pistons",
    "GoldenState":    "Golden State Warriors",
    "Houston":        "Houston Rockets",
    "Indiana":        "Indiana Pacers",
    "LAClippers":     "Los Angeles Clippers",
    "LALakers":       "Los Angeles Lakers",
    "Memphis":        "Memphis Grizzlies",
    "Miami":          "Miami Heat",
    "Milwaukee":      "Milwaukee Bucks",
    "Minnesota":      "Minnesota Timberwolves",
    "NewOrleans":     "New Orleans Pelicans",
    "NewYork":        "New York Knicks",
    "OklahomaCity":   "Oklahoma City Thunder",
    "Orlando":        "Orlando Magic",
    "Philadelphia":   "Philadelphia 76ers",
    "Phoenix":        "Phoenix Suns",
    "Portland":       "Portland Trail Blazers",
    "Sacramento":     "Sacramento Kings",
    "SanAntonio":     "San Antonio Spurs",
    "Toronto":        "Toronto Raptors",
    "Utah":           "Utah Jazz",
    "Washington":     "Washington Wizards",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_bytes(url, retries=3):
    """Download bytes from a URL with retry logic."""
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                # Handle gzip encoding manually if needed
                enc = resp.headers.get("Content-Encoding", "")
                if enc == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:
            print(f"  [retry {attempt+1}/{retries}] {e}")
            time.sleep(2)
    return None

def fetch_html(url):
    """Fetch a page as a decoded string."""
    raw = fetch_bytes(url)
    if raw is None:
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")

def safe_int(val, default=None):
    """Convert to int, returning default on failure."""
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return default

def safe_float(val, default=None):
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return default

def norm_mlb(abbr):
    return MLB_TEAM_MAP.get(str(abbr).upper().strip(), str(abbr))

def norm_nba(short):
    return NBA_TEAM_MAP.get(str(short).strip(), str(short))

def init_db(conn):
    """Ensure historical_odds table exists (safe if already created by script 1)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_odds (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sport         TEXT,
            game_date     TEXT,
            home_team     TEXT,
            away_team     TEXT,
            book          TEXT,
            home_ml       INTEGER,
            away_ml       INTEGER,
            total_line    REAL,
            over_odds     INTEGER,
            under_odds    INTEGER,
            spread        REAL,
            open_home_ml  INTEGER,
            open_away_ml  INTEGER,
            open_total    REAL,
            open_spread   REAL,
            recorded_at   TEXT,
            UNIQUE(sport, game_date, home_team, away_team, book)
        )
    """)
    # Add new columns if upgrading from script 1's older schema
    for col, coltype in [("spread", "REAL"), ("open_home_ml", "INTEGER"),
                          ("open_away_ml", "INTEGER"), ("open_total", "REAL"),
                          ("open_spread", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE historical_odds ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # column already exists
    conn.commit()

# ── MLB: Parse xlsx files from SBR ───────────────────────────────────────────
#
# Column layout (confirmed from live data):
#   0=Date(MMDD int)  1=Rot  2=VH  3=Team  4=Pitcher
#   5-13=Innings(1-9)  14=Final  15=Open(ML)  16=Close(ML)
#   17=RunLine  18=RunLineOdds  19=OpenOU  20=OpenOUOdds
#   21=CloseOU  22=CloseOUOdds
#
# Each game = 2 consecutive rows (V row then H row).
# Both rows carry the SAME OpenOU/CloseOU (just repeated).
# Open/Close on each row = that team's opening/closing moneyline.

def parse_mlb_xlsx(raw_bytes, year):
    """Parse an SBR MLB xlsx file and return list of game dicts."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    games = []
    i = 1  # skip header row 0
    current_month = 4  # MLB starts in April; we track month rollovers

    while i + 1 < len(rows):
        v_row = rows[i]
        h_row = rows[i + 1]

        # Validate: must be a V/H pair
        if str(v_row[2]).strip().upper() != "V" or str(h_row[2]).strip().upper() != "H":
            i += 1
            continue

        # Date: stored as MMDD integer (e.g., 401 = April 1, 1031 = October 31)
        raw_date = v_row[0]
        if raw_date is None:
            i += 2
            continue
        raw_date = int(raw_date)
        month = raw_date // 100
        day   = raw_date % 100
        game_date = f"{year}-{month:02d}-{day:02d}"

        away_abbr  = str(v_row[3]).strip()
        home_abbr  = str(h_row[3]).strip()
        away_team  = norm_mlb(away_abbr)
        home_team  = norm_mlb(home_abbr)
        away_start = str(v_row[4]).strip() if v_row[4] else ""
        home_start = str(h_row[4]).strip() if h_row[4] else ""

        # Final scores
        away_score = safe_int(v_row[14])
        home_score = safe_int(h_row[14])
        if away_score is None or home_score is None:
            i += 2
            continue

        # Moneylines: each row carries its own team's ML
        open_away_ml  = safe_int(v_row[15])
        close_away_ml = safe_int(v_row[16])
        open_home_ml  = safe_int(h_row[15])
        close_home_ml = safe_int(h_row[16])

        # Run line (same for both rows, just flipped sign)
        run_line      = safe_float(h_row[17])  # home team's run line (-1.5 if fav, +1.5 if dog)
        run_line_odds = safe_int(h_row[18])

        # Over/Under (same value on both rows — use V row)
        open_total    = safe_float(v_row[19])
        open_ou_odds  = safe_int(v_row[20])
        close_total   = safe_float(v_row[21])
        close_ou_odds = safe_int(v_row[22])

        games.append({
            "sport":         "mlb",
            "game_date":     game_date,
            "home_team":     home_team,
            "away_team":     away_team,
            "home_starter":  home_start,
            "away_starter":  away_start,
            "home_score":    home_score,
            "away_score":    away_score,
            # Closing lines (what we use for model vs. market comparison)
            "home_ml":       close_home_ml,
            "away_ml":       close_away_ml,
            "total_line":    close_total,
            "over_odds":     close_ou_odds,
            "under_odds":    close_ou_odds,  # SBR doesn't split over/under odds separately
            "spread":        run_line,
            # Opening lines (for line movement feature)
            "open_home_ml":  open_home_ml,
            "open_away_ml":  open_away_ml,
            "open_total":    open_total,
            "open_spread":   run_line,       # run line doesn't move like spread does
            "book":          "pinnacle",
        })
        i += 2

    return games


def collect_mlb_odds(conn):
    """Download SBR MLB xlsx files and store odds in DB."""
    print("\n[MLB ODDS] Downloading SBR Excel files (Pinnacle lines)...")
    c   = conn.cursor()
    total = 0

    for year, path in sorted(MLB_XLSX.items()):
        url = SBR_BASE + path
        print(f"  {year}... ", end="", flush=True)
        raw = fetch_bytes(url)
        if raw is None:
            print("FAILED (skipping)")
            continue

        games = parse_mlb_xlsx(raw, year)
        inserted = 0
        for g in games:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO historical_odds
                    (sport, game_date, home_team, away_team, book,
                     home_ml, away_ml, total_line, over_odds, under_odds,
                     spread, open_home_ml, open_away_ml, open_total, open_spread)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    g["sport"], g["game_date"], g["home_team"], g["away_team"], g["book"],
                    g["home_ml"], g["away_ml"], g["total_line"], g["over_odds"], g["under_odds"],
                    g["spread"], g["open_home_ml"], g["open_away_ml"], g["open_total"], g["open_spread"],
                ))
                inserted += c.rowcount
            except Exception as e:
                print(f"\n  [WARN] {g['game_date']} {g['away_team']}@{g['home_team']}: {e}")

        conn.commit()
        total += inserted
        print(f"{inserted} games saved.")
        time.sleep(0.5)

    print(f"[MLB ODDS] Done. {total} total games saved.")

# ── NBA: Scrape HTML tables from SBR article pages ───────────────────────────
#
# Column layout (confirmed from live data):
#   0=Date(MMDD str)  1=Rot  2=VH  3=Team
#   4=Q1  5=Q2  6=Q3  7=Q4  8=Final
#   9=Open  10=Close  11=ML  12=2H
#
# Each game = 2 consecutive rows (V row then H row).
#
# KEY RULE (derived from live data inspection):
#   The FAVORITE's row → Open/Close = SPREAD (the points they give)
#   The UNDERDOG's row → Open/Close = TOTAL (the combined score line)
#   ML on each row = that team's closing moneyline
#
# Detect favorite by: negative ML = favorite, positive ML = underdog.

def parse_nba_html(html, season_label, start_year):
    """Parse an SBR NBA HTML table and return list of game dicts."""
    # Extract the table body
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL | re.IGNORECASE)
    if not tables:
        return []

    # Parse all rows from the first (main) table
    all_rows = []
    for raw_row in re.findall(r'<tr[^>]*>(.*?)</tr>', tables[0], re.DOTALL | re.IGNORECASE):
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', raw_row, re.DOTALL | re.IGNORECASE)
        cleaned = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cleaned) >= 12:
            all_rows.append(cleaned)

    games = []
    i = 0
    # Skip the header row
    if all_rows and all_rows[0][0].lower() == "date":
        i = 1

    while i + 1 < len(all_rows):
        v_row = all_rows[i]
        h_row = all_rows[i + 1]

        # Must be a valid V/H pair
        if v_row[2].upper() != "V" or h_row[2].upper() != "H":
            i += 1
            continue

        # Date: stored as MMDD integer string (e.g., "1018" = October 18)
        raw_date = v_row[0].strip()
        if not raw_date.isdigit() or len(raw_date) < 3:
            i += 2
            continue

        raw_date = int(raw_date)
        month = raw_date // 100
        day   = raw_date % 100
        # NBA seasons span two calendar years: Oct-Dec = start_year, Jan-Jun = start_year+1
        cal_year = start_year if month >= 10 else start_year + 1
        game_date = f"{cal_year}-{month:02d}-{day:02d}"

        away_short = v_row[3].strip()
        home_short = h_row[3].strip()
        away_team  = norm_nba(away_short)
        home_team  = norm_nba(home_short)

        # Final scores
        away_score = safe_int(v_row[8])
        home_score = safe_int(h_row[8])
        if away_score is None or home_score is None:
            i += 2
            continue

        # Moneylines (each row = that team's ML)
        away_ml = safe_int(v_row[11])
        home_ml = safe_int(h_row[11])

        if away_ml is None or home_ml is None:
            i += 2
            continue

        # Determine which row is the favorite (negative ML = favored)
        # Favorite's row: Open/Close = SPREAD
        # Underdog's row: Open/Close = TOTAL
        v_open  = safe_float(v_row[9])
        v_close = safe_float(v_row[10])
        h_open  = safe_float(h_row[9])
        h_close = safe_float(h_row[10])

        # Heuristic: NBA totals are 200-260. Spreads are 0-25.
        # If the value is > 100, it's the total. If < 50, it's the spread.
        def is_total(val):
            return val is not None and val > 100

        # Convention stored in DB: negative spread = home is favorite (giving points)
        #                          positive spread = home is underdog (receiving points)
        if is_total(v_open):
            # Visitor row has total → HOME is the favorite
            open_total   = v_open
            close_total  = v_close
            open_spread  = -h_open  if h_open  is not None else None
            close_spread = -h_close if h_close is not None else None
        else:
            # Visitor row has spread → VISITOR is the favorite
            open_total   = h_open
            close_total  = h_close
            open_spread  = v_open    # home receives v_open pts → positive (home underdog)
            close_spread = v_close

        games.append({
            "sport":        "nba",
            "game_date":    game_date,
            "home_team":    home_team,
            "away_team":    away_team,
            "home_score":   home_score,
            "away_score":   away_score,
            "home_ml":      home_ml,
            "away_ml":      away_ml,
            "total_line":   close_total,
            "over_odds":    -110,   # SBR doesn't include juice on totals separately
            "under_odds":   -110,
            "spread":       close_spread,
            "open_home_ml": home_ml,   # SBR HTML only gives one ML snapshot (closing)
            "open_away_ml": away_ml,
            "open_total":   open_total,
            "open_spread":  open_spread,
            "book":         "pinnacle",
            "season":       season_label,
        })
        i += 2

    return games


def collect_nba_odds(conn):
    """Scrape SBR NBA HTML pages and store odds in DB."""
    print("\n[NBA ODDS] Scraping SBR HTML tables (Pinnacle lines)...")
    c     = conn.cursor()
    total = 0

    for season, (slug, start_year) in sorted(NBA_PAGES.items()):
        url = SBR_BASE + slug
        print(f"  {season}... ", end="", flush=True)
        html = fetch_html(url)
        if html is None:
            print("FAILED (skipping)")
            continue

        # Quick sanity check — if we got the homepage, bail
        if "<table" not in html.lower():
            print("no table found (page may have moved — skipping)")
            continue

        games = parse_nba_html(html, season, start_year)
        inserted = 0
        for g in games:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO historical_odds
                    (sport, game_date, home_team, away_team, book,
                     home_ml, away_ml, total_line, over_odds, under_odds,
                     spread, open_home_ml, open_away_ml, open_total, open_spread)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    g["sport"], g["game_date"], g["home_team"], g["away_team"], g["book"],
                    g["home_ml"], g["away_ml"], g["total_line"], g["over_odds"], g["under_odds"],
                    g["spread"], g["open_home_ml"], g["open_away_ml"], g["open_total"], g["open_spread"],
                ))
                inserted += c.rowcount
            except Exception as e:
                print(f"\n  [WARN] {g['game_date']} {g['away_team']}@{g['home_team']}: {e}")

        conn.commit()
        total += inserted
        print(f"{inserted} games saved.")
        time.sleep(1.0)  # Be polite to SBR's server

    print(f"[NBA ODDS] Done. {total} total games saved.")

# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(conn):
    c = conn.cursor()
    print("\n" + "━"*55)
    print("ODDS DATABASE SUMMARY")
    print("━"*55)

    for sport in ("mlb", "nba"):
        c.execute("""
            SELECT COUNT(*), MIN(game_date), MAX(game_date)
            FROM historical_odds WHERE sport=?
        """, (sport,))
        count, min_d, max_d = c.fetchone()
        print(f"  {sport.upper()} odds: {count:>6,} games   ({min_d} → {max_d})")

    print("━"*55)
    print(f"\nDatabase: {DB_PATH}")
    print("\nWhat the odds cover:")
    print("  Opening & closing moneylines (Pinnacle — sharpest book)")
    print("  Opening & closing totals (O/U)")
    print("  Spread / run line")
    print("\nNext step: run 3_engineer_features.py\n")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("━"*55)
    print("FREE HISTORICAL ODDS COLLECTOR")
    print("Source: sportsbookreviewsonline.com (no API key)")
    print("━"*55)

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("\n[ERROR] Missing openpyxl. Run:")
        print("  pip3 install --user openpyxl")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        collect_mlb_odds(conn)
        collect_nba_odds(conn)
        print_summary(conn)
    except KeyboardInterrupt:
        print("\n\n[STOPPED] Progress saved. Re-run to continue.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
