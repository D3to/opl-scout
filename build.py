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


def team_opl(tid, matches):
    """Map-Pool, Match-Liste und Liga-Stats der Spieler für ein Team."""
    maps, players, mlist = {}, {}, []
    tagents = {}   # Agent → auf wie vielen Maps vom Team gespielt
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
        entry = {"id": m["id"], "start": m.get("start"), "state": m.get("state"), "format": m.get("format"),
                 "opp": opp, "score": [won, lost] if played else None,
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
            for ag in {p["agent"] for p in x["players"] if p["team"] == me and p["agent"]}:
                tagents[ag] = tagents.get(ag, 0) + 1
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
                if p["agent"]:
                    agg["agents"][p["agent"]] = agg["agents"].get(p["agent"], 0) + 1
    for agg in players.values():
        n = agg["maps"] or 1
        agg["acs"] = round(agg["acs"] / n)
        agg["econ"] = round(agg["econ"] / n)
        agg["kd"] = round(agg["k"] / agg["d"], 2) if agg["d"] else float(agg["k"])
        agg["winrate"] = round(100 * agg["won"] / n)
        # [Agent, Anzahl Maps, Anteil in %] – meistgespielte zuerst
        agg["agents"] = [[a, c, round(100 * c / n)] for a, c in sorted(agg["agents"].items(), key=lambda kv: -kv[1])]
    total = sum(1 for m in mlist for _ in m["maps"])
    team_agents = [[a, c, round(100 * c / total)] for a, c in sorted(tagents.items(), key=lambda kv: -kv[1])] if total else []
    return {"maps": maps, "players": players, "matches": mlist, "agents": team_agents, "maps_played": total}


def export(out, leagues, region, repo, ref, base_cfg):
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta_leagues = []
    team_ids = set()
    league_matches = {lg["id"]: [m for m in (core.cache.peek(f"match:{mid}") for mid in
                                  core.cache.peek(f"league_matches:{lg['id']}") or []) if m]
                      for lg in leagues}
    opl_by_team = {}
    for lg in leagues:
        st = core.cache.peek("standing:" + lg["url"])
        if not st:
            continue
        summaries = {}
        for t in st["teams"]:
            opl = opl_by_team[t["id"]] = team_opl(t["id"], league_matches[lg["id"]])
            # aktive Spieler = mind. ein Ligaspiel; ohne gespielte Matches → OPL-Position
            active = set(opl["players"]) if opl["maps_played"] else None
            summaries[t["id"]] = core.team_summary(t["id"], region, active)
        (data / f"league-{lg['id']}.json").write_text(json.dumps(
            {**st, "id": lg["id"], "name": lg["name"], "summaries": summaries,
             "updated": time.time() - (core.cache.age("standing:" + lg["url"]) or 0)},
            ensure_ascii=False, separators=(",", ":")))
        meta_leagues.append({"id": lg["id"], "name": lg["name"], "url": lg["url"], "title": st["title"]})
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
                    st = core.opl_standings(lg["url"], refresh=True)
                    log(f"{st['title']}: {len(st['teams'])} Teams")
                    for t in st["teams"]:
                        # Kader stündlich neu, Riot-IDs nur 1x täglich (außer neue Mitglieder)
                        try:
                            team = core.opl_team(t["id"], refresh=True, refresh_ids=False)
                        except Exception as e:
                            log(f"  Kader {t['name']} fehlgeschlagen: {e}")
                            team = core.cache.peek(f"team:{t['id']}")
                            if not team:
                                continue
                        riot_ids += [m["riot_id"] for m in team["members"] if m.get("riot_id")]
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
