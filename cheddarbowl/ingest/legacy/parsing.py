"""HTML parsing helpers for NFL.com fantasy pages.

The player-row markup is identical on the roster page and the players page, so the
player-link parser lives here and is reused by both. Everything keys on the stable
NFL ``playerId``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

# NFL.com position labels -> our canonical set (see config.POSITIONS)
_POS_MAP = {"DEF": "DST", "DST": "DST", "D/ST": "DST"}

_ID_CLASS_RE = re.compile(r"playerNameId-(\d+)")
_ID_HREF_RE = re.compile(r"playerId=(\d+)")
_POS_TEAM_RE = re.compile(r"^\s*([A-Za-z/]+)\s*-\s*([A-Za-z]+)\s*$")
_POS_ONLY_RE = re.compile(r"^\s*([A-Za-z/]+)\s*$")
_CTEAM_RE = re.compile(r"^c-([a-z]{2,4})$")


def norm_pos(pos: str) -> str:
    return _POS_MAP.get(pos.upper(), pos.upper())


def _team_from_wrap(a: Tag) -> str:
    """NFL team abbrev from the wrapping ``div.c.c-<team>`` class (authoritative)."""
    wrap = a.parent
    if wrap:
        for cls in wrap.get("class", []):
            m = _CTEAM_RE.match(cls)
            if m and m.group(1) != "fa":  # c-fa == free agent / no team
                return m.group(1).upper()
    return ""


@dataclass(frozen=True)
class PlayerRef:
    player_id: str
    name: str
    pos: str
    nfl_team: str  # e.g. "PHI"; "" if unknown (rare)

    def as_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "player": self.name,
            "pos": self.pos,
            "nfl_team": self.nfl_team,
        }


def _player_id_from_anchor(a: Tag) -> str | None:
    """Extract playerId from an ``a.playerName`` anchor via its class or href."""
    for cls in a.get("class", []):
        m = _ID_CLASS_RE.match(cls)
        if m:
            return m.group(1)
    href = a.get("href", "")
    m = _ID_HREF_RE.search(href)
    return m.group(1) if m else None


def parse_player_anchor(a: Tag) -> PlayerRef | None:
    """Turn an ``a.playerName`` anchor into a PlayerRef using its sibling ``<em>``.

    Markup shape (roster + players pages):
        <div class="c c-phi">
          <a class="... playerName ... playerNameId-2564832 ..." href="...playerId=2564832">Jalen Hurts</a>
          <em>QB - PHI</em>
        </div>
    """
    pid = _player_id_from_anchor(a)
    if not pid:
        return None
    name = a.get_text(strip=True)
    pos = ""
    team = _team_from_wrap(a)  # from div.c-<team>; works for offense + DEF
    em = a.find_next_sibling("em") or (a.parent.find("em") if a.parent else None)
    if em:
        txt = em.get_text(strip=True)
        m = _POS_TEAM_RE.match(txt)  # "QB - PHI"
        if m:
            pos = norm_pos(m.group(1))
            team = team or m.group(2).upper()
        else:
            m2 = _POS_ONLY_RE.match(txt)  # "DEF"  (team defenses)
            if m2:
                pos = norm_pos(m2.group(1))
    return PlayerRef(player_id=pid, name=name, pos=pos, nfl_team=team)


@dataclass(frozen=True)
class RosterEntry:
    slot: str  # roster slot label: QB/RB/WR/TE/W-R-T (flex)/K/DEF/BN/IR
    player: PlayerRef


def parse_roster(html: str) -> list[RosterEntry]:
    """Parse a team page (``/league/{id}/team/{n}``) into its roster entries.

    Only rows that actually contain a player are returned (empty draft slots are skipped),
    so ``len(parse_roster(...))`` equals that team's pick count during a draft.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[RosterEntry] = []
    for tr in soup.select("tr"):
        a = tr.find("a", class_="playerName")
        if not a:
            continue
        ref = parse_player_anchor(a)
        if not ref:
            continue
        slot_cell = tr.find("td", class_="teamPosition")
        slot = slot_cell.get_text(strip=True) if slot_cell else ""
        entries.append(RosterEntry(slot=slot, player=ref))
    return entries


@dataclass(frozen=True)
class OwnerInfo:
    team_id: int
    team_name: str
    manager: str
    co_manager: str = ""
    avatar_url: str = ""


def _upscale_avatar(src: str) -> str:
    """Request a crisper team avatar than the 40x40 thumbnail on the owners page."""
    return re.sub(r"([xy])=\d+", r"\1=120", src) if src else src


def parse_owners(html: str) -> list[OwnerInfo]:
    """Parse the owners page: team_id, team_name, manager, co-manager, avatar URL."""
    soup = BeautifulSoup(html, "lxml")
    owners: dict[int, OwnerInfo] = {}
    for a in soup.select("a.teamName"):
        m = re.search(r"/team/(\d+)", a.get("href", ""))
        if not m:
            continue
        team_id = int(m.group(1))
        if team_id in owners:  # teamImg + teamName both link to /team/N
            continue
        row = a.find_parent("tr")
        mgr_cell = row.find("td", class_="teamOwnerName") if row else None
        co_cell = row.find("td", class_="teamCoManagerName") if row else None
        img = row.select_one("a.teamImg img") if row else None

        def _name(cell):
            el = cell.find("span", class_="userName") if cell else None
            return el.get_text(strip=True) if el else ""

        owners[team_id] = OwnerInfo(
            team_id=team_id,
            team_name=a.get_text(strip=True),
            manager=_name(mgr_cell),
            co_manager=_name(co_cell),
            avatar_url=_upscale_avatar(img.get("src", "")) if img else "",
        )
    return sorted(owners.values(), key=lambda o: o.team_id)


@dataclass(frozen=True)
class DraftResultPick:
    pick: int          # overall pick number (1-based), read straight off the page
    team_id: int       # destination fantasy team
    player: PlayerRef


_COUNT_RE = re.compile(r"(\d+)")
_TEAMID_CLASS_RE = re.compile(r"teamId-(\d+)")
_TEAMID_HREF_RE = re.compile(r"/team/(\d+)")


def parse_draft_results(html: str) -> "list[DraftResultPick] | None":
    """Parse the server-rendered draft-results page into ordered picks.

    Returns ``None`` if the HTML isn't the draft-results page at all (login wall, error,
    layout change) so the caller can fall back to roster-diffing. Returns a (possibly empty)
    list if it *is* the results page -- an empty list legitimately means "no picks yet".

    Each pick row:
        <li><span class="count">1.</span> ... <a class="playerName playerNameId-2565941">Ja'Marr Chase</a>
            <em>WR - CIN</em> <span class="to">To</span> <a class="teamImg teamId-6" href="/league/.../team/6">…</a></li>
    """
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one("#leagueDraftResults, .draftResults, .draftResultNav") is None:
        return None  # not the draft-results page -> signal the caller to use the fallback
    results = soup.select_one("#leagueDraftResults, .draftResults") or soup

    picks: list[DraftResultPick] = []
    for li in results.select("li"):
        count = li.find("span", class_="count")
        a = li.find("a", class_="playerName")
        if not count or not a:
            continue  # manager-name / non-pick <li>
        m = _COUNT_RE.search(count.get_text())
        ref = parse_player_anchor(a)
        if not m or not ref:
            continue
        team_id = None
        for t in li.find_all("a"):
            classes = " ".join(t.get("class", []))
            cm = _TEAMID_CLASS_RE.search(classes) or _TEAMID_HREF_RE.search(t.get("href", ""))
            if cm:
                team_id = int(cm.group(1))
                break
        if team_id is None:
            continue
        picks.append(DraftResultPick(pick=int(m.group(1)), team_id=team_id, player=ref))

    picks.sort(key=lambda p: p.pick)
    return picks


def parse_players_list(html: str) -> list[PlayerRef]:
    """Parse a players-list page into PlayerRefs (used to build the crosswalk)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[PlayerRef] = []
    for a in soup.select("a.playerName"):
        ref = parse_player_anchor(a)
        if ref:
            out.append(ref)
    return out


def parse_headshot(html: str) -> str | None:
    """Extract a player's headshot (or DST logo) URL from their card page HTML.

    Card page: /players/card?leagueId={id}&playerId={pid}. The image is served from
    NFL's CDN keyed by an opaque id, so we resolve it here rather than build a URL.
    """
    soup = BeautifulSoup(html, "lxml")
    img = soup.find("img", class_=re.compile("headshot", re.I))
    if img and (img.get("src") or img.get("data-src")):
        return img.get("src") or img.get("data-src")
    m = re.search(r'headshot[^>]*?\bsrc=["\']([^"\']+)', html, re.I)
    return m.group(1) if m else None


def players_total_count(html: str) -> int | None:
    """Read the '1 - 25 of 872' pagination title to know how many pages to fetch."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(class_="paginationTitle")
    if not el:
        return None
    m = re.search(r"of\s+([\d,]+)", el.get_text())
    return int(m.group(1).replace(",", "")) if m else None
