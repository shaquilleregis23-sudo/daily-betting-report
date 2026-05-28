#!/usr/bin/env python3
"""
model/app.py — Sports Edge Dashboard
Deploy: streamlit run model/app.py
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sports Edge",
    page_icon="🎯",
    layout="centered",
    initial_sidebar_state="collapsed",
)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PICKS_FILE = os.path.join(BASE_DIR, "picks_today.json")
LOG_FILE   = os.path.join(BASE_DIR, "picks_log.json")
EASTERN    = ZoneInfo("America/New_York")

# ── Minimal dark-friendly CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
.pick-card {
    border-radius: 10px;
    padding: 14px 18px;
    margin: 8px 0 14px 0;
    font-family: sans-serif;
}
.pick-sport { font-size: 0.75em; font-weight: bold; letter-spacing: 1px; opacity: 0.7; }
.pick-game  { font-size: 0.9em; opacity: 0.7; margin: 2px 0; }
.pick-bet   { font-size: 1.25em; font-weight: bold; margin: 6px 0; }
.pick-edge  { font-size: 1.05em; font-weight: bold; }
.pick-probs { font-size: 0.85em; opacity: 0.75; margin-top: 6px; }
</style>
""", unsafe_allow_html=True)

# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_picks():
    if os.path.exists(PICKS_FILE):
        try:
            with open(PICKS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None

def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

# ── Header ─────────────────────────────────────────────────────────────────────
st.title("🎯 Sports Edge")

picks_data = load_picks()
today_date = datetime.now(EASTERN).strftime("%B %d, %Y")

if picks_data:
    gen = picks_data.get("generated_at", "")[:16].replace("T", " ")
    st.caption(f"{picks_data.get('date', today_date)}  ·  Updated {gen} ET")
else:
    st.caption(f"{today_date}  ·  No picks file yet — run 6_predict_today.py")

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["🎯 Today's Picks", "📊 Performance", "📋 Odds Board"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — TODAY'S PICKS
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    if not picks_data:
        st.warning("No picks file found. Run `python3 model/6_predict_today.py` to generate picks.")
        st.stop()

    picks = picks_data.get("picks", [])
    summary = picks_data.get("summary", {})

    if not picks:
        st.info("No edges found today — model agrees with the market on all games.")
    else:
        # ── Summary strip ──────────────────────────────────────────────────────
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Picks", summary.get("total_picks", len(picks)))
        c2.metric("MLB", summary.get("mlb_picks", 0))
        c3.metric("NBA", summary.get("nba_picks", 0))
        top_edge = max(p["edge"] for p in picks) if picks else 0
        c4.metric("Top Edge", f"+{top_edge:.1%}")

        st.divider()

        # ── Pick cards ─────────────────────────────────────────────────────────
        for pick in picks:
            edge  = pick["edge"]
            grade = pick.get("grade", "")

            if edge >= 0.08:
                color = "#ff6b35"
                emoji = "🔥🔥"
            elif edge >= 0.05:
                color = "#4ecca3"
                emoji = "✅"
            else:
                color = "#7ec8e3"
                emoji = "📈"

            grade_badge = ""
            if grade == "A+":
                grade_badge = " &nbsp;<span style='background:#ff6b35;color:#fff;border-radius:4px;padding:1px 7px;font-size:0.78em;font-weight:bold'>A+</span>"
            elif grade == "A":
                grade_badge = " &nbsp;<span style='background:#4ecca3;color:#111;border-radius:4px;padding:1px 7px;font-size:0.78em;font-weight:bold'>A</span>"
            elif grade == "B+":
                grade_badge = " &nbsp;<span style='background:#7ec8e3;color:#111;border-radius:4px;padding:1px 7px;font-size:0.78em;font-weight:bold'>B+</span>"
            elif grade:
                grade_badge = " &nbsp;<span style='background:#888;color:#fff;border-radius:4px;padding:1px 7px;font-size:0.78em;font-weight:bold'>B</span>"

            st.markdown(f"""
<div class="pick-card" style="border-left: 5px solid {color}; background: rgba(255,255,255,0.04)">
  <div class="pick-sport">{pick['sport']}</div>
  <div class="pick-game">{pick['matchup']}</div>
  <div class="pick-bet">{pick['bet']}{grade_badge}</div>
  <div class="pick-edge" style="color:{color}">{emoji}&nbsp; {pick['label']} &nbsp;·&nbsp; Edge: {pick['edge_pct']}</div>
  <div class="pick-probs">
    Model: <strong>{pick['model_p']}</strong>
    &nbsp;|&nbsp;
    Market: <strong>{pick['market_p']}</strong>
  </div>
</div>
""", unsafe_allow_html=True)

            # Confidence meter (bar from 50% baseline to 100%)
            conf = pick["model_prob"]
            bar_val = max(0.0, min(1.0, (conf - 0.5) * 2))
            st.progress(bar_val, text=f"Model confidence: {conf:.1%}")

            # ── L10 breakdown table ────────────────────────────────────────────
            hl = pick.get("home_l10")
            al = pick.get("away_l10")
            if hl and al:
                bet_team = pick["bet_team"]
                rows = [hl, al]
                tbl_data = []
                for s in rows:
                    star = "★" if s["team"] == bet_team else ""
                    rec  = f"{s['w']}-{s['l']}"
                    # Colour code the record cell
                    wr = s["wr"]
                    if wr >= 0.6:
                        rec_disp = f"🟢 {rec}"
                    elif wr <= 0.3:
                        rec_disp = f"🔴 {rec}"
                    else:
                        rec_disp = f"🟡 {rec}"
                    tbl_data.append({
                        "Pick": star,
                        "Team": s["team"],
                        "L10":  rec_disp,
                        "R/G":  s["rspg"],
                        "RA/G": s["rapg"],
                        "Home": s["home"],
                        "Away": s["away"],
                    })
                l10_df = pd.DataFrame(tbl_data)
                st.dataframe(
                    l10_df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Pick": st.column_config.TextColumn("", width="small"),
                        "Team": st.column_config.TextColumn("Team"),
                        "L10":  st.column_config.TextColumn("L10"),
                        "R/G":  st.column_config.NumberColumn("R/G",  format="%.1f"),
                        "RA/G": st.column_config.NumberColumn("RA/G", format="%.1f"),
                        "Home": st.column_config.TextColumn("Home"),
                        "Away": st.column_config.TextColumn("Away"),
                    },
                )
                st.caption("★ = model's pick  ·  🟢 ≥6-4  🟡 5-5  🔴 ≤4-6  ·  2026 season L10")
            st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — PERFORMANCE TRACKER
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    log = load_log()

    if not log:
        st.info("No bet history yet. Picks are logged automatically each time you run 6_predict_today.py.")
    else:
        df = pd.DataFrame(log)

        # Separate resolved vs pending
        resolved = df[df["result"].isin(["W", "L"])].copy()
        pending  = df[~df["result"].isin(["W", "L"])].copy()

        # ── Stats ──────────────────────────────────────────────────────────────
        if len(resolved) > 0:
            wins   = (resolved["result"] == "W").sum()
            losses = (resolved["result"] == "L").sum()
            wr     = wins / len(resolved)

            def pnl(row):
                ml = row["ml"]
                if row["result"] == "W":
                    return 100 / abs(ml) if ml < 0 else ml / 100
                return -1.0

            resolved["pnl"] = resolved.apply(pnl, axis=1)
            net_units = resolved["pnl"].sum()
            roi       = net_units / len(resolved) * 100

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Win Rate",  f"{wr:.1%}",   f"{wins}W–{losses}L")
            c2.metric("ROI",       f"{roi:+.1f}%")
            c3.metric("Net Units", f"{net_units:+.2f}")
            c4.metric("Resolved",  len(resolved))

            # By sport
            st.divider()
            st.subheader("By Sport")
            for sport in sorted(resolved["sport"].unique()):
                sdf = resolved[resolved["sport"] == sport]
                sw  = (sdf["result"] == "W").sum()
                sl  = (sdf["result"] == "L").sum()
                spl = sdf["pnl"].sum()
                swr = sw / len(sdf)
                st.markdown(f"**{sport}** &nbsp; {sw}W–{sl}L &nbsp; ({swr:.1%}) &nbsp; {spl:+.2f} units")

            # History table
            st.divider()
            st.subheader("Bet History")
            show = resolved[["date","sport","matchup","bet_team","ml","edge_pct","result","pnl"]].copy()
            show = show.rename(columns={
                "date": "Date", "sport": "Sport", "matchup": "Game",
                "bet_team": "Bet", "ml": "ML", "edge_pct": "Edge",
                "result": "Result", "pnl": "Units",
            }).sort_values("Date", ascending=False)
            show["Units"] = show["Units"].apply(lambda x: f"{x:+.2f}")
            st.dataframe(show, hide_index=True, use_container_width=True)
        else:
            st.info("No resolved bets yet. Mark your results below.")

        # ── Pending results ────────────────────────────────────────────────────
        if len(pending) > 0:
            st.divider()
            st.subheader(f"Mark Results ({len(pending)} pending)")
            st.caption("Check the Result column, then download and commit picks_log.json to save permanently.")

            # Editable table — user sets result column
            editable = pending[["date","sport","matchup","bet_team","ml","edge_pct","result"]].copy()
            editable["result"] = editable["result"].fillna("")

            edited = st.data_editor(
                editable,
                column_config={
                    "result": st.column_config.SelectboxColumn(
                        "Result",
                        options=["", "W", "L"],
                        required=False,
                    ),
                    "date":      st.column_config.TextColumn("Date", disabled=True),
                    "sport":     st.column_config.TextColumn("Sport", disabled=True),
                    "matchup":   st.column_config.TextColumn("Game", disabled=True),
                    "bet_team":  st.column_config.TextColumn("Bet", disabled=True),
                    "ml":        st.column_config.NumberColumn("ML", disabled=True),
                    "edge_pct":  st.column_config.TextColumn("Edge", disabled=True),
                },
                hide_index=True,
                use_container_width=True,
            )

            # Merge edits back into full log and offer download
            updated_log = log.copy()
            pending_indices = pending.index.tolist()
            for i, (orig_idx, edit_row) in enumerate(zip(pending_indices, edited.itertuples())):
                result_val = edit_row.result if edit_row.result in ("W", "L") else None
                updated_log[orig_idx]["result"] = result_val

            st.download_button(
                label="⬇️ Download updated picks_log.json",
                data=json.dumps(updated_log, indent=2),
                file_name="picks_log.json",
                mime="application/json",
                help="Download, then replace model/picks_log.json in your GitHub repo to save results permanently.",
            )

            st.caption("After downloading: go to your GitHub repo → model/picks_log.json → edit (pencil icon) → paste contents → commit.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — ODDS BOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    if not picks_data:
        st.warning("No data available.")
    else:
        all_games = picks_data.get("all_games", [])

        if not all_games:
            st.info("No games data. Run 2_fetch_odds_espn.py then 6_predict_today.py.")
        else:
            df = pd.DataFrame(all_games)

            # Sport filter
            sports = sorted(df["sport"].unique().tolist())
            sport_filter = st.radio("Sport", ["All"] + sports, horizontal=True)
            if sport_filter != "All":
                df = df[df["sport"] == sport_filter].copy()

            # Derive display columns
            df["Model"] = df["model_prob"].apply(lambda x: f"{x:.1%}")
            df["Market"] = df["market_prob"].apply(lambda x: f"{x:.1%}")

            def edge_display(row):
                e = row["edge"]  # positive = home edge, negative = away edge
                if abs(e) < 0.01:
                    return "—"
                direction = "H" if e > 0 else "A"
                return f"{direction} +{abs(e):.1%}"

            df["Edge"] = df.apply(edge_display, axis=1)
            df["Pick"] = df["has_pick"].apply(lambda x: "⭐" if x else "")
            df["Home ML"] = df["home_ml"].apply(lambda x: f"+{x}" if x >= 0 else str(x))
            df["Away ML"] = df["away_ml"].apply(lambda x: f"+{x}" if x >= 0 else str(x))

            # Sort: picks first, then by abs edge
            df["abs_edge"] = df["edge"].abs()
            df = df.sort_values(["has_pick", "abs_edge"], ascending=[False, False])

            display = df[["Pick", "sport", "matchup", "Away ML", "Home ML", "Model", "Market", "Edge"]].rename(
                columns={"sport": "Sport", "matchup": "Game"}
            )

            st.dataframe(display, hide_index=True, use_container_width=True)

            st.caption(
                "Edge direction: H = home team has edge, A = away team has edge. "
                "⭐ = model generated a pick on this game."
            )
