"""
OPL Scout – gemeinsame Logik: OPLeague auslesen, HenrikDev-API abfragen, Cache.
Wird von build.py (GitHub Action / lokal) benutzt. Nur Python-Standardbibliothek.
"""
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_URL = ("https://www.opleague.pro/event/8285-UniligaVALORANTWinterseason202627-"
               "Gruppenphase-3Liga/standing")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
HENRIK_BASE = "https://api.henrikdev.xyz"
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / ".cache.json"



def load_env(path):
    """Liest KEY=VALUE-Zeilen aus .env; bereits gesetzte Umgebungsvariablen haben Vorrang."""
    placeholders = ("", "HDEV-...", "HDEV-dein-key")
    if os.environ.get("HENRIKDEV_API_KEY", "").strip() in placeholders:
        os.environ.pop("HENRIKDEV_API_KEY", None)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
    # Platzhalter aus der Vorlage nicht als Key werten
    if os.environ.get("HENRIKDEV_API_KEY", "").strip() in placeholders:
        os.environ.pop("HENRIKDEV_API_KEY", None)


load_env(HERE / ".env")

# Cache-Dauer (per .env überschreibbar, Angaben in Stunden). Manuell aktualisieren geht jederzeit per Button.
_h = lambda name, default: float(os.environ.get(name, default)) * 3600
OPL_TTL = _h("STANDINGS_TTL_HOURS", 1)      # Standings-Tabelle
TEAM_TTL = _h("TEAM_TTL_HOURS", 24 * 7)     # Kader + Riot-IDs
STATS_TTL = _h("STATS_TTL_HOURS", 24 * 7)   # Spieler-Stats


# --------------------------------------------------------------------------- cache
class Cache:
    def __init__(self, path):
        self.path = path
        self.autosave = True
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}

    def get(self, key, ttl):
        with self.lock:
            e = self.data.get(key)
        if e and time.time() - e["t"] < ttl:
            v = e["v"]
            if isinstance(v, dict) and "updated" not in v:
                v["updated"] = e["t"]
            return v
        return None

    def peek(self, key):
        """Wert unabhängig vom Alter (für Übersichten)."""
        with self.lock:
            e = self.data.get(key)
        return e["v"] if e else None

    def set(self, key, value):
        with self.lock:
            self.data[key] = {"t": time.time(), "v": value}
        if self.autosave:
            self.save()

    def age(self, key):
        with self.lock:
            e = self.data.get(key)
        return time.time() - e["t"] if e else None

    def save(self):
        with self.lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, separators=(",", ":")))
                tmp.replace(self.path)  # atomar: nie eine halb geschriebene Datei
            except Exception as e:
                print("Cache speichern fehlgeschlagen:", e)


cache = Cache(CACHE_FILE)


def use_cache(path, autosave=True):
    """Anderen Cache-Pfad verwenden (z. B. im data-Branch der GitHub Action)."""
    global cache
    cache = Cache(Path(path))
    cache.autosave = autosave
    return cache


# --------------------------------------------------------------------------- http
def fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# --------------------------------------------------------------------------- OPLeague
def opl_standings(url, refresh=False):
    key = "standing:" + url
    hit = None if refresh else cache.get(key, OPL_TTL)
    if hit:
        return hit
    page = fetch(url)
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    title = text(title.group(1)).split("|")[0].replace("Standing - ", "").strip() if title else ""
    m = re.search(r'<table class="event-ranking__container.*?</table>', page, re.S)
    if not m:
        # Gruppen-Ansicht (z. B. 1./2. Liga): Daten liegen als JSON in der Seite
        j = re.search(r'var data = (\{"standing_overview".*?\});?\s*</script>', page, re.S)
        if not j:
            raise RuntimeError("Standing-Tabelle nicht gefunden")
        res = {"title": title, "url": url, **parse_group_overview(json.loads(j.group(1)))}
        cache.set(key, res)
        return res
    table = m.group(0)
    headers = [text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    teams = []
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
        link = re.search(r'href="(https://www\.opleague\.pro/team/(\d+)-[^"]*)"\s+title="([^"]*)"', row)
        if not link:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        stripe = re.search(r'indicator-stripe\s+--(\w+)', row)
        logo = re.search(r'<img src="([^"]+)"', row)
        vals = [text(c) for c in cells]
        stats = {}
        for i, h in enumerate(headers):
            if h and i < len(vals):
                stats[h] = vals[i]
        teams.append({
            "group": "",
            "id": link.group(2),
            "url": link.group(1),
            "name": html.unescape(link.group(3)),
            "rank": vals[0].rstrip(".") if vals else "",
            "zone": stripe.group(1) if stripe else "",
            "logo": logo.group(1) if logo else "",
            "stats": stats,
        })
    res = {"title": title, "url": url, "columns": [h for h in headers if h], "groups": [], "teams": teams}
    cache.set(key, res)
    return res


GROUP_COLUMNS = ["played", "wins", "draws", "losses", "points", "round dif"]


def parse_group_overview(data):
    teams, groups = [], []
    for g in data.get("standing_overview") or []:
        gname = (g.get("event_info") or {}).get("name", "")
        gm = re.search(r"Gruppe\s+(\S+)\s*$", gname) or re.search(r"Group\s+(\S+)\s*$", gname)
        group = gm.group(1) if gm else str(len(groups) + 1)
        groups.append(group)
        for it in (g.get("standing") or {}).get("list") or []:
            ti, st = it.get("team_info") or {}, it.get("standing") or {}
            logos = ti.get("logo_array") or {}
            logo = next((logos[k] for k in ("30", "32", "50", "16") if isinstance(logos.get(k), str)), "")
            teams.append({
                "group": group,
                "id": str(ti.get("ID") or ti.get("identifier")),
                "url": ti.get("url") or f"https://www.opleague.pro/team/{ti.get('ID')}",
                "name": ti.get("name") or ti.get("title") or "",
                "rank": str(it.get("place", "")),
                "zone": (it.get("color") or "").lstrip("-"),
                "logo": logo,
                "stats": {
                    "played": st.get("played"), "wins": st.get("wins_total", st.get("wins")),
                    "draws": st.get("draws"), "losses": st.get("loss_total", st.get("loss")),
                    "points": st.get("points"), "round dif": st.get("rounds_dif"),
                },
            })
    return {"columns": GROUP_COLUMNS, "groups": groups, "teams": teams}


def opl_riot_id(user_url, refresh=False):
    key = "riot:" + user_url
    hit = None if refresh else cache.get(key, TEAM_TTL)
    if hit is not None:
        return hit
    page = fetch(user_url)
    m = re.search(r"<td>Riot ID \(Valorant\)</td>\s*<td[^>]*>(.*?)</td>", page, re.S)
    rid = text(m.group(1)) if m else ""
    cache.set(key, rid)
    return rid


def opl_team(team_id, refresh=False, refresh_ids=None):
    if refresh_ids is None:
        refresh_ids = refresh
    key = f"team:{team_id}"
    hit = None if refresh else cache.get(key, TEAM_TTL)
    if hit:
        return hit
    page = fetch(f"https://www.opleague.pro/team/{team_id}")
    name = re.search(r"<title>(.*?) - Team Profile", page)
    m = re.search(r'<table[^>]*id="team_members".*?</table>', page, re.S)
    members = []
    if m:
        for row in re.findall(r"<tr sort-row>(.*?)</tr>", m.group(0), re.S):
            link = re.search(r'href="(https://www\.opleague\.pro/user/\d+-[^"]*)"', row)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if not link or not cells:
                continue
            members.append({
                "opl_name": text(cells[0]),
                "opl_url": link.group(1),
                "position": text(cells[1]) if len(cells) > 1 else "",
            })
    with ThreadPoolExecutor(6) as ex:
        for mem, rid in zip(members, ex.map(lambda x: opl_riot_id(x["opl_url"], refresh_ids), members)):
            mem["riot_id"] = rid
    order = {"Captain": 0, "Player": 1, "Substitute": 2, "Stand-In": 2, "Coach": 3, "Manager": 4}
    members.sort(key=lambda x: (order.get(x["position"], 5), x["opl_name"].lower()))
    res = {"id": team_id, "name": text(name.group(1)) if name else "", "members": members,
           "updated": time.time()}
    cache.set(key, res)
    return res


# --------------------------------------------------------------------------- HenrikDev
class RateLimiter:
    """Hält sich an das Limit des HenrikDev-Basic-Keys (30 Requests/Minute)."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                delay = 60 - (now - self.calls[0]) + 0.1
            time.sleep(delay)


class HenrikError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


limiter = RateLimiter(int(os.environ.get("HENRIKDEV_RPM", "25")))


def henrik(path):
    key = os.environ.get("HENRIKDEV_API_KEY", "").strip()
    if not key:
        raise HenrikError(401, "Kein HENRIKDEV_API_KEY gesetzt")
    for attempt in range(3):
        limiter.wait()
        try:
            body = fetch(HENRIK_BASE + path, headers={"Authorization": key, "Accept": "application/json"})
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                # Server-Limit erreicht: bis zum Reset warten und erneut versuchen
                reset = e.headers.get("x-ratelimit-reset") or e.headers.get("retry-after") or "10"
                try:
                    delay = min(max(float(reset), 1.0), 60.0)
                except ValueError:
                    delay = 10.0
                time.sleep(delay + 0.5)
                continue
            try:
                err = json.loads(e.read().decode())
                msg = (err.get("errors") or [{}])[0].get("message") or str(err)
            except Exception:
                msg = e.reason
            raise HenrikError(e.code, msg)


def q(s):
    return urllib.parse.quote(s, safe="")


def player_stats(riot_id, region, refresh=False):
    if "#" not in riot_id:
        return {"error": "Keine gültige Riot-ID"}
    name, tag = riot_id.rsplit("#", 1)
    ck = f"stats:{region}:{riot_id.lower()}"
    hit = None if refresh else cache.get(ck, STATS_TTL)
    if hit:
        return hit

    out = {"riot_id": riot_id, "updated": time.time(),
           "tracker_url": f"https://tracker.gg/valorant/profile/riot/{q(riot_id)}/overview"}

    # Rang + Peak
    try:
        mmr = henrik(f"/valorant/v3/mmr/{region}/pc/{q(name)}/{q(tag)}").get("data") or {}
        cur = mmr.get("current") or {}
        peak = mmr.get("peak") or {}
        out["current"] = {
            "tier": (cur.get("tier") or {}).get("id"),
            "name": (cur.get("tier") or {}).get("name"),
            "rr": cur.get("rr"),
            "elo": cur.get("elo"),
            "last_change": cur.get("last_change"),
        }
        out["peak"] = {
            "tier": (peak.get("tier") or {}).get("id"),
            "name": (peak.get("tier") or {}).get("name"),
            "season": (peak.get("season") or {}).get("short"),
        }
        acc = mmr.get("account") or {}
        if acc.get("name"):
            out["riot_id"] = f'{acc["name"]}#{acc.get("tag", tag)}'
    except HenrikError as e:
        if e.status in (401, 403):
            return {"error": f"API-Key ungültig/fehlt: {e}", "fatal": True, **out}
        if e.status == 404:
            res = {"error": "Account nicht gefunden (Riot-ID geändert?)", **out}
            cache.set(ck, res)  # spart beim Komplett-Laden unnötige Requests
            return res
        out["mmr_error"] = str(e)

    # Letzte Competitive-Matches
    try:
        sm = henrik(f"/valorant/v1/stored-matches/{region}/{q(name)}/{q(tag)}?mode=competitive&size=20")
        out["recent"] = summarize_matches(sm.get("data") or [])
    except HenrikError as e:
        out["matches_error"] = str(e)

    if "mmr_error" not in out and "matches_error" not in out:
        cache.set(ck, out)
    return out


def summarize_matches(matches):
    n = k = d = a = hs = shots = dmg = rounds = wins = score = 0
    agents = {}
    for m in matches:
        s = m.get("stats") or {}
        teams = m.get("teams") or {}
        my = (s.get("team") or "").lower()
        if not s:
            continue
        n += 1
        k += s.get("kills") or 0
        d += s.get("deaths") or 0
        a += s.get("assists") or 0
        sh = s.get("shots") or {}
        hs += sh.get("head") or 0
        shots += (sh.get("head") or 0) + (sh.get("body") or 0) + (sh.get("leg") or 0)
        dmg += (s.get("damage") or {}).get("made") or 0
        score += s.get("score") or 0
        r = sum(v or 0 for v in teams.values() if isinstance(v, (int, float)))
        rounds += r
        other = [v for t, v in teams.items() if t.lower() != my]
        if my in teams and other and (teams[my] or 0) > (other[0] or 0):
            wins += 1
        ag = (s.get("character") or {}).get("name")
        if ag:
            agents[ag] = agents.get(ag, 0) + 1
    if not n:
        return {"matches": 0}
    return {
        "matches": n,
        "wins": wins,
        "winrate": round(100 * wins / n),
        "kd": round(k / d, 2) if d else float(k),
        "kda": f"{k}/{d}/{a}",
        "hs": round(100 * hs / shots, 1) if shots else None,
        "adr": round(dmg / rounds) if rounds else None,
        "acs": round(score / rounds) if rounds else None,
        "agents": [x for x, _ in sorted(agents.items(), key=lambda kv: -kv[1])[:3]],
    }


# --------------------------------------------------------------------------- Team-Übersicht
CORE_POSITIONS = ("Player", "Captain")


def tier_from_elo(elo):
    return max(3, min(27, int(elo // 100) + 3))


def is_active(member, active_names):
    """Aktiv = mind. ein Ligaspiel diese Saison (Riot-Name oder OPL-Name in den Match-Stats).
    Ohne gespielte Ligaspiele (active_names is None) zählt die OPL-Position."""
    if active_names is None:
        return member.get("position") in CORE_POSITIONS
    return ((member.get("riot_id") or "").split("#")[0].lower() in active_names
            or (member.get("opl_name") or "").lower() in active_names)


def team_summary(team_id, region, active_names=None, banned=()):
    """Ø-Elo aus dem Cache (ohne neue Requests). banned = Riot-IDs (klein) mit Saisonsperre → nicht aktiv."""
    team = cache.peek(f"team:{team_id}")
    if not team:
        return None
    core, allm, updated = [], [], [team.get("updated") or 0]
    with_id = 0
    for m in team["members"]:
        if not m.get("riot_id"):
            continue
        act = is_active(m, active_names) and m["riot_id"].lower() not in banned
        with_id += act
        st = cache.peek(f"stats:{region}:{m['riot_id'].lower()}")
        elo = ((st or {}).get("current") or {}).get("elo")
        if st and st.get("updated"):
            updated.append(st["updated"])
        if elo:
            allm.append(elo)
            if act:
                core.append(elo)

    def avg(lst):
        if not lst:
            return None
        e = round(sum(lst) / len(lst))
        return {"elo": e, "tier": tier_from_elo(e), "n": len(lst)}

    top5 = sorted(core, reverse=True)[:5]  # 5 höchste Ränge unter den aktiven Spielern
    return {"core": avg(core), "all": avg(allm), "top5": avg(top5), "core_total": with_id,
            "all_total": sum(1 for m in team["members"] if m.get("riot_id")),
            "updated": min(u for u in updated if u) if any(updated) else None,
            "active_by": "opl" if active_names is not None else "position"}




# --------------------------------------------------------------------------- OPL-Matches (Veto, Maps, Spieler-Stats)
def _course_url(league_url):
    return re.sub(r"/standing.*$", "", league_url.rstrip("/")) + "/course"


def opl_course(league_url, refresh=False):
    """Alle Match-IDs einer Liga (inkl. Gruppen-Unterevents) + erlaubte Event-IDs."""
    key = "course:" + league_url
    hit = None if refresh else cache.get(key, OPL_TTL)
    if hit:
        return hit
    url = _course_url(league_url)
    m = re.search(r"/event/(\d+)-([^/]+)", url)
    lid, slug = m.group(1), m.group(2)
    events, mids = {lid}, []
    page = fetch(url)
    mids += re.findall(r"match\\?/(\d+)-", page)
    # Gruppen-Ligen: Matches stehen in den Unterevents "...-GruppeA", "...-GruppeB"
    subs = dict.fromkeys(re.findall(r"event\\?/(\d+)-" + re.escape(slug) + r"-Gruppe[A-Za-z0-9]*", page))
    for sid in subs:
        if sid == lid:
            continue
        sub_page = fetch(re.sub(r"/event/\d+-[^/]+", f"/event/{sid}-{slug}", url))
        events.add(sid)
        mids += re.findall(r"match\\?/(\d+)-", sub_page)
        time.sleep(0.3)
    res = {"events": sorted(events), "matches": list(dict.fromkeys(mids))}
    cache.set(key, res)
    return res


def opl_season_events(league_url, refresh=False):
    """Alle Events der Saison aus dem OPL-Menü (Gruppenphase, Wildcard, Playoffs, Relegation …).
    → [{"id", "label", "url" (course), "slug"}]"""
    key = "season_events:" + league_url
    hit = None if refresh else cache.get(key, OPL_TTL)
    if hit is not None:
        return hit
    page = fetch(_course_url(league_url))
    out, seen = [], set()
    for href, label in re.findall(r'"href":"(https:\\/\\/www\.opleague\.pro\\/event\\/[^"]+?\\/course)","title":"[^"]*"\},"label":"([^"]+)"', page):
        url = href.replace("\\/", "/")
        m = re.search(r"/event/(\d+)-([^/]+)/course", url)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append({"id": m.group(1), "label": html.unescape(label), "url": url, "slug": m.group(2)})
    cache.set(key, out)
    return out


def opl_bracket(course_url, refresh=False):
    """K.-o.-Baum eines Events (Wildcard, Playoffs, Relegation) aus dem Datenblock `full_data` der Kursseite.
    Runden in Reihenfolge; pro Match Position (0 = Upper, 1 = Lower Bracket) und Ziel für Sieger/Verlierer."""
    key = "bracket:" + course_url
    hit = None if refresh else cache.get(key, OPL_TTL)
    if hit is not None:
        return hit
    page = fetch(course_url)
    i = page.find("var full_data = ")
    if i < 0:   # Event angelegt, aber (noch) nicht ausgelost
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        res = {"title": html.unescape(title.group(1).strip()) if title else "", "rounds": [],
               "unseeded": "not seeded" in page}
        cache.set(key, res)
        return res
    data, _ = json.JSONDecoder().raw_decode(page[i + len("var full_data = "):])
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    rounds = []
    for k in sorted(data.get("rounds", {}), key=int):
        r = data["rounds"][k]
        ms = []
        for x in r.get("matches", []):
            ms.append({
                "id": str(x["id"]), "pos": int(x.get("position") or 0), "state": x.get("state_key"),
                "date": (x.get("popover") or {}).get("match_date"),
                "teams": [{"id": str(t.get("id")), "name": html.unescape(t.get("name") or ""), "short": t.get("short_name"),
                           "logo": t.get("logo"), "score": t.get("score"), "cls": t.get("class") or ""} for t in x.get("teams", [])],
                "winner": [str(v) for v in x.get("winner") or []], "loser": [str(v) for v in x.get("loser") or []],
                "before": [str(v) for v in x.get("before") or []]})
        rounds.append({"key": int(k), "titles": r.get("titles") or ["", ""], "matches": ms})
    res = {"title": html.unescape(title.group(1).strip()) if title else "", "rounds": rounds}
    cache.set(key, res)
    return res


def _num(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_match(page, mid):
    info = {}
    for title, content in re.findall(r'"title":"([^"]+)","value":\{"type":"(?:string|link)","content":"([^"]*)"', page):
        info[title] = content
    start = re.search(r'"date_1":\{"options":\{[^}]*\},"unix":(\d+)', page)
    t = {}
    for k in ("Team1Name", "Team2Name", "Team1ID", "Team2ID"):
        mm = re.search(r'var %s = "([^"]*)"' % k, page)
        t[k] = html.unescape(mm.group(1)) if mm else ""
    names = {t["Team1Name"]: 1, t["Team2Name"]: 2}

    veto, plan = [], []
    for cls, body, name in re.findall(r'<div class="mapban-v2__map( --pick)?\s*">(.*?)<div class="mapban-v2__map-name">([^<]*)</div>', page, re.S):
        action = (re.search(r'map-indicator">([^<]*)', body) or [None, ""])[1].strip()
        by = names.get(html.unescape((re.search(r'mapban-v2__team-image"\s*title="([^"]*)"', body) or [None, ""])[1]))
        # Veto-Reihenfolge steht schon vor dem Match fest (Slots mit Team-Logo, Map „TBD“)
        plan.append({"action": action, "by": by})
        if name.strip().upper() == "TBD":
            continue
        side = re.search(r'side-indicator[^"]*"\s*title="([^"]*)"', body)
        side_team, side_name = None, None
        if side:
            sm = re.match(r"(.*) starts on (\w+)", html.unescape(side.group(1)))
            if sm:
                side_team, side_name = names.get(sm.group(1)), sm.group(2)
        veto.append({"action": action, "map": name.strip().lower(), "by": by,
                     "side_team": side_team, "side": side_name})

    maps = []
    parts = page.split('<div class="map-result__container">')[1:]
    for p in parts:
        sc1 = re.search(r'map-result__map-score-team-1">\s*<div>([^<]*)</div>', p)
        sc2 = re.search(r'map-result__map-score-team-2">\s*<div>([^<]*)</div>', p)
        mname = re.search(r'map-result__map-name">\s*([^<]*)<', p)
        s1, s2 = _num(sc1.group(1).strip()) if sc1 else None, _num(sc2.group(1).strip()) if sc2 else None
        if s1 is None or s2 is None:
            continue
        players = []
        for team, row in re.findall(r'map-result-valorant__row team-(\d)">(.*?)(?=map-result-valorant__row team-|map-result__map-image|$)', p, re.S):
            nick = re.search(r'class="nickname">([^<]*)<', row)
            if not nick:
                continue
            agent = re.search(r'class="character-name">([^<]*)<', row)
            st = {k.strip(): v.strip() for k, v in re.findall(r'map-result-valorant__stat">\s*<div>([^<]*)</div>\s*<div>([^<]*)</div>', row)}
            kda = [_num(x.strip()) for x in st.get("kda", "").split("/")] + [None] * 3
            players.append({
                "team": int(team), "name": html.unescape(nick.group(1).strip()),
                "agent": agent.group(1).strip() if agent else "",
                "acs": _num(st.get("avg cs")), "k": kda[0], "d": kda[1], "a": kda[2],
                "econ": _num(st.get("econ rating")), "fb": _num(st.get("first blood")),
                "plants": _num(st.get("plants")), "defuses": _num(st.get("defuses")),
            })
        maps.append({"map": mname.group(1).strip().lower() if mname else "?", "s1": s1, "s2": s2, "players": players})

    # Ergebnis-Block: Map-Stand (z. B. 2/0) oder bei kampflos/Freilos "W"/"L"; Reihenfolge = Team 1, Team 2
    result = []
    blk = re.search(r'report__result-container">(.*?)</section>', page, re.S)
    if blk:
        result = [{"val": v.strip(), "winner": "--winner" in c}
                  for c, v in re.findall(r'<div class="result([^"]*)">([^<]*)<', blk.group(1))][:2]

    return {
        "id": str(mid), "event": info.get("event ID", ""), "state": info.get("match state", ""),
        "result": result, "v": MATCH_PARSER_VERSION,
        "format": info.get("format", ""), "start": int(start.group(1)) if start else None,
        "t1": {"id": t["Team1ID"], "name": t["Team1Name"]}, "t2": {"id": t["Team2ID"], "name": t["Team2Name"]},
        "defwin": '"is_defwin_team_in_match":true' in page,
        "veto": veto, "plan": plan, "first": next((x["by"] for x in plan if x["by"]), None),
        "maps": maps, "fetched": time.time(),
    }


MATCH_PARSER_VERSION = 3   # 3: Veto-Plan/Startteam; erhöhen, wenn parse_match neue Felder liefert → alle Matches einmal neu laden


def opl_match(mid, refresh=False):
    """Fertige Matches werden nie neu geladen; offene je nach Startzeit."""
    key = f"match:{mid}"
    old = cache.peek(key)
    # Fertige Matches mit älterer Parser-Version behalten (neue Felder betreffen nur offene Matches)
    current = old and (old.get("v") == MATCH_PARSER_VERSION or (old.get("state") == "closed" and old.get("v", 0) >= 2))
    if old and not refresh and current:
        age = cache.age(key) or 0
        start = old.get("start") or 0
        live = start < time.time() < start + 12 * 3600   # läuft gerade / heute gespielt
        if old.get("state") == "closed":
            # ohne Ergebnis-Details: alle 6 h erneut prüfen, am Spieltag alle 15 min
            if old.get("maps") or age < (15 * 60 if live else 6 * 3600):
                return old
        # am Spieltag (Start vorbei, < 12 h): alle 15 min; Start in < 3 h: stündlich; sonst alle 12 h
        limit = 15 * 60 if live else 50 * 60 if start < time.time() + 3 * 3600 else 12 * 3600
        if age < limit:
            return old
    res = parse_match(fetch(f"https://www.opleague.pro/match/{mid}"), mid)
    cache.set(key, res)
    return res
