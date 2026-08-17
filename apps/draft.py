"""Live draft-day dashboard (Streamlit).

Reads data/draft_state.json (written by the scraper poll loop), runs the strategy engine,
and renders recommendations, the best-available board, positional scarcity, and the draft
log. Auto-refreshes every few seconds.

    streamlit run app.py

The scraper runs separately:  python -m cheddarbowl.ingest.legacy.nfl_scraper --poll
For testing without a live draft, flip "Data source" to Simulated in the sidebar.
"""

from __future__ import annotations

import os
import re
import sys

# Make the repo root importable so `import cheddarbowl` resolves when Streamlit runs this file
# directly (Streamlit puts only apps/ on sys.path; the package lives one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from cheddarbowl import config
from cheddarbowl.data_loader import load_draft_state, load_headshots, load_teams
from cheddarbowl.draft.engine import Engine

st.set_page_config(page_title="Cheddarbowl Draft", page_icon="🧀", layout="wide")

# Neon position palette (validated: normal-vision separation + surface contrast pass;
# positions always appear with their text label, so color is reinforcement).
POS_COLORS = {"QB": "#ff2bd6", "RB": "#00e5ff", "WR": "#39ff14",
              "TE": "#ffb000", "K": "#b26bff", "DST": "#ff3860"}

# Retro 80s arcade cyberpunk theme. Fonts load from Google Fonts with local fallbacks
# so it still works offline. Also includes the anti-flicker rules (keep stale content
# opaque during auto-refresh) so periodic reruns don't fade.
_THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Press+Start+2P&family=Orbitron:wght@600;800;900&family=Chakra+Petch:wght@400;600;700&display=swap');

:root{
  --cb-magenta:#ff2bd6; --cb-cyan:#00e5ff; --cb-green:#39ff14; --cb-bg:#0b0714;
  --cb-bg2:#160b28; --cb-ink:#e8e6ff; --cb-muted:#9a8fc7;
}

/* --- background: dark + faint neon grid + corner glows --- */
[data-testid="stAppViewContainer"]{
  background:
    radial-gradient(1100px 520px at 50% -12%, rgba(255,43,214,.14), transparent 60%),
    radial-gradient(900px 500px at 100% 108%, rgba(0,229,255,.12), transparent 60%),
    linear-gradient(rgba(0,229,255,.045) 1px, transparent 1px) 0 0/44px 44px,
    linear-gradient(90deg, rgba(255,43,214,.045) 1px, transparent 1px) 0 0/44px 44px,
    var(--cb-bg);
}
[data-testid="stHeader"]{ background:transparent; }
[data-testid="stStatusWidget"], [data-testid="stAppDeployButton"]{ display:none !important; }

/* faint CRT scanlines */
[data-testid="stAppViewContainer"]::before{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:9999;
  background:repeating-linear-gradient(180deg, rgba(0,0,0,0) 0 2px, rgba(0,0,0,.10) 2px 3px);
  mix-blend-mode:multiply; opacity:.5;
}

/* --- typography --- */
html, body, [data-testid="stAppViewContainer"], .stMarkdown, p, span, div, td, th, li{
  font-family:'Chakra Petch', ui-monospace, monospace;
}
h1,h2,h3,h4{ font-family:'Orbitron', sans-serif !important; letter-spacing:.06em;
  color:var(--cb-cyan) !important; text-shadow:0 0 10px rgba(0,229,255,.45); }
h3{ color:var(--cb-magenta) !important; text-shadow:0 0 12px rgba(255,43,214,.5); }

/* --- Cheddarbowl title --- */
.cb-title{ text-align:center; margin:.1rem 0 .2rem; line-height:1; }
.cb-title .cb-word{ font-family:'Press Start 2P', monospace; font-size:clamp(1.6rem,5.2vw,3.6rem);
  letter-spacing:.04em; }
.cb-title .cheddar{ color:var(--cb-magenta); text-shadow:0 0 6px #ff2bd6,0 0 18px #ff2bd6,0 0 42px rgba(255,43,214,.6); }
.cb-title .bowl{ color:var(--cb-cyan); text-shadow:0 0 6px #00e5ff,0 0 18px #00e5ff,0 0 42px rgba(0,229,255,.6); }
.cb-sub{ text-align:center; font-family:'Orbitron',sans-serif; color:var(--cb-muted);
  letter-spacing:.42em; font-size:.72rem; margin:.35rem 0 .2rem; text-transform:uppercase; }
.cb-rule{ height:2px; border:0; margin:.5rem 0 1rem;
  background:linear-gradient(90deg, transparent, var(--cb-magenta), var(--cb-cyan), transparent);
  box-shadow:0 0 12px rgba(255,43,214,.5); }

/* --- metric tiles --- */
[data-testid="stMetric"]{
  background:linear-gradient(180deg, rgba(22,11,40,.92), rgba(11,7,20,.92));
  border:1px solid rgba(255,43,214,.5); border-radius:10px; padding:10px 14px;
  box-shadow:0 0 14px rgba(255,43,214,.22), inset 0 0 14px rgba(0,229,255,.05);
}
[data-testid="stMetricLabel"] *{ color:var(--cb-magenta) !important; text-transform:uppercase;
  letter-spacing:.12em; font-size:.7rem; }
[data-testid="stMetricValue"]{ color:var(--cb-cyan) !important; font-family:'Orbitron',sans-serif;
  text-shadow:0 0 10px rgba(0,229,255,.5); }

/* --- bordered containers (recommendation cards) --- */
[data-testid="stVerticalBlockBorderWrapper"]{
  border:1px solid rgba(0,229,255,.45) !important; border-radius:12px;
  background:linear-gradient(180deg, rgba(22,11,40,.8), rgba(11,7,20,.9));
  box-shadow:0 0 16px rgba(0,229,255,.16), inset 0 0 22px rgba(255,43,214,.05);
}

/* --- images: neon ring on headshots/avatars --- */
[data-testid="stImage"] img{ border-radius:8px; border:1.5px solid rgba(255,43,214,.55);
  box-shadow:0 0 10px rgba(255,43,214,.35); }

/* --- tabs --- */
[data-baseweb="tab"]{ color:var(--cb-muted) !important; font-family:'Orbitron',sans-serif; }
[aria-selected="true"][data-baseweb="tab"]{ color:var(--cb-cyan) !important; }
[data-baseweb="tab-highlight"]{ background:var(--cb-magenta) !important;
  box-shadow:0 0 12px var(--cb-magenta); }

/* --- sidebar --- */
[data-testid="stSidebar"]{ border-right:1px solid rgba(255,43,214,.35);
  box-shadow:inset -12px 0 24px rgba(255,43,214,.05); }
/* Trim the width, but ONLY while expanded -- clamping it unconditionally froze the collapse
   (Streamlit's collapse just slides the element; if we pin its flow width the main area never
   reclaims the space and a sliver stays). Scope the clamp to expanded and zero it when
   collapsed so the sidebar fully leaves the flow and the page reflows to full width. */
[data-testid="stSidebar"][aria-expanded="true"]{
  width:230px !important; min-width:230px !important; max-width:230px !important; }
[data-testid="stSidebar"][aria-expanded="false"]{
  width:0 !important; min-width:0 !important; max-width:0 !important;
  border-right:0 !important; box-shadow:none !important; overflow:hidden !important; }

/* --- status banners (custom) --- */
.cb-banner{ font-family:'Orbitron',sans-serif; font-weight:800; letter-spacing:.1em;
  padding:.7rem 1rem; border-radius:10px; text-align:center; text-transform:uppercase; }
.cb-yourpick{ color:#001; background:linear-gradient(90deg,var(--cb-magenta),var(--cb-cyan));
  box-shadow:0 0 18px rgba(255,43,214,.6),0 0 30px rgba(0,229,255,.4); animation:cbpulse 1.1s ease-in-out infinite; }
.cb-waiting{ color:var(--cb-cyan); background:rgba(22,11,40,.85);
  border:1px solid rgba(0,229,255,.5); box-shadow:0 0 14px rgba(0,229,255,.25); }
.cb-done{ color:var(--cb-muted); background:rgba(22,11,40,.7); border:1px solid rgba(154,143,199,.4); }
@keyframes cbpulse{ 0%,100%{ filter:brightness(1); } 50%{ filter:brightness(1.25); } }

/* --- draft-flow cards ---
   Streamlit 1.60: st.container(border=True) is a stVerticalBlock WITH a border. We target
   the exact card block via a child-combinator :has() on the hidden flag marker (so only
   the card, not its ancestor blocks, is styled). */
.cb-mine-flag, .cb-onclock-flag, .cb-pulse-flag, .cb-fcard-flag{ display:none; }
/* equal height: let the flex row's align-items:stretch equalize the COLUMNS (leave their
   height auto), then make each card + its inner wrapper chain fill 100% of that height so
   a wrapped name or the last-pick player line just sets the shared height. */
[data-testid="stHorizontalBlock"]:has(.cb-fcard-flag) [data-testid="stColumn"] [data-testid="stVerticalBlock"],
[data-testid="stHorizontalBlock"]:has(.cb-fcard-flag) [data-testid="stColumn"] [data-testid="stLayoutWrapper"]{
  height:100%; }
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-fcard-flag){
  height:100%; min-height:150px; text-align:center; border-radius:12px;
  border:1px solid rgba(0,229,255,.45) !important;
  background:linear-gradient(180deg, rgba(22,11,40,.9), rgba(11,7,20,.94));
  box-shadow:0 0 14px rgba(0,229,255,.14); }
/* my cards (including me on the clock) -> green */
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-mine-flag){
  border-color:var(--cb-green) !important; box-shadow:0 0 20px rgba(57,255,20,.35) !important; }
/* another team on the clock -> magenta */
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-onclock-flag){
  border-color:var(--cb-magenta) !important; box-shadow:0 0 20px rgba(255,43,214,.4) !important; }
/* whoever is on the clock pulses */
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-pulse-flag){
  animation:cbpulse 1.2s ease-in-out infinite; }
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-fcard-flag) [data-testid="stMarkdownContainer"]{
  text-align:center; }
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cb-fcard-flag) [data-testid="stElementContainer"]:has([data-testid="stImage"]){
  align-self:center; margin:0 auto 2px; }
.cb-frole{ font-family:'Orbitron',sans-serif; font-size:.6rem; letter-spacing:.12em;
  text-transform:uppercase; margin-bottom:3px; }
.cb-fteam2{ font-weight:700; color:var(--cb-ink); font-size:.92rem; line-height:1.14;
  /* always reserve 2 lines (cap at 2) so 1-line names align with wrapped ones */
  min-height:2.28em; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
  overflow:hidden; }
.cb-fowner{ color:var(--cb-muted); font-size:.78rem; margin-top:3px; }
.cb-fmeta{ font-family:'Orbitron',sans-serif; font-size:.84rem; margin-top:4px; letter-spacing:.06em; }
.cb-fplayer2{ display:block; margin-top:6px; padding-top:6px; font-size:.8rem; color:var(--cb-ink);
  border-top:1px solid rgba(255,255,255,.08); }
.cb-fplayer2 b{ font-family:'Orbitron',sans-serif; font-size:.7rem; }
/* the » jump between card 3 and 4 */
.cb-jump{ display:flex; align-items:center; justify-content:center; height:100%; min-height:178px;
  color:var(--cb-magenta); font-family:'Press Start 2P',monospace; font-size:1.05rem;
  text-shadow:0 0 14px var(--cb-magenta); }

/* --- dividers --- */
hr{ border:0 !important; height:2px !important;
  background:linear-gradient(90deg, transparent, rgba(255,43,214,.6), rgba(0,229,255,.6), transparent) !important; }

/* --- anti-flicker: keep content opaque during auto-refresh reruns --- */
[data-stale="true"], [data-testid="stElementContainer"][data-stale="true"]{ opacity:1 !important; }
[data-testid="stElementContainer"], .element-container{ transition:none !important; }

/* --- scraper heartbeat indicator (sidebar bottom): a pixel-art light in the theme palette,
   blinking with a hard, stepped (choppy) glow so it reads as a retro arcade LED --- */
@keyframes cb-hb-beat{
  0%,100%{ box-shadow:0 0 0 1px currentColor; opacity:.6; }
  50%    { box-shadow:0 0 0 3px currentColor; opacity:1; }
}
.cb-hb{ position:relative; display:flex; align-items:center; gap:9px; font-size:.8rem;
  line-height:1.3; padding:2px 0; max-width:100%; cursor:help; }
.cb-hb-dot{ flex:0 0 auto; width:8px; height:8px; border-radius:1px;
  background:currentColor; box-shadow:0 0 0 2px currentColor; image-rendering:pixelated; }
.cb-hb-live .cb-hb-dot{ animation:cb-hb-beat 1.2s steps(3,jump-none) infinite; }
.cb-hb-text{ color:var(--cb-muted); min-width:0; overflow-wrap:anywhere; }
.cb-hb-text b{ color:var(--cb-ink); }
/* shared themed tooltip box — used by the heartbeat indicator AND the Mode/Realism labels */
.cb-hb-tip, .cb-lbl-tip{ position:absolute; left:0; width:180px; max-width:76vw;
  background:linear-gradient(180deg, rgba(22,11,40,.98), rgba(11,7,20,.98));
  border:1px solid var(--cb-magenta); border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.55), 0 0 12px rgba(255,43,214,.30);
  padding:9px 11px 10px; text-align:left; font-size:.72rem; line-height:1.5;
  opacity:0; visibility:hidden; z-index:1000; pointer-events:none;
  transition:opacity .12s ease, transform .12s ease; }
.cb-hb-tip{ bottom:calc(100% + 8px); transform:translateY(5px); }   /* indicator: opens upward */
.cb-lbl-tip{ top:calc(100% + 6px);  transform:translateY(-5px); }   /* label: opens downward */
.cb-hb:hover .cb-hb-tip, .cb-lbl:hover .cb-lbl-tip{
  opacity:1; visibility:visible; transform:translateY(0); }
.cb-lbl{ position:relative; display:inline-block; margin-bottom:.3rem; cursor:help; }
.cb-lbl-txt{ font-size:.875rem; font-weight:600; color:var(--cb-ink);
  border-bottom:1px dotted var(--cb-muted); }
.cb-tip-title{ display:block; font-weight:700; color:var(--cb-magenta); margin-bottom:5px; }
.cb-tip-row{ display:block; color:var(--cb-ink); margin:2px 0; }
.cb-tip-row b{ color:#fff; }
.cb-tip-dot{ display:inline-block; width:7px; height:7px; border-radius:1px; margin-right:7px;
  vertical-align:middle; }
.cb-tip-foot{ display:block; color:var(--cb-muted); margin-top:6px; font-size:.68rem; }

/* primary buttons: dark-purple neon-magenta outline at rest -> solid glowing magenta on hover */
button[data-testid="stBaseButton-primary"]{
  background-color:#160b28 !important; border-color:var(--cb-magenta) !important;
  color:var(--cb-magenta) !important; }
button[data-testid="stBaseButton-primary"]:hover{
  background-color:var(--cb-magenta) !important; border-color:var(--cb-magenta) !important;
  color:#0b0714 !important;
  box-shadow:0 0 16px rgba(255,43,214,.55), 0 0 3px var(--cb-magenta) !important; }
.cb-hb-running  { color:var(--cb-green); }    /* live  -> neon green  */
.cb-hb-preseason{ color:var(--cb-cyan); }     /* pre-draft -> cyan    */
.cb-hb-error    { color:#ffb000; }            /* error -> amber (TE)  */
.cb-hb-stale    { color:#ff3860; }            /* stale -> red  (DST)  */
.cb-hb-down     { color:var(--cb-muted); }    /* starting -> muted    */
/* pin the heartbeat to the sidebar bottom via absolute positioning (out of flow -> can never
   add a scrollbar). The sidebar <section> is already position:relative and full-height. */
/* left/bottom only (no `right`) so the container is just as wide as its short content and can
   never overhang the sidebar (its offsetParent is wider than the visible sidebar). */
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.cb-hb){
  position:absolute; left:1.5rem; bottom:1.25rem; margin:0; z-index:3; max-width:calc(100% - 3rem);
}
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.cb-hb) .cb-hb{ line-height:1.25; }

/* sticky top bar: make the ELEMENT CONTAINER sticky (a short wrapper can't stick across the
   page -- its parent is the tall main block). Holds a compact title + compressed draft-flow. */
[data-testid="stMainBlockContainer"]{ padding-top:0 !important; }  /* was ~6rem: close the gap */
[data-testid="stMainBlockContainer"] [data-testid="stElementContainer"]:has(.cb-sticky){
  position:sticky; top:0; z-index:990;
  /* Pull flush to the very top: cancel the vertical-block row-gap + hidden CSS element above
     it, so the header's rest position IS top:0 -- no gap that closes as you scroll. */
  margin-top:-32px; }
/* --cbc is fixed at 0 = the full expanded header (big title + subtitle + flow), pinned
   permanently at the top. (Previously scroll-driven collapse; now locked open by request.) */
.cb-sticky{ --cbc:0;
  display:flex; align-items:center; justify-content:center; gap:6px 22px; flex-wrap:wrap;
  padding:calc(8px + 8px*(1 - var(--cbc))) 12px; border-radius:0 0 10px 10px;
  background:linear-gradient(180deg, rgba(11,7,20,.96), rgba(11,7,20,.84));
  backdrop-filter:blur(7px); -webkit-backdrop-filter:blur(7px);
  border-bottom:1px solid rgba(255,43,214,.28); box-shadow:0 6px 16px rgba(0,0,0,.45); }
.cb-sticky-title{ display:flex; flex-direction:column; align-items:center; line-height:1;
  flex:0 1 auto; }
.cb-wordrow{ display:flex; gap:.12em; white-space:nowrap; }
.cb-sticky-title .cb-word{ font-family:'Press Start 2P',monospace;
  font-size:1.8rem; }  /* sized so title + fixed-width card strip fit one row at ~1920 */
.cb-sticky-title .cheddar{ color:var(--cb-magenta); text-shadow:0 0 6px #ff2bd6,0 0 15px rgba(255,43,214,.6); }
.cb-sticky-title .bowl{ color:var(--cb-cyan); text-shadow:0 0 6px #00e5ff,0 0 15px rgba(0,229,255,.6); }
.cb-sticky-sub{ font-family:'Orbitron',sans-serif; color:var(--cb-muted); text-align:center;
  font-size:.54rem; letter-spacing:.11em; overflow:hidden;
  opacity:calc(1 - var(--cbc)); max-height:calc(1.4em*(1 - var(--cbc)));
  margin-top:calc(5px*(1 - var(--cbc))); }
.cb-flowstrip{ display:flex; align-items:center; justify-content:center; gap:4px; flex-wrap:wrap;
  flex:0 1 auto; }
/* Fixed size for every card (photo + role/manager/meta) so all cards are identical
   regardless of value; text ellipsizes as a safety. Photo sits to the left. */
.cb-mini{ display:flex; flex-direction:row; align-items:center; gap:8px;
  width:140px; height:46px; box-sizing:border-box; padding:0 10px; line-height:1.18;
  border:1px solid var(--a,#00e5ff); border-radius:7px; background:rgba(22,11,40,.6); }
.cb-mini-av{ flex:0 0 34px; width:34px; height:34px; border-radius:6px; object-fit:cover;
  border:1px solid var(--a,#00e5ff); box-shadow:0 0 5px -1px var(--a,#00e5ff); }
.cb-mini-av-ph{ background:rgba(154,143,199,.18); }
.cb-mini-txt{ display:flex; flex-direction:column; min-width:0; overflow:hidden; }
.cb-mini-role{ font-size:.6rem; font-weight:700; letter-spacing:.03em; color:var(--a,#00e5ff);
  text-transform:uppercase; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cb-mini-mgr{ font-size:.82rem; font-weight:700; color:var(--cb-ink); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.cb-mini-meta{ font-size:.6rem; color:var(--cb-muted); white-space:nowrap; }
.cb-mini-empty{ justify-content:center; color:var(--cb-muted); border-color:rgba(154,143,199,.4); }
.cb-mini-pulse{ animation:cbpulse 1.4s ease-in-out infinite; }
/* Fixed-width separator so the gap between cards is identical whether it holds a ">" or ">>". */
.cb-mini-sep{ flex:0 0 14px; text-align:center; color:var(--cb-muted); font-size:1rem; }
.cb-mini-jump{ color:var(--cb-magenta); font-weight:700; }
</style>
"""


def _title_html() -> str:
    return (
        "<div class='cb-title'>"
        "<span class='cb-word cheddar'>CHEDDAR</span>"
        "<span class='cb-word bowl'>BOWL</span></div>"
        f"<div class='cb-sub'>League ID: {config.LEAGUE_ID} &#9646; {config.NUM_TEAMS} Teams &#9646; Half-PPR</div>"
        "<hr class='cb-rule'>"
    )


def _rec_css() -> str:
    """Neon recommendation cards: position-colored border, mini P(gone) bar, reason chips,
    and equal height within each row (columns stretch; card fills the column)."""
    rules = [
        ".cb-rec-flag{ display:none; }",
        "[data-testid='stVerticalBlock']:has(> [data-testid='stElementContainer'] .cb-rec-flag){"
        " border-radius:12px; border:1px solid rgba(0,229,255,.4) !important; height:100%;"
        " background:linear-gradient(180deg, rgba(22,11,40,.85), rgba(11,7,20,.92));"
        " box-shadow:0 0 12px rgba(0,229,255,.1); }",
        # equal height per row: leave columns auto (flex-stretch), fill the card's wrappers
        "[data-testid='stHorizontalBlock']:has(.cb-rec-flag) > [data-testid='stColumn'] > [data-testid='stVerticalBlock'],"
        "[data-testid='stHorizontalBlock']:has(.cb-rec-flag) > [data-testid='stColumn'] > [data-testid='stVerticalBlock'] > [data-testid='stLayoutWrapper']{ height:100%; }",
        # header
        ".cb-recname{ font-weight:700; font-size:.96rem; line-height:1.14; }",
        ".cb-recpos{ font-size:.8rem; color:var(--cb-muted); margin-top:1px; }",
        ".cb-recmetric{ font-size:.82rem; margin-top:5px; }",
        ".cb-recmetric2{ font-size:.8rem; color:var(--cb-muted); margin-top:1px; }",
        # mini P(gone) bar
        ".cb-pbar{ height:7px; border-radius:5px; background:rgba(255,255,255,.12); overflow:hidden; margin:3px 0 2px; }",
        ".cb-pbar-fill{ height:100%; border-radius:5px; }",
        # reason chips (fixed 2-line zone -> uniform card height)
        ".cb-chips{ display:flex; flex-wrap:wrap; gap:4px; margin-top:7px; height:3.1em;"
        " overflow:hidden; align-content:flex-start; }",
        ".cb-chip{ font-size:.66rem; padding:1px 8px; border-radius:10px; white-space:nowrap;"
        " border:1px solid rgba(0,229,255,.35); background:rgba(0,229,255,.08);"
        " color:var(--cb-ink); font-family:'Chakra Petch',ui-monospace,monospace; }",
    ]
    for pos, color in POS_COLORS.items():
        rules.append(
            f"[data-testid='stVerticalBlock']:has(> [data-testid='stElementContainer'] .cb-pos-{pos})"
            f"{{ border-color:{color} !important; box-shadow:0 0 16px {color}44 !important; }}")
    return "<style>" + "\n".join(rules) + "</style>"


def _urgency_color(p: float) -> str:
    return "#ff3860" if p >= 0.60 else ("#39ff14" if p <= 0.35 else "#ffb000")


# Shorten the reason sentence's phrases into compact chip labels.
_CHIP_RULES = [
    (r"fills your open (\w+)", r"fills \1"),
    (r"fills your FLEX", "fills FLEX"),
    (r"(\w+) run \(\d+ of \d+\)", "run"),
    (r"likely gone \([^)]*\)", ""),                       # redundant with the bar
    (r"next \w+ drops (\d+) pts.*", r"cliff \1"),
    (r"tier cliff: (\d+)-pt drop.*", r"cliff \1"),
    (r"safe to wait.*", "can wait"),
    (r"fell (\d+) past ADP", r"value +\1"),
    (r"last in (\w+) tier (\d+)", r"tier \2"),
    (r"best available.*", "best avail"),
]


def _reason_chips(reason: str) -> list[str]:
    chips = []
    for part in str(reason).split(";"):
        s = part.strip()
        for pat, rep in _CHIP_RULES:
            s = re.sub(pat, rep, s).strip()
        if s:
            chips.append(s)
    return chips


@st.cache_resource(show_spinner="Loading projections + building value board…")
def get_engine() -> Engine:
    return Engine()


@st.cache_data
def get_headshots() -> dict:
    return load_headshots()


@st.cache_data
def get_teams() -> dict:
    return load_teams()


def _team_label(teams: dict, team_id) -> str:
    t = teams.get(int(team_id)) if team_id is not None and str(team_id).isdigit() else None
    if not t:
        return "TBD" if team_id is None else f"Team {team_id}"
    mgr = t.get("manager", "")
    return f"{t['team_name']}" + (f" — {mgr}" if mgr else "")


# ---------- interactive simulation: draft YOUR team by clicking the board, bots draft the rest.
# Model: `sim_my_picks` {overall_pick: player_id} are your choices; `sim_cursor` is the playback
# head (how many picks are shown). The draft is deterministic given the seed + your picks, so the
# board = simulate(cursor, overrides=your picks). The cursor can't advance past your next open
# pick, so forward motion always stops on your clock until you click a player. -----------------
def _sim_order() -> list:
    """Draft-slot order [slot1_team..slotN_team]; defaults to seating you at DRAFT_SLOT. Editable
    via the Reorder button (mirrors Manual mode)."""
    if "sim_order" not in st.session_state:
        from cheddarbowl.draft.simulate import _ORDER
        st.session_state["sim_order"] = list(_ORDER)
    return list(st.session_state["sim_order"])


def _sim_my_pick_numbers() -> list[int]:
    from cheddarbowl.draft.draft_context import snake_pick_numbers
    order = _sim_order()
    my_slot = (order.index(config.MY_TEAM_ID) + 1) if config.MY_TEAM_ID in order else config.DRAFT_SLOT
    return list(snake_pick_numbers(my_slot))


def _sim_seed() -> int:
    import time
    if "sim_seed" not in st.session_state:
        st.session_state["sim_seed"] = int(time.time() * 1000) & 0x7FFFFFFF
    return int(st.session_state["sim_seed"])


def _sim_my_picks() -> dict:
    return {int(k): str(v) for k, v in st.session_state.get("sim_my_picks", {}).items()}


def _sim_cap() -> int:
    """Furthest the cursor may reveal: up to (not into) your next open pick (or the whole draft
    once all your picks are in)."""
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    picks = _sim_my_picks()
    nxt = min((p for p in _sim_my_pick_numbers() if p not in picks), default=None)
    return total if nxt is None else nxt - 1


def _sim_do_back() -> None:
    picks = _sim_my_picks()
    cursor = int(st.session_state.get("sim_cursor", 0))
    if picks:                                           # undo your most recent pick
        last = max(picks)
        picks.pop(last, None)
        st.session_state["sim_my_picks"] = picks
        st.session_state["sim_cursor"] = last - 1
    else:                                               # no picks yet: step back one
        st.session_state["sim_cursor"] = max(0, cursor - 1)
    st.session_state["sim_playing"] = False


def _sim_do_finish(chaos: float) -> None:
    """Auto-draft your remaining picks (the tool's own VBD logic) and jump to the full draft."""
    from cheddarbowl.draft.simulate import simulate
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    picks = _sim_my_picks()
    full = simulate(total, chaos=chaos, seed=_sim_seed(), overrides=picks)
    my_nums = set(_sim_my_pick_numbers())
    for pk in full["picks"]:
        if pk["pick"] in my_nums and pk["pick"] not in picks:
            picks[pk["pick"]] = pk["player_id"]
    st.session_state["sim_my_picks"] = picks
    st.session_state["sim_cursor"] = total
    st.session_state["sim_playing"] = False


def _sim_make_pick(player_id: str, at_pick: int) -> None:
    """Record your pick and fluidly advance to your next clock (used by both the board Draft
    buttons and the search-bar Add button)."""
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    my_nums = _sim_my_pick_numbers()
    p = _sim_my_picks()
    p[int(at_pick)] = str(player_id)
    nxt = min((q for q in my_nums if q not in p), default=None)
    st.session_state["sim_my_picks"] = p
    st.session_state["sim_cursor"] = total if nxt is None else nxt - 1
    st.session_state["sim_playing"] = False
    st.rerun()


def _sim_entry_panel(board: pd.DataFrame, my_next: int) -> None:
    """Search-and-add pick entry for Simulation, mirroring Manual mode's bar (search + Add / Undo
    / Reset / Reorder). `board` is the available-player pool."""
    avail = board.sort_values("vob", ascending=False)
    labels = (avail["name"].astype(str) + "  ·  " + avail["pos"].astype(str)
              + "-" + avail["team"].fillna("").astype(str)).tolist()
    id_by_label = dict(zip(labels, avail["player_id"].astype(str)))
    picks = _sim_my_picks()

    c1, c2, c3, c4, c5 = st.columns([5, 1, 1, 1.2, 1.4], vertical_alignment="bottom")
    choice = c1.selectbox("Your pick", labels, index=None, placeholder="Search a player…",
                          key=f"sim_pick_{my_next}")
    if c2.button("➕ Add", width="stretch", disabled=choice is None, key="sim_add"):
        _sim_make_pick(id_by_label[choice], my_next)
    if c3.button("↩ Undo", width="stretch", disabled=not picks, key="sim_undo_panel"):
        _sim_do_back()
        st.rerun()
    with c4.popover("🗑 Reset", width="stretch", disabled=not picks):
        st.caption(f"Clear all {len(picks)} of your picks and start the draft over?")
        if st.button("Yes, reset the draft", type="primary", width="stretch",
                     key="sim_reset_panel"):
            st.session_state["sim_my_picks"] = {}
            st.session_state["sim_cursor"] = min(_sim_my_pick_numbers()) - 1
            st.session_state["sim_playing"] = False
            st.rerun()
    if c5.button("⚙ Reorder", width="stretch", key="sim_reorder_btn",
                 help="Rearrange the draft order (which team drafts in which slot)"):
        st.session_state["sim_reorder"] = True
        st.rerun()


def _sim_transport_bar(cursor: int, cap: int, total: int, chaos: float) -> None:
    st.markdown("<div style='height:0.7rem'></div>", unsafe_allow_html=True)  # padding above buttons
    playing = bool(st.session_state.get("sim_playing"))
    my_nums = _sim_my_pick_numbers()

    # spacer columns on both sides center the 7-button group; every button fills its column
    # identically (incl. the speed toggle), so it stays centered at any width.
    b = st.columns([3, 1, 1, 1, 1, 1, 1, 1, 3], vertical_alignment="center")
    if b[1].button("⏮", help="Restart — clear your picks", width="stretch", key="sim_restart"):
        st.session_state["sim_my_picks"] = {}
        st.session_state["sim_cursor"] = min(my_nums) - 1
        st.session_state["sim_playing"] = False
        st.rerun()
    if b[2].button("◀", help="Back / undo your last pick", width="stretch",
                   disabled=(cursor <= 0 and not _sim_my_picks()), key="sim_back"):
        _sim_do_back()
        st.rerun()
    if b[3].button("⏸" if playing else "▶", help="Play / pause the draft", width="stretch",
                   disabled=(cursor >= cap and not playing), key="sim_play"):
        st.session_state["sim_playing"] = not playing
        st.rerun()
    if b[4].button("▶❙", help="Step one pick", width="stretch",
                   disabled=cursor >= cap, key="sim_step"):
        st.session_state["sim_cursor"] = min(cap, cursor + 1)
        st.session_state["sim_playing"] = False
        st.rerun()
    if b[5].button("⏭", help="Skip to your next pick", width="stretch",
                   disabled=cursor >= cap, key="sim_skip"):
        st.session_state["sim_cursor"] = cap
        st.session_state["sim_playing"] = False
        st.rerun()
    if b[6].button("🏁", help="Finish — auto-draft your remaining picks and show the full draft",
                   width="stretch", disabled=cursor >= total, key="sim_finish"):
        _sim_do_finish(chaos)
        st.rerun()
    speed = int(st.session_state.get("sim_speed", 1))
    if b[7].button(f"{speed}×", help="Playback speed — click to cycle 1× / 2× / 4×",
                   width="stretch", key="sim_speed_btn"):
        st.session_state["sim_speed"] = {1: 2, 2: 4, 4: 1}[speed]
        st.rerun()


def _sim_order_setup(teams: dict) -> None:
    """Drag-to-reorder gate for Simulation (mirrors Manual). Confirming resets the sim draft,
    since your picks are tied to your slot in the order."""
    from streamlit_sortables import sort_items
    from cheddarbowl.draft.draft_context import snake_pick_numbers

    st.subheader("Set the draft order")
    if not teams:
        st.warning("No team list found. Start the scraper once (Live mode) to fetch team names, "
                   "or add data/teams.csv.")
        return
    st.caption("Drag each manager into draft-slot order (slot 1 at the top), then confirm. "
               "Changing the order restarts the simulated draft.")
    order = _sim_order()
    items = [f"{tid} · {teams.get(tid, {}).get('manager') or f'Team {tid}'}" for tid in order]
    sortable_css = (
        ".sortable-item{background-color:#160b28 !important;border:1px solid #ff2bd6 !important;"
        "color:#ff2bd6 !important;}"
        ".sortable-item:hover{background-color:#ff2bd6 !important;border-color:#ff2bd6 !important;"
        "color:#0b0714 !important;box-shadow:0 0 14px rgba(255,43,214,.5) !important;}")
    ordered = sort_items(items, direction="vertical", key="sim_order_sortable",
                         custom_style=sortable_css)
    st.caption("Order top→bottom = draft slots 1→N.  ★ = your team.")
    c1, c2 = st.columns([1, 1])
    if c1.button("✅ Confirm order & restart", type="primary", width="stretch"):
        new_order = [int(x.split(" · ", 1)[0]) for x in ordered]
        st.session_state["sim_order"] = new_order
        st.session_state["sim_reorder"] = False
        st.session_state["sim_my_picks"] = {}          # picks were tied to your old slot
        my_slot = (new_order.index(config.MY_TEAM_ID) + 1) if config.MY_TEAM_ID in new_order else config.DRAFT_SLOT
        st.session_state["sim_cursor"] = min(snake_pick_numbers(my_slot)) - 1
        st.session_state["sim_playing"] = False
        st.rerun()
    if c2.button("Cancel", width="stretch"):
        st.session_state["sim_reorder"] = False
        st.rerun()


def render_simulation(ui: dict) -> None:
    from cheddarbowl.draft.simulate import simulate

    if st.session_state.get("sim_reorder"):            # Reorder button -> drag-to-reorder gate
        _sticky_header()
        _sim_order_setup(get_teams())
        return

    total = config.NUM_TEAMS * config.NUM_ROUNDS
    chaos = ui["chaos"]
    seed = _sim_seed()
    order = _sim_order()
    my_nums = _sim_my_pick_numbers()
    picks = _sim_my_picks()
    my_next = min((p for p in my_nums if p not in picks), default=None)
    cap = total if my_next is None else my_next - 1

    if "sim_cursor" not in st.session_state:            # first entry: sit on your first clock
        st.session_state["sim_cursor"] = min(my_nums) - 1
    cursor = max(0, min(int(st.session_state["sim_cursor"]), cap))
    st.session_state["sim_cursor"] = cursor

    state = simulate(cursor, chaos=chaos, seed=seed, overrides=picks, order=order)
    on_clock_mine = my_next is not None and cursor == my_next - 1

    on_pick = entry_panel = None
    if on_clock_mine:
        def on_pick(player_id: str) -> None:      # board Draft buttons
            _sim_make_pick(player_id, my_next)

        def entry_panel(bd: pd.DataFrame) -> None:  # search bar + Add/Undo/Reset (like Manual)
            _sim_entry_panel(bd, my_next)

    def _transport(_ctx) -> None:
        _sim_transport_bar(cursor, cap, total, chaos)

    render(state, chaos, transport=_transport, on_pick=on_pick, entry_panel=entry_panel,
           lens=ui["lens"], my_team=ui["my_team"], host=ui["host"])

    # Auto-play: after the current frame is on screen, wait a beat, advance by the speed, and
    # rerun. (A post-render sleep+rerun drives a reliable full-page refresh; a run_every fragment
    # only reruns its own scope, so the board wouldn't update.) We only get here on a passive
    # rerun -- any button/pick click raises its own rerun first, so it can't be interrupted here.
    if st.session_state.get("sim_playing"):
        if cursor >= cap:
            st.session_state["sim_playing"] = False
        else:
            import time
            speed = max(1, int(st.session_state.get("sim_speed", 1)))
            time.sleep(0.55)
            st.session_state["sim_cursor"] = min(cap, cursor + speed)
            st.rerun()


# ------------------------------------------------------------------ sidebar
_MODE_TIP_ROWS = [
    ("Live", "var(--cb-green)", "connected to the live scraper"),
    ("Manual", "var(--cb-cyan)", "enter draft picks by hand"),
    ("Simulation", "var(--cb-magenta)", "auto-drafts all teams"),
]
_REALISM_TIP_ROWS = [
    ("Chalky", "var(--cb-cyan)", "players go near their ADP"),
    ("Normal", "var(--cb-magenta)", "a balanced draft"),
    ("Chaotic", "var(--cb-green)", "more players fall to you"),
]
_REALISM_FOOT = "How far the draft strays from ADP — drives the forecast and the sim."
_LENS_TIP_ROWS = [
    ("Proj", "var(--cb-magenta)", "expected points — the default valuation"),
    ("Floor", "var(--cb-cyan)", "safe/reliable — bias toward high-floor starters"),
    ("Ceil", "var(--cb-green)", "upside — bias toward high-ceiling swings for depth"),
]
_LENS_FOOT = "Which projection value + VONA are measured on — re-ranks the whole board & chart y-axis."


def _tip_label(label: str, title: str, rows: list, foot: str = "") -> str:
    """A sidebar label that reveals the same themed tooltip as the heartbeat on hover."""
    row_html = "".join(
        f"<span class='cb-tip-row'><span class='cb-tip-dot' style='background:{c}'></span>"
        f"<b>{name}</b> — {desc}</span>" for name, c, desc in rows)
    foot_html = f"<span class='cb-tip-foot'>{foot}</span>" if foot else ""
    return (f"<span class='cb-lbl'><span class='cb-lbl-txt'>{label}</span>"
            f"<span class='cb-lbl-tip'><span class='cb-tip-title'>{title}</span>"
            f"{row_html}{foot_html}</span></span>")


def _is_host() -> bool:
    """Shared-LAN role check. A session is HOST only if its ?host= query matches the token in
    data/host_token.txt (written by launch.py, passed only to the host's own window). LAN viewers
    open the plain URL -> read-only. No token file (dev / single-user run) -> everyone is host.

    Sticky once host: Streamlit can drop custom query params after the first load, so we latch the
    host role in session_state (never latch viewer, so a late-arriving query can still promote)."""
    if st.session_state.get("_host_role") == "host":
        return True
    try:
        with open(config.HOST_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        return True                              # no token file -> single-user/dev: host
    is_host = bool(tok) and st.query_params.get("host") == tok
    if is_host:
        st.session_state["_host_role"] = "host"
    return is_host


def sidebar() -> dict:
    host = _is_host()
    # Apply a programmatic mode switch (e.g. failsafe -> Manual) before the widget is built.
    if "_pending_mode" in st.session_state:
        st.session_state["mode"] = st.session_state.pop("_pending_mode")
    st.sidebar.header("Settings")
    st.sidebar.divider()

    if host:
        st.sidebar.markdown(_tip_label("Mode", "Mode", _MODE_TIP_ROWS), unsafe_allow_html=True)
        mode = st.sidebar.radio(
            "Mode", ["Live", "Manual", "Simulation"], key="mode", label_visibility="collapsed")
    else:
        mode = "Live"          # viewers are read-only Live; no mode switching
    ui = {"mode": mode, "host": host}

    st.sidebar.divider()
    st.sidebar.markdown(_tip_label("Realism", "Realism", _REALISM_TIP_ROWS, _REALISM_FOOT),
                        unsafe_allow_html=True)
    levels = list(config.CHAOS_LEVELS)
    chaos_label = st.sidebar.radio(
        "Realism", levels, index=levels.index(config.CHAOS_DEFAULT), label_visibility="collapsed")
    ui["chaos"] = config.CHAOS_LEVELS[chaos_label]

    st.sidebar.divider()
    st.sidebar.markdown(_tip_label("Value lens", "Value lens", _LENS_TIP_ROWS, _LENS_FOOT),
                        unsafe_allow_html=True)
    ui["lens"] = st.sidebar.radio(
        "Value lens", list(Engine.LENS_COL), index=0, label_visibility="collapsed")

    # Your team: per-session, so each LAN viewer tailors recommendations/forecast/"your pick" to
    # THEIR draft slot. Defaults to the host's detected team.
    teams = get_teams()
    if teams:
        st.sidebar.divider()
        tids = sorted(teams)
        default_tid = config.MY_TEAM_ID if config.MY_TEAM_ID in tids else tids[0]
        if st.session_state.get("my_team") not in tids:
            st.session_state["my_team"] = default_tid
        ui["my_team"] = st.sidebar.selectbox(
            "🏈 Your team", tids, key="my_team",
            format_func=lambda t: teams[t].get("manager") or teams[t].get("team_name") or f"Team {t}")
    else:
        ui["my_team"] = config.MY_TEAM_ID

    if not host:
        st.sidebar.caption("👀 Viewer — read-only, tailored to your team.")
    _sidebar_heartbeat()   # pulsing health indicator, absolutely pinned to the sidebar bottom
    return ui


_HEALTH_BADGE = {
    "running":   ("🟢", "Scraper running"),
    "preseason": ("🟡", "Pre-draft — rosters stale"),
    "error":     ("🟠", "Scraper reachable but erroring"),
    "stale":     ("🔴", "Scraper heartbeat stale"),
    "down":      ("⚪", "Scraper starting…"),
}


_HB_LABEL = {
    "running": "live", "preseason": "pre-draft", "error": "error",
    "stale": "stale", "down": "starting",
}
_HB_TIP_ROWS = [
    ("live", "var(--cb-green)", "scraping picks off the draft-results page"),
    ("pre-draft", "var(--cb-cyan)", "last season's rosters — board blank until the draft"),
    ("error", "#ffb000", "reachable, but failing (usually needs NFL.com login cookies)"),
    ("stale", "#ff3860", "no heartbeat 12s+ — the poller stopped"),
    ("starting", "var(--cb-muted)", "just spawned; first heartbeat pending"),
]
_HB_TIP_HTML = (
    "<span class='cb-hb-tip'><span class='cb-tip-title'>Scraper heartbeat</span>"
    + "".join(
        f"<span class='cb-tip-row'><span class='cb-tip-dot' style='background:{c}'></span>"
        f"<b>{name}</b> — {desc}</span>"
        for name, c, desc in _HB_TIP_ROWS)
    + "<span class='cb-tip-foot'>Falls back to roster-diffing if the results page is "
      "unavailable.</span></span>"
)


def _sidebar_heartbeat() -> None:
    """Pulsing scraper-health indicator pinned to the sidebar bottom. Lives in its own 3s
    auto-refresh fragment so the sidebar (which the main Live loop can't touch) stays current;
    the dot pulses only while a heartbeat is actually arriving."""
    from cheddarbowl.ingest import runner

    @st.fragment(run_every=config.POLL_SECONDS)
    def _hb() -> None:
        h = runner.health()
        state = h["state"]
        live = "cb-hb-live" if state in ("running", "preseason", "error") else ""
        age = (f"updated {int(h['age'])}s ago" if h["age"] is not None
               else "waiting…")
        # Own themed tooltip (a hover-revealed child) instead of Streamlit's help= whose icon
        # overflowed the sidebar. The '?' badge carries the tooltip as a child element.
        st.sidebar.markdown(
            "<div class='cb-hb cb-hb-%s %s'>"
            "<span class='cb-hb-dot'></span>"
            "<span class='cb-hb-text'><b>%s</b> · %s</span>"
            "%s</div>"
            % (state, live, _HB_LABEL.get(state, "?"), age, _HB_TIP_HTML),
            unsafe_allow_html=True)

    _hb()


# ------------------------------------------------------------------ panels
def _flow_title(pk: int, C: int, my_next: int | None, is_me: bool) -> str:
    # No "You ·"/"Your" prefixes -- the green highlight already marks your slots, and dropping
    # them lets the cards be much narrower (the prefix was the widest text in the card).
    if pk == C - 1:
        return "Last Pick"
    if pk == C:
        return "On The Clock"
    if is_me and pk == my_next:
        return "Your Pick"        # your upcoming pick (far off, shown past a » jump); kept
    if pk == C + 1:               # distinct from the generic "Next Pick" one slot over
        return "Next Pick"
    return "On Deck"


def _flow_card(pk: int | None, C: int, my_next: int | None, ctx, teams: dict,
               last_player: dict | None) -> None:
    # One hidden, NON-EMPTY marker span (empty spans get stripped) carrying the scoping
    # classes; CSS :has() reads it to style/center/highlight this card.
    if pk is None:
        st.markdown("<span class='cb-fcard-flag'>​</span><div class='cb-frole'>&mdash;</div>",
                    unsafe_allow_html=True)
        return
    team_id = ctx.team_at_pick(pk)
    t = teams.get(team_id, {})
    is_me = ctx.is_me(team_id)
    rnd = (pk - 1) // config.NUM_TEAMS + 1
    last = pk == C - 1
    on_clock = pk == C
    green = is_me and not last              # my cards are green (my last pick stays cyan)
    flag = "cb-fcard-flag"
    if green:
        flag += " cb-mine-flag"             # green (incl. me on the clock)
    elif on_clock:
        flag += " cb-onclock-flag"          # another team on the clock -> magenta
    if on_clock:
        flag += " cb-pulse-flag"            # whoever is on the clock pulses
    # last pick -> cyan (always); my cards -> green; another team on the clock -> magenta; else cyan
    accent = ("var(--cb-cyan)" if last else
              "var(--cb-green)" if is_me else
              "var(--cb-magenta)" if on_clock else "var(--cb-cyan)")

    if t.get("avatar_url"):
        st.image(t["avatar_url"], width=52)  # centered at top via CSS
    extra = ""
    if pk == C - 1 and last_player:
        pos = last_player["pos"]
        col = POS_COLORS.get(pos, "#8e8e8e")
        extra = (f"<div class='cb-fplayer2'>{last_player['player']} "
                 f"<b style='color:{col}'>{pos}</b></div>")
    st.markdown(f"<span class='{flag}'>​</span>"
                f"<div class='cb-frole' style='color:{accent}'>{_flow_title(pk, C, my_next, is_me)}</div>"
                f"<div class='cb-fteam2'>{t.get('team_name', 'TBD')}</div>"
                f"<div class='cb-fowner'>{t.get('manager', '')}</div>"
                f"<div class='cb-fmeta' style='color:{accent}'>R{rnd} &middot; #{pk}</div>{extra}",
                unsafe_allow_html=True)


def _flow_strip_html(ctx, teams: dict) -> str:
    """Compressed one-line draft flow: fixed-size cards, each with the team photo + role ·
    manager · R#·#N. Same slot logic + accent colors as the full flow cards."""
    from cheddarbowl.draft.draft_context import snake_pick_numbers
    C = ctx.current_pick()
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    my_next = next((p for p in snake_pick_numbers(ctx.slot) if p >= C), None)
    slots = [C - 1, C, C + 1]
    slots.append(C + 2 if (my_next is None or my_next in (C, C + 1)) else my_next)
    slots = [p if (p is not None and 1 <= p <= total) else None for p in slots]
    jump = slots[3] is not None and slots[2] is not None and slots[3] != slots[2] + 1

    def _item(pk):
        if pk is None:
            return "<div class='cb-mini cb-mini-empty'>—</div>"
        team_id = ctx.team_at_pick(pk)
        t = teams.get(team_id, {})
        is_me = ctx.is_me(team_id)
        last, on_clock = pk == C - 1, pk == C
        accent = ("var(--cb-cyan)" if last else "var(--cb-green)" if is_me else
                  "var(--cb-magenta)" if on_clock else "var(--cb-cyan)")
        rnd = (pk - 1) // config.NUM_TEAMS + 1
        mgr = t.get("manager") or (f"Team {team_id}" if team_id is not None else "TBD")
        pulse = " cb-mini-pulse" if on_clock else ""
        av = t.get("avatar_url") or ""
        photo = (f"<img class='cb-mini-av' src='{av}' alt=''>" if av
                 else "<div class='cb-mini-av cb-mini-av-ph'></div>")
        return (f"<div class='cb-mini{pulse}' style='--a:{accent}'>{photo}"
                f"<div class='cb-mini-txt'>"
                f"<span class='cb-mini-role'>{_flow_title(pk, C, my_next, is_me)}</span>"
                f"<span class='cb-mini-mgr'>{mgr}</span>"
                f"<span class='cb-mini-meta'>R{rnd} &middot; #{pk}</span></div></div>")

    parts = [_item(slots[0])]
    for i in range(1, 4):
        cls = "cb-mini-sep cb-mini-jump" if (i == 3 and jump) else "cb-mini-sep"
        parts.append(f"<span class='{cls}'>{'&raquo;' if (i == 3 and jump) else '&rsaquo;'}</span>")
        parts.append(_item(slots[i]))
    return "<div class='cb-flowstrip'>" + "".join(parts) + "</div>"


def _sticky_header(ctx=None, teams: dict | None = None) -> None:
    """Sticky top bar: CHEDDARBOWL wordmark + (when a draft is loaded) the compressed flow."""
    flow = _flow_strip_html(ctx, teams) if ctx is not None else ""
    st.markdown(
        "<div class='cb-sticky'><div class='cb-sticky-title'>"
        "<div class='cb-wordrow'><span class='cb-word cheddar'>CHEDDAR</span>"
        "<span class='cb-word bowl'>BOWL</span></div>"
        f"<div class='cb-sticky-sub'>League ID: {config.LEAGUE_ID} &#9646; "
        f"{config.NUM_TEAMS} Teams &#9646; Half-PPR</div></div>"
        + flow + "</div>",
        unsafe_allow_html=True)


def _sticky_segmented(label: str, opts: list, default: str, key: str, container=st) -> str:
    """A segmented_control whose selection survives a pick's mid-render st.rerun().

    Adding a pick reruns before some panels render, so Streamlit garbage-collects those
    widgets' keys. We mirror the value into a plain (non-widget) session_state key that isn't
    cleaned up, and reseed the widget from it — so tabs (roster/board) stay put across picks.
    """
    keep = "_keep_" + key
    if key not in st.session_state:
        st.session_state[key] = st.session_state.get(keep, default)
    val = container.segmented_control(label, opts, label_visibility="collapsed", key=key) or default
    st.session_state[keep] = val
    return val


def draft_flow(ctx, teams: dict, state: dict) -> None:
    """Row of cards: last pick · current · next · my next pick (with a » jump if far)."""
    C = ctx.current_pick()
    total = config.NUM_TEAMS * config.NUM_ROUNDS
    from cheddarbowl.draft.draft_context import snake_pick_numbers
    my_next = next((p for p in snake_pick_numbers(ctx.slot) if p >= C), None)
    last_player = next((p for p in state.get("picks", []) if p["pick"] == C - 1), None)

    slots = [C - 1, C, C + 1]
    slots.append(C + 2 if (my_next is None or my_next in (C, C + 1)) else my_next)
    slots = [p if (p is not None and 1 <= p <= total) else None for p in slots]
    jump = slots[3] is not None and slots[2] is not None and slots[3] != slots[2] + 1

    # a narrow chevron column sits between card 3 and card 4 only when there's a jump
    if jump:
        cols = st.columns([6, 6, 6, 1, 6])
        card_cols, chevron = [cols[0], cols[1], cols[2], cols[4]], cols[3]
    else:
        card_cols, chevron = list(st.columns(4)), None

    for col, pk in zip(card_cols, slots):
        with col, st.container(border=True):
            _flow_card(pk, C, my_next, ctx, teams, last_player)
    if chevron is not None:
        with chevron:
            st.markdown("<div class='cb-jump'>&raquo;</div>", unsafe_allow_html=True)


def _urgency_tag(p: float) -> str:
    if p >= 0.60:
        return "🔴 take now"
    if p <= 0.35:
        return "🟢 can wait"
    return "🟡 lean"


def _render_cards(df: pd.DataFrame, headshots: dict, per_row: int = 3, on_pick=None) -> None:
    """Render players as neon cards (3 wide), border colored by position. Uses
    ``display_rank`` for the number so both Recommended and position views work.

    If ``on_pick`` is given (Manual mode), each card gets a Draft button that records that
    player as the current pick."""
    for i in range(0, len(df), per_row):
        cols = st.columns(per_row)
        for col, (_, r) in zip(cols, df.iloc[i:i + per_row].iterrows()):
            with col, st.container(border=True):
                st.markdown(f"<span class='cb-rec-flag cb-pos-{r['pos']}'>​</span>",
                            unsafe_allow_html=True)
                pos_col = POS_COLORS.get(r["pos"], "#888")
                ic, tc = st.columns([1, 3], vertical_alignment="center")
                url = headshots.get(str(r["player_id"]))
                with ic:
                    if url:
                        st.image(url, width=54)
                    else:
                        st.markdown(f"### {r['pos']}")
                with tc:
                    st.markdown(
                        f"<div class='cb-recname'>{int(r['display_rank'])}. {r['name']}</div>"
                        f"<div class='cb-recpos'><span style='color:{pos_col}'>{r['pos']}</span> · "
                        f"{r['team']}</div>",
                        unsafe_allow_html=True)
                pct = r["p_taken"] * 100
                chips = "".join(f"<span class='cb-chip'>{c}</span>" for c in _reason_chips(r["reason"]))
                st.markdown(
                    f"<div class='cb-recmetric'>P(gone) <b>{pct:.0f}%</b> {_urgency_tag(r['p_taken'])}</div>"
                    f"<div class='cb-pbar'><div class='cb-pbar-fill' "
                    f"style='width:{pct:.0f}%;background:{_urgency_color(r['p_taken'])}'></div></div>"
                    f"<div class='cb-recmetric2'>VONA {r['vona']:.0f} &nbsp;·&nbsp; Val {r['vob']:.0f}</div>"
                    f"<div class='cb-chips'>{chips}</div>",
                    unsafe_allow_html=True)
                # What-if: pin this player at your next pick in the team forecast. A plain button
                # reads the player directly -> no Altair selection, so it never fights the scatter's
                # hover-card. Click the active one again to clear (empty request -> baseline).
                _pid = str(r["player_id"])
                _considering = st.session_state.get("consider_select") == _pid
                if st.button("🔮 Considering ✓" if _considering else "🔮 Consider",
                             key=f"consider_{_pid}", width="stretch",
                             type="primary" if _considering else "secondary"):
                    st.session_state["_consider_request"] = "" if _considering else _pid
                    st.rerun()
                if on_pick is not None:
                    if st.button(f"➕ Draft {r['name']}", key=f"pick_{r['player_id']}",
                                 width="stretch", type="primary"):
                        on_pick(r["player_id"])


def _value_tag(pick: int, adp) -> str:
    """Reach/steal vs ADP: drafted earlier than ADP = reach, later = steal (magnitude sized)."""
    if adp is None or pd.isna(adp):
        return ""
    diff = round(float(adp) - pick)   # + = reached up (early); - = fell to them (steal)
    mag = abs(diff)
    if diff >= 5:
        return f"{'Big reach' if mag >= 15 else 'Reach'} {mag}"
    if diff <= -5:
        return f"{'Big steal' if mag >= 15 else 'Steal'} {mag}"
    return "Fair"


def _render_table(df: pd.DataFrame, on_pick=None, key: str = "board_table") -> None:
    # Missing numbers -> +/-inf so they render BLANK (not "None") and always sort to the WORST
    # end regardless of direction: ADP is better when low (fill +inf -> last ascending); the
    # rest are better when high (fill -inf -> last descending). Display copy only -- the board
    # keeps real NaN so the recommendation math is unaffected.
    NEG, POS = float("-inf"), float("inf")
    view = pd.DataFrame({
        "#": df["display_rank"].astype(int),
        "Player": df["name"],
        "Pos": df["pos"],
        "Tm": df["team"],
        "Floor": df["floor"].fillna(NEG),
        "Proj": df["proj_pts"].fillna(NEG),
        "Ceil": df["ceiling"].fillna(NEG),
        "ADP": df["adp"].fillna(POS),
        "Value": df["vob"].fillna(NEG),
        "P(gone)": (df["p_taken"] * 100).round(),
        "VONA": df["vona"].fillna(NEG),
        "Next avail": df["next_available"].fillna("").astype(str),
    })
    S = "small"
    num = st.column_config.NumberColumn
    cfg = {
        "#": num(width=S, pinned=True),
        "Player": st.column_config.TextColumn(width="medium", pinned=True),
        "Pos": st.column_config.TextColumn(width=S),
        "Tm": st.column_config.TextColumn(width=S),
        "Floor": num(width=S, format="%.0f"),
        "Proj": num(width=S, format="%.0f"),
        "Ceil": num(width=S, format="%.0f"),
        "ADP": num(width=S, format="%.1f"),
        "Value": num(width=S, format="%.0f"),
        "P(gone)": st.column_config.ProgressColumn(width=S, format="%.0f%%",
                                                   min_value=0.0, max_value=100.0),
        "VONA": num(width=S, format="%.0f",
                    help="Value Over Next Available: points this player beats the best same-"
                         "position player you'd expect to still get at your NEXT pick (see "
                         "'Next avail'). High = draft-now cliff; low = the position is deep."),
        "Next avail": st.column_config.TextColumn(width="medium",
                    help="Best player at this position expected to still be available at YOUR "
                         "next pick (from survival probabilities). VONA is measured vs. them."),
    }
    # Complete lists can be long -> cap the viewport at ~10 rows and scroll the rest, so the
    # table doesn't push the roster/log sections far down the page.
    height = 38 + min(len(view), 10) * 35
    if on_pick is None:
        st.dataframe(view, hide_index=True, width="stretch", column_config=cfg, height=height)
        return
    # Manual mode: tick a row's checkbox to draft that player. The key carries the tab + pick
    # count so the selection resets after each pick / tab switch, otherwise the persisted
    # selection would re-fire on the next rerun (wrong player).
    st.caption("Tick a row's checkbox (far left) to draft that player.")
    event = st.dataframe(view, hide_index=True, width="stretch", column_config=cfg, height=height,
                         on_select="rerun", selection_mode="single-row", key=key)
    rows = event.selection["rows"]
    if rows:
        on_pick(str(df.iloc[rows[0]]["player_id"]))


def _render_scatter(df: pd.DataFrame, rec_ids: set, ctx=None, lens: str = "Proj",
                    top_n: int = 40, height: int = 400) -> None:
    """Value-vs-cost view: x = ADP (lower = drafted earlier), y = the projection selected by the
    global Value lens (Floor / Proj / Ceil), with a vertical floor->ceiling bar and a horizontal
    ADP +/-1 SD bar per player. Colored by position; recommendations ringed; a dashed line marks
    your next pick. Hover a point to fill the pinned mini-card (top-right, tracks the right edge
    responsively via a Vega `width` expression). Altair -> hover is client-side, no rerun.
    Shows the top-N *available* players in the tab (slides forward as picks come off the board)."""
    import altair as alt

    ycol_by = {"Floor": "floor", "Proj": "proj_pts", "Ceil": "ceiling"}
    ymetric = lens if lens in ycol_by else "Proj"
    ycol = ycol_by[ymetric]

    d = df.head(top_n).copy()
    for c in ("adp", "adp_sd", "proj_pts", "floor", "ceiling", "vona", "vob", "p_taken"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d = d[d["adp"].notna() & d["proj_pts"].notna()].copy()      # need ADP + a projection to place a point
    if d.empty:
        st.caption("Not enough ADP / projection data to chart this view — use Cards or Table.")
        return
    d["floor"] = d["floor"].fillna(d["proj_pts"])
    d["ceiling"] = d["ceiling"].fillna(d["proj_pts"])
    # horizontal error = ADP +/- 1 SD (ffanalytics adp_sd). No sd -> zero-width (point only).
    sd = d["adp_sd"].fillna(0.0).clip(lower=0.0)
    d["adp_lo"] = (d["adp"] - sd).clip(lower=0.5)
    d["adp_hi"] = d["adp"] + sd
    d["pid"] = d["player_id"].astype(str)
    d["is_rec"] = d["pid"].isin(rec_ids)
    d["_size"] = d["is_rec"].map({True: 170, False: 70})
    # pre-rendered label strings for the pinned card (text marks reference columns directly)
    d["l_name"] = [f"{int(r)}. {n}" for r, n in zip(d["display_rank"], d["name"])]
    d["l_pos"] = [f"{p}  ·  {t}  ·  ADP {a:.0f}" + (f" ±{s:.0f}" if pd.notna(s) and s > 0 else "")
                  for p, t, a, s in zip(d["pos"], d["team"].astype(str), d["adp"], d["adp_sd"])]
    d["l_gone"] = [f"P(gone) {g * 100:.0f}%" if pd.notna(g) else "P(gone) —" for g in d["p_taken"]]
    d["l_val"] = [f"VONA {v:.0f}   ·   Val {b:.0f}"
                  for v, b in zip(d["vona"].fillna(0), d["vob"].fillna(0))]
    d["l_proj"] = [f"Floor {f:.0f}  ·  Proj {p:.0f}  ·  Ceil {c:.0f}"
                   for f, p, c in zip(d["floor"], d["proj_pts"], d["ceiling"])]

    pos_scale = alt.Scale(domain=list(POS_COLORS), range=list(POS_COLORS.values()))
    color = alt.Color("pos:N", scale=pos_scale, legend=alt.Legend(title=None, orient="top"))
    # HOVER selection drives the pinned card entirely client-side (no on_select), so it costs no
    # server reruns. (A click-to-consider selection would need on_select="rerun", which reruns on
    # ANY selection change incl. this hover -> laggy; so consideration lives in the picker instead.)
    hover = alt.selection_point(on="pointerover", nearest=True, fields=["pid"],
                                empty=False, clear="pointerout")
    xtitle = "ADP  (lower = drafted earlier)"
    ytitle = f"{ymetric} points"
    x = alt.X("adp:Q", title=xtitle, scale=alt.Scale(zero=False, nice=True))
    y = alt.Y(f"{ycol}:Q", title=ytitle, scale=alt.Scale(zero=False, nice=True))

    vbar = alt.Chart(d).mark_rule(strokeWidth=2, opacity=0.30).encode(
        x=x, y=alt.Y("floor:Q", title=ytitle), y2="ceiling:Q", color=color)
    hbar = alt.Chart(d).mark_rule(strokeWidth=2, opacity=0.22).encode(
        y=y, x=alt.X("adp_lo:Q", title=xtitle), x2="adp_hi:Q", color=color)
    pts = alt.Chart(d).mark_point(filled=True).encode(
        x=x, y=y, color=color, tooltip=alt.value(None),        # no browser tooltip; the pinned card only
        size=alt.condition(hover, alt.value(320), alt.Size("_size:Q", scale=None, legend=None)),
        stroke=alt.condition("datum.is_rec", alt.value("#e8e6ff"), alt.value("transparent")),
        strokeWidth=alt.condition("datum.is_rec", alt.value(1.6), alt.value(0)),
        opacity=alt.condition(hover, alt.value(1.0), alt.value(0.85))).add_params(hover)
    layers = [vbar, hbar, pts]

    my_next = getattr(ctx, "my_next_pick", None) if ctx is not None else None
    if my_next:
        ref = pd.DataFrame({"x": [my_next], "t": [f"your pick #{my_next}"]})
        layers.append(alt.Chart(ref).mark_rule(color="#00e5ff", strokeDash=[4, 4],
                                               opacity=0.7).encode(x="x:Q"))
        layers.append(alt.Chart(ref).mark_text(color="#00e5ff", align="left", dx=4, dy=8,
                      baseline="top", fontSize=10).encode(x="x:Q", y=alt.value(6), text="t:N"))

    # Pinned mini-card (top-right), revealed only while hovering a point (empty=False). Positioned
    # with Vega `width` expressions so it hugs the right edge at ANY container width -- no fixed
    # chart width, so a narrow window no longer clips it.
    cw, ch, pad = 214, 98, 8
    x_left = alt.value({"expr": f"width - {cw + pad}"})
    x_right = alt.value({"expr": f"width - {pad}"})
    x_text = alt.value({"expr": f"width - {cw}"})
    layers.append(alt.Chart(d).mark_rect(cornerRadius=6, fill="#160b28", fillOpacity=0.97,
                  strokeWidth=1.6, strokeOpacity=0.9).encode(
                  x=x_left, x2=x_right, y=alt.value(pad), y2=alt.value(pad + ch),
                  stroke=alt.Color("pos:N", scale=pos_scale, legend=None)).transform_filter(hover))

    def _line(field, dy, col, size, weight="normal"):
        return alt.Chart(d).mark_text(align="left", baseline="top", fontSize=size,
                                      fontWeight=weight, color=col).encode(
            x=x_text, y=alt.value(dy), text=f"{field}:N").transform_filter(hover)

    layers += [
        _line("l_name", pad + 6, "#e8e6ff", 13, "bold"),
        _line("l_pos", pad + 26, "#9a8fc7", 11),
        _line("l_gone", pad + 44, "#00e5ff", 11),
        _line("l_val", pad + 61, "#39ff14", 11),
        _line("l_proj", pad + 78, "#9a8fc7", 10),
    ]

    chart = (alt.layer(*layers)
             .properties(width="container", height=height)
             .configure_view(fill="#0b0714", stroke="#2a1f45")
             .configure_axis(labelColor="#9a8fc7", titleColor="#9a8fc7", gridColor="#241a3d",
                             domainColor="#3a2c5c", tickColor="#3a2c5c",
                             labelFontSize=11, titleFontSize=11)
             .configure_legend(labelColor="#e8e6ff", titleColor="#9a8fc7", labelFontSize=11)
             .configure(background="#0b0714"))
    st.caption(f"Top {top_n} available · y = {ymetric} · hover a point for its card · "
               "ringed = recommended · vertical bar = floor→ceiling · horizontal bar = ADP ±1σ")
    st.altair_chart(chart, use_container_width=True)


def draft_board(board: pd.DataFrame, recs: pd.DataFrame, headshots: dict, top_n: int = 12,
                manual_ctx: dict | None = None, on_pick=None, entry_panel=None, ctx=None,
                lens: str = "Proj") -> None:
    """Unified board: '★ Recommended' (needs-aware picks) or a position (best available by
    value), shown as cards or a table. Only the selected view renders (fast under refresh).

    In Manual mode (manual_ctx set) the pick-entry panel sits at the top and each card gets a
    'Draft' button. In interactive Simulation, `entry_panel(board)` renders the same search +
    Add/Undo/Reset bar while you're on the clock, and `on_pick` wires the board Draft buttons."""
    st.subheader("Draft board")

    if manual_ctx is not None:
        _manual_entry_panel(manual_ctx["order"], manual_ctx["picks"], manual_ctx["teams"])

        def on_pick(player_id: str) -> None:
            from cheddarbowl.draft import manual as _m
            _m.save_manual(manual_ctx["order"], manual_ctx["picks"] + [str(player_id)])
            st.rerun()
    elif entry_panel is not None:
        entry_panel(board)

    c1, c2 = st.columns([5, 1])
    # ★ Recommended = needs-aware; OVR = every position by value; FLEX = RB/WR by value, slotted
    # right after WR (next to its RB/WR neighbors); then the remaining positions.
    _pos = list(config.POSITIONS)
    _tabs = ["★ Recommended", "OVR", *_pos[:_pos.index("WR") + 1], "FLEX", *_pos[_pos.index("WR") + 1:]]
    sel = _sticky_segmented("board", _tabs, "★ Recommended", "board_sel", c1)
    mode = _sticky_segmented("view", ["Cards", "Table", "Chart"], "Cards", "board_view", c2)

    # `full` is the COMPLETE available list for the tab (used by the Table view); `cards` is the
    # top-N slice (used by the Cards view -- kept short so it stays scannable).
    if sel == "★ Recommended":
        st.caption("★ Needs-aware picks across all positions — who to draft now.")
        full = board.copy()                             # every available player, in rec order
        full["display_rank"] = full["rec_rank"].astype(int)
        cards = recs.copy()                             # the engine's top-N shortlist
        cards["display_rank"] = cards["rec_rank"].astype(int)
    else:
        if sel == "OVR":
            st.caption("Best available overall — ranked by value across all positions.")
            pool = board
        elif sel == "FLEX":
            st.caption(f"Best available FLEX ({'/'.join(config.FLEX_ELIGIBLE)}) — ranked by value.")
            pool = board[board["pos"].isin(config.FLEX_ELIGIBLE)]
        else:
            st.caption(f"Best available at **{sel}** — ranked by value, *not* a recommendation.")
            pool = board[board["pos"] == sel]
        full = pool.sort_values("vob", ascending=False).copy()
        full["display_rank"] = range(1, len(full) + 1)
        cards = full.head(top_n)

    if full.empty:
        st.write("None available.")
    elif mode == "Cards":
        _render_cards(cards, headshots, on_pick=on_pick)          # cards: top-N only
    elif mode == "Chart":
        rec_ids = set(recs["player_id"].astype(str)) if recs is not None and not recs.empty else set()
        _render_scatter(full, rec_ids, ctx=ctx, lens=lens)        # scatter: top-40 of the tab
    else:
        table_key = (f"board_table_{sel}_{len(manual_ctx['picks'])}"
                     if manual_ctx is not None else "board_table")
        _render_table(full, on_pick=on_pick, key=table_key)       # table: complete list


def _draft_log(state: dict, teams: dict, players_all: pd.DataFrame) -> None:
    picks = state.get("picks", [])
    if not picks:
        st.write("No picks yet.")
        return
    look = players_all.set_index(players_all["player_id"].astype(str))

    def _get(pid, col):
        pid = str(pid)
        return look.loc[pid, col] if pid in look.index else float("nan")

    src = pd.DataFrame(picks[::-1])
    S = "small"
    # No per-row team avatar here: the log can be up to 180 rows, and an ImageColumn loads one
    # image per row that re-renders on every interaction -- the heaviest widget on the page. Team
    # identity is carried by the text "By" column (name — manager); images stay on the 12 board
    # cards where they earn their cost.
    out = pd.DataFrame({
        "#": src["pick"],
        "Rd": src["round"],
        "By": src["team_id"].map(lambda t: _team_label(teams, t)),
        "Player": src["player"],
        "Pos": src["pos"],
        "Tm": src["nfl_team"],
        "Floor": [_get(p, "floor") for p in src["player_id"]],
        "Proj": [_get(p, "proj_pts") for p in src["player_id"]],
        "Ceil": [_get(p, "ceiling") for p in src["player_id"]],
        "Value": [_value_tag(pk, _get(p, "adp"))
                  for pk, p in zip(src["pick"], src["player_id"])],
    })
    num = st.column_config.NumberColumn
    st.dataframe(
        out, hide_index=True, width="stretch", height=360,
        column_config={
            "#": num(width=S),
            "Rd": num(width=S),
            "By": st.column_config.TextColumn(width="medium"),
            "Player": st.column_config.TextColumn(width="medium"),
            "Pos": st.column_config.TextColumn(width=S),
            "Tm": st.column_config.TextColumn(width=S),
            "Floor": num(width=S, format="%.0f"),
            "Proj": num(width=S, format="%.0f"),
            "Ceil": num(width=S, format="%.0f"),
            "Value": st.column_config.TextColumn("Value", width="medium",
                    help="Reach/steal vs ADP: drafted earlier than ADP = reach, later = steal "
                         "(number = spots off ADP)."),
        },
    )


def _roster_table(recs: list[dict], slot: bool) -> None:
    def _s(x):
        return "" if x is None or pd.isna(x) else f"{x:.0f}"

    rows = []
    for r in recs:
        row = {"Player": r.get("name") or "—", "Pos": r.get("pos", ""), "Tm": r.get("team", ""),
               "Floor": _s(r.get("floor")), "Proj": round(r.get("proj_pts", 0) or 0),
               "Ceil": _s(r.get("ceiling")), "ADP": _s(r.get("adp")), "Via": r.get("note", "")}
        if slot:
            row = {"Slot": r["slot"], **row}
        rows.append({**row, "_proj": bool(r.get("proj", False))})
    df = pd.DataFrame(rows)
    proj = df["_proj"].tolist()
    disp = df.drop(columns=["_proj"])
    # dim projected rows (muted text) across every column
    styled = disp.style.apply(
        lambda col: [f"color:{'#9a8fc7' if proj[i] else ''}" for i in range(len(col))], axis=0)
    S = "small"
    st.dataframe(styled, hide_index=True, width="stretch", column_config={
        "Slot": st.column_config.TextColumn(width=S),
        "Player": st.column_config.TextColumn(width="medium"),
        "Pos": st.column_config.TextColumn(width=S),
        "Tm": st.column_config.TextColumn(width=S),
        "Floor": st.column_config.TextColumn(width=S),
        "Proj": st.column_config.NumberColumn(width=S, format="%.0f"),
        "Ceil": st.column_config.TextColumn(width=S),
        "ADP": st.column_config.TextColumn(width=S),
        "Via": st.column_config.TextColumn(width="medium"),
    })


def _team_roster(team_id: int, state: dict, teams: dict, players_all: pd.DataFrame,
                 board: pd.DataFrame, ctx, chaos: float = 1.0, pin_first=None) -> None:
    from cheddarbowl.draft.forecast import forecast_team
    from cheddarbowl.lib.rosters import optimal_lineup, starter_points, team_players

    t = teams.get(team_id, {})
    look = players_all.set_index(players_all["player_id"].astype(str))

    tp = team_players(state, team_id, players_all)
    tp = tp.assign(proj=False, note=tp["pick"].map(
        lambda pk: f"R{(int(pk) - 1) // config.NUM_TEAMS + 1} · #{int(pk)}"))
    drafted = tp[["player_id", "name", "pos", "team", "proj_pts", "proj", "note"]]

    def _lineup(pin):
        """Drafted players + the follow-the-recommendations forecast (optionally pinning `pin` at
        the first future pick) -> (starters, bench) via optimal_lineup."""
        df = drafted
        proj = forecast_team(board, state, ctx, team_id, chaos=chaos, pin_first=pin)
        if proj:
            pdf = pd.DataFrame(proj).assign(proj=True)
            pdf["note"] = pdf.apply(lambda r: f"proj R{r['round']} · {r['p_survive']*100:.0f}%", axis=1)
            df = pd.concat([drafted, pdf[["player_id", "name", "pos", "team", "proj_pts", "proj", "note"]]],
                           ignore_index=True)
        pid_s = df["player_id"].astype(str)   # join ADP/floor/ceiling for the roster columns
        df = df.assign(**{c: pid_s.map(lambda p, _c=c: look.loc[p, _c] if p in look.index else float("nan"))
                          for c in ("adp", "floor", "ceiling")})
        return optimal_lineup(df)

    starters, bench = _lineup(pin_first)
    hc, tc = st.columns([1, 8], vertical_alignment="center")
    with hc:
        if t.get("avatar_url"):
            st.image(t["avatar_url"], width=56)
    with tc:
        st.markdown(f"**{t.get('team_name', '')}** — {t.get('manager', '')} &nbsp;·&nbsp; "
                    f"best-case projected starter points: **{starter_points(starters):.0f}**")

    # What-if banner: how the considered pick moves best-case starter points vs. the baseline path.
    if pin_first is not None:
        pinname = look.loc[str(pin_first), "name"] if str(pin_first) in look.index else str(pin_first)
        base_starters, _ = _lineup(None)
        wpts, bpts = starter_points(starters), starter_points(base_starters)
        delta = wpts - bpts
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        st.info(f"🔮 **What-if:** taking **{pinname}** at your next pick → best-case starter points "
                f"**{wpts:.0f}** vs baseline **{bpts:.0f}**  ({arrow} {delta:+.0f})")

    _roster_table(starters, slot=True)
    if bench:
        st.caption("Bench")
        _roster_table(bench, slot=False)


def _consider_control(board: pd.DataFrame) -> "str | None":
    """Searchable 'what-if' picker (my-team forecast only): choose an available player to consider
    at your next pick. Selecting '— none —' clears it. Returns the chosen player_id, or None.

    The widget owns its state via key='consider_select'; a board card's '🔮 Consider' button feeds
    the same state via a '_consider_request' that team_forecast drains before this is built (a plain
    button, so it can't fight the scatter's hover-card the way an Altair click selection would)."""
    if board is None or board.empty:
        st.caption("🔮 What-if picker appears once the board loads.")
        return None
    ids = [None] + board["player_id"].astype(str).tolist()
    lab = {str(pid): f"{n} · {p}-{tm}" for pid, n, p, tm
           in zip(board["player_id"], board["name"], board["pos"], board["team"])}
    if st.session_state.get("consider_select") not in ids:
        st.session_state["consider_select"] = None
    sel = st.selectbox(
        "🔮 What-if — consider a player at your next pick (follows recommendations after)",
        ids, key="consider_select",
        format_func=lambda p: "— none (baseline) —" if p is None else lab.get(str(p), str(p)))
    return sel


def team_forecast(state: dict, teams: dict, players_all: pd.DataFrame,
                  board: pd.DataFrame, ctx, chaos: float = 1.0) -> None:
    """Team-by-team best-case roster forecast, shown above the board. Team-name tabs, defaulting to
    your team; forecast-only (the Current sub-tab was removed -- the forecast already includes each
    team's drafted players, with projected future picks dimmed). My team's tab also gets the
    'what-if' picker (pin a player at your next pick and see the downstream roster re-flow)."""
    st.subheader("Team forecast")
    # Drain a pending "Consider" request (from a board card, rendered below this) into the picker's
    # widget state BEFORE the picker is built -- setting a widget key after it exists is illegal.
    if "_consider_request" in st.session_state:
        st.session_state["consider_select"] = st.session_state.pop("_consider_request") or None

    opts, tid_by_label = [], {}
    for tid, t in sorted(teams.items()):
        mgr = t.get("manager") or f"Team {tid}"
        label = f"★ {mgr}" if ctx.is_me(tid) else mgr
        opts.append(label)
        tid_by_label[label] = tid
    if not opts:
        st.caption("Waiting for the team list…")
        return
    my_label = next((lab for lab, tid in tid_by_label.items() if ctx.is_me(tid)), opts[0])
    sel = _sticky_segmented("teamsel", opts, my_label, "team_sel")
    tid = tid_by_label[sel]
    pin = _consider_control(board) if ctx.is_me(tid) else None
    _team_roster(tid, state, teams, players_all, board, ctx, chaos, pin_first=pin)


def _live_edit_panel(state: dict, teams: dict) -> None:
    """Live-mode manual override: correct a pick the tracker missed or mis-recorded WITHOUT leaving
    Live. The fix is stored in pick_overrides.json; the scraper applies it on its next write (~sec)
    and keeps it. Uses the ESPN-keyed player pool so a chosen player joins the board correctly."""
    from cheddarbowl.draft import manual, pick_overrides

    with st.expander("✎ Fix a pick (manual override)"):
        st.caption("Missed or wrong pick? Set it here and stay in Live — the scraper applies your fix "
                   "on its next update and keeps it. Clear a fix any time.")
        total = config.NUM_TEAMS * config.NUM_ROUNDS
        order = [int(t) for t in (state.get("draft_order") or [])]
        cur_total = int(state.get("total_picks", 0))

        c1, c2 = st.columns([1, 3])
        overall = int(c1.number_input("Overall pick #", min_value=1, max_value=total,
                                      value=min(cur_total + 1, total), step=1, key="ovr_pick"))
        default_team = manual._team_for(overall, order, config.NUM_TEAMS)   # snake owner of that pick
        tids = sorted(teams) or list(range(1, config.NUM_TEAMS + 1))
        tlabels = {tid: (teams.get(tid, {}).get("manager") or f"Team {tid}") for tid in tids}
        tidx = tids.index(default_team) if default_team in tids else 0
        team = c2.selectbox("Team", tids, index=tidx,
                            format_func=lambda t: f"{t} · {tlabels[t]}", key="ovr_team")

        avail = manual.all_players().sort_values(["pos", "name"])
        labels = [f"{r.name} · {r.pos}-{r.nfl_team}" for r in avail.itertuples(index=False)]
        id_by_label = dict(zip(labels, avail["player_id"].astype(str)))
        chosen = st.selectbox("Player drafted", labels, index=None,
                              placeholder="Search a player…", key="ovr_player")
        if st.button("Save fix", type="primary", disabled=chosen is None, key="ovr_save"):
            pick_overrides.set_override(overall, int(team), id_by_label[chosen])
            st.rerun()

        existing = pick_overrides.load()
        if existing:
            st.caption("Active fixes (these override the scraper):")
            pinfo = manual.player_info()
            for ov in sorted(existing):
                o = existing[ov]
                nm = pinfo.get(str(o["player_id"]), {}).get("name", o["player_id"])
                r1, r2 = st.columns([6, 1])
                r1.write(f"**#{ov}** → {nm} · {tlabels.get(o['team_id'], 'Team ' + str(o['team_id']))}")
                if r2.button("✕", key=f"ovr_rm_{ov}", help="Remove this fix"):
                    pick_overrides.remove_override(ov)
                    st.rerun()


# ------------------------------------------------------------------ main
def render(state: dict, chaos: float = 1.0, manual_ctx: dict | None = None,
           transport=None, on_pick=None, entry_panel=None, editable: bool = False,
           lens: str = "Proj", my_team: int | None = None, host: bool = True) -> None:
    eng = get_engine()
    ctx, board, recs = eng.evaluate(state, top_n=12, lens=lens, team_id=my_team)
    headshots = get_headshots()
    teams = get_teams()

    _sticky_header(ctx, teams)          # pinned: CHEDDARBOWL + compressed draft flow
    if transport is not None:           # Simulation: playback controls under the header
        transport(ctx)
    if editable and host:               # Live: "Fix a pick" (host only) at the top, below the header
        st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)   # padding above it
        _live_edit_panel(state, teams)
    team_forecast(state, teams, eng.players, board, ctx, chaos)   # team tabs + best-case forecast
    # manual_ctx -> Manual entry panel; entry_panel/on_pick -> interactive-sim search bar + board
    draft_board(board, recs, headshots, manual_ctx=manual_ctx, on_pick=on_pick,
                entry_panel=entry_panel, ctx=ctx, lens=lens)
    st.divider()
    st.subheader("Draft log")
    _draft_log(state, teams, eng.players)   # just the log, at the bottom of the page


# ------------------------------------------------------------------ manual mode
def _manual_order_setup(teams: dict) -> None:
    """Drag-to-order gate: set slot 1..N -> manager, then confirm to start entering picks."""
    from streamlit_sortables import sort_items

    from cheddarbowl.draft import manual

    st.subheader("Set the draft order")
    if not teams:
        st.warning("No team list found. Start the scraper once (Live mode) to fetch team names, "
                   "or add data/teams.csv.")
        return
    st.caption("Drag each manager into the real draft-slot order (slot 1 at the top), then "
               "confirm. You can re-open this later if the live order differs.")
    items = [f"{tid} · {teams.get(tid, {}).get('manager') or f'Team {tid}'}"
             for tid in sorted(teams)]
    # CSS is injected into the component's iframe (parent CSS can't reach it): palette purple
    # by default, magenta on hover -- matching the primary buttons.
    sortable_css = (
        ".sortable-item{background-color:#160b28 !important;border:1px solid #ff2bd6 !important;"
        "color:#ff2bd6 !important;}"
        ".sortable-item:hover{background-color:#ff2bd6 !important;border-color:#ff2bd6 !important;"
        "color:#0b0714 !important;box-shadow:0 0 14px rgba(255,43,214,.5) !important;}")
    ordered = sort_items(items, direction="vertical", key="manual_order_sortable",
                         custom_style=sortable_css)
    st.caption("Order top→bottom = draft slots 1→N.  ★ = your team (slot "
               f"{config.DRAFT_SLOT}).")
    if st.button("✅ Confirm order & start drafting", type="primary"):
        order = [int(x.split(" · ", 1)[0]) for x in ordered]
        manual.save_manual(order, manual.load_manual()["picks"])
        st.rerun()


def _manual_entry_panel(order: list, picks: list, teams: dict) -> None:
    """Player picker for the current pick, plus Undo / Reset / Reorder. (Whose pick it is and
    the pick number are already shown in the sticky header's flow strip, so no banner here.)"""
    from cheddarbowl.draft import manual

    nxt = len(picks) + 1
    total = config.NUM_TEAMS * config.NUM_ROUNDS

    def _undo(col):
        if col.button("↩ Undo", width="stretch", disabled=not picks):
            manual.save_manual(order, picks[:-1])
            st.rerun()

    def _reset(col):
        # popover = built-in two-click confirmation, so a stray click can't wipe the draft
        with col.popover("🗑 Reset", width="stretch", disabled=not picks):
            st.caption(f"Clear all {len(picks)} entered picks and start the draft over? "
                       "The team order is kept.")
            if st.button("Yes, reset the draft", type="primary", width="stretch"):
                manual.save_manual(order, [])
                st.rerun()

    def _reorder(col):
        if col.button("⚙ Reorder", width="stretch", help="Rearrange teams (keeps entered picks)"):
            manual.save_manual([], picks)
            st.rerun()

    if nxt > total:
        st.success("Draft complete — all picks entered.")
        _, c3, c4, c5 = st.columns([6, 1, 1.2, 1.4])
        _undo(c3)
        _reset(c4)
        _reorder(c5)
        return

    avail = manual.available_players(picks)
    labels = [f"{r.name}  ·  {r.pos}-{r.nfl_team}" for r in avail.itertuples(index=False)]
    id_by_label = {lab: r.player_id for lab, r in zip(labels, avail.itertuples(index=False))}
    c1, c2, c3, c4, c5 = st.columns([5, 1, 1, 1.2, 1.4], vertical_alignment="bottom")
    choice = c1.selectbox("Player drafted", labels, index=None,
                          placeholder="Search a player…", key=f"manual_pick_{nxt}")
    if c2.button("➕ Add", width="stretch", disabled=choice is None):
        manual.save_manual(order, picks + [id_by_label[choice]])
        st.rerun()
    _undo(c3)
    _reset(c4)
    _reorder(c5)


def _publish_shared_state(state: dict) -> None:
    """Host-side: write the authoritative state to the shared draft_state.json so LAN viewers (who
    always read that file in Live) see the host's Manual fallback in real time. Atomic write with a
    Windows retry (os.replace throws WinError 5 if a reader holds the file open)."""
    import json
    import tempfile
    import time
    try:
        d = os.path.dirname(config.DRAFT_STATE_JSON)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        for _ in range(5):
            try:
                os.replace(tmp, config.DRAFT_STATE_JSON)
                return
            except OSError:
                time.sleep(0.1)
        try:
            os.remove(tmp)
        except OSError:
            pass
    except OSError:
        pass


def render_manual(ui: dict) -> None:
    from cheddarbowl.draft import manual

    teams = get_teams()
    md = manual.load_manual()
    if not md["order"]:                       # not configured yet -> ordering gate
        _sticky_header()                       # title only (no draft loaded yet)
        _manual_order_setup(teams)
        return
    # The pick-entry panel now lives at the top of the Draft board section (see draft_board),
    # and each board card gets a Draft button -- both driven by this manual context.
    state = manual.build_state(md["order"], md["picks"])
    _publish_shared_state(state)   # so LAN viewers (reading draft_state.json) see manual fallback
    render(state, ui["chaos"], lens=ui["lens"], my_team=ui["my_team"], host=ui["host"],
           manual_ctx={"order": md["order"], "picks": md["picks"], "teams": teams})


# ------------------------------------------------------------------ live failsafe
def _switch_live_to_manual() -> None:
    """Seed Manual mode from the last good scraped state, then flip the Mode radio."""
    from cheddarbowl.draft import manual

    live = load_draft_state()
    order = live.get("draft_order") or []
    if len(order) != config.NUM_TEAMS:
        order = []                            # incomplete -> user sets it in the ordering gate
    manual.save_manual(order, [str(p["player_id"]) for p in live.get("picks", [])])
    # Can't set the `mode` widget's state after it's instantiated this run; stash it and let
    # sidebar() apply it before the radio is built next run.
    st.session_state["_pending_mode"] = "Manual"


def _failsafe_banner() -> None:
    """If the scraper looks down/erroring, prompt (don't force) a switch to Manual."""
    from cheddarbowl.ingest import runner

    h = runner.health()
    if h["state"] in ("running", "preseason"):   # healthy (preseason = pre-draft, board blank)
        return
    with st.container(border=True):
        msg = {"down": "The scraper hasn’t started or has no heartbeat.",
               "stale": "The scraper’s heartbeat went stale — it may have stopped.",
               "error": f"The scraper is erroring: {h.get('error') or 'unknown error'}."}
        st.warning("⚠️ " + msg.get(h["state"], "Scraper status unknown.")
                   + "  Switch to Manual to keep drafting (picks so far are carried over).")
        c1, c2 = st.columns([1, 3])
        if c1.button("Switch to Manual", type="primary", key="failsafe_switch"):
            _switch_live_to_manual()
            st.rerun()
        c2.caption("Or leave it on Live and it’ll recover on its own if the scraper comes back.")


# ------------------------------------------------------------------ main
def _live_signature(state: dict) -> tuple:
    """Cheap 'did the draft change?' fingerprint for the Live-mode watcher: total picks + each
    pick's (overall#, team, player) + the draft order. When this is unchanged there's nothing new
    to show, so the watcher skips the heavy full re-render. Kept self-contained here (not borrowed
    from Engine) so a Streamlit hot-reload of app.py alone can't break it."""
    picks = tuple((p.get("pick"), p.get("team_id"), str(p.get("player_id")))
                  for p in state.get("picks", []))
    return (int(state.get("total_picks", 0)), tuple(state.get("draft_order") or ()), picks)


def main() -> None:
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
    st.markdown(_rec_css(), unsafe_allow_html=True)
    ui = sidebar()
    mode = ui["mode"]

    if mode == "Simulation":
        render_simulation(ui)
    elif mode == "Manual":
        render_manual(ui)
    else:  # Live: auto-start the scraper, then render ONCE and re-render only on real change.
        from cheddarbowl.ingest import runner
        if ui["host"]:
            runner.ensure_running()   # host owns the scraper; viewers just read shared state

        # Render the current draft once (in the main script body, not a timer fragment). What made
        # Live mode feel sluggish was re-rendering this whole page -- 12 rec cards + the up-to-180-row
        # draft-log dataframe (with avatar images) + rosters -- every few seconds even when nothing
        # changed. That constant DOM rebuild grew heavier as the UI did. Now the heavy render happens
        # only when the draft actually advances.
        state = load_draft_state()
        sig = (_live_signature(state), runner.health().get("state"))
        st.session_state["_live_sig"] = sig
        if ui["host"]:
            _failsafe_banner()        # only the host can switch to Manual / fix picks
        render(state, ui["chaos"], editable=True, lens=ui["lens"],
               my_team=ui["my_team"], host=ui["host"])

        # A cheap watcher fragment reruns on the timer, but only reads the tiny state file + health
        # heartbeat; it triggers a full-app rerun (re-rendering everything ONCE) exactly when a new
        # pick lands or the scraper's health changes. Between picks it does almost nothing, so the
        # board/log/rosters aren't rebuilt on every tick. (On-the-clock pulsing is CSS -> unaffected.)
        @st.fragment(run_every=config.POLL_SECONDS)
        def _watch():
            now = (_live_signature(load_draft_state()), runner.health().get("state"))
            if st.session_state.get("_live_sig") != now:
                st.session_state["_live_sig"] = now
                st.rerun(scope="app")
        _watch()


if __name__ == "__main__":
    main()
