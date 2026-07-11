# Clawd — Claude Code Desktop Pet

**English** · [Deutsch](README.de.md)

A tiny, always-on-top desktop pet that live-tracks your Claude Code token
usage. He chills while you have budget left — and panics when you don't.

![Clawd with usage panel](docs/hero.png)

## Quick start (Windows — no Python needed)

1. Download **[ClawdPet.exe](https://github.com/malzinger/clawd-pet/releases/latest/download/ClawdPet.exe)** (direct link)
2. Double-click it. That's it — Clawd appears on your desktop.

> Note: the green "Code → Download ZIP" button only contains the source
> code — the exe lives under [Releases](https://github.com/malzinger/clawd-pet/releases).

> The exe is unsigned, so Windows SmartScreen may warn on first launch:
> click "More info" → "Run anyway".

## Run from source (Windows / macOS / Linux)

```bash
git clone https://github.com/malzinger/clawd-pet.git
cd clawd-pet
pip install -r requirements.txt
python clawd_pet.py
```

On Windows use `py` instead of `python` if it is not on your PATH — or just
double-click `start_clawd.bat` (starts without a console window).

Build the standalone exe yourself:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name ClawdPet --icon "docs/clawd.ico" --add-data "sprites;sprites" --add-data "clawd_hook.py;." clawd_pet.py
```

## Clawd's moods

| Usage           | Mood     | What you see                     |
| --------------- | -------- | -------------------------------- |
| no activity     | Sleeping | curled up, snoring               |
| 0 – 50 %        | Chill    | relaxed idling                   |
| 50 – 80 %       | Working  | hammering away with a hard hat   |
| 80 – 100 %      | Panic    | frantic debugging                |
| ≥ 100 %         | Limit    | flat on his back, X eyes, ERROR  |

## What it does

- Scans your local Claude Code logs (`~/.claude/projects/**/*.jsonl`) every
  20 seconds on a background thread, reconstructs Anthropic's **fixed 5-hour
  session window** from the activity timestamps (it starts with your first
  message and fully resets 5 h later — matching Claude's own display) and
  sums only the tokens of the current window (streaming duplicates are
  deduplicated). Nothing ever leaves your machine — no account, no cloud.
- Click or hover Clawd for a Claude-style panel: 5-hour limit with progress
  bar, **per-model breakdown** (Fable, Opus, Sonnet, …) and a countdown until
  the window resets.
- **See what Claude is working on:** the panel's top section names the current
  project, your latest prompt, and the running tool with its concrete target
  ("editing clawd_pet.py", "running: git push") — read straight from the local
  session log, nothing sent anywhere. Clawd himself animates to match — typing
  while editing, reading while grepping, thinking between tools, and popping a
  notification when Claude needs your input.
- **Self-calibrating:** Anthropic does not publish the actual token quotas, so
  right-click → "Limit kalibrieren …", type in the percentage Claude's own
  `/usage` popup shows — the app derives your real budget from it and stores it.
- **Burn-rate forecast:** the panel projects when you would hit the limit at
  your current pace ("At this pace: limit around 16:40") — or confirms the
  pace lasts until the reset.
- **Notifications:** tray toasts when usage crosses 80 % / 95 %, when the
  5-hour window resets ("Fresh budget!"), and — the useful one — **when Claude
  finishes a turn or asks for your input** while you are looking elsewhere. A
  live turn timer ("· 2:14") in the task view shows how long the current turn
  has been running. Toggle notifications (and an optional sound) in the tray menu.
- **Usage history:** the panel draws a 24-hour sparkline of your usage from a
  local history file (`~/.clawd/history.json`), so you can see when you burn
  the most. Nothing leaves your machine.
- **Update check:** on launch (and every 6 h) Clawd asks GitHub whether a
  newer release exists and, if so, shows a bubble you can click to download.
  Toggle it off in the tray menu.
- **Start with Windows:** a tray-menu checkbox registers or removes
  autostart (Windows only).
- **Reacts in real time:** a lightweight watcher follows the newest session
  log — Clawd hammers away while Claude runs tools, turns happy when the turn
  finishes, and speech bubbles announce what is happening ("führt Befehle
  aus …"). Mute the bubbles via the tray menu.
- **Optional hooks (beta):** tray menu → "Echtzeit-Hooks aktivieren" registers
  Claude Code hooks so the pet reacts instantly — including "Claude is
  waiting for your input". Needs Python on PATH; a `.clawd-bak` backup of
  `settings.json` is kept and the entry can be removed from the same menu.
- **Pet him:** double-click Clawd and hearts float up.
- **Bilingual UI:** the whole app (panel, bubbles, menus, dialogs, number
  formats) switches between English and German — tray menu → "Sprache/Language".
- Drag him anywhere; the position is remembered. Tray icon with manual
  refresh, hide and quit.
- Only one instance runs at a time — starting the exe again simply tells you
  Clawd is already on your desktop.

## Platform notes

- Windows / macOS: transparency works out of the box.
- Linux: requires a compositing window manager (default on KDE/GNOME).
- Headless smoke test: `python clawd_pet.py --selftest`

## Credits & license

MIT — see [LICENSE](LICENSE). The pixel-art animations are from the
MIT-licensed community project
[KebeliSamet0/clawd](https://github.com/KebeliSamet0/clawd); if the
`sprites/` folder is missing, a built-in vector Clawd is drawn instead.
Unofficial fan project, not affiliated with Anthropic.
