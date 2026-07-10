# Clawd — Claude Code Desktop Pet 

A tiny, always-on-top desktop widget that renders **Clawd**, the pixel-art
mascot of Claude Code, and live-tracks your Claude Code token usage from the
local session logs. He chills when you have budget, panics when you don't.

![Clawd mit Nutzungs-Panel](docs/hero.png)

## Setup

```bash
git clone https://github.com/malzinger/clawd-pet.git
cd clawd-pet
pip install -r requirements.txt
python clawd_pet.py
```

**Windows-Hinweis:** Wenn `pip`/`python` nicht im PATH sind (Installation über
den Python-Launcher), nimm `py -m pip install -r requirements.txt` und
`py clawd_pet.py` — oder einfach **Doppelklick auf `start_clawd.bat`**, das
startet Clawd ohne Konsolenfenster.

### Ohne Python: fertige .exe (Windows)

Unter **Releases** liegt eine eigenständige `ClawdPet.exe` — herunterladen,
doppelklicken, fertig. Kein Python nötig.

> Hinweis: Die exe ist mit PyInstaller gebaut und unsigniert; Windows
> SmartScreen fragt beim ersten Start eventuell nach („Weitere Informationen"
> → „Trotzdem ausführen").

Selbst bauen:

```powershell
py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed --name ClawdPet --add-data "sprites;sprites" clawd_pet.py
# Ergebnis: dist/ClawdPet.exe
```

Optional headless smoke test (scans your logs, renders every mood offscreen):

```bash
py clawd_pet.py --selftest      # Windows
python clawd_pet.py --selftest  # macOS/Linux
```

## How it works

Die App hat zwei Datenquellen und nutzt automatisch die beste verfügbare:

- **Lokale Schätzung (Normalfall).** Ein Hintergrund-Thread scannt alle 20 s
  `~/.claude/projects/**/*.jsonl`, summiert `input_tokens` und `output_tokens`
  der letzten **5 Stunden** (Streaming-Duplikate werden per Message-ID
  dedupliziert) und vergleicht gegen `MAX_TOKENS`. Die Tokenzahl ist exakt,
  der Prozentwert eine Schätzung — Anthropic veröffentlicht das echte
  Kontingent nicht.
- **Live-Sync mit der Anthropic-API (best effort).** Liegt in
  `~/.claude/.credentials.json` ein *gültiger* OAuth-Token, holt die App die
  exakten Prozentwerte, die Claude selbst anzeigt (5-Stunden-Limit + echte
  Wochenlimits mit Zurücksetzungszeiten). **Einschränkung:** Unter Windows
  hält die Claude-Desktop-App ihren Token im Credential-Manager, die Datei ist
  dort meist veraltet — dann greift die lokale Schätzung. Die App liest den
  Token nur, sie schreibt **nie** in den Credential-Store und erneuert keine
  Tokens. Abschalten mit `USE_API_USAGE = False` oder der Umgebungsvariablen
  `CLAWD_NO_API=1`.

Der aktive Modus steht in der Fußzeile des Panels: `(live)` oder `(lokal)`.

## Limit kalibrieren (einmalig, empfohlen)

Anthropic veröffentlicht die Token-Kontingente der Pläne **nicht** — auch die
88.000 sind nur ein Platzhalter. Du musst dein Limit aber nicht kennen, die App
rechnet es aus:

1. In Claude Code `/usage` öffnen und den Prozentwert des **5-Stunden-Limits**
   ablesen (z. B. `65 %`).
2. Rechtsklick auf Clawd oder das Tray-Icon → **„Limit kalibrieren …"**
3. Prozentwert eintippen.

Die App teilt die im selben Zeitfenster gezählten Tokens durch diesen Prozentwert
und kennt damit dein echtes Budget (bei 178.000 Tokens und 65 % also ≈ 274.000).
Der Wert wird gespeichert; ab dann entspricht Clawds Anzeige Claudes eigener.
Rückgängig über „Kalibrierung zurücksetzen".

Konfiguration am Dateikopf von `clawd_pet.py`:

  ```python
  MAX_TOKENS = 88_000
  COUNT_CACHE_READ = False      # Cache-Reads zählen standardmäßig NICHT mit
  COUNT_CACHE_CREATION = False
  ```

  > **Warum kein Cache?** Bei Agent-Workloads sind `cache_read`-Tokens
  > hundertfach größer als Input/Output (z. B. 9 Mio. vs. 80 k) und zählen
  > real kaum ins Kontingent. Mit `COUNT_CACHE_READ = True` stünde die
  > Anzeige dauerhaft bei zigtausend Prozent.

## Clawd's moods

| Auslastung      | Mood  | Sprite (GIF)         |
| --------------- | ----- | -------------------- |
| keine Aktivität | Sleep | `clawd-sleeping.gif` |
| 0 – 50 %        | Chill | `clawd-idle.gif`     |
| 50 – 80 %       | Focus | `clawd-building.gif` |
| 80 – 100 %      | Panic | `clawd-debugger.gif` |
| ≥ 100 %         | Limit | `clawd-error.gif`    |

Die Animationen im Ordner `sprites/` stammen aus dem **MIT-lizenzierten**
Community-Projekt [KebeliSamet0/clawd](https://github.com/KebeliSamet0/clawd)
(pixelgenaue Nachbildung des offiziellen Maskottchens — die Original-GIFs
stecken im Claude-Code-Frontend und liegen nicht als Dateien auf der Platte).
Dort gibt es noch mehr Animationen (`juggling`, `sweeping`, `conducting` …) —
einfach herunterladen und in `SPRITE_FILES` in `clawd_pet.py` ummappen.
Fehlen die Dateien, zeichnet die App als Fallback einen eingebauten
Vektor-Clawd.

## Controls

- **Drag** Clawd with the left mouse button to move him.
- **Hover** to peek at the usage panel, **left-click** to pin it open.
- **Right-click** Clawd or the tray icon: refresh, hide, quit.
- Window position is remembered between runs (via `QSettings`).

## Platform notes

- Windows / macOS: transparency works out of the box.
- Linux: requires a compositing window manager (default on KDE/GNOME).
