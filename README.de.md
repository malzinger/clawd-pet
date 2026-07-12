# Clawd — Claude Code Desktop-Pet

[English](README.md) · **Deutsch**

Ein kleines, immer sichtbares Desktop-Pet, das deinen Claude-Code-Verbrauch
live überwacht. Solange Budget da ist, chillt er — wird es knapp, gerät er
in Panik.

![Clawd mit Nutzungs-Panel](docs/hero.png)

## Schnellstart (Windows — kein Python nötig)

1. **[ClawdPet.exe](https://github.com/malzinger/clawd-pet/releases/latest/download/ClawdPet.exe)** herunterladen (Direktlink)
2. Doppelklicken. Fertig — Clawd erscheint auf deinem Desktop.

> Hinweis: Der grüne „Code → Download ZIP"-Button enthält nur den Quellcode —
> die exe liegt unter [Releases](https://github.com/malzinger/clawd-pet/releases).

> Die exe ist unsigniert, Windows SmartScreen warnt eventuell beim ersten
> Start: „Weitere Informationen" → „Trotzdem ausführen".

## Aus dem Quellcode starten (Windows / macOS / Linux)

```bash
git clone https://github.com/malzinger/clawd-pet.git
cd clawd-pet
pip install -r requirements.txt
python clawd_pet.py
```

Unter Windows `py` statt `python`, falls Python nicht im PATH liegt — oder
einfach Doppelklick auf `start_clawd.bat` (startet ohne Konsolenfenster).

Die eigenständige exe selbst bauen:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name ClawdPet --icon "docs/clawd.ico" --add-data "sprites;sprites" --add-data "clawd_hook.py;." clawd_pet.py
```

## Clawds Stimmungen

| Auslastung      | Stimmung   | Was du siehst                       |
| --------------- | ---------- | ----------------------------------- |
| keine Aktivität | Schläft    | eingerollt, schnarcht               |
| 0 – 50 %        | Chillt     | entspanntes Herumstehen             |
| 50 – 80 %       | Werkelt    | hämmert fleißig mit Bauhelm         |
| 80 – 100 %      | Panik      | hektisches Debugging                |
| ≥ 100 %         | Limit      | auf dem Rücken, X-Augen, ERROR      |

## Was es macht

- Scannt alle 2 Sekunden deine lokalen Claude-Code-Logs
  (`~/.claude/projects/**/*.jsonl`) auf einem Hintergrund-Thread,
  rekonstruiert aus den Zeitstempeln Anthropics **festes 5-Stunden-Fenster**
  (startet mit deiner ersten Nachricht und setzt sich nach 5 h komplett
  zurück — wie in Claudes eigener Anzeige) und summiert nur die Tokens des
  aktuellen Fensters (Streaming-Duplikate werden dedupliziert). Nichts
  verlässt deinen Rechner — kein Account, keine Cloud.
- Klick oder Hover auf Clawd öffnet ein Panel im Claude-Look: 5-Stunden-Limit
  mit Fortschrittsbalken, **Aufschlüsselung pro Modell** (Fable, Opus,
  Sonnet, …) und Countdown bis zum Fenster-Reset.
- **Sieh, woran Claude arbeitet:** Ganz oben im Panel stehen das aktuelle
  Projekt, dein letzter Prompt und das laufende Tool mit konkretem Ziel
  („bearbeitet clawd_pet.py", „führt aus: git push") — direkt aus dem lokalen
  Session-Log gelesen, nichts wird gesendet. Clawd selbst animiert passend
  dazu — tippt beim Bearbeiten, liest beim Suchen, denkt zwischen Tools und
  zeigt eine Benachrichtigung, wenn Claude auf dich wartet. Wenn er nichts zu
  tun hat, spielt er ab und zu eine zufällige Animation (jonglieren, fegen,
  dirigieren …), und wenn du ihn zu oft streichelst, wird er genervt.
- **Live-Sync (exakte Zahlen), read-only:** Clawd liest das OAuth-Token, das
  Claude Code ohnehin gespeichert hat, und zeigt exakt die Auslastung aus
  Claudes eigenem `/usage`-Popup — alle paar Sekunden aktualisiert. Clawd
  erneuert oder schreibt das Token nie (ein passiver Monitor darf Claude Codes
  rotierenden Login nicht anfassen): Solange das Token gültig ist, siehst du
  exakte Zahlen; ist es abgelaufen, nutzt Clawd die Schätzung — kalibriert aus
  der letzten Live-Messung, sodass sie nah dran bleibt.
- **Selbst-kalibrierend (Fallback):** Ist der Live-Sync nicht verfügbar,
  Rechtsklick → „Limit kalibrieren …" und den Prozentwert aus Claudes
  eigenem `/usage`-Popup eintippen — die App leitet daraus dein echtes Budget ab.
- **Burn-Rate-Prognose:** Das Panel rechnet hoch, wann du bei aktuellem
  Tempo das Limit erreichst („Bei diesem Tempo: Limit ca. 16:40 Uhr") —
  oder bestätigt, dass das Tempo bis zum Reset reicht.
- **Benachrichtigungen:** Tray-Toasts beim Überschreiten von 80 % / 95 %, beim
  Reset des 5-Stunden-Fensters („Budget wieder frisch!") und — der nützliche
  Fall — **wenn Claude einen Turn beendet oder auf deine Eingabe wartet**,
  während du woanders hinschaust. Ein Turn-Timer („· 2:14") in der Aufgaben-
  Ansicht zeigt, wie lange der aktuelle Turn schon läuft. Benachrichtigungen
  (und optional ein Ton) im Tray-Menü umschaltbar.
- **Nutzungsverlauf:** Das Panel zeichnet eine 24-Stunden-Sparkline aus einer
  lokalen Verlaufsdatei (`~/.clawd/history.json`) — so siehst du, wann du am
  meisten verbrauchst. Nichts verlässt deinen Rechner.
- **Update-Prüfung:** Beim Start (und alle 6 Std.) fragt Clawd bei GitHub
  nach, ob eine neuere Version existiert, und zeigt bei Bedarf eine Sprechblase
  zum Herunterladen. Im Tray-Menü abschaltbar.
- **Mit Windows starten:** Ein Häkchen im Tray-Menü registriert oder
  entfernt den Autostart (nur Windows).
- **Reagiert in Echtzeit:** Ein leichtgewichtiger Watcher verfolgt das neueste
  Session-Log — Clawd hämmert, während Claude Tools ausführt, freut sich, wenn
  der Turn fertig ist, und Sprechblasen verraten, was gerade passiert („führt
  Befehle aus …"). Abschaltbar über das Tray-Menü.
- **Optionale Hooks (Beta):** Tray-Menü → „Echtzeit-Hooks aktivieren"
  registriert Claude-Code-Hooks für Sofort-Reaktionen — inklusive „Claude
  wartet auf deine Eingabe". Benötigt Python im PATH; von `settings.json`
  wird ein `.clawd-bak`-Backup angelegt, Deaktivieren geht im selben Menü.
- **Streicheln:** Doppelklick auf Clawd lässt Herzchen aufsteigen.
- **Zweisprachig:** Die komplette Oberfläche (Panel, Sprechblasen, Menüs,
  Dialoge, Zahlenformate) schaltet zwischen Deutsch und Englisch um —
  Tray-Menü → „Language/Sprache".
- Frei per Drag verschiebbar, Position wird gemerkt. Tray-Icon mit manuellem
  Refresh, Verstecken und Beenden.
- Es läuft immer nur eine Instanz — startest du die exe erneut, sagt sie dir
  nur, dass Clawd schon auf deinem Desktop sitzt.

## Plattform-Hinweise

- Windows / macOS: Transparenz funktioniert out of the box.
- Linux: benötigt einen Compositing-Fenstermanager (Standard bei KDE/GNOME).
- Headless-Smoke-Test: `python clawd_pet.py --selftest`

## Credits & Lizenz

MIT — siehe [LICENSE](LICENSE). Die Pixel-Art-Animationen stammen aus dem
MIT-lizenzierten Community-Projekt
[KebeliSamet0/clawd](https://github.com/KebeliSamet0/clawd); fehlt der
`sprites/`-Ordner, zeichnet die App einen eingebauten Vektor-Clawd.
Inoffizielles Fan-Projekt, nicht mit Anthropic verbunden.
