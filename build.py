#!/usr/bin/env python3
"""
OPL Scout – Daten aktualisieren und statische Seite bauen.

Läuft stündlich als GitHub Action, geht aber auch lokal:

    python3 build.py                       # alle Ligen, Budget 30 min
    python3 build.py --league 8283         # nur eine Liga (Event-ID oder URL)
    python3 build.py --team 11688          # ein Team sofort komplett neu laden
    python3 build.py --export-only         # nur aus dem Cache exportieren, keine Requests
    python3 -m http.server -d _site 8765   # Ergebnis ansehen

Zustand (Cache + zusätzlich hinzugefügte Ligen) liegt in --state (Standard: .state/),
in der GitHub Action ist das der Branch `data`.
"""
import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import scout_core as core

HERE = Path(__file__).resolve().parent
NOT_FOUND_RETRY = 24 * 3600   # nicht gefundene Riot-IDs nur 1x täglich erneut probieren


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def league_id(url):
    m = re.search(r"/event/(\d+)", url)
    return m.group(1) if m else re.sub(r"\W+", "-", url)[-40:]


def load_leagues(state):
    base = json.loads((HERE / "config" / "leagues.json").read_text())
    extra_file = state / "leagues.json"
    extra = json.loads(extra_file.read_text()) if extra_file.exists() else []
    seen, out = set(), []
    for lg in base["leagues"] + extra:
        lid = league_id(lg["url"])
        if lid not in seen:
            seen.add(lid)
            out.append({**lg, "id": lid})
    return base, out


def add_league(state, url, name):
    st = core.opl_standings(url, refresh=True)  # prüft, ob die URL lesbar ist
    extra_file = state / "leagues.json"
    extra = json.loads(extra_file.read_text()) if extra_file.exists() else []
    if all(league_id(x["url"]) != league_id(url) for x in extra):
        extra.append({"name": name or st["title"].split(" - ")[-1], "url": url})
        extra_file.write_text(json.dumps(extra, indent=2, ensure_ascii=False))
    log(f"Liga hinzugefügt: {st['title']} ({len(st['teams'])} Teams)")


def stats_key(region, riot_id):
    return f"stats:{region}:{riot_id.lower()}"


def refresh_players(riot_ids, region, deadline, force=False, max_age=50 * 60):
    """Lädt Spieler-Stats, älteste zuerst, bis das Zeitbudget aufgebraucht ist."""
    todo = []
    for rid in dict.fromkeys(riot_ids):           # Reihenfolge behalten, Duplikate raus
        key = stats_key(region, rid)
        age = core.cache.age(key)
        val = core.cache.peek(key) or {}
        limit = NOT_FOUND_RETRY if "nicht gefunden" in (val.get("error") or "") else max_age
        if force or age is None or age > limit:
            todo.append((age if age is not None else float("inf"), rid))
    todo.sort(reverse=True)
    log(f"{len(todo)} von {len(set(riot_ids))} Spielern brauchen ein Update")
    done = errors = 0
    stop = threading.Event()
    with ThreadPoolExecutor(3) as ex:
        pending = set()
        it = iter(todo)
        while True:
            while len(pending) < 3 and time.time() < deadline and not stop.is_set():
                nxt = next(it, None)
                if nxt is None:
                    break
                pending.add(ex.submit(core.player_stats, nxt[1], region, True))
            if not pending:
                break
            fin, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in fin:
                done += 1
                try:
                    res = f.result()
                except Exception as e:
                    errors += 1
                    log("Fehler:", e)
                    continue
                if res.get("fatal"):
                    log("ABBRUCH:", res.get("error"))
                    stop.set()
                if res.get("error") or res.get("mmr_error") or res.get("matches_error"):
                    errors += 1
                if done % 40 == 0:
                    core.cache.save()
                    log(f"… {done}/{len(todo)} Spieler")
    left = len(todo) - done
    log(f"Spieler aktualisiert: {done} ({errors} ohne Daten), offen: {left}")
    return left


def crawl_matches(lg, refresh_course=True):
    """Lädt alle Matches einer Liga. Gültig sind nur Matches, deren Event-ID zur Liga
    (bzw. ihren Gruppen) gehört – damit zählt nur die ausgewählte Saison."""
    course = core.opl_course(lg["url"], refresh=refresh_course)
    ok, fetched = [], 0
    for mid in course["matches"]:
        before = core.cache.age(f"match:{mid}")
        try:
            m = core.opl_match(mid)
        except Exception as e:
            log(f"  Match {mid} fehlgeschlagen: {e}")
            continue
        if core.cache.age(f"match:{mid}") != before:
            fetched += 1
            time.sleep(0.3)   # OPL schonen
        if m.get("event") in course["events"]:
            ok.append(mid)
    core.cache.set(f"league_matches:{lg['id']}", ok)
    log(f"  Matches: {len(ok)} gültig, {fetched} neu geladen (Events {', '.join(course['events'])})")
    return ok


def league_meta(matches):
    """Map → {Agent: Anteil der Team-Comps in der Liga, die ihn auf dieser Map spielen}."""
    cnt, tot = {}, {}
    for m in matches:
        for x in m["maps"]:
            for team in (1, 2):
                ags = {p["agent"] for p in x["players"] if p["team"] == team and p["agent"]}
                if not ags:
                    continue
                tot[x["map"]] = tot.get(x["map"], 0) + 1
                for a in ags:
                    cnt.setdefault(x["map"], {})[a] = cnt.setdefault(x["map"], {}).get(a, 0) + 1
    return {mp: {a: c / tot[mp] for a, c in ags.items()} for mp, ags in cnt.items()}


def likely_comp(mp, map_comps, players, tagents, meta=None):
    """Gespielte Comp (häufigste, bei Gleichstand die neueste) – sonst Prognose aus den
    Agent-Vorlieben der aktiven Spieler (erst auf dieser Map, dann insgesamt)."""
    if map_comps:
        comp, (n, _) = max(map_comps.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        return {"agents": list(comp), "src": "played", "n": n, "of": sum(v[0] for v in map_comps.values())}
    taken, picks = set(), []
    # Spieler mit den meisten Maps zuerst – sie spielen am wahrscheinlichsten
    for agg in sorted(players.values(), key=lambda a: -a["maps"])[:5]:
        overall = {a: c for a, c, _ in agg["agents"]}
        on_map = agg.get("map_agents", {}).get(mp, {})
        mm = (meta or {}).get(mp, {})
        # 1. selbst auf dieser Map gespielt, 2. eigener Anteil + Liga-Meta der Map
        cands = sorted(overall, key=lambda a: (-on_map.get(a, 0), -(overall[a] / agg["maps"] + mm.get(a, 0))))
        ag = next((a for a in cands if a not in taken), None)
        if ag:
            taken.add(ag)
            picks.append({"player": agg["name"], "agent": ag})
    for ag, _ in sorted(tagents.items(), key=lambda kv: -kv[1]):   # auffüllen, falls nötig
        if len(picks) >= 5:
            break
        if ag not in taken:
            taken.add(ag)
            picks.append({"player": None, "agent": ag})
    if not picks:
        return None
    return {"agents": [p["agent"] for p in picks], "players": picks, "src": "predicted"}


def team_opl(tid, matches, meta=None):
    """Map-Pool, Match-Liste und Liga-Stats der Spieler für ein Team."""
    maps, players, mlist = {}, {}, []
    tagents = {}   # Agent → auf wie vielen Maps vom Team gespielt
    comps = {}     # Map → {(5 Agents): [Anzahl, zuletzt]}
    def mp(name):
        return maps.setdefault(name, {"pick": 0, "ban": 0, "opp_pick": 0, "opp_ban": 0, "decider": 0,
                                      "played": 0, "won": 0, "rw": 0, "rl": 0, "atk": 0, "def": 0})
    for m in sorted(matches, key=lambda x: x.get("start") or 0):
        if tid not in (m["t1"]["id"], m["t2"]["id"]):
            continue
        me = 1 if m["t1"]["id"] == tid else 2
        opp = m["t2"] if me == 1 else m["t1"]
        played = [x for x in m["maps"]]
        won = sum(1 for x in played if (x["s1"] > x["s2"]) == (me == 1) and x["s1"] != x["s2"])
        lost = len(played) - won
        # Ergebnis: offizieller OPL-Ergebnisblock (deckt auch kampflos/Freilos ab), sonst aus den Maps
        res = m.get("result") or []
        mine, theirs = (res[me - 1], res[2 - me]) if len(res) == 2 else ({}, {})
        if mine.get("winner"):
            outcome = "W"
        elif theirs.get("winner"):
            outcome = "L"
        elif played:
            outcome = "W" if won > lost else "L" if lost > won else "D"
        elif m.get("state") == "closed" and mine.get("val") and mine.get("val") == theirs.get("val"):
            outcome = "D"
        else:
            outcome = None
        kind = "played" if played else ("bye" if opp.get("id") in ("-1", "") or "default" in opp.get("name", "").lower()
                                         else "ff" if outcome else None)
        entry = {"id": m["id"], "start": m.get("start"), "state": m.get("state"), "format": m.get("format"),
                 "opp": opp, "home": me == 1, "score": [won, lost] if played else None, "outcome": outcome, "kind": kind,
                 "veto": [{"action": v["action"], "map": v["map"],
                           "by": "self" if v["by"] == me else "opp" if v["by"] else None,
                           "side": v["side"] if v["side_team"] == me else
                                   ({"atk": "def", "def": "atk"}.get(v["side"]) if v["side_team"] else None)}
                          for v in m["veto"]],
                 "maps": [{"map": x["map"], "us": x["s1"] if me == 1 else x["s2"],
                           "them": x["s2"] if me == 1 else x["s1"]} for x in played]}
        mlist.append(entry)
        if not played:
            continue
        # Veto zählen (nur gespielte Matches, damit offene Planungen nicht mitzählen)
        picks = [v for v in entry["veto"]]
        for i, v in enumerate(picks):
            s = mp(v["map"])
            if v["action"] == "ban":
                s["ban" if v["by"] == "self" else "opp_ban"] += 1
            elif v["action"] == "pick":
                if i == len(picks) - 1 and len(picks) >= 5:
                    s["decider"] += 1
                else:
                    s["pick" if v["by"] == "self" else "opp_pick"] += 1
            if v["side"] in ("atk", "def") and any(x["map"] == v["map"] for x in played):
                s[v["side"]] += 1   # Startseite nur für tatsächlich gespielte Maps
        for x in entry["maps"]:
            s = mp(x["map"])
            s["played"] += 1
            s["won"] += x["us"] > x["them"]
            s["rw"] += x["us"]
            s["rl"] += x["them"]
        for x in played:
            mine = [p["agent"] for p in x["players"] if p["team"] == me and p["agent"]]
            for ag in set(mine):
                tagents[ag] = tagents.get(ag, 0) + 1
            if len(mine) == 5:
                comp = tuple(sorted(mine))
                c = comps.setdefault(x["map"], {}).setdefault(comp, [0, 0])
                c[0] += 1
                c[1] = max(c[1], m.get("start") or 0)   # zuletzt gespielt
            for p in x["players"]:
                if p["team"] != me:
                    continue
                k = p["name"].lower()
                agg = players.setdefault(k, {"name": p["name"], "maps": 0, "acs": 0, "k": 0, "d": 0, "a": 0,
                                             "econ": 0, "fb": 0, "plants": 0, "defuses": 0, "won": 0, "agents": {}})
                agg["maps"] += 1
                agg["won"] += (x["s1"] > x["s2"]) == (me == 1)
                for f in ("acs", "k", "d", "a", "econ", "fb", "plants", "defuses"):
                    agg[f] += p[f] or 0
                us, them = (x["s1"], x["s2"]) if me == 1 else (x["s2"], x["s1"])
                agg.setdefault("history", []).append({
                    "m": m["id"], "start": m.get("start"), "opp": opp.get("name"), "opp_id": opp.get("id"),
                    "map": x["map"], "agent": p["agent"], "us": us, "them": them,
                    **{f: p[f] or 0 for f in ("acs", "k", "d", "a", "econ", "fb", "plants", "defuses")}})
                if p["agent"]:
                    agg["agents"][p["agent"]] = agg["agents"].get(p["agent"], 0) + 1
                    ma = agg.setdefault("map_agents", {}).setdefault(x["map"], {})
                    ma[p["agent"]] = ma.get(p["agent"], 0) + 1
    for agg in players.values():
        n = agg["maps"] or 1
        agg["acs"] = round(agg["acs"] / n)
        agg["econ"] = round(agg["econ"] / n)
        agg["kd"] = round(agg["k"] / agg["d"], 2) if agg["d"] else float(agg["k"])
        agg["winrate"] = round(100 * agg["won"] / n)
        # [Agent, Anzahl Maps, Anteil in %] – meistgespielte zuerst
        agg["agents"] = [[a, c, round(100 * c / n)] for a, c in sorted(agg["agents"].items(), key=lambda kv: -kv[1])]
    # Wahrscheinlichste Comp pro Map
    for mp, st in maps.items():
        st["comp"] = likely_comp(mp, comps.get(mp, {}), players, tagents, meta)
    for agg in players.values():
        agg.pop("map_agents", None)
    total = sum(1 for m in mlist for _ in m["maps"])
    team_agents = [[a, c, round(100 * c / total)] for a, c in sorted(tagents.items(), key=lambda kv: -kv[1])] if total else []
    record = {k: sum(1 for x in mlist if x["outcome"] == k) for k in ("W", "D", "L")}
    record["ff"] = sum(1 for x in mlist if x["kind"] in ("ff", "bye") and x["outcome"])
    return {"maps": maps, "players": players, "matches": mlist, "agents": team_agents, "maps_played": total,
            "record": record}


def league_overview(matches, opl_by_team, teams):
    """Liga-weite Auswertung: Map-Raten, Startseite, Agents, Comps, Spieler-Bestenliste."""
    maps, agents, comps = {}, {}, {}
    nv = nmaps = 0
    bo = {}
    def mp(name):
        return maps.setdefault(name, {"played": 0, "pick": 0, "ban": 0, "decider": 0, "pick_won": 0, "pick_n": 0,
                                      "atk_won": 0, "atk_n": 0, "rounds": 0, "close": 0, "agents": {}, "teams": 0})
    for m in matches:
        if not m["maps"]:
            continue
        b = re.search(r"BO(\d)", m.get("format") or "", re.I)
        if b:
            bo[b.group(1)] = bo.get(b.group(1), 0) + 1
        winner = {x["map"]: (1 if x["s1"] > x["s2"] else 2 if x["s2"] > x["s1"] else 0) for x in m["maps"]}
        if m["veto"]:
            nv += 1
        for i, v in enumerate(m["veto"]):
            s = mp(v["map"])
            if v["action"] == "ban":
                s["ban"] += 1
            elif v["by"] and not (i == len(m["veto"]) - 1 and len(m["veto"]) >= 5):   # letzter Pick = Decider
                s["pick"] += 1
                if winner.get(v["map"]):
                    s["pick_n"] += 1
                    s["pick_won"] += winner[v["map"]] == v["by"]
            else:
                s["decider"] += 1
            if v["side_team"] and v["side"] in ("atk", "def") and winner.get(v["map"]):
                atk_team = v["side_team"] if v["side"] == "atk" else 3 - v["side_team"]
                s["atk_n"] += 1
                s["atk_won"] += winner[v["map"]] == atk_team
        for x in m["maps"]:
            nmaps += 1
            s = mp(x["map"])
            s["played"] += 1
            s["rounds"] += x["s1"] + x["s2"]
            s["close"] += abs(x["s1"] - x["s2"]) <= 3
            for team in (1, 2):
                ags = sorted({p["agent"] for p in x["players"] if p["team"] == team and p["agent"]})
                if not ags:
                    continue
                s["teams"] += 1
                for a in ags:
                    s["agents"][a] = s["agents"].get(a, 0) + 1
                    agents[a] = agents.get(a, 0) + 1
                if len(ags) == 5:
                    key = tuple(ags)
                    c = comps.setdefault(x["map"], {}).setdefault(key, [0, 0])
                    c[0] += 1
                    c[1] += x["s1" if team == 1 else "s2"] > x["s2" if team == 1 else "s1"]
    out_maps = {}
    for name, s in maps.items():
        t = s.pop("teams")
        s["agents"] = [[a, c, round(100 * c / t)] for a, c in sorted(s["agents"].items(), key=lambda kv: -kv[1])] if t else []
        s["comps"] = [[list(k), n, w] for k, (n, w) in sorted(comps.get(name, {}).items(), key=lambda kv: -kv[1][0])[:3]]
        s["avg_rounds"] = round(s.pop("rounds") / s["played"], 1) if s["played"] else None
        out_maps[name] = s
    total_teams = 2 * nmaps
    players = []
    names = {t["id"]: t["name"] for t in teams}
    for tid in names:
        for agg in (opl_by_team.get(tid) or {}).get("players", {}).values():
            players.append({"name": agg["name"], "team": tid, "team_name": names[tid], "maps": agg["maps"],
                            "acs": agg["acs"], "kd": agg["kd"], "k": agg["k"], "d": agg["d"], "a": agg["a"],
                            "econ": agg["econ"], "fb": agg["fb"], "fbpm": round(agg["fb"] / agg["maps"], 2) if agg["maps"] else 0,
                            "winrate": agg["winrate"], "agents": agg["agents"][:3]})
    return {"matches": sum(1 for m in matches if m["maps"]), "veto_matches": nv, "maps_played": nmaps,
            "bo": int(max(bo.items(), key=lambda kv: kv[1])[0]) if bo else 3,
            "maps": out_maps,
            "agents": [[a, c, round(100 * c / total_teams)] for a, c in sorted(agents.items(), key=lambda kv: -kv[1])] if total_teams else [],
            "players": players}


def export(out, leagues, region, repo, ref, base_cfg):
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta_leagues = []
    team_ids = set()
    league_matches = {lg["id"]: [m for m in (core.cache.peek(f"match:{mid}") for mid in
                                  core.cache.peek(f"league_matches:{lg['id']}") or []) if m]
                      for lg in leagues}
    opl_by_team = {}
    metas = {lid: league_meta(ms) for lid, ms in league_matches.items()}
    for lg in leagues:
        st = core.cache.peek("standing:" + lg["url"])
        if not st:
            continue
        summaries = {}
        for t in st["teams"]:
            opl = opl_by_team[t["id"]] = team_opl(t["id"], league_matches[lg["id"]], metas[lg["id"]])
            # aktive Spieler = mind. ein Ligaspiel; ohne gespielte Matches → OPL-Position
            active = set(opl["players"]) if opl["maps_played"] else None
            summaries[t["id"]] = core.team_summary(t["id"], region, active)
        overview = league_overview(league_matches[lg["id"]], opl_by_team, st["teams"])
        (data / f"league-{lg['id']}.json").write_text(json.dumps(
            {**st, "id": lg["id"], "name": lg["name"], "summaries": summaries, "overview": overview,
             "updated": time.time() - (core.cache.age("standing:" + lg["url"]) or 0)},
            ensure_ascii=False, separators=(",", ":")))
        meta_leagues.append({"id": lg["id"], "name": lg["name"], "url": lg["url"], "title": st["title"],
                             "zones": lg.get("zones") or []})
        team_ids.update(t["id"] for t in st["teams"])


    for tid in team_ids:
        team = core.cache.peek(f"team:{tid}")
        if not team:
            continue
        stats = {}
        for m in team["members"]:
            if m.get("riot_id"):
                s = core.cache.peek(stats_key(region, m["riot_id"]))
                if s:
                    stats[m["riot_id"].lower()] = s
        opl = opl_by_team.get(tid) or {"maps": {}, "players": {}, "matches": [], "agents": [], "maps_played": 0}
        (data / f"team-{tid}.json").write_text(json.dumps(
            {**team, "stats": stats, "opl": opl}, ensure_ascii=False, separators=(",", ":")))

    (data / "meta.json").write_text(json.dumps({
        "generated": time.time(),
        "repo": repo, "ref": ref, "region": region,
        "workflow": "update.yml",
        "schedule_minute": base_cfg.get("schedule_minute", 17),
        "opl_daily": base_cfg.get("opl_daily", "23:00"),
        "opl_updated": max([core.cache.peek("standing:" + lg["url"]) and
                            time.time() - (core.cache.age("standing:" + lg["url"]) or 0) or 0 for lg in leagues] or [0]),
        "leagues": meta_leagues,
    }, ensure_ascii=False, indent=1))

    for f in (HERE / "web").iterdir():
        if f.is_file():
            shutil.copy(f, out / f.name)
    (out / ".nojekyll").write_text("")
    log(f"Export: {len(meta_leagues)} Ligen, {len(team_ids)} Teams → {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=str(HERE / ".state"), help="Ordner für Cache + Liga-Liste")
    p.add_argument("--out", default=str(HERE / "_site"), help="Ausgabeordner der Webseite")
    p.add_argument("--league", default="all", help="'all', Event-ID oder Standing-URL")
    p.add_argument("--team", default="", help="Team-ID: nur dieses Team, komplett neu")
    p.add_argument("--add-league", default="", help="neue Standing-URL dauerhaft hinzufügen")
    p.add_argument("--add-league-name", default="", help="Anzeigename der neuen Liga")
    p.add_argument("--budget-minutes", type=float, default=30, help="max. Zeit für Spieler-Updates")
    p.add_argument("--stats-max-age", type=float, default=50, help="Stats älter als X min neu laden")
    p.add_argument("--export-only", action="store_true")
    p.add_argument("--matches-only", action="store_true", help="nur OPL-Matches crawlen (keine API-Anfragen)")
    p.add_argument("--skip-opl", action="store_true",
                   help="OPL nicht abfragen (Standings/Kader/Matches aus dem Cache), nur Ranked-Stats aktualisieren")
    a = p.parse_args()

    state, out = Path(a.state), Path(a.out)
    state.mkdir(parents=True, exist_ok=True)
    cache_file = state / "cache.json"
    if not cache_file.exists() and (HERE / ".cache.json").exists() and not os.environ.get("GITHUB_ACTIONS"):
        shutil.copy(HERE / ".cache.json", cache_file)   # lokaler Start: vorhandenen Cache übernehmen
    core.use_cache(cache_file, autosave=False)
    deadline = time.time() + a.budget_minutes * 60

    if a.add_league:
        add_league(state, a.add_league.strip(), a.add_league_name.strip())
    base_cfg, leagues = load_leagues(state)
    region = base_cfg.get("region", "eu")
    has_key = bool(os.environ.get("HENRIKDEV_API_KEY"))
    if not has_key and not a.export_only:
        log("WARNUNG: kein HENRIKDEV_API_KEY – es werden nur OPL-Daten aktualisiert")

    try:
        if a.matches_only:
            for lg in leagues if a.league in ("", "all") else [x for x in leagues if x["id"] == league_id(a.league)]:
                log(lg["name"]); crawl_matches(lg)
        elif not a.export_only:
            sel = leagues
            if a.league not in ("", "all"):
                sel = [lg for lg in leagues if lg["id"] == league_id(a.league) or lg["url"] == a.league]
                if not sel:
                    sys.exit(f"Liga nicht gefunden: {a.league}")

            if a.team:
                # Manuelles Team-Update: Kader, Riot-IDs und alle Spieler sofort neu
                team = core.opl_team(a.team, refresh=True, refresh_ids=True)
                for lg in sel if a.league not in ("", "all") else []:
                    crawl_matches(lg)
                log(f"Team {team['name']}: {len(team['members'])} Mitglieder")
                if has_key:
                    refresh_players([m["riot_id"] for m in team["members"] if m.get("riot_id")],
                                    region, deadline, force=True)
            else:
                riot_ids = []
                for lg in sel:
                    # --skip-opl (stündlicher Lauf): OPL nur lesen, wenn noch gar nichts im Cache ist
                    st = (core.cache.peek("standing:" + lg["url"]) if a.skip_opl else None) \
                        or core.opl_standings(lg["url"], refresh=True)
                    log(f"{st['title']}: {len(st['teams'])} Teams" + (" (OPL aus Cache)" if a.skip_opl else ""))
                    for t in st["teams"]:
                        team = core.cache.peek(f"team:{t['id']}") if a.skip_opl else None
                        if not team:
                            # täglicher OPL-Lauf: Kader + Riot-IDs komplett neu
                            try:
                                team = core.opl_team(t["id"], refresh=True, refresh_ids=not a.skip_opl)
                            except Exception as e:
                                log(f"  Kader {t['name']} fehlgeschlagen: {e}")
                                team = core.cache.peek(f"team:{t['id']}")
                                if not team:
                                    continue
                        riot_ids += [m["riot_id"] for m in team["members"] if m.get("riot_id")]
                    if not a.skip_opl:
                        crawl_matches(lg)
                    core.cache.save()
                if has_key:
                    refresh_players(riot_ids, region, deadline, max_age=a.stats_max_age * 60)
    finally:
        core.cache.save()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    export(out, leagues, region, repo, ref, base_cfg)


if __name__ == "__main__":
    main()
