# OPL Scout

Web-Tool für die Uniliga-VALORANT-Ligen auf OPLeague: zeigt die Standings, per Klick den Kader
jedes Teams (Riot-IDs aus den OPL-Profilen) und für jeden Spieler:

- aktueller Rang + RR (+ letzte Änderung), Peak-Rang (mit Season)
- K/D, HS %, ADR, ACS, Winrate, meistgespielte Agents (mit Bild) aus den letzten ≤ 20 Competitive-Matches
- Link zum tracker.gg-Profil
- Team-Durchschnitt als Rang-Symbol: **Top 5 Spieler** (Standard), nur Spieler, alle Mitglieder
- **Map-Pool pro Team** aus den Ligaspielen: eigene/gegnerische Picks und Bans, Decider, W–L und
  Runden pro Map, Startseite
- **Match-Liste** der Saison mit Ergebnis, Map-Scores und Veto-Reihenfolge
- Spieler-Stats wahlweise aus der **OPL-Liga** (Standard: ACS, K/D, Econ, First Bloods, Map-Winrate,
  Agents) oder aus **Ranked** (letzte 20 Spiele). Rang und Peak kommen immer aus Ranked.

- **K.-o.-Runden** (Wildcard, Playoffs, Relegation): werden automatisch im OPL-Menü der Saison gefunden,
  sobald OPL sie anlegt. Bracket-Ansicht (Upper/Lower Bracket), Runde in der Match-Liste, „Weg im Bracket“
  auf der Teamseite (nächstes Match, Ziel bei Sieg/Niederlage). Playoffs und Relegation zählen in die Stats,
  das Wildcardturnier nur per Umschalter (Standard aus). Noch nicht angelegte Playoffs erscheinen als Vorschau
  mit der Struktur der Vorsaison (`bracket_template` in `config/leagues.json`).

Gezählt werden nur Matches, deren Event-ID zur gewählten Liga gehört (bei Gruppen-Ligen auch die
Gruppen-Events, z. B. „1. Liga – Gruppe A/B“). Fertige Matches werden nur einmal von OPL geladen.

Läuft als statische Seite auf **GitHub Pages**. Eine **GitHub Action** aktualisiert die Daten stündlich.

## Wie es funktioniert

```
GitHub Action (stündlich :17, oder manuell)
  ├─ OPLeague: Standings + Kader + Riot-IDs
  ├─ HenrikDev-API: Rang & Stats (älteste zuerst, max. 30 min pro Lauf)
  ├─ HenrikDev-API: Rundendaten fertiger OPL-Matches (Custom-Games, siehe unten)
  ├─ Zustand (Cache) → Branch `data`  (immer nur 1 Commit, keine wachsende Historie)
  └─ statische Seite + JSON → GitHub Pages
```

**Rundendaten (Match-Übersicht):** OPL-Matches werden als Custom-Games gespielt und stehen in der
Riot-Historie der Spieler. Der Export sucht sie über HenrikDev (`v4/matches …?mode=custom`, für ältere
Spiele `v1/stored-matches` + `v4/match`) und ordnet sie über **Map, Endstand und Spielernamen** zu – nicht über
die Uhrzeit, weil Matches oft verschoben werden. Gespeichert wird eine verdichtete Fassung (Rundenverlauf,
Loadout, Plants, ADR, HS%, KAST, First Kills, Multikills, Clutches) als `data/match-<id>.json`.
Anfragen pro Lauf: täglich 40, stündlich 8, manuell 250 (`--round-requests`). Nicht gefundene Matches werden
einmal am Tag erneut gesucht, höchstens 7-mal.

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
   `https://<user>.github.io/<repo>/` erreichbar. Ab dann läuft es stündlich automatisch.
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
Der Key für lokale Läufe steht in `.env` (`HENRIKDEV_API_KEY=…`). `.env` wird nie committet.

## Ligen

Die Standard-Ligen stehen in [`config/leagues.json`](config/leagues.json). Weitere Ligen kann ein Admin
auf der Seite hinzufügen: Standing-URL ins Feld oben eintragen → *Öffnen* (landet im Branch `data`).

## Grenzen

- Der kostenlose HenrikDev-Key erlaubt 30 Anfragen/min (2 pro Spieler). Alle ~500 Spieler der
  drei Ligen brauchen ca. 40 min. Ein stündlicher Lauf aktualisiert 30 min lang die **ältesten**
  Daten zuerst, jeder Spieler ist also spätestens nach ~1,5 h wieder aktuell. Standings und Kader
  werden jede Stunde komplett neu geladen.
- Ein manuelles Admin-Update wartet, falls gerade das stündliche Update läuft (max. ~30 min).
- GitHub startet geplante Läufe oft einige Minuten verspätet und pausiert sie in Repos ohne
  Aktivität nach 60 Tagen (dann im Actions-Tab wieder aktivieren).
- Riot-IDs stammen aus den OPL-Profilen; ist dort eine alte ID hinterlegt, findet die API den Account nicht.
