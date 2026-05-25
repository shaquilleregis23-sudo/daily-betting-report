#!/usr/bin/env python3
"""
Script 4: 4_train_model.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Trains an XGBoost + Logistic Regression ensemble to predict home team
win probability, then backtests a betting strategy against historical odds.

Key design principle:
  TRAINING features: opening line + stats (before sharp money acts)
  EDGE calculation:  model_prob vs closing line (after sharp money)
  BET when:         |model_prob - market_close_prob| > EDGE_THRESHOLD

Walk-forward validation:
  MLB  → Time-split within 2021 season (train on first 70%, test on last 30%)
  NBA  → Season-split: train on 2021-22, test on 2022-23

Outputs:
  models/mlb_model.pkl
  models/nba_model.pkl
  Printed backtest report (ROI, win rate, CLV, drawdown)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import warnings
import joblib
import numpy as np
import pandas as pd

from sklearn.linear_model    import LogisticRegression
from sklearn.preprocessing   import StandardScaler
from sklearn.metrics         import log_loss, brier_score_loss
import xgboost as xgb

warnings.filterwarnings("ignore")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DATA_DIR  = os.path.dirname(os.path.abspath(__file__))

# ── Betting config ─────────────────────────────────────────────────────────────
EDGE_THRESHOLD = 0.03    # Minimum edge to place a bet (3 percentage points)
MIN_BET_ODDS   = -250    # Never bet a team heavier than -250 (too much juice)
BET_SIZE       = 1.0     # Flat 1 unit per bet

# ── Helpers ───────────────────────────────────────────────────────────────────

def american_to_prob(ml):
    ml = float(ml)
    return 100 / (ml + 100) if ml >= 0 else abs(ml) / (abs(ml) + 100)

def no_vig_prob(home_ml, away_ml):
    ph, pa = american_to_prob(home_ml), american_to_prob(away_ml)
    t = ph + pa
    return ph / t, pa / t

def payout(ml):
    """Profit per 1-unit bet if correct."""
    ml = float(ml)
    return ml / 100 if ml >= 0 else 100 / abs(ml)

def max_drawdown(equity_curve):
    """Max peak-to-trough drop in units."""
    peak = equity_curve[0]
    dd   = 0.0
    for val in equity_curve:
        peak = max(peak, val)
        dd   = max(dd, peak - val)
    return round(dd, 2)

def sharpe(returns):
    """Annualized Sharpe ratio (assumes ~500 bets/yr for sports)."""
    if len(returns) < 2 or np.std(returns) == 0:
        return 0.0
    return round(np.mean(returns) / np.std(returns) * np.sqrt(500), 2)

# ── Feature prep ──────────────────────────────────────────────────────────────

def prep_features(df, feature_cols):
    """
    Add open-line market probability as a training feature.
    Remove closing-line probs (true_home_prob) from training to avoid
    training the model to copy the market.
    Returns X (features), y (target), close_prob (for edge calc).
    """
    df = df.copy()

    # Compute opening-line no-vig probability (before sharp action)
    mask = df["open_home_ml"].notna() & df["open_away_ml"].notna()
    df.loc[mask, "open_home_prob"] = df[mask].apply(
        lambda r: no_vig_prob(r["open_home_ml"], r["open_away_ml"])[0], axis=1
    )
    df["open_home_prob"] = df["open_home_prob"].fillna(0.5)

    # Closing-line no-vig prob (edge target — NOT a training feature)
    df.loc[mask, "close_home_prob"] = df[mask].apply(
        lambda r: no_vig_prob(r["home_ml"], r["away_ml"])[0], axis=1
    )

    # Build training feature list: replace closing probs with open prob
    train_feats = [c for c in feature_cols
                   if c not in ("true_home_prob", "true_away_prob")]
    if "open_home_prob" not in train_feats:
        train_feats = ["open_home_prob"] + train_feats

    # Keep only rows where at least 60% of features are non-null
    X = df[train_feats].copy()
    row_fill = X.notna().mean(axis=1)
    valid = row_fill >= 0.6
    df = df[valid].reset_index(drop=True)
    X  = X[valid].reset_index(drop=True)

    # Fill remaining NaNs with column medians
    X = X.fillna(X.median())

    y           = df["home_won"].values
    close_prob  = df["close_home_prob"].values
    home_ml     = df["home_ml"].values
    away_ml     = df["away_ml"].values
    game_dates  = df["game_date"].values if "game_date" in df.columns else None

    return X, y, close_prob, home_ml, away_ml, game_dates, train_feats

# ── Model training ────────────────────────────────────────────────────────────

def train_ensemble(X_train, y_train, n_feats):
    """
    Train XGBoost + Logistic Regression ensemble.
    Conservative hyperparameters to avoid overfitting on small datasets.
    """
    # XGBoost: shallow trees, high min_child_weight → prevents overfitting
    xgb_model = xgb.XGBClassifier(
        n_estimators      = 200,
        max_depth         = 3,
        learning_rate     = 0.05,
        subsample         = 0.8,
        colsample_bytree  = 0.8,
        min_child_weight  = max(10, len(y_train) // 50),
        eval_metric       = "logloss",
        use_label_encoder = False,
        random_state      = 42,
        verbosity         = 0,
    )
    xgb_model.fit(X_train, y_train)

    # Logistic Regression: strong regularization (C=0.1)
    scaler   = StandardScaler()
    X_scaled = scaler.fit_transform(X_train)
    lr_model = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
    lr_model.fit(X_scaled, y_train)

    return xgb_model, lr_model, scaler

def predict_ensemble(xgb_model, lr_model, scaler, X_test):
    """Weighted ensemble: 65% XGBoost, 35% Logistic Regression."""
    prob_xgb = xgb_model.predict_proba(X_test)[:, 1]
    prob_lr  = lr_model.predict_proba(scaler.transform(X_test))[:, 1]
    return 0.65 * prob_xgb + 0.35 * prob_lr

# ── Betting simulation ────────────────────────────────────────────────────────

def simulate_bets(model_prob, close_prob, y_true, home_ml, away_ml):
    """
    For each game, decide whether to bet home or away based on edge.
    Edge = model_prob - market_close_prob.
    Only bet when edge > EDGE_THRESHOLD and odds aren't too juiced.
    Returns a results DataFrame.
    """
    records = []
    for i in range(len(model_prob)):
        mp   = model_prob[i]
        cp   = close_prob[i]
        hml  = home_ml[i]
        aml  = away_ml[i]
        won  = int(y_true[i])

        if np.isnan(cp) or np.isnan(hml) or np.isnan(aml):
            continue

        edge_home = mp - cp        # positive = we like home more than market
        edge_away = (1 - mp) - (1 - cp)  # = -(edge_home)

        bet_side  = None
        bet_odds  = None
        bet_edge  = None

        if edge_home > EDGE_THRESHOLD and hml >= MIN_BET_ODDS:
            bet_side = "home"
            bet_odds = hml
            bet_edge = edge_home
        elif edge_away > EDGE_THRESHOLD and aml >= MIN_BET_ODDS:
            bet_side = "away"
            bet_odds = aml
            bet_edge = edge_away

        if bet_side is None:
            continue

        # Did we win?
        bet_won = (bet_side == "home" and won == 1) or \
                  (bet_side == "away" and won == 0)

        profit = payout(bet_odds) * BET_SIZE if bet_won else -BET_SIZE

        records.append({
            "bet_side":  bet_side,
            "bet_odds":  bet_odds,
            "edge":      round(bet_edge, 4),
            "model_prob": round(mp, 4),
            "market_prob": round(cp, 4),
            "bet_won":   int(bet_won),
            "profit":    round(profit, 4),
        })

    return pd.DataFrame(records)

# ── Metrics report ────────────────────────────────────────────────────────────

def print_metrics(bets, sport, split_label, model_prob_all, y_all, close_prob_all):
    if len(bets) == 0:
        print(f"  No bets placed (edge threshold {EDGE_THRESHOLD:.0%} too tight?)")
        return

    total_bets  = len(bets)
    wins        = bets["bet_won"].sum()
    win_rate    = wins / total_bets
    total_stake = total_bets * BET_SIZE
    total_profit= bets["profit"].sum()
    roi         = total_profit / total_stake * 100

    # Equity curve and drawdown
    equity = np.cumsum(bets["profit"].values)
    equity = np.insert(equity, 0, 0.0)
    dd     = max_drawdown(equity)
    sr     = sharpe(bets["profit"].values)

    # Closing line value: did our bets beat the closing line?
    # CLV > 0 means we bet at better prices than where the line closed
    clv_vals = []
    for _, row in bets.iterrows():
        if row["bet_side"] == "home":
            clv_vals.append(row["model_prob"] - row["market_prob"])
        else:
            clv_vals.append((1 - row["model_prob"]) - (1 - row["market_prob"]))
    avg_clv = np.mean(clv_vals) * 100  # in percentage points

    # Model calibration (on ALL test games)
    valid = ~np.isnan(close_prob_all)
    if valid.sum() > 10:
        ll  = log_loss(y_all[valid], model_prob_all[valid])
        bs  = brier_score_loss(y_all[valid], model_prob_all[valid])
    else:
        ll = bs = float("nan")

    # Odds distribution
    home_bets = bets[bets["bet_side"] == "home"]
    away_bets = bets[bets["bet_side"] == "away"]

    print(f"\n  {'─'*48}")
    print(f"  {sport} — {split_label}")
    print(f"  {'─'*48}")
    print(f"  Bets placed:    {total_bets:>6}  "
          f"(home: {len(home_bets)}, away: {len(away_bets)})")
    print(f"  Win rate:       {win_rate:>6.1%}  "
          f"(break-even ≈ 52.4% on -110 odds)")
    print(f"  Total profit:   {total_profit:>+6.1f} units")
    print(f"  ROI:            {roi:>+6.1f}%")
    print(f"  Avg edge/bet:   {np.mean(clv_vals)*100:>+5.1f} pp")
    print(f"  Max drawdown:   {dd:>6.1f} units")
    print(f"  Sharpe ratio:   {sr:>6.2f}")
    print(f"  Log-loss:       {ll:>6.4f}  (market baseline ≈ 0.68)")
    print(f"  Brier score:    {bs:>6.4f}  (lower = better calibrated)")

    if roi > 0:
        print(f"\n  ✅ Positive ROI on test set ({roi:+.1f}%)")
    else:
        print(f"\n  ⚠  Negative ROI ({roi:+.1f}%) — refine features or threshold")

# ── Feature importance ────────────────────────────────────────────────────────

def print_importance(xgb_model, feature_names, top_n=8):
    imp = dict(zip(feature_names, xgb_model.feature_importances_))
    sorted_imp = sorted(imp.items(), key=lambda x: x[1], reverse=True)[:top_n]
    print(f"\n  Top {top_n} features (XGBoost importance):")
    for feat, score in sorted_imp:
        bar = "█" * int(score * 200)
        print(f"    {feat:<30} {score:.4f}  {bar}")

# ── Per-sport pipeline ────────────────────────────────────────────────────────

def run_sport(sport, csv_name, feature_cols, split_strategy):
    """
    Full train → backtest pipeline for one sport.
    split_strategy: 'time_split' (single season) or 'season_split' (multi-season)
    """
    print(f"\n{'━'*52}")
    print(f" {sport.upper()} MODEL")
    print(f"{'━'*52}")

    path = os.path.join(DATA_DIR, csv_name)
    df   = pd.read_csv(path)
    df["game_date"] = pd.to_datetime(df["game_date"])

    # Only keep rows with odds (can't backtest without knowing the market line)
    df = df[df["home_ml"].notna()].copy()
    print(f" {len(df):,} games with odds loaded")

    X, y, close_prob, home_ml, away_ml, dates, train_feats = prep_features(df, feature_cols)
    print(f" {len(X):,} games after feature filtering")
    print(f" Training features ({len(train_feats)}): {train_feats}")

    # ── Train / test split ──
    if split_strategy == "season_split" and "season" in df.columns:
        # For NBA: train on 2021-22, test on 2022-23
        df_valid = df[df["home_ml"].notna()].reset_index(drop=True)
        # Align indices to X (which may have dropped rows)
        seasons = df_valid["season"].values[:len(X)]
        unique  = sorted(df_valid["season"].unique())
        if len(unique) >= 2:
            train_seasons = unique[:-1]
            test_season   = unique[-1]
            train_mask = np.isin(seasons, train_seasons)
            test_mask  = np.isin(seasons, [test_season])
            split_label = f"train={','.join(train_seasons)}  test={test_season}"
        else:
            # Fallback to time split
            split_strategy = "time_split"

    if split_strategy == "time_split":
        # 70/30 chronological split
        n       = len(X)
        cutoff  = int(n * 0.70)
        train_mask = np.zeros(n, dtype=bool)
        test_mask  = np.zeros(n, dtype=bool)
        train_mask[:cutoff] = True
        test_mask[cutoff:]  = True
        split_label = "first 70% → last 30% (chronological)"

    X_train, y_train = X[train_mask], y[train_mask]
    X_test,  y_test  = X[test_mask],  y[test_mask]
    cp_test  = close_prob[test_mask]
    hml_test = home_ml[test_mask]
    aml_test = away_ml[test_mask]

    print(f" Split: {split_label}")
    print(f" Train: {train_mask.sum():,} games  |  Test: {test_mask.sum():,} games")

    if train_mask.sum() < 50 or test_mask.sum() < 20:
        print(" ⚠ Not enough data for reliable training. Collect more seasons.")
        return None, None, None

    # ── Train ──
    print(" Training ensemble (XGBoost + Logistic Regression)...")
    xgb_model, lr_model, scaler = train_ensemble(X_train, y_train, len(train_feats))

    # ── Predict ──
    model_prob_train = predict_ensemble(xgb_model, lr_model, scaler, X_train)
    model_prob_test  = predict_ensemble(xgb_model, lr_model, scaler, X_test)

    train_ll = log_loss(y_train, model_prob_train)
    test_ll  = log_loss(y_test,  model_prob_test)
    print(f" Log-loss — train: {train_ll:.4f}  test: {test_ll:.4f}  "
          f"({'overfit' if test_ll > train_ll * 1.15 else 'OK'})")

    # ── Backtest ──
    bets = simulate_bets(model_prob_test, cp_test, y_test, hml_test, aml_test)
    print_metrics(bets, sport, split_label, model_prob_test, y_test, cp_test)
    print_importance(xgb_model, train_feats)

    # ── Calibration check ──
    # Does model probability match actual win rate in buckets?
    print(f"\n  Calibration (model prob bucket → actual win rate):")
    probs_df = pd.DataFrame({
        "model_prob": model_prob_test,
        "won": y_test,
        "has_close": ~np.isnan(cp_test),
    })
    probs_df = probs_df[probs_df["has_close"]]
    probs_df["bucket"] = pd.cut(probs_df["model_prob"],
                                 bins=[0, .45, .50, .55, .60, 1.0],
                                 labels=["<45%","45-50%","50-55%","55-60%",">60%"])
    cal = probs_df.groupby("bucket")["won"].agg(["mean","count"])
    for bucket, row in cal.iterrows():
        bar = "█" * int(row["mean"] * 20)
        print(f"    {bucket:<8} → actual {row['mean']:.1%}  n={int(row['count']):<4}  {bar}")

    # ── Save model ──
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, f"{sport.lower()}_model.pkl")
    joblib.dump({
        "xgb":       xgb_model,
        "lr":        lr_model,
        "scaler":    scaler,
        "features":  train_feats,
        "sport":     sport,
    }, model_path)
    print(f"\n  Model saved → {model_path}")

    return xgb_model, lr_model, scaler

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("━"*52)
    print(" PREDICTION MODEL TRAINER + BACKTESTER")
    print(f" Edge threshold: {EDGE_THRESHOLD:.0%}  |  Bet size: {BET_SIZE} unit")
    print("━"*52)

    feat_path = os.path.join(DATA_DIR, "feature_cols.json")
    if not os.path.exists(feat_path):
        print("[ERROR] feature_cols.json not found. Run 3_engineer_features.py first.")
        return

    with open(feat_path) as f:
        feature_cols = json.load(f)

    # ── MLB ──
    run_sport(
        sport          = "MLB",
        csv_name       = "features_mlb.csv",
        feature_cols   = feature_cols["mlb"],
        split_strategy = "time_split",   # one season only
    )

    # ── NBA ──
    run_sport(
        sport          = "NBA",
        csv_name       = "features_nba.csv",
        feature_cols   = feature_cols["nba"],
        split_strategy = "season_split",  # train 2021-22, test 2022-23
    )

    print("\n" + "━"*52)
    print(" WHAT TO DO NEXT")
    print("━"*52)
    print("""
  1. Run 1_collect_historical.py to completion (adds pitcher + wRC+ stats)
     → Re-run this script — MLB model improves significantly.

  2. When you have 3+ seasons of data, switch MLB to 'season_split'
     for proper out-of-season backtesting.

  3. If ROI is positive on the test set:
     → Run 6_predict_today.py each morning for daily picks.

  4. If ROI is negative:
     → Lower EDGE_THRESHOLD to 0.02 and re-test
     → Or collect more seasons of data
     → Or run 1_collect_historical.py to add pitcher stats
""")

if __name__ == "__main__":
    main()
