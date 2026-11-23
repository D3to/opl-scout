# OPL Scout

Web-Tool für die Uniliga-VALORANT-Ligen auf OPLeague: Tabelle, Teams, Spieler, Matches und Meta der
Saison – mit Rängen aus Ranked und Rundendaten der Ligaspiele. Läuft als statische Seite auf
**GitHub Pages**, eine **GitHub Action** aktualisiert die Daten. Jede Ansicht hat einen teilbaren Link (🔗).

## Was die Seite zeigt

**Seitenleiste** (Reiter oben): **Tabelle** mit Zonen (Playoffs, Klassenerhalt, Relegation, Abstieg),
Pfeilen gegenüber dem letzten Spieltag und Ø-Rang je Team (⚙ = Anzeige/Sortierung) · **Meta** · **K.-o.** ·
**Verlauf** (alle gespielten Matches, neueste zuerst, Suche nach Team/Map/Spieler/Runde, ⚙ = Sortierung und
kampflose Siege/Freilose ausblenden). Nach der Gruppenphase zeigt die Leiste **Endstand | Playoffs | Bilanz**
(Status im Bracket, Saisonergebnis); zum Ansehen vorab: `?demo=playoffs` bzw. `?demo=ende`.

| Seite | Inhalt |
|---|---|
| **Teamseite** | Kennzahlen-Chips (Platz, Zone, S-U-N, Runden, K.-o.-Ergebnisse), Ø-Rang aktuell/Peak, Spieler (OPL- oder Ranked-Stats, ADR/KAST, Badges, errechnete Rolle), Map-Pool mit Angriff/Verteidigung, **Spielweise** je Map (Pistol, First Kills, Thrifty, Operator, Plants/Retakes je Site, Kaufarten, ungewöhnliche Waffen), Agents, Rollen, K.-o.-Weg, Matches |
| **Spielerprofil** | Ranked, Liga-Stats und Runden-Stats mit Ligaplatz und Farbe nach Liga-Ø, Badges, Waffen (Kills/Käufe), Form-Diagramm (ACS, K/D, Kills, FB, ADR, KAST), Agents, Maps, Verlauf |
| **Vorbereitung** | Ban/Pick-Empfehlung, simuliertes Veto (Startteam laut OPL), erwartete Comps, **Tendenzen des Gegners** je Map, Map-Vergleich, Spieler-Duell, Rollen, bisherige Duelle, VODs |
| **Match-Übersicht** | Ergebnis, Veto, **Rundenverlauf** (Ausgang, Thrifties, Loadout, Tooltip mit Kaufwaffen), Teamvergleich (Pistol, Angriff/Verteidigung, First Kills, Thrifties, Plants, Retakes), Scoreboard je Map und gesamt mit Badges |
| **Liga-Meta** | Maps (Pick/Ban, Picker gewinnt, Angriff/Verteidigung, Pistol → R2), Agents, Waffen, Bestenliste (OPL- und Rundendaten-Werte) |
| **Map-Seite** | Kennzahlen, Runden-Stats (Seitenbalance, Plants/Retakes je Site, Rundenausgänge, Waffen, Operator-Agent), Agents nach Rolle mit Mirror-Hinweis, Meta-Diagramm, Comps, Teams, Spieler |
| **Agent-Seite** | Klick auf jedes Agent-Symbol: Pick-/Winrate je Map, Spieler (ADR/KAST auf dem Agent), Teams, Comps |
| **K.-o.-Runden** | Wildcard, Playoffs, Relegation aus dem OPL-Menü der Saison (Vorschau mit der Struktur der Vorsaison, solange OPL das Event nicht angelegt hat). Playoffs/Relegation zählen in die Stats, das Wildcardturnier nur per Schalter |

Gezählt werden nur Matches, deren Event-ID zur gewählten Liga gehört (bei Gruppen-Ligen auch die
Gruppen-Events). Fertige Matches werden nur einmal von OPL geladen.

## Wie es funktioniert

```
GitHub Action (Update data)
  ├─ stündlich :15   HenrikDev: Rang & Ranked-Stats (älteste zuerst, max. 30 min) + bis zu 8 Rundendaten-Anfragen
  ├─ täglich 22:00 UTC  OPLeague: Standings, Kader, Riot-IDs, Matches, Brackets, VODs + bis zu 40 Rundendaten-Anfragen
  ├─ manuell         Liga/Team wie angegeben + bis zu 250 Rundendaten-Anfragen
  ├─ Zustand (Cache) → Branch `data` (immer nur 1 Commit)
  └─ statische Seite + JSON → GitHub Pages
```

Die Zusammenfassung jedes Laufs (Actions → Lauf öffnen) zeigt je Liga, wie viele Matches erkannt und gespielt
wurden (ohne kampflose Siege/Freilose) und wie viele davon Rundendaten haben.

### Rundendaten (HenrikDev, Custom-Games)

OPL-Matches werden als Custom-Games gespielt und stehen in der Riot-Historie der Spieler. Der Export sucht sie über
`v4/matches …?mode=custom` (ältere über `v1/stored-matches` + `v4/match`) und ordnet sie über **Map, Endstand und
Spielernamen** zu – nicht über die Uhrzeit, weil Matches oft verschoben werden (weicht die Spielzeit stark ab,
steht „verlegt“ an der Match-Übersicht). Gespeichert wird eine verdichtete Fassung ohne Positionsdaten als
`data/match-<id>.json`; Team-, Spieler-, Map- und Agent-Werte rechnet der Export daraus zusammen.

- Nicht gefundene Matches werden einmal am Tag erneut gesucht, höchstens 7-mal.
- **Version** (`HD_VERSION` in `build.py`): Liefert die Verdichtung neue Werte, wird die Version erhöht und jedes
  Match einmal neu geholt (bis dahin bleiben die alten Daten). Am schnellsten per manuellem Lauf.

### Begriffe

| Begriff | Bedeutung |
|---|---|
| ADR / KAST / HS% | Schaden pro Runde / Runden mit Kill, Assist, überlebt oder getradet (5 s) / Anteil Kopftreffer |
| ESR | Entry Success Rate: gewonnene Erst-Duelle der Runde (First Kills ÷ (First Kills + First Deaths)) |
| Clutch 1vX | letzter Spieler gewinnt die Runde und überlebt; Kills nach Rundenende zählen nicht |
| Spike-Clutch | letzter Spieler stirbt, Runde wird durch die Spike-Explosion gewonnen (eigener lila Badge) |
| Thrifty | Runde mit mindestens 12.500 weniger Loadout als der Gegner gewonnen – dieselbe Grenze wie Riots Thrifty-Einblendung (aus 188 Maps abgeleitet) |
| Kaufarten | Team-Loadout: Eco < 5.000, Semi-Eco < 10.000, Semi-Buy < 20.000, Full Buy ab 20.000; Pistol-Runden 1 und 13 |
| Haupt-Planter | meiste Plants im Team (mind. 25 % der Plants) |
| Entry-Fragger | meiste Erst-Duelle pro Runde (Stammspieler, mind. 3) |
| Operator-Spieler | meiste Runden mit Operator (mind. 3 und 8 % seiner Runden) |
| Ungewöhnliche Waffen | Kill-Anteil des Teams mind. doppelt so hoch wie im Liga-Ø (mind. 3 % und 5 Kills) |

Werte mit wenig Daten (unter 5 Runden bzw. 3 Plants) erscheinen ohne Grün/Rot.

**Manuelle Updates** auf der Seite (Button „Login“):

| Login | Was passiert | Wer sieht das Ergebnis |
|---|---|---|
| **Admin** (GitHub-Token) | startet die Action für ein Team, eine Liga oder fügt eine neue Liga hinzu | alle, nach 1–5 min |
| **Eigener API-Key** (HenrikDev) | lädt die Stats eines Teams direkt im Browser | nur man selbst (im Browser gespeichert) |
| ohne Login | nur ansehen | – |

Warum nicht direkt tracker.gg? tracker.gg blockiert automatisierte Abfragen (Cloudflare) und hat
keine öffentliche Valorant-API. Die Stats kommen aus der inoffiziellen **HenrikDev-API**; tracker.gg ist pro Spieler verlinkt.

## Einrichtung auf GitHub (einmalig)

1. **Repo anlegen**: auf github.com ein neues, **öffentliches** Repo erstellen (GitHub Pages ist
   für private Repos kostenpflichtig). Dann diesen Ordner pushen:
   ```bash
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```
2. **API-Key als Secret**: Repo → *Settings → Secrets and variables → Actions → New repository secret*
   Name `HENRIKDEV_API_KEY`, Wert = dein Key (`HDEV-…`).
3. **Pages aktivieren**: *Settings → Pages → Build and deployment → Source: GitHub Actions*.
4. **Erster Lauf**: *Actions → Update data → Run workflow*. Danach ist die Seite unter
   `https://<user>.github.io/<repo>/` erreichbar. Ab dann läuft es automatisch (stündlich Ränge, täglich OPL).
   (Optional schneller: den lokalen Cache als Startzustand hochladen, siehe unten.)
5. **Admin-Token** (für den Login auf der Seite): *GitHub → Settings → Developer settings →
   Fine-grained personal access tokens → Generate new token*
   - Repository access: *Only select repositories* → dieses Repo
   - Permissions: **Actions: Read and write** (sonst nichts)
   - Token auf der Seite unter *Login → Admin* eintragen. Er wird nur im eigenen Browser
     gespeichert und nur an api.github.com geschickt.

### Startzustand aus dem lokalen Cache (optional)
Damit der erste Lauf nicht alle ~500 Spieler neu laden muss:
```bash
python3 build.py --export-only            # legt .state/cache.json an
cd .state && git init -q -b data && git add cache.json && git commit -qm "Startzustand" \
  && git push -f https://github.com/<user>/<repo>.git data && cd ..
```

## Lokal testen

```bash
python3 build.py --export-only            # nur exportieren, keine API-Anfragen
python3 build.py --matches-only           # nur OPL-Matches crawlen (Veto, Maps, Liga-Stats)
python3 build.py --league 8285            # eine Liga aktualisieren (braucht Key in .env)
python3 build.py --team 11688             # ein Team komplett neu laden
python3 -m http.server -d _site 8765      # → http://localhost:8765
```
Seitenleiste nach der Gruppenphase vorab ansehen: `http://localhost:8765/?demo=playoffs` bzw. `?demo=ende`.
Der Key für lokale Läufe steht in `.env` (`HENRIKDEV_API_KEY=…`). `.env` wird nie committet.

## Ligen

Die Standard-Ligen stehen in [`config/leagues.json`](config/leagues.json). Weitere Ligen kann ein Admin
auf der Seite hinzufügen: Standing-URL ins Feld oben eintragen → *Öffnen* (landet im Branch `data`).

## Grenzen

- Der kostenlose HenrikDev-Key erlaubt 30 Anfragen/min (2 pro Spieler). Alle ~500 Spieler der
  drei Ligen brauchen ca. 40 min. Ein stündlicher Lauf aktualisiert 30 min lang die **ältesten**
  Daten zuerst, jeder Spieler ist also spätestens nach ~1,5 h wieder aktuell. Standings, Kader und
  Matches lädt der tägliche Lauf neu.
- Ein manuelles Admin-Update wartet, falls gerade das stündliche Update läuft (max. ~30 min).
- GitHub startet geplante Läufe oft einige Minuten verspätet und pausiert sie in Repos ohne
  Aktivität nach 60 Tagen (dann im Actions-Tab wieder aktivieren).
- Riot-IDs stammen aus den OPL-Profilen; ist dort eine alte ID hinterlegt, findet die API den Account nicht.
- Rundendaten: Die Riot-Historie liefert nur die letzten ~10 Custom-Games eines Spielers; ältere Spiele findet
  der Export über die gespeicherte Historie von HenrikDev. Ohne Riot-ID bei beiden Teams wird ein Match nicht gefunden.
- Rundendaten-Anfragen teilen sich das Ratenlimit mit den Rang-Updates (pro Lauf begrenzt, siehe oben).
