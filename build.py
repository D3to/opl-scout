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
import unicodedata
import urllib.parse
import urllib.request
from html import unescape
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


def league_number(lg):
    m = re.search(r"(\d)\.\s*Liga", lg.get("name", "")) or re.search(r"(\d)Liga", lg.get("url", ""))
    return m.group(1) if m else None


def find_brackets(lg):
    """K.-o.-Events (Wildcard, Playoffs, Relegation) dieser Liga im OPL-Menü finden.
    Das Menü der Gruppenphase zeigt nur je Turnier einen Eintrag, die Geschwister-Events
    (z. B. „Wildcardturnier 2. Liga“) stehen im Menü dieses Turniers → eine Ebene tiefer suchen."""
    n = league_number(lg)
    if not n:
        return []
    found, seen = {}, set()
    queue = [e for e in core.opl_season_events(lg["url"]) if "Gruppenphase" not in e["slug"]]
    while queue:
        e = queue.pop(0)
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        tail = e["slug"].split("-", 1)[-1]
        digits = set("".join(re.findall(r"([\d\-]+)Liga", tail)).replace("-", ""))
        if n in digits:
            found[e["id"]] = e
        try:
            for sub in core.opl_season_events(e["url"].replace("/course", "/standing")):
                if "Gruppenphase" not in sub["slug"] and sub["id"] not in seen:
                    queue.append(sub)
        except Exception as ex:
            log(f"  Menü {e['label']}: {ex}")
        time.sleep(0.2)
    return list(found.values())


def crawl_brackets(lg):
    """Brackets der Liga laden und alle Matches darin (Veto, Maps, Stats) wie in der Gruppenphase crawlen."""
    out = []
    for e in find_brackets(lg):
        b = core.opl_bracket(e["url"])
        if not b.get("rounds"):
            # nicht ausgelost: trotzdem anzeigen (Hinweis „hat nicht stattgefunden“ bzw. „noch nicht ausgelost“)
            name = (b.get("title") or "").replace(" | OPL", "").split(" - ")[-1].strip() or e["label"]
            out.append({"event": e["id"], "label": name, "url": e["url"], "stage": e["slug"].split("-", 1)[-1], "empty": True})
            log(f"  {name}: nicht ausgelost")
            continue
        n = 0
        for r in b["rounds"]:
            for x in r["matches"]:
                if not x["id"].isdigit():
                    continue
                before = (core.cache.peek(f"match:{x['id']}") or {}).get("fetched")
                try:
                    core.opl_match(x["id"])
                except Exception as ex:
                    log(f"  Match {x['id']} fehlgeschlagen: {ex}")
                    continue
                if (core.cache.peek(f"match:{x['id']}") or {}).get("fetched") != before:
                    n += 1
                    time.sleep(0.3)
        name = b["title"].replace(" | OPL", "").split(" - ")[-1].strip()
        out.append({"event": e["id"], "label": name or e["label"], "url": e["url"], "stage": e["slug"].split("-", 1)[-1]})
        log(f"  Bracket {name}: {sum(len(r['matches']) for r in b['rounds'])} Matches, {n} neu geladen")
    core.cache.set(f"league_brackets:{lg['id']}", out)
    return out


def _all_menu_events(start_url):
    """Alle Events einer Saison über die OPL-Menüs (rekursiv, jedes Event nur einmal)."""
    found, queue = {}, list(core.opl_season_events(start_url))
    while queue:
        e = queue.pop(0)
        if e["id"] in found:
            continue
        found[e["id"]] = e
        try:
            queue += [x for x in core.opl_season_events(e["url"].replace("/course", "/standing")) if x["id"] not in found]
        except Exception as ex:
            log(f"  Menü {e['label']}: {ex}")
        time.sleep(0.2)
    return list(found.values())


def _liga_digits(slug):
    tail = slug.split("-", 1)[-1]
    return sorted(set("".join(re.findall(r"([\d\-]+)Liga", tail) + re.findall(r"Liga(\d)", tail)).replace("-", "")))


def build_templates(cfg):
    """Leere Playoff-/Relegations-Brackets als Vorschau: Struktur aus der Vorsaison (config `bracket_template`),
    Startplätze als Tabellenplätze der Vorsaison (z. B. „1. Liga A3“). Wird wöchentlich neu gebaut."""
    url = cfg.get("bracket_template")
    if not url:
        return
    if core.cache.get("bracket_templates", 7 * 24) is not None:
        return
    events = _all_menu_events(url.replace("/course", "/standing"))
    # Tabellen der Vorsaison → Startplatz jedes Teams
    seeds, done = {}, set()
    for e in events:
        if "Gruppenphase" not in e["slug"]:
            continue
        n = (_liga_digits(e["slug"]) or ["?"])[0]
        page = core.fetch(e["url"].replace("/course", "/standing"))
        # Gruppen-Ligen: Gesamttabelle des Elternevents (jede Seite verlinkt die Tabellen aller Ligen → passende wählen)
        parents = re.findall(r'event\\?/(\d+-[A-Za-z0-9]+-Gruppenphase-[A-Za-z0-9]*?)\\?/standing\\?/overview', page)
        parent = next((x for x in parents if _liga_digits(x) == [n]), None)
        st_url = (f"https://www.opleague.pro/event/{parent}/standing/overview" if parent and "Gruppe" in e["slug"]
                  else e["url"].replace("/course", "/standing"))
        if st_url in done:
            continue
        done.add(st_url)
        try:
            st = core.opl_standings(st_url)
        except Exception as ex:
            log(f"  Vorlage: Tabelle {e['label']} nicht lesbar: {ex}")
            continue
        for t in st["teams"]:
            seeds[t["id"]] = {"n": n, "group": t.get("group") or "", "rank": _int(t["rank"])}
    out = []
    for e in events:
        if not re.search(r"Playoff|Relegation", e["slug"], re.I):
            continue
        b = core.opl_bracket(e["url"])
        if not b.get("rounds"):
            continue
        idx = {x["id"]: (r, x) for r in b["rounds"] for x in r["matches"]}
        rounds = []
        for r in b["rounds"]:
            ms = []
            for x in r["matches"]:
                slots = []
                for t in x["teams"]:
                    src = next((idx[v] for v in x["before"] if v in idx and any(tt["id"] == t["id"] for tt in idx[v][1]["teams"])), None)
                    if src:
                        pr, px = src
                        slots.append({"from": "Verlierer" if x["id"] in px["loser"] else "Sieger", "round": _bracket_title(pr, px)})
                    elif str(t["id"]).startswith("-"):
                        slots.append({"bye": True})          # Freilos / Default Win
                    elif t["id"] in seeds:
                        slots.append({"seed": seeds[t["id"]]})
                    else:
                        slots.append({})
                ms.append({"id": x["id"], "pos": x["pos"], "slots": slots, "winner": x["winner"], "loser": x["loser"],
                           "title": _bracket_title(r, x)})
            rounds.append({"key": r["key"], "titles": r["titles"], "matches": ms})
        label = b["title"].replace(" | OPL", "").split(" - ")[-1].strip().replace("\\/", "/")
        season = re.search(r"(Sommer|Winter)season \d{4}(?:/\d{2})?", b["title"])
        out.append({"label": label, "stage": e["slug"].split("-", 1)[-1], "digits": _liga_digits(e["slug"]),
                    "source": season.group(0) if season else "Vorsaison", "url": e["url"], "rounds": rounds})
        log(f"  Vorlage {label}: {sum(len(r['matches']) for r in rounds)} Matches")
    core.cache.set("bracket_templates", out)


def crawl_matches(lg, refresh_course=True):
    """Lädt alle Matches einer Liga. Gültig sind nur Matches, deren Event-ID zur Liga
    (bzw. ihren Gruppen) gehört – damit zählt nur die ausgewählte Saison."""
    course = core.opl_course(lg["url"], refresh=refresh_course)
    ok, fetched = [], 0
    for mid in course["matches"]:
        before = (core.cache.peek(f"match:{mid}") or {}).get("fetched")
        try:
            m = core.opl_match(mid)
        except Exception as e:
            log(f"  Match {mid} fehlgeschlagen: {e}")
            continue
        if (core.cache.peek(f"match:{mid}") or {}).get("fetched") != before:   # wirklich neu geladen
            fetched += 1
            time.sleep(0.3)   # OPL schonen
        if m.get("event") in course["events"]:
            ok.append(mid)
    core.cache.set(f"league_matches:{lg['id']}", ok)
    log(f"  Matches: {len(ok)} gültig, {fetched} neu geladen (Events {', '.join(course['events'])})")
    try:
        crawl_brackets(lg)
    except Exception as e:
        log(f"  Brackets fehlgeschlagen: {e}")
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


# Rollen der Agents (valorant-api.com, wöchentlich; Fallback falls die API nicht erreichbar ist)
AGENT_ROLES_FALLBACK = {
    "Gekko": "Initiator", "Fade": "Initiator", "Breach": "Initiator", "Tejo": "Initiator", "KAY/O": "Initiator",
    "Skye": "Initiator", "Sova": "Initiator", "Deadlock": "Sentinel", "Chamber": "Sentinel", "Cypher": "Sentinel",
    "Killjoy": "Sentinel", "Vyse": "Sentinel", "Veto": "Sentinel", "Sage": "Sentinel", "Miks": "Controller",
    "Harbor": "Controller", "Viper": "Controller", "Astra": "Controller", "Brimstone": "Controller", "Clove": "Controller",
    "Omen": "Controller", "Raze": "Duelist", "Hush": "Duelist", "Phoenix": "Duelist", "Iso": "Duelist", "Neon": "Duelist",
    "Yoru": "Duelist", "Waylay": "Duelist", "Reyna": "Duelist", "Jett": "Duelist"}
_ROLES = None


def agent_roles():
    global _ROLES
    if _ROLES is None:
        roles = core.cache.get("agent_roles", 7 * 24)
        if not roles:
            try:
                d = json.loads(_http("https://valorant-api.com/v1/agents?isPlayableCharacter=true"))["data"]
                roles = {a["displayName"]: a["role"]["displayName"] for a in d if a.get("role")}
                core.cache.set("agent_roles", roles)
            except Exception:
                roles = core.cache.peek("agent_roles") or {}
        _ROLES = {**AGENT_ROLES_FALLBACK, **roles}
    return _ROLES


ROLE_LIMITS = {"Duelist": 2, "Initiator": 3, "Controller": 2, "Sentinel": 2}


def likely_comp(mp, map_comps, players, tagents, meta=None, mlist=None):
    """Wahrscheinlichste Comp einer Map.
    1. Aufstellung: Spieler der letzten 3 Matches (neuere zählen mehr).
    2. Pro Spieler Agent-Wahrscheinlichkeiten: auf dieser Map gespielt > eigener Pool > Liga-Meta der Map,
       neuere Maps zählen mehr.
    3. Beste Gesamtzuordnung (alle Kombinationen) mit Rollenregeln: mind. 1 Controller, Rollen-Obergrenzen,
       Nähe zur üblichen Rollenverteilung der Map in der Liga.
    Hat das Team die Map gespielt, wird die zuletzt gespielte Comp übernommen und nur die Slots von Spielern
    neu besetzt, die nicht mehr zur Aufstellung gehören."""
    import itertools, math
    roles = agent_roles()
    mm = (meta or {}).get(mp, {})
    played = [m for m in (mlist or []) if m.get("maps")]
    order = {m["id"]: i for i, m in enumerate(reversed(played))}          # 0 = neuestes Match
    decay = lambda mid: 0.75 ** order.get(mid, len(order))
    plist = list(players.values())

    # 1. Aufstellung
    recent = [m["id"] for m in reversed(played)][:3]
    def lineup_score(agg):
        hist = agg.get("history", [])
        seen = {h["m"] for h in hist}
        return sum((3 - i) * 10 for i, mid in enumerate(recent) if mid in seen) + agg["maps"]
    lineup = sorted(plist, key=lambda a: -lineup_score(a))[:5]

    # Übliche Rollenverteilung auf dieser Map (Ø Agents je Rolle pro Comp)
    template = {}
    for a, share in mm.items():
        r = roles.get(a)
        if r:
            template[r] = template.get(r, 0) + share

    # 2. Agent-Wahrscheinlichkeiten pro Spieler
    def probs(agg):
        w = {}
        for h in agg.get("history", []):
            if not h.get("agent"):
                continue
            k = decay(h["m"])
            w[h["agent"]] = w.get(h["agent"], 0) + k * (3.0 if h["map"] == mp else 1.0)
        tot = sum(w.values())
        p = {a: 0.85 * v / tot for a, v in w.items()} if tot else {}
        # Liga-Meta der Map als kleiner Grundwert (ganz ohne eigene Daten: nur Meta)
        meta_w = 0.15 if tot else 1.0
        msum = sum(mm.values()) or 1
        for a, share in sorted(mm.items(), key=lambda kv: -kv[1])[:8]:
            p[a] = p.get(a, 0) + meta_w * share / msum
        return dict(sorted(p.items(), key=lambda kv: -kv[1])[:6])

    def best(slots):
        """slots: Liste von (Name, {Agent: p}) bzw. (Name, fester Agent). Liefert beste Zuordnung."""
        cand = [[(a, p) for a, p in (opts.items() if isinstance(opts, dict) else [(opts, 1.0)])] or [(None, 1e-6)]
                for _, opts in slots]
        top, best_combo = -1e18, None
        for combo in itertools.product(*cand):
            ags = [a for a, _ in combo if a]
            if len(set(ags)) != len(ags):
                continue
            cnt = {}
            for a in ags:
                r = roles.get(a, "?")
                cnt[r] = cnt.get(r, 0) + 1
            if any(cnt.get(r, 0) > lim for r, lim in ROLE_LIMITS.items()):
                continue
            score = sum(math.log(max(p, 1e-6)) for _, p in combo)
            if len(ags) >= 4 and not cnt.get("Controller"):
                score -= 4.0                      # ohne Smokes praktisch nie
            if template and len(ags) == 5:
                score -= 0.6 * sum(abs(cnt.get(r, 0) - template.get(r, 0)) for r in ROLE_LIMITS)
            if score > top:
                top, best_combo = score, combo
        return best_combo

    # Gespielte Comp: zuletzt gespielte Besetzung dieser Map (Spieler → Agent)
    by_match = {}
    for agg in plist:
        for h in agg.get("history", []):
            if h["map"] == mp and h.get("agent"):
                by_match.setdefault(h["m"], {})[agg["name"]] = h["agent"]
    played_on_map = [mid for mid in by_match if len(by_match[mid]) == 5]
    # noch aktiv = in einem der letzten 3 Matches gespielt (Teams rotieren oft 6–7 Spieler)
    active = {a["name"] for a in plist if any(h["m"] in recent for h in a.get("history", []))}
    if played_on_map:
        last = min(played_on_map, key=lambda mid: order.get(mid, 99))
        assign = by_match[last]
        keep = {n: a for n, a in assign.items() if n in active}
        free = [a for a in lineup if a["name"] not in keep][:5 - len(keep)]
        slots = [(n, a) for n, a in keep.items()] + [(a["name"], probs(a)) for a in free]
        combo = best(slots)
        if combo:
            picks = [{"player": n, "agent": a, "p": None if n in keep else round(p, 2), "new": n not in keep}
                     for (n, _), (a, p) in zip(slots, combo) if a]
            n_same = sum(v[0] for k, v in map_comps.items() if list(k) == sorted(assign.values()))
            return {"agents": [x["agent"] for x in picks], "players": picks, "src": "played",
                    "n": n_same or 1, "of": sum(v[0] for v in map_comps.values()) or len(played_on_map),
                    "changed": [x["player"] for x in picks if x["new"]]}

    # Prognose
    slots = [(a["name"], probs(a)) for a in lineup]
    while len(slots) < 5:   # zu wenige Spieler mit Daten → mit Liga-Meta auffüllen
        slots.append((None, probs({"history": []}) or {a: 0.1 for a, _ in sorted(tagents.items(), key=lambda kv: -kv[1])[:6]}))
    combo = best(slots)
    if not combo:
        return None
    picks = [{"player": n, "agent": a, "p": round(p, 2)} for (n, _), (a, p) in zip(slots, combo) if a]
    return {"agents": [x["agent"] for x in picks], "players": picks, "src": "predicted"}


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
        if mine.get("val") == "Q":
            outcome = "Q"            # Wildcard: Match nicht nötig, beide Teams qualifiziert
        elif mine.get("winner"):
            outcome = "W"
        elif theirs.get("winner"):
            outcome = "L"
        elif played:
            outcome = "W" if won > lost else "L" if lost > won else "D"
        elif m.get("state") == "closed" and mine.get("val") and mine.get("val") == theirs.get("val"):
            outcome = "D"
        else:
            outcome = None
        kind = "played" if played else "qual" if outcome == "Q" else ("bye" if opp.get("id") in ("-1", "") or "default" in opp.get("name", "").lower()
                                         else "ff" if outcome else None)
        entry = {"id": m["id"], "start": m.get("start"), "state": m.get("state"), "format": m.get("format"),
                 "opp": opp, "home": me == 1,
                 "first": ("self" if m["first"] == me else "opp") if m.get("first") else None, "score": [won, lost] if played else None, "outcome": outcome, "kind": kind,
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
        st["comp"] = likely_comp(mp, comps.get(mp, {}), players, tagents, meta, mlist)
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
                                      "atk_won": 0, "atk_n": 0, "rounds": 0, "close": 0, "agents": {}, "agent_won": {}, "mirror": {}, "nm_n": {}, "nm_w": {}, "teams": 0})
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
            side = {t: {p["agent"] for p in x["players"] if p["team"] == t and p["agent"]} for t in (1, 2)}
            for a in side[1] & side[2]:          # Mirror: beide Teams spielen den Agent
                s["mirror"][a] = s["mirror"].get(a, 0) + 1
            for team in (1, 2):
                ags = sorted(side[team])
                if not ags:
                    continue
                s["teams"] += 1
                won = x["s1" if team == 1 else "s2"] > x["s2" if team == 1 else "s1"]
                for a in ags:
                    s["agents"][a] = s["agents"].get(a, 0) + 1
                    s["agent_won"][a] = s["agent_won"].get(a, 0) + won
                    if a not in side[3 - team]:      # nur ein Team hatte ihn
                        s["nm_n"][a] = s["nm_n"].get(a, 0) + 1
                        s["nm_w"][a] = s["nm_w"].get(a, 0) + won
                    agents[a] = agents.get(a, 0) + 1
                if len(ags) == 5:
                    key = tuple(ags)
                    c = comps.setdefault(x["map"], {}).setdefault(key, [0, 0])
                    c[0] += 1
                    c[1] += x["s1" if team == 1 else "s2"] > x["s2" if team == 1 else "s1"]
    out_maps = {}
    for name, s in maps.items():
        t = s.pop("teams")
        aw, mir, nmn, nmw = s.pop("agent_won"), s.pop("mirror"), s.pop("nm_n"), s.pop("nm_w")
        # [Agent, Team-Maps, Anteil %, gewonnen, Mirror-Maps, Team-Maps ohne Mirror, davon gewonnen]
        s["agents"] = [[a, c, round(100 * c / t), aw.get(a, 0), mir.get(a, 0), nmn.get(a, 0), nmw.get(a, 0)]
                       for a, c in sorted(s["agents"].items(), key=lambda kv: -kv[1])] if t else []
        s["comps"] = [[list(k), n, w] for k, (n, w) in sorted(comps.get(name, {}).items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:6]]
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
    # Pro Map: Bilanz jedes Teams und Stats jedes Spielers
    map_teams, map_players = {}, {}
    for tid in names:
        o = opl_by_team.get(tid) or {}
        for mpn, v in (o.get("maps") or {}).items():
            if v["played"] or v["pick"] or v["ban"]:
                map_teams.setdefault(mpn, []).append({"team": tid, "name": names[tid], **{k: v[k] for k in
                    ("played", "won", "rw", "rl", "pick", "ban", "opp_pick", "opp_ban", "decider", "atk", "def")}})
        for agg in (o.get("players") or {}).values():
            per = {}
            for h in agg.get("history", []):
                x = per.setdefault(h["map"], {"maps": 0, "won": 0, "acs": 0, "k": 0, "d": 0, "a": 0, "fb": 0, "agents": {}})
                x["maps"] += 1; x["won"] += h["us"] > h["them"]; x["acs"] += h["acs"]
                for f in ("k", "d", "a", "fb"):
                    x[f] += h[f]
                if h.get("agent"):
                    x["agents"][h["agent"]] = x["agents"].get(h["agent"], 0) + 1
            for mpn, x in per.items():
                map_players.setdefault(mpn, []).append({
                    "name": agg["name"], "team": tid, "team_name": names[tid], "maps": x["maps"], "won": x["won"],
                    "acs": round(x["acs"] / x["maps"]), "kd": round(x["k"] / x["d"], 2) if x["d"] else float(x["k"]),
                    "k": x["k"], "d": x["d"], "a": x["a"], "fbpm": round(x["fb"] / x["maps"], 2),
                    "agents": [a for a, _ in sorted(x["agents"].items(), key=lambda kv: -kv[1])]})
    for mpn, s in out_maps.items():
        s["teams_list"] = sorted(map_teams.get(mpn, []), key=lambda t: (-t["played"], -t["won"]))
        s["players"] = map_players.get(mpn, [])
    return {"matches": sum(1 for m in matches if m["maps"]), "veto_matches": nv, "maps_played": nmaps,
            "bo": int(max(bo.items(), key=lambda kv: kv[1])[0]) if bo else 3,
            "maps": out_maps,
            "agents": [[a, c, round(100 * c / total_teams)] for a, c in sorted(agents.items(), key=lambda kv: -kv[1])] if total_teams else [],
            "players": players}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def matchday(teams):
    """Aktueller Spieltag = häufigste Spielzahl (robust gegen Freilose und Nachholspiele)."""
    cnt = {}
    for t in teams:
        n = _int(t["stats"].get("played")) or 0
        cnt[n] = cnt.get(n, 0) + 1
    return max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0] if cnt else 0


def rebuild_previous(teams, opl_by_team, swiss):
    """Startwert ohne gespeicherte Historie: Tabelle vor dem letzten Spieltag aus den Ergebnissen
    zurückrechnen (letztes Ergebnis jedes Teams mit voller Spielzahl abziehen). Sortierung wie OPL:
    Punkte (Swiss: Siege, dann Buchholz), dann Rundendifferenz – Tiebreaker nur angenähert."""
    md = matchday(teams)
    rows = []
    for t in teams:
        st = t["stats"]
        wins, draws = _int(st.get("wins")) or 0, _int(st.get("draws")) or 0
        pts = _int(st.get("points")) if not swiss else wins
        rd = _int(st.get("round dif")) or 0
        if pts is None:
            pts = 2 * wins + draws
        res = [m for m in (opl_by_team.get(t["id"]) or {}).get("matches", []) if m["outcome"]]
        res.sort(key=lambda m: m.get("start") or 0)
        for last in res[max(0, md - 1):]:   # alles nach Spieltag md-1 abziehen
            if swiss:
                pts -= last["outcome"] == "W"
            else:
                pts -= {"W": 2, "D": 1}.get(last["outcome"], 0)
            rd -= sum(x["us"] - x["them"] for x in last["maps"])
        tb = _int(st.get("tiebreaker")) or 0   # Buchholz lässt sich nicht zurückrechnen → aktueller Wert als Näherung
        rows.append((t, pts, tb, rd))
    ranks = {}
    for g in {t.get("group", "") for t in teams}:
        grp = sorted([r for r in rows if r[0].get("group", "") == g], key=lambda r: (-r[1], -r[2], -r[3], _int(r[0]["rank"]) or 99))
        for i, (t, *_rest) in enumerate(grp):
            ranks[t["id"]] = i + 1
    return md - 1, ranks


def rank_history(lg, st, opl_by_team):
    """Speichert pro Spieltag den letzten Tabellenstand und liefert den Vergleichsstand
    (Ende des vorigen Spieltags). Spieltag = höchste Spielzahl eines Teams."""
    teams = st["teams"]
    md = matchday(teams)
    key = f"standing_hist:{lg['id']}"
    hist = core.cache.peek(key) or []
    if not hist and md > 0:
        pmd, pranks = rebuild_previous(teams, opl_by_team, "swiss" in (st.get("title", "") + json.dumps(st.get("columns", []))).lower()
                                       or "lives" in st.get("columns", []))
        hist = [{"md": pmd, "t": 0, "src": "rebuilt", "ranks": pranks}]
    cur = {"md": md, "t": time.time(), "src": "opl", "ranks": {t["id"]: _int(t["rank"]) for t in teams}}
    if hist and hist[-1]["md"] == md:
        hist[-1] = cur
    else:
        hist.append(cur)
    hist = hist[-12:]
    core.cache.set(key, hist)
    prev = next((h for h in reversed(hist) if h["md"] < md), None)
    return prev


# ------------------------------------------------------------------ YouTube-VODs (RSS, ohne API-Schlüssel)
def _http(url, cookie=None):
    h = {"User-Agent": "Mozilla/5.0 (OPL Scout)"}
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def fetch_youtube(cfg):
    """Neue Videos der konfigurierten Kanäle (RSS: jeweils die 15 neuesten) und einzeln
    eingetragener Videos (oEmbed) dauerhaft im Cache sammeln."""
    yt = cfg.get("youtube") or {}
    videos = core.cache.peek("yt:videos") or {}
    new = 0
    for ch in yt.get("channels", []):
        try:
            x = _http("https://www.youtube.com/feeds/videos.xml?channel_id=" + ch["id"])
        except Exception as e:
            log(f"  YouTube {ch.get('name')}: {e}")
            continue
        for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
            vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", e).group(1)
            pub = re.search(r"<published>(.*?)</published>", e).group(1)
            new += vid not in videos
            videos[vid] = {"id": vid, "title": unescape(re.search(r"<title>(.*?)</title>", e, re.S).group(1)),
                           "channel": ch.get("name"), "official": bool(ch.get("official")),
                           "published": time.mktime(time.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone}
    for vid in yt.get("videos", []):
        vid = vid if isinstance(vid, str) else vid.get("id")
        if vid in videos:
            continue
        try:
            o = json.loads(_http("https://www.youtube.com/oembed?format=json&url=" +
                                 urllib.parse.quote("https://www.youtube.com/watch?v=" + vid)))
            # Upload-Datum steht nicht im oEmbed → von der Videoseite (Cookie überspringt die Zustimmungsseite)
            pub = None
            try:
                page = _http("https://www.youtube.com/watch?v=" + vid, cookie="SOCS=CAI")
                d = re.search(r'itemprop="(?:uploadDate|datePublished)" content="([^"]+)"', page)
                if d:
                    from datetime import datetime
                    pub = datetime.fromisoformat(d.group(1)).timestamp()
            except Exception:
                pass
            official = any(c.get("official") and c.get("name") == o.get("author_name") for c in yt.get("channels", []))
            videos[vid] = {"id": vid, "title": o.get("title", ""), "channel": o.get("author_name"),
                           "official": official, "published": pub}
            new += 1
        except Exception as e:
            log(f"  YouTube-Video {vid}: {e}")
    core.cache.set("yt:videos", videos)
    log(f"YouTube: {len(videos)} Videos bekannt, {new} neu")


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[^0-9a-zäöüß]+", "", s)


SEASON_RE = re.compile(r"(sommer|summer|winter)\s*(?:season)?\s*'?(\d{2,4})", re.I)


def _season_ok(title, league_title):
    """Titel mit fremder Saison (z. B. „Sommer 26“ in der Wintersaison) aussortieren."""
    m = SEASON_RE.search(title or "")
    if not m:
        return True
    cur = SEASON_RE.search(league_title or "")
    if not cur:
        return True
    same_kind = (m.group(1).lower() in ("sommer", "summer")) == (cur.group(1).lower() in ("sommer", "summer"))
    return same_kind and m.group(2)[-2:] == cur.group(2)[-2:]


def match_vods(matches, league_title):
    """Match-ID → [Videos]. Zuordnung: beide Teamnamen im Titel, passende Saison und
    Video nach Matchbeginn (bis 45 Tage) veröffentlicht."""
    videos = core.cache.peek("yt:videos") or {}
    out = {}
    for v in videos.values():
        if not _season_ok(v["title"], league_title):
            continue
        nt = _norm(v["title"])
        # Paarungen im Titel („A ⚔️ B | C ⚔️ D“ oder „A vs B“): beide Teams müssen in derselben stehen
        parts = [_norm(p) for p in re.split(r"\|", unicodedata.normalize("NFKC", v["title"])) if "⚔" in p or re.search(r"\bvs\.?\b", p, re.I)]
        # YouTube kürzt Titel auf 100 Zeichen → letzte Paarung evtl. abgeschnitten („KIT SC HN…“)
        cut = len(v["title"]) >= 95 and parts
        last_sides = [_norm(x) for x in re.split(r"⚔️?|\bvs\.?\b", unicodedata.normalize("NFKC", v["title"].split("|")[-1]), flags=re.I)] if cut else []
        for m in matches:
            a, b = _norm(m["t1"].get("name")), _norm(m["t2"].get("name"))
            if len(a) < 3 or len(b) < 3:
                continue
            start = m.get("start") or 0
            if v["published"] and start and not (start - 6 * 3600 <= v["published"] <= start + 45 * 86400):
                continue
            idx = next((i + 1 for i, p in enumerate(parts) if a in p and b in p), None)
            if idx is None and cut and len(last_sides) == 2 and v["published"] and start and 0 <= v["published"] - start <= 2 * 86400:
                l, r = last_sides
                if (l == a and b.startswith(r)) or (l == b and a.startswith(r)):
                    idx = len(parts)   # abgeschnittene letzte Paarung, nur bei Upload kurz nach dem Match
            if idx is None and (parts or a not in nt or b not in nt):
                continue
            # Position im Video (offizielle Streams zeigen 2 Matches hintereinander)
            out.setdefault(m["id"], []).append({"id": v["id"], "title": v["title"], "channel": v["channel"],
                                                 "official": v["official"], "part": idx if len(parts) > 1 else None})
    for lst in out.values():
        lst.sort(key=lambda x: (not x["official"], x["channel"] or ""))
    return out


def _bracket_title(r, x):
    t = r["titles"]
    return (t[x["pos"]] if x["pos"] < len(t) else "") or next((v for v in t if v), "")


def bracket_paths(tid, brackets):
    """Wo steht das Team in jedem Bracket? → nächstes Match mit Zielen bei Sieg/Niederlage,
    ausgeschieden, qualifiziert oder Turniersieg."""
    out = []
    for d, b in brackets:
        idx = {x["id"]: (r, x) for r in b["rounds"] for x in r["matches"]}
        mine = [(r, x) for r in b["rounds"] for x in r["matches"] if any(t["id"] == tid for t in x["teams"])]
        if not mine:
            continue
        r, x = mine[-1]   # letzte Runde, in der das Team steht = aktuelle Position
        me = next(t for t in x["teams"] if t["id"] == tid)
        opp = next((t for t in x["teams"] if t["id"] != tid), None)
        def target(ids, how=None):
            if not ids or ids[0] not in idx:
                return None
            tr, tx = idx[ids[0]]
            known = [t for t in tx["teams"] if t["id"] not in (tid, "", "0") and t["name"] not in ("TBD", "")]
            others = [idx[v][1] for v in tx["before"] if v in idx and v != x["id"]]
            if known:
                vs = known[0]["name"]
            elif others:
                o = others[0]
                who = "Verlierer" if tx["id"] in o["loser"] else "Sieger"   # wer aus dem anderen Match dorthin kommt
                vs = f"{who} aus " + " vs ".join(t["name"] for t in o["teams"] if t["name"]) if o["teams"] else "offen"
            else:
                vs = "offen"
            return {"round": _bracket_title(tr, tx), "match": tx["id"], "vs": vs}
        entry = {"label": d["label"], "event": d["event"], "url": d["url"], "round": _bracket_title(r, x), "match": x["id"],
                 "opp": opp["name"] if opp else None, "opp_id": opp["id"] if opp else None}
        sc = str(me.get("score") or "")
        if x["state"] != "closed":
            entry.update(state="next", win=target(x["winner"], "Sieger"), lose=target(x["loser"], "Verlierer"))
        elif sc == "Q":
            entry["state"] = "qualified"
        else:
            won = "--winner" in me["cls"] or sc == "W" or (opp and sc.isdigit() and str(opp.get("score") or "").isdigit() and int(sc) > int(opp["score"]))
            if won and not x["winner"]:
                entry["state"] = "champion"
            elif won:
                entry.update(state="waiting", win=target(x["winner"], "Sieger"))
            elif x["loser"]:
                entry.update(state="waiting", lose=target(x["loser"], "Verlierer"))
            else:
                entry["state"] = "out"
        out.append(entry)
    return out


def export(out, leagues, region, repo, ref, base_cfg):
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta_leagues = []
    team_ids = set()
    team_league = {}   # Team-ID → Liga-ID (Links in Brackets auf Teamseiten anderer Ligen)
    league_matches = {lg["id"]: [m for m in (core.cache.peek(f"match:{mid}") for mid in
                                  core.cache.peek(f"league_matches:{lg['id']}") or []) if m]
                      for lg in leagues}
    opl_by_team, opl_all_by_team, paths_by_team, gs_by_team = {}, {}, {}, {}
    metas = {lid: league_meta(ms) for lid, ms in league_matches.items()}
    # K.-o.-Brackets (Wildcard, Playoffs, Relegation) aller Ligen
    league_brackets, bmatch, bmeta = {}, {}, {}
    for lg in leagues:
        lst = []
        for d in core.cache.peek(f"league_brackets:{lg['id']}") or []:
            b = core.cache.peek("bracket:" + d["url"])
            if not b:
                continue
            lst.append((d, b))
            for r in b["rounds"]:
                for x in r["matches"]:
                    m = core.cache.peek(f"match:{x['id']}")
                    if m:
                        bmatch[x["id"]] = m
                        bmeta[x["id"]] = {"stage": _bracket_title(r, x), "bracket": d["label"], "event": d["event"],
                                          "wildcard": "Wildcard" in d.get("stage", "") + d["label"], "league": lg["id"]}
        league_brackets[lg["id"]] = lst
    all_brackets = [x for lst in league_brackets.values() for x in lst]
    # Vorschau-Brackets (Vorlage aus der Vorsaison), solange OPL das Event noch nicht angelegt hat
    templates = core.cache.peek("bracket_templates") or []
    cur_seed = {}   # (Liga-Nr., Gruppe, Platz) → Team nach aktuellem Tabellenstand
    for lg in leagues:
        st = core.cache.peek("standing:" + lg["url"])
        for t in (st or {}).get("teams", []):
            cur_seed[(league_number(lg), t.get("group") or "", _int(t["rank"]))] = {"id": t["id"], "name": t["name"], "logo": t.get("logo")}
    def template_for(lg):
        n, real = league_number(lg), {d.get("stage") for d, _ in league_brackets.get(lg["id"], [])}
        out = []
        for tp in templates:
            if n not in tp["digits"] or tp["stage"] in real:
                continue
            rounds = [{"key": r["key"], "titles": r["titles"], "matches": [{**x, "slots": [
                {**sl, "team": cur_seed.get((sl["seed"]["n"], sl["seed"]["group"], sl["seed"]["rank"]))} if sl.get("seed") else sl
                for sl in x["slots"]]} for x in r["matches"]]} for r in tp["rounds"]]
            out.append({"event": "tpl-" + tp["stage"], "label": tp["label"], "template": True, "source": tp["source"],
                        "digits": tp["digits"], "url": tp["url"], "rounds": rounds})
        return out
    bracket_league = {d["event"]: lid for lid, lst in league_brackets.items() for d, _ in lst}
    for lg in leagues:
        st = core.cache.peek("standing:" + lg["url"])
        if not st:
            continue
        summaries = {}
        for t in st["teams"]:
            # Statistik = Gruppenphase + Playoffs/Relegation; Wildcardturnier nur optional (Umschalter, Standard aus)
            bm = [m for m in bmatch.values() if t["id"] in (m["t1"]["id"], m["t2"]["id"])]
            ko = [m for m in bm if not bmeta[m["id"]]["wildcard"]]
            wc = [m for m in bm if bmeta[m["id"]]["wildcard"]]
            opl = opl_by_team[t["id"]] = team_opl(t["id"], league_matches[lg["id"]] + ko, metas[lg["id"]])
            gs_by_team[t["id"]] = team_opl(t["id"], league_matches[lg["id"]], metas[lg["id"]]) if ko else opl
            if wc:
                full = team_opl(t["id"], league_matches[lg["id"]] + ko + wc, metas[lg["id"]])
                opl["matches"] = full["matches"]          # Match-Liste immer komplett
                opl_all_by_team[t["id"]] = full
            for e in opl["matches"]:
                if e["id"] in bmeta:
                    e.update({k: v for k, v in bmeta[e["id"]].items() if k != "league"})
            paths_by_team[t["id"]] = [{**p, "league": bracket_league.get(p["event"])} for p in bracket_paths(t["id"], all_brackets)]
            # aktive Spieler = mind. ein Ligaspiel; ohne gespielte Matches → OPL-Position
            active = set(opl["players"]) if opl["maps_played"] else None
            summaries[t["id"]] = core.team_summary(t["id"], region, active)
        ko_league = [m for mid, m in bmatch.items() if bmeta[mid]["league"] == lg["id"] and not bmeta[mid]["wildcard"]]
        overview = league_overview(league_matches[lg["id"]] + ko_league, opl_by_team, st["teams"])
        vods = match_vods(league_matches[lg["id"]] + list(bmatch.values()), st.get("title", ""))
        for t in st["teams"]:
            for o in (opl_by_team.get(t["id"]), opl_all_by_team.get(t["id"])):
                for e in (o or {}).get("matches", []):
                    if e["id"] in vods:
                        e["vods"] = vods[e["id"]]
        # Brackets dieser Liga für die Playoff-Ansicht (mit Termin, Format, VODs aus den Match-Details)
        brackets_out = []
        for d, b in league_brackets.get(lg["id"], []):
            rounds = []
            for r in b["rounds"]:
                ms = []
                for x in r["matches"]:
                    m = bmatch.get(x["id"]) or {}
                    ms.append({**x, "start": m.get("start"), "format": m.get("format"), "vods": vods.get(x["id"], []),
                               "maps": [{"map": mp["map"], "s1": mp["s1"], "s2": mp["s2"]} for mp in m.get("maps", [])],
                               "title": _bracket_title(r, x)})
                rounds.append({"key": r["key"], "titles": r["titles"], "matches": ms})
            entry = {"event": d["event"], "label": d["label"], "url": d["url"], "rounds": rounds}
            if not rounds:
                entry["empty"] = True
                # Gruppenphase läuft schon → ein nicht ausgelostes Turnier vor der Saison (Wildcard) fand nicht statt
                entry["note"] = ("Hat diese Saison nicht stattgefunden – auf OPL nie ausgelost."
                                 if "Wildcard" in d.get("stage", "") else "Noch nicht ausgelost.")
            brackets_out.append(entry)
        log(f"  {lg['name']}: {len(vods)} Matches mit VOD")
        prev = rank_history(lg, st, gs_by_team)   # Tabelle: nur Gruppenphase
        movement = {"md": prev["md"], "src": prev["src"], "ranks": prev["ranks"]} if prev else None
        (data / f"league-{lg['id']}.json").write_text(json.dumps(
            {**st, "id": lg["id"], "name": lg["name"], "summaries": summaries, "overview": overview, "movement": movement, "brackets": brackets_out + template_for(lg),
             "updated": time.time() - (core.cache.age("standing:" + lg["url"]) or 0)},
            ensure_ascii=False, separators=(",", ":")))
        meta_leagues.append({"id": lg["id"], "name": lg["name"], "url": lg["url"], "title": st["title"],
                             "zones": lg.get("zones") or []})
        team_ids.update(t["id"] for t in st["teams"])
        team_league.update({t["id"]: lg["id"] for t in st["teams"]})


    rank_times = []
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
                    if s.get("updated") and s.get("current"):
                        rank_times.append(s["updated"])
        gs = opl_by_team.get(tid) or {"maps": {}, "players": {}, "matches": [], "agents": [], "maps_played": 0}
        full = opl_all_by_team.get(tid)
        extra = {}
        if full:   # Teamseite: Umschalter „inkl. Wildcardturnier“ (Standard aus)
            extra["opl_wc"] = {k: full[k] for k in ("maps", "players", "agents", "maps_played", "record")}
        if paths_by_team.get(tid):
            extra["brackets"] = paths_by_team[tid]
        (data / f"team-{tid}.json").write_text(json.dumps(
            {**team, "stats": stats, "opl": gs, **extra}, ensure_ascii=False, separators=(",", ":")))

    (data / "meta.json").write_text(json.dumps({
        "generated": time.time(),
        "repo": repo, "ref": ref, "region": region,
        "workflow": "update.yml",
        "schedule_minute": base_cfg.get("schedule_minute", 17),
        "opl_daily": base_cfg.get("opl_daily", "23:00"),
        # Rang-Updates: neuestes und ältestes Spieler-Update (stündlich werden die ältesten zuerst erneuert)
        "ranks_updated": max(rank_times) if rank_times else None,
        "ranks_oldest": min(rank_times) if rank_times else None,
        "opl_updated": max([core.cache.peek("standing:" + lg["url"]) and
                            time.time() - (core.cache.age("standing:" + lg["url"]) or 0) or 0 for lg in leagues] or [0]),
        "leagues": meta_leagues,
        "team_league": team_league,
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

    if not a.export_only and not a.skip_opl:
        try:
            build_templates(base_cfg)
        except Exception as e:
            log(f"Bracket-Vorlagen fehlgeschlagen: {e}")
    if not a.export_only:
        try:
            fetch_youtube(base_cfg)
        except Exception as e:
            log(f"YouTube fehlgeschlagen: {e}")
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
    core.cache.save()   # Tabellen-Historie (für die Pfeile) sichern


if __name__ == "__main__":
    main()
