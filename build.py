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


def export(out, leagues, region, repo, ref, base_cfg):
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta_leagues = []
    team_ids = set()
    for lg in leagues:
        st = core.cache.peek("standing:" + lg["url"])
        if not st:
            continue
        summaries = {t["id"]: core.team_summary(t["id"], region) for t in st["teams"]}
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
        (data / f"team-{tid}.json").write_text(json.dumps(
            {**team, "stats": stats}, ensure_ascii=False, separators=(",", ":")))

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
        if not a.export_only:
            sel = leagues
            if a.league not in ("", "all"):
                sel = [lg for lg in leagues if lg["id"] == league_id(a.league) or lg["url"] == a.league]
                if not sel:
                    sys.exit(f"Liga nicht gefunden: {a.league}")

            if a.team:
                # Manuelles Team-Update: Kader, Riot-IDs und alle Spieler sofort neu
                team = core.opl_team(a.team, refresh=True, refresh_ids=True)
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
