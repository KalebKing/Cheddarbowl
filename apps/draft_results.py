"""Post-draft results & analysis dashboard — a SEPARATE Streamlit app.

    streamlit run results.py

Reads the completed data/draft_state.json + projections and shows: league power rankings with
draft grades, biggest steals & reaches vs ADP, a value-vs-pick scatter, and a per-team explorer
(optimal lineup + bench + positional spend). Self-contained (doesn't import app.py, which has
set_page_config side effects) but reuses the strategy engine + roster math so numbers match.
"""
from __future__ import annotations

import os
import sys

# Make the repo root importable so `import cheddarbowl` resolves when Streamlit runs this file
# directly (Streamlit puts only apps/ on sys.path; the package lives one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from cheddarbowl import config
from cheddarbowl.data_loader import load_draft_state, load_teams
from cheddarbowl.draft.engine import Engine
from cheddarbowl.lib.rosters import optimal_lineup, starter_points, team_players

st.set_page_config(page_title="Cheddarbowl Results", page_icon="🧀", layout="wide")

# Neon position palette (shared with the main app).
POS_COLORS = {"QB": "#ff2bd6", "RB": "#00e5ff", "WR": "#39ff14",
              "TE": "#ffb000", "K": "#b26bff", "DST": "#ff3860"}

# Trimmed cyberpunk theme (base only — this app doesn't need the live-board classes).
_THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Press+Start+2P&family=Orbitron:wght@600;800;900&family=Chakra+Petch:wght@400;600;700&display=swap');
:root{ --cb-magenta:#ff2bd6; --cb-cyan:#00e5ff; --cb-green:#39ff14; --cb-bg:#0b0714;
  --cb-bg2:#160b28; --cb-ink:#e8e6ff; --cb-muted:#9a8fc7; }
[data-testid="stAppViewContainer"]{ background:
  radial-gradient(1100px 520px at 50% -12%, rgba(255,43,214,.14), transparent 60%),
  radial-gradient(900px 500px at 100% 108%, rgba(0,229,255,.12), transparent 60%),
  linear-gradient(rgba(0,229,255,.045) 1px, transparent 1px) 0 0/44px 44px,
  linear-gradient(90deg, rgba(255,43,214,.045) 1px, transparent 1px) 0 0/44px 44px, var(--cb-bg); }
[data-testid="stHeader"]{ background:transparent; }
[data-testid="stAppViewContainer"]::before{ content:""; position:fixed; inset:0; pointer-events:none;
  z-index:9999; background:repeating-linear-gradient(180deg, rgba(0,0,0,0) 0 2px, rgba(0,0,0,.10) 2px 3px);
  mix-blend-mode:multiply; opacity:.5; }
html, body, .stMarkdown, p, span, div, td, th, li{ font-family:'Chakra Petch', ui-monospace, monospace; }
h1,h2,h3,h4{ font-family:'Orbitron', sans-serif !important; letter-spacing:.06em;
  color:var(--cb-cyan) !important; text-shadow:0 0 10px rgba(0,229,255,.45); }
h3{ color:var(--cb-magenta) !important; text-shadow:0 0 12px rgba(255,43,214,.5); }
.cb-title{ text-align:center; margin:.1rem 0 .2rem; line-height:1; }
.cb-title .cb-word{ font-family:'Press Start 2P', monospace; font-size:clamp(1.4rem,4.6vw,3rem); letter-spacing:.04em; }
.cb-title .cheddar{ color:var(--cb-magenta); text-shadow:0 0 6px #ff2bd6,0 0 18px #ff2bd6,0 0 42px rgba(255,43,214,.6); }
.cb-title .bowl{ color:var(--cb-cyan); text-shadow:0 0 6px #00e5ff,0 0 18px #00e5ff,0 0 42px rgba(0,229,255,.6); }
.cb-sub{ text-align:center; font-family:'Orbitron',sans-serif; color:var(--cb-muted); letter-spacing:.42em;
  font-size:.72rem; margin:.35rem 0 .2rem; text-transform:uppercase; }
.cb-rule{ height:2px; border:0; margin:.5rem 0 1rem;
  background:linear-gradient(90deg, transparent, var(--cb-magenta), var(--cb-cyan), transparent);
  box-shadow:0 0 12px rgba(255,43,214,.5); }
[data-testid="stMetric"]{ background:linear-gradient(180deg, rgba(22,11,40,.92), rgba(11,7,20,.92));
  border:1px solid rgba(255,43,214,.5); border-radius:10px; padding:10px 14px;
  box-shadow:0 0 14px rgba(255,43,214,.22), inset 0 0 14px rgba(0,229,255,.05); }
[data-testid="stMetricLabel"] *{ color:var(--cb-magenta) !important; text-transform:uppercase;
  letter-spacing:.12em; font-size:.7rem; }
[data-testid="stMetricValue"]{ color:var(--cb-cyan) !important; font-family:'Orbitron',sans-serif;
  text-shadow:0 0 10px rgba(0,229,255,.5); }
[data-testid="stImage"] img{ border-radius:8px; border:1.5px solid rgba(255,43,214,.55);
  box-shadow:0 0 10px rgba(255,43,214,.35); }
[data-baseweb="tab"]{ color:var(--cb-muted) !important; font-family:'Orbitron',sans-serif; }
[aria-selected="true"][data-baseweb="tab"]{ color:var(--cb-cyan) !important; }
[data-baseweb="tab-highlight"]{ background:var(--cb-magenta) !important; box-shadow:0 0 12px var(--cb-magenta); }
</style>
"""

_GRADE_COLOR = {"A": "#39ff14", "B": "#00e5ff", "C": "#ffb000", "D": "#ff3860"}


@st.cache_resource
def get_engine() -> Engine:
    return Engine()


def _grade(rank: int, n: int) -> str:
    """Fun draft grade from finish rank (1 = best) across n teams."""
    pct = 1.0 - (rank - 1) / max(1, n - 1)          # 1.0 best .. 0.0 worst
    for thr, g in ((0.92, "A+"), (0.80, "A"), (0.68, "A-"), (0.56, "B+"), (0.44, "B"),
                   (0.32, "B-"), (0.20, "C+"), (0.08, "C"), (0.0, "C-")):
        if pct >= thr:
            return g
    return "D"


def _mgr(teams: dict, tid: int) -> str:
    t = teams.get(tid, {})
    return t.get("manager") or t.get("team_name") or f"Team {tid}"


def build_analysis():
    """Return (state, board, picks_df, standings_df, teams). picks_df is every pick joined with its
    valuation (proj_pts, vob, adp) + value_vs_adp; standings_df is one row per team, ranked."""
    state = load_draft_state()
    teams = load_teams()
    board = get_engine().players
    look = board.set_index(board["player_id"].astype(str))

    rows = []
    for p in state.get("picks", []):
        pid = str(p["player_id"])
        r = look.loc[pid] if pid in look.index else None
        adp = float(r["adp"]) if (r is not None and pd.notna(r["adp"])) else None
        rows.append({
            "pick": int(p["pick"]), "round": int(p["round"]), "team_id": int(p["team_id"]),
            "name": r["name"] if r is not None else p.get("player", pid),
            "pos": r["pos"] if r is not None else p.get("pos", "?"),
            "nfl_team": (r["team"] if r is not None else p.get("nfl_team", "")) or "",
            "proj_pts": float(r["proj_pts"]) if r is not None else 0.0,
            "vob": float(r["vob"]) if (r is not None and pd.notna(r["vob"])) else 0.0,
            "adp": adp,
        })
    picks_df = pd.DataFrame(rows)
    # value vs ADP: + means they got the player LATER than the market expected (a steal); - a reach.
    picks_df["value_vs_adp"] = picks_df.apply(
        lambda x: (x["adp"] - x["pick"]) if x["adp"] is not None else None, axis=1)
    picks_df["manager"] = picks_df["team_id"].map(lambda t: _mgr(teams, t))

    srows = []
    for tid in sorted(teams):
        tp = team_players(state, tid, board)
        starters, _bench = optimal_lineup(tp)
        sub = picks_df[picks_df["team_id"] == tid]
        srows.append({"team_id": tid, "manager": _mgr(teams, tid),
                      "proj_pts": starter_points(starters),
                      "value": round(float(sub["vob"].sum()), 1),
                      "picks": int(len(sub))})
    sdf = pd.DataFrame(srows).sort_values("proj_pts", ascending=False).reset_index(drop=True)
    sdf["rank"] = range(1, len(sdf) + 1)
    sdf["grade"] = sdf["rank"].map(lambda r: _grade(r, len(sdf)))
    sdf["mine"] = sdf["team_id"] == config.MY_TEAM_ID
    return state, board, picks_df, sdf, teams


def _theme_chart(chart):
    return (chart.configure_view(fill="#0b0714", stroke="#2a1f45")
            .configure_axis(labelColor="#9a8fc7", titleColor="#9a8fc7", gridColor="#241a3d",
                            domainColor="#3a2c5c", tickColor="#3a2c5c", labelFontSize=11, titleFontSize=11)
            .configure_legend(labelColor="#e8e6ff", titleColor="#9a8fc7", labelFontSize=11)
            .configure(background="#0b0714"))


def power_rankings(sdf: pd.DataFrame, teams: dict) -> None:
    import altair as alt
    st.subheader("🏆 Power rankings — best-case projected starter points")
    d = sdf.copy()
    d["label"] = d.apply(lambda r: ("★ " if r["mine"] else "") + r["manager"], axis=1)
    bar = alt.Chart(d).mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X("proj_pts:Q", title="Projected starter points", scale=alt.Scale(zero=False)),
        y=alt.Y("label:N", sort="-x", title=None),
        color=alt.condition("datum.mine", alt.value("#00e5ff"), alt.value("#ff2bd6")),
        tooltip=[alt.Tooltip("manager:N", title="Team"), alt.Tooltip("proj_pts:Q", title="Proj pts"),
                 alt.Tooltip("value:Q", title="Value (ΣVOB)"), alt.Tooltip("grade:N")])
    txt = alt.Chart(d).mark_text(align="left", dx=4, color="#e8e6ff", fontSize=11).encode(
        x="proj_pts:Q", y=alt.Y("label:N", sort="-x"), text=alt.Text("proj_pts:Q", format=".0f"))
    st.altair_chart(_theme_chart((bar + txt).properties(height=34 * len(d) + 20)),
                    use_container_width=True)

    view = pd.DataFrame({
        "Rank": d["rank"], "": d["mine"].map({True: "★", False: ""}), "Team": d["manager"],
        "Proj pts": d["proj_pts"], "Value (ΣVOB)": d["value"], "Grade": d["grade"]})
    st.dataframe(view, hide_index=True, use_container_width=True, column_config={
        "Rank": st.column_config.NumberColumn(width="small"),
        "": st.column_config.TextColumn(width="small"),
        "Proj pts": st.column_config.NumberColumn(format="%.0f"),
        "Value (ΣVOB)": st.column_config.NumberColumn(format="%.0f",
            help="Sum of value-over-baseline across the team's picks — total draft-capital captured."),
        "Grade": st.column_config.TextColumn(width="small")})


def steals_reaches(picks_df: pd.DataFrame) -> None:
    st.subheader("💎 Steals & reaches vs ADP")
    st.caption("Steal = drafted later than market ADP (value fell to them). Reach = drafted earlier "
               "than ADP. (Players without an ADP are excluded.)")
    v = picks_df[picks_df["value_vs_adp"].notna()].copy()

    def _tbl(df, asc):
        df = df.sort_values("value_vs_adp", ascending=asc).head(10)
        out = pd.DataFrame({
            "Player": df["name"], "Pos": df["pos"], "By": df["manager"],
            "Pick": df["pick"].astype(int), "ADP": df["adp"].round(0),
            "Δ": df["value_vs_adp"].round(0)})
        return out

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**🟢 Biggest steals** (fell past ADP)")
        st.dataframe(_tbl(v, asc=False), hide_index=True, use_container_width=True, column_config={
            "Pick": st.column_config.NumberColumn(width="small"),
            "ADP": st.column_config.NumberColumn(format="%.0f", width="small"),
            "Δ": st.column_config.NumberColumn(format="+%.0f", width="small", help="ADP − pick")})
    with c2:
        st.markdown("**🔴 Biggest reaches** (picked ahead of ADP)")
        st.dataframe(_tbl(v, asc=True), hide_index=True, use_container_width=True, column_config={
            "Pick": st.column_config.NumberColumn(width="small"),
            "ADP": st.column_config.NumberColumn(format="%.0f", width="small"),
            "Δ": st.column_config.NumberColumn(format="+%.0f", width="small", help="ADP − pick")})


def value_chart(picks_df: pd.DataFrame) -> None:
    import altair as alt
    st.subheader("📊 Value by pick — where the studs went")
    st.caption("Each dot is a pick: overall pick # (x) vs value over baseline (y). High dots late = "
               "great value; low dots early = pricey. Colored by position.")
    d = picks_df.copy()
    pos_scale = alt.Scale(domain=list(POS_COLORS), range=list(POS_COLORS.values()))
    ch = alt.Chart(d).mark_circle(size=90, opacity=0.85).encode(
        x=alt.X("pick:Q", title="Overall pick #", scale=alt.Scale(zero=False, nice=True)),
        y=alt.Y("vob:Q", title="Value over baseline (VOB)"),
        color=alt.Color("pos:N", scale=pos_scale, legend=alt.Legend(title=None, orient="top")),
        tooltip=[alt.Tooltip("name:N", title="Player"), alt.Tooltip("pos:N"),
                 alt.Tooltip("manager:N", title="By"), alt.Tooltip("pick:Q", title="Pick"),
                 alt.Tooltip("vob:Q", title="VOB", format=".0f"),
                 alt.Tooltip("adp:Q", title="ADP", format=".0f")])
    st.altair_chart(_theme_chart(ch.properties(height=440)), use_container_width=True)


def team_explorer(state: dict, board: pd.DataFrame, picks_df: pd.DataFrame, sdf: pd.DataFrame,
                  teams: dict) -> None:
    st.subheader("🔎 Team explorer")
    tids = list(sdf["team_id"])
    default = config.MY_TEAM_ID if config.MY_TEAM_ID in tids else tids[0]
    tid = st.selectbox("Team", tids, index=tids.index(default),
                       format_func=lambda t: ("★ " if t == config.MY_TEAM_ID else "") + _mgr(teams, t))
    t = teams.get(tid, {})
    row = sdf[sdf["team_id"] == tid].iloc[0]

    hc, mc = st.columns([1, 6], vertical_alignment="center")
    with hc:
        if t.get("avatar_url"):
            st.image(t["avatar_url"], width=64)
    with mc:
        st.markdown(f"### {_mgr(teams, tid)}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Finish", f"#{int(row['rank'])} / {len(sdf)}")
        m2.metric("Grade", row["grade"])
        m3.metric("Proj starter pts", f"{row['proj_pts']:.0f}")
        m4.metric("Value (ΣVOB)", f"{row['value']:.0f}")

    tp = team_players(state, tid, board)
    starters, bench = optimal_lineup(tp)

    def _lineup_view(rows, with_slot):
        recs = []
        for r in rows:
            recs.append({**({"Slot": r["slot"]} if with_slot else {}),
                         "Player": r.get("name") or "—", "Pos": r.get("pos", ""),
                         "Tm": r.get("team", ""), "Proj": round(r.get("proj_pts", 0) or 0)})
        return pd.DataFrame(recs)

    cL, cR = st.columns([1, 1])
    with cL:
        st.markdown("**Optimal starting lineup**")
        st.dataframe(_lineup_view(starters, True), hide_index=True, use_container_width=True,
                     column_config={"Proj": st.column_config.NumberColumn(format="%.0f", width="small")})
        st.caption(f"Starter points: **{starter_points(starters):.0f}**")
    with cR:
        st.markdown("**Bench**")
        if bench:
            st.dataframe(_lineup_view(bench, False), hide_index=True, use_container_width=True,
                         column_config={"Proj": st.column_config.NumberColumn(format="%.0f", width="small")})
        else:
            st.caption("No bench players.")

    sub = picks_df[picks_df["team_id"] == tid]
    counts = sub["pos"].value_counts().to_dict()
    strip = "  ·  ".join(f"<b style='color:{POS_COLORS.get(p, '#888')}'>{p}</b> {counts.get(p, 0)}"
                         for p in config.POSITIONS)
    st.markdown(f"<div style='margin-top:.4rem'>Positional spend: {strip}</div>", unsafe_allow_html=True)

    best = sub[sub["value_vs_adp"].notna()].sort_values("value_vs_adp", ascending=False).head(1)
    if not best.empty:
        b = best.iloc[0]
        st.caption(f"💎 Best value: **{b['name']}** at #{int(b['pick'])} (ADP {b['adp']:.0f}, "
                   f"+{b['value_vs_adp']:.0f})")


def main() -> None:
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
    st.markdown("<div class='cb-title'><span class='cb-word cheddar'>CHEDDAR</span>"
                "<span class='cb-word bowl'>BOWL</span></div>"
                "<div class='cb-sub'>Draft Results &amp; Analysis</div>"
                "<hr class='cb-rule'>", unsafe_allow_html=True)

    state = load_draft_state()
    picks = state.get("picks", [])
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    if not picks:
        st.warning("No draft found yet. Run a draft (the main app writes `data/draft_state.json`), "
                   "then reload this page.")
        return
    if len(picks) < total:
        st.info(f"Showing a partial draft: {len(picks)} of {total} picks recorded. "
                "Analysis will be complete once all picks are in.")

    state, board, picks_df, sdf, teams = build_analysis()

    # Headline row for YOUR team.
    mine = sdf[sdf["team_id"] == config.MY_TEAM_ID]
    if not mine.empty:
        r = mine.iloc[0]
        st.markdown("#### Your draft")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Finish", f"#{int(r['rank'])} / {len(sdf)}")
        c2.metric("Grade", r["grade"])
        c3.metric("Proj starter pts", f"{r['proj_pts']:.0f}")
        c4.metric("Value (ΣVOB)", f"{r['value']:.0f}")

    t1, t2, t3, t4 = st.tabs(["🏆 Power rankings", "💎 Steals & reaches",
                              "📊 Value chart", "🔎 Team explorer"])
    with t1:
        power_rankings(sdf, teams)
    with t2:
        steals_reaches(picks_df)
    with t3:
        value_chart(picks_df)
    with t4:
        team_explorer(state, board, picks_df, sdf, teams)


main()
