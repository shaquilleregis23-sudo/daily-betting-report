#!/usr/bin/env python3
"""
Script 3: 3_engineer_features.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Joins game results + odds + stats from sports_history.db into two
model-ready feature matrices (one per sport).

Outputs:
  model/features_mlb.csv   — one row per MLB game
  model/features_nba.csv   — one row per NBA game
  model/feature_cols.json  — which columns the model should train on

Run AFTER 1_collect_historical.py and 1b_collect_odds_free.py.
Runtime: ~30 seconds.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import sqlite3
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_history.db")
OUT_DIR  = os.path.dirname(os.path.abspath(__file__))

# ── Park run factors (1.0 = league average) ───────────────────────────────────
# Source: multi-year average from Baseball Reference
PARK_FACTORS = {
    "Colorado Rockies":       1.38, "Cincinnati Reds":        1.18,
    "Boston Red Sox":         1.14, "New York Yankees":       1.12,
    "Texas Rangers":          1.10, "Baltimore Orioles":      1.08,
    "Philadelphia Phillies":  1.07, "Chicago Cubs":           1.06,
    "Atlanta Braves":         1.05, "Toronto Blue Jays":      1.04,
    "Milwaukee Brewers":      1.03, "Minnesota Twins":        1.02,
    "Kansas City Royals":     1.01, "Detroit Tigers":         1.00,
    "Houston Astros":         1.00, "Cleveland Guardians":    0.99,
    "Cleveland Indians":      0.99, "St. Louis Cardinals":    0.98,
    "New York Mets":          0.97, "Pittsburgh Pirates":     0.96,
    "Chicago White Sox":      0.95, "Washington Nationals":   0.95,
    "Los Angeles Angels":     0.94, "Los Angeles Dodgers":    0.93,
    "Arizona Diamondbacks":   0.92, "Tampa Bay Rays":         0.91,
    "San Diego Padres":       0.90, "Oakland Athletics":      0.90,
    "Athletics":              0.90, "Seattle Mariners":       0.89,
    "Miami Marlins":          0.88, "San Francisco Giants":   0.86,
}

# FanGraphs team abbreviations → full names (for joining mlb_team_batting)
FANGRAPHS_TEAM = {
    "ARI": "Arizona Diamondbacks",   "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",      "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",           "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",    "COL": "Colorado Rockies",
    "CWS": "Chicago White Sox",      "DET": "Detroit Tigers",
    "HOU": "Houston Astros",         "KC":  "Kansas City Royals",
    "LAA": "Los Angeles Angels",     "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",          "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",        "NYM": "New York Mets",
    "NYY": "New York Yankees",       "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",  "PIT": "Pittsburgh Pirates",
    "SD":  "San Diego Padres",       "SEA": "Seattle Mariners",
    "SF":  "San Francisco Giants",   "STL": "St. Louis Cardinals",
    "TB":  "Tampa Bay Rays",         "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",      "WSH": "Washington Nationals",
    "WAS": "Washington Nationals",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_table(conn, table):
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)

def american_to_prob(ml):
    """American moneyline → raw implied probability (still has vig)."""
    ml = float(ml)
    return 100 / (ml + 100) if ml >= 0 else abs(ml) / (abs(ml) + 100)

def no_vig_prob(home_ml, away_ml):
    """Remove bookmaker's juice, return true win probability for home team."""
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    total = ph + pa
    return round(ph / total, 4), round(pa / total, 4)

def rolling_stat(df, team_col, date_col, stat_col, window=10, new_col=None):
    """
    For each game row, compute the rolling mean of `stat_col`
    for that team over the previous `window` games (excludes current game).
    df must be sorted by date ascending.
    """
    new_col = new_col or f"roll_{window}_{stat_col}"
    result = []
    for _, row in df.iterrows():
        team = row[team_col]
        date = row[date_col]
        prior = df[(df[team_col] == team) & (df[date_col] < date)].tail(window)
        result.append(prior[stat_col].mean() if len(prior) > 0 else np.nan)
    df[new_col] = result
    return df

# ── MLB Feature Engineering ───────────────────────────────────────────────────

def build_mlb_features(conn):
    print("[MLB] Loading tables...")
    games    = load_table(conn, "mlb_games")
    odds     = load_table(conn, "historical_odds")
    pitchers = load_table(conn, "mlb_pitcher_stats")
    batting  = load_table(conn, "mlb_team_batting")

    # Only regular season final games with valid scores
    games = games[games["status"] == "Final"].copy()
    games = games.dropna(subset=["home_score", "away_score"])
    games["home_won"] = (games["winner"] == "home").astype(int)
    games["game_date"] = pd.to_datetime(games["game_date"])

    # Keep only games we have odds for
    odds_mlb = odds[odds["sport"] == "mlb"].copy()
    odds_mlb["game_date"] = pd.to_datetime(odds_mlb["game_date"])

    print(f"  {len(games):,} MLB games  |  {len(odds_mlb):,} odds rows")

    # ── Join odds ──
    df = games.merge(
        odds_mlb[["game_date","home_team","away_team",
                  "home_ml","away_ml","open_home_ml","open_away_ml",
                  "total_line","open_total","spread"]],
        on=["game_date","home_team","away_team"],
        how="inner",
    )
    print(f"  After odds join: {len(df):,} games")

    # ── Odds-derived features ──
    df["home_ml"]      = pd.to_numeric(df["home_ml"],      errors="coerce")
    df["away_ml"]      = pd.to_numeric(df["away_ml"],      errors="coerce")
    df["open_home_ml"] = pd.to_numeric(df["open_home_ml"], errors="coerce")
    df["open_away_ml"] = pd.to_numeric(df["open_away_ml"], errors="coerce")
    df = df.dropna(subset=["home_ml","away_ml"])

    probs = df.apply(lambda r: no_vig_prob(r["home_ml"], r["away_ml"]), axis=1)
    df["true_home_prob"] = [p[0] for p in probs]
    df["true_away_prob"] = [p[1] for p in probs]

    # Line movement: positive = home ML moved up (became less favored)
    df["ml_move_home"]  = df["home_ml"]    - df["open_home_ml"]
    df["total_move"]    = df["total_line"] - df["open_total"]

    # Was a large move? Sharp money signal
    df["sharp_home"] = ((df["ml_move_home"] > 10) & (df["home_ml"] < 0)).astype(int)
    df["sharp_away"] = ((df["ml_move_home"] < -10) & (df["away_ml"] < 0)).astype(int)

    # ── Park factor ──
    df["park_factor"] = df["home_team"].map(PARK_FACTORS).fillna(1.0)

    # ── Rolling 10-game win rate and run differential ──
    print("  Computing rolling stats...")
    # Build per-game records for both home and away teams
    home_records = games[["game_date","home_team","home_score","away_score","home_won"]].copy()
    home_records.columns = ["game_date","team","scored","allowed","won"]
    away_records = games[["game_date","away_team","away_score","home_score","home_won"]].copy()
    away_records["won"] = 1 - away_records["home_won"]
    away_records = away_records[["game_date","away_team","away_score","home_score","won"]]
    away_records.columns = ["game_date","team","scored","allowed","won"]

    all_records = pd.concat([home_records, away_records]).sort_values("game_date")
    all_records["margin"] = all_records["scored"] - all_records["allowed"]

    def get_team_rolling(team, before_date, window=10):
        prior = all_records[
            (all_records["team"] == team) &
            (all_records["game_date"] < before_date)
        ].tail(window)
        if len(prior) == 0:
            return np.nan, np.nan, np.nan
        return (
            round(prior["won"].mean(), 4),       # win rate
            round(prior["margin"].mean(), 3),    # avg run diff
            len(prior),                          # games available
        )

    home_rolls, away_rolls = [], []
    for _, row in df.iterrows():
        hw, hm, _ = get_team_rolling(row["home_team"], row["game_date"])
        aw, am, _ = get_team_rolling(row["away_team"], row["game_date"])
        home_rolls.append((hw, hm))
        away_rolls.append((aw, am))

    df["home_win_rate_l10"] = [r[0] for r in home_rolls]
    df["home_run_diff_l10"] = [r[1] for r in home_rolls]
    df["away_win_rate_l10"] = [r[0] for r in away_rolls]
    df["away_run_diff_l10"] = [r[1] for r in away_rolls]
    df["win_rate_diff"]     = df["home_win_rate_l10"] - df["away_win_rate_l10"]
    df["run_diff_diff"]     = df["home_run_diff_l10"] - df["away_run_diff_l10"]

    # ── Pitcher stats (if populated) ──
    if len(pitchers) > 0:
        print(f"  Joining {len(pitchers):,} pitcher stat rows...")
        # FanGraphs names should match MLB Stats API names in most cases
        for side, starter_col in [("home", "home_starter"), ("away", "away_starter")]:
            p = pitchers[["season","player_name","xfip","fip","k_pct","bb_pct","era"]].copy()
            p.columns = ["season"] + [f"{side}_sp_{c}" for c in ["player","xfip","fip","k_pct","bb_pct","era"]]
            df = df.merge(
                p.rename(columns={f"{side}_sp_player": starter_col}),
                on=["season", starter_col],
                how="left",
            )
        df["xfip_diff"] = df["home_sp_xfip"] - df["away_sp_xfip"]
    else:
        print("  ⚠ Pitcher stats not yet collected — run 1_collect_historical.py")
        for col in ["home_sp_xfip","away_sp_xfip","home_sp_fip","away_sp_fip",
                    "home_sp_k_pct","away_sp_k_pct","home_sp_era","away_sp_era","xfip_diff"]:
            df[col] = np.nan

    # ── Team batting stats (if populated) ──
    if len(batting) > 0:
        print(f"  Joining {len(batting):,} team batting rows...")
        batting["full_name"] = batting["team"].map(FANGRAPHS_TEAM)
        for side, team_col in [("home","home_team"),("away","away_team")]:
            b = batting[["season","full_name","wrc_plus","obp","iso"]].copy()
            b.columns = ["season", team_col, f"{side}_wrc_plus", f"{side}_obp", f"{side}_iso"]
            df = df.merge(b, on=["season", team_col], how="left")
        df["wrc_diff"] = df["home_wrc_plus"] - df["away_wrc_plus"]
    else:
        print("  ⚠ Team batting stats not yet collected — run 1_collect_historical.py")
        for col in ["home_wrc_plus","away_wrc_plus","home_obp","away_obp",
                    "home_iso","away_iso","wrc_diff"]:
            df[col] = np.nan

    # ── Final column selection and ordering ──
    meta_cols = ["game_id","game_date","season","home_team","away_team",
                 "home_starter","away_starter","home_score","away_score","home_won"]
    odds_cols = ["home_ml","away_ml","open_home_ml","open_away_ml",
                 "total_line","open_total","spread"]
    feature_cols = [
        "true_home_prob", "true_away_prob",
        "ml_move_home", "total_move",
        "sharp_home", "sharp_away",
        "park_factor",
        "home_win_rate_l10", "away_win_rate_l10", "win_rate_diff",
        "home_run_diff_l10", "away_run_diff_l10", "run_diff_diff",
        "home_sp_xfip", "away_sp_xfip", "xfip_diff",
        "home_sp_era", "away_sp_era",
        "home_sp_k_pct", "away_sp_k_pct",
        "home_wrc_plus", "away_wrc_plus", "wrc_diff",
        "home_obp", "away_obp",
    ]

    all_cols = [c for c in meta_cols + odds_cols + feature_cols if c in df.columns]
    df = df[all_cols].sort_values("game_date").reset_index(drop=True)

    # Report fill rate
    feat_available = [c for c in feature_cols if c in df.columns and df[c].notna().mean() > 0.1]
    print(f"  Features with >10% data: {len(feat_available)}/{len(feature_cols)}")

    return df, [c for c in feature_cols if c in df.columns]

# ── NBA Feature Engineering ───────────────────────────────────────────────────

def build_nba_features(conn):
    print("\n[NBA] Loading tables...")
    raw_games = load_table(conn, "nba_games")
    advanced  = load_table(conn, "nba_team_advanced")
    odds      = load_table(conn, "historical_odds")

    # Build one row per game: home team perspective
    home = raw_games[raw_games["is_home"] == 1].copy()
    away = raw_games[raw_games["is_home"] == 0].copy()

    games = home.merge(
        away[["game_id","team_name","team_id","pts","opp_pts","winner"]],
        on="game_id",
        suffixes=("_home", "_away"),
    )
    games = games.rename(columns={
        "team_name_home": "home_team", "team_id_home": "home_team_id",
        "team_name_away": "away_team", "team_id_away": "away_team_id",
        "pts_home": "home_score",     "opp_pts_home": "away_score",
        "winner_home": "home_won",
    })
    games["game_date"] = pd.to_datetime(games["game_date"])
    games = games.sort_values("game_date").reset_index(drop=True)

    odds_nba = odds[odds["sport"] == "nba"].copy()
    odds_nba["game_date"] = pd.to_datetime(odds_nba["game_date"])

    print(f"  {len(games):,} NBA games  |  {len(odds_nba):,} odds rows")

    # ── Join odds ──
    # Team names between SBR (odds) and NBA API may not match perfectly.
    # Use fuzzy date + team matching.
    df = games.merge(
        odds_nba[["game_date","home_team","away_team",
                  "home_ml","away_ml","open_home_ml","open_away_ml",
                  "total_line","open_total","spread"]],
        on=["game_date","home_team","away_team"],
        how="left",  # left join — keep games even if odds not found
    )
    odds_matched = df["home_ml"].notna().sum()
    print(f"  Odds matched: {odds_matched:,} / {len(df):,} games ({100*odds_matched//len(df)}%)")

    # ── Odds-derived features ──
    df["home_ml"]      = pd.to_numeric(df["home_ml"],      errors="coerce")
    df["away_ml"]      = pd.to_numeric(df["away_ml"],      errors="coerce")
    df["open_home_ml"] = pd.to_numeric(df["open_home_ml"], errors="coerce")
    df["open_away_ml"] = pd.to_numeric(df["open_away_ml"], errors="coerce")

    mask = df["home_ml"].notna() & df["away_ml"].notna()
    df.loc[mask, "true_home_prob"] = df[mask].apply(
        lambda r: no_vig_prob(r["home_ml"], r["away_ml"])[0], axis=1
    )
    df.loc[mask, "true_away_prob"] = df[mask].apply(
        lambda r: no_vig_prob(r["home_ml"], r["away_ml"])[1], axis=1
    )
    df["ml_move_home"] = df["home_ml"]    - df["open_home_ml"]
    df["total_move"]   = df["total_line"] - df["open_total"]
    df["spread_move"]  = df["spread"]     - df["open_spread"] if "open_spread" in df.columns else np.nan

    df["sharp_home"] = ((df["ml_move_home"] > 5) & (df["home_ml"] < 0)).astype(float)
    df["sharp_away"] = ((df["ml_move_home"] < -5) & (df["away_ml"] < 0)).astype(float)

    # ── Season-level advanced ratings ──
    print("  Joining advanced ratings...")
    adv = advanced[["team_id","season","off_rating","def_rating","net_rating","pace"]].copy()

    for side, id_col in [("home","home_team_id"), ("away","away_team_id")]:
        a = adv.rename(columns={
            "team_id":    id_col,
            "off_rating": f"{side}_off_rating",
            "def_rating": f"{side}_def_rating",
            "net_rating": f"{side}_net_rating",
            "pace":       f"{side}_pace",
        })
        df = df.merge(a, on=["season", id_col], how="left")

    df["net_rating_diff"] = df["home_net_rating"] - df["away_net_rating"]
    df["off_rating_diff"] = df["home_off_rating"] - df["away_off_rating"]
    df["def_rating_diff"] = df["home_def_rating"] - df["away_def_rating"]
    df["pace_diff"]       = (df["home_pace"] - df["away_pace"]).abs()

    # ── Rest days ──
    print("  Computing rest days...")
    # Build last-game-date lookup from the raw game log
    raw_games["game_date"] = pd.to_datetime(raw_games["game_date"])
    raw_sorted = raw_games.sort_values("game_date")

    def get_rest(team_id, before_date):
        prior = raw_sorted[
            (raw_sorted["team_id"] == team_id) &
            (raw_sorted["game_date"] < before_date)
        ]
        if len(prior) == 0:
            return 3  # default to normal rest if no prior game found
        last_game = prior["game_date"].max()
        return (before_date - last_game).days

    df["home_rest_days"] = df.apply(
        lambda r: get_rest(r["home_team_id"], r["game_date"]), axis=1
    )
    df["away_rest_days"] = df.apply(
        lambda r: get_rest(r["away_team_id"], r["game_date"]), axis=1
    )
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]
    df["home_b2b"]  = (df["home_rest_days"] <= 1).astype(int)
    df["away_b2b"]  = (df["away_rest_days"] <= 1).astype(int)
    df["b2b_diff"]  = df["away_b2b"] - df["home_b2b"]  # positive = away has disadvantage

    # ── Rolling 10-game scoring margin ──
    print("  Computing rolling scoring margins...")
    raw_sorted["margin"] = raw_sorted["pts"] - raw_sorted["opp_pts"]

    def get_rolling(team_id, before_date, window=10):
        prior = raw_sorted[
            (raw_sorted["team_id"] == team_id) &
            (raw_sorted["game_date"] < before_date)
        ].tail(window)
        if len(prior) == 0:
            return np.nan, np.nan
        return (
            round(prior["winner"].mean(), 4),
            round(prior["margin"].mean(), 3),
        )

    home_rolls, away_rolls = [], []
    for _, row in df.iterrows():
        hw, hm = get_rolling(row["home_team_id"], row["game_date"])
        aw, am = get_rolling(row["away_team_id"], row["game_date"])
        home_rolls.append((hw, hm))
        away_rolls.append((aw, am))

    df["home_win_rate_l10"]  = [r[0] for r in home_rolls]
    df["home_margin_l10"]    = [r[1] for r in home_rolls]
    df["away_win_rate_l10"]  = [r[0] for r in away_rolls]
    df["away_margin_l10"]    = [r[1] for r in away_rolls]
    df["win_rate_diff"]      = df["home_win_rate_l10"] - df["away_win_rate_l10"]
    df["margin_diff"]        = df["home_margin_l10"]   - df["away_margin_l10"]

    # ── Final column selection ──
    meta_cols = ["game_id","game_date","season","home_team","away_team",
                 "home_team_id","away_team_id","home_score","away_score","home_won"]
    odds_cols = ["home_ml","away_ml","open_home_ml","open_away_ml",
                 "total_line","open_total","spread"]
    feature_cols = [
        "true_home_prob", "true_away_prob",
        "ml_move_home", "total_move", "spread_move",
        "sharp_home", "sharp_away",
        "home_off_rating", "away_off_rating", "off_rating_diff",
        "home_def_rating", "away_def_rating", "def_rating_diff",
        "home_net_rating", "away_net_rating", "net_rating_diff",
        "home_pace", "away_pace", "pace_diff",
        "home_rest_days", "away_rest_days", "rest_diff",
        "home_b2b", "away_b2b", "b2b_diff",
        "home_win_rate_l10", "away_win_rate_l10", "win_rate_diff",
        "home_margin_l10",  "away_margin_l10",  "margin_diff",
    ]

    all_cols = [c for c in meta_cols + odds_cols + feature_cols if c in df.columns]
    df = df[all_cols].sort_values("game_date").reset_index(drop=True)

    feat_available = [c for c in feature_cols if c in df.columns and df[c].notna().mean() > 0.1]
    print(f"  Features with >10% data: {len(feat_available)}/{len(feature_cols)}")

    return df, [c for c in feature_cols if c in df.columns]

# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(mlb_df, mlb_feats, nba_df, nba_feats):
    print("\n" + "━"*55)
    print("FEATURE MATRIX SUMMARY")
    print("━"*55)

    for sport, df, feats in [("MLB", mlb_df, mlb_feats), ("NBA", nba_df, nba_feats)]:
        games_with_odds = df["home_ml"].notna().sum() if "home_ml" in df.columns else 0
        home_win_rate   = df["home_won"].mean() if "home_won" in df.columns else 0
        print(f"\n  {sport}")
        print(f"    Total games:       {len(df):,}")
        print(f"    Games with odds:   {games_with_odds:,}")
        print(f"    Home win rate:     {home_win_rate:.1%}  (expect ~52-54%)")
        print(f"    Date range:        {df['game_date'].min().date()} → {df['game_date'].max().date()}")
        print(f"    Features ready:    {len(feats)}")
        missing = [c for c in feats if df[c].isna().mean() > 0.5]
        if missing:
            print(f"    ⚠ >50% missing:   {missing}")

    print("\n  Files saved:")
    print(f"    features_mlb.csv    ({len(mlb_df):,} rows)")
    print(f"    features_nba.csv    ({len(nba_df):,} rows)")
    print(f"    feature_cols.json")
    print("\n  Next step: run 4_train_model.py")
    print("━"*55)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("━"*55)
    print("FEATURE ENGINEERING")
    print(f"  Database: {DB_PATH}")
    print("━"*55)

    if not os.path.exists(DB_PATH):
        print(f"\n[ERROR] Database not found: {DB_PATH}")
        print("  Run 1_collect_historical.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        mlb_df, mlb_feats = build_mlb_features(conn)
        nba_df, nba_feats = build_nba_features(conn)
    finally:
        conn.close()

    # Save outputs
    mlb_path  = os.path.join(OUT_DIR, "features_mlb.csv")
    nba_path  = os.path.join(OUT_DIR, "features_nba.csv")
    feat_path = os.path.join(OUT_DIR, "feature_cols.json")

    mlb_df.to_csv(mlb_path, index=False)
    nba_df.to_csv(nba_path, index=False)

    # Only include features that have actual data (>50% non-null)
    mlb_model_feats = [c for c in mlb_feats if mlb_df[c].notna().mean() > 0.5]
    nba_model_feats = [c for c in nba_feats if nba_df[c].notna().mean() > 0.5]

    with open(feat_path, "w") as f:
        json.dump({"mlb": mlb_model_feats, "nba": nba_model_feats}, f, indent=2)

    print_summary(mlb_df, mlb_feats, nba_df, nba_feats)

if __name__ == "__main__":
    main()
