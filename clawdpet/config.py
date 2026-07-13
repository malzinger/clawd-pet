"""Static configuration: plan constants, paths, weights, timings."""
from pathlib import Path

# ======================================================================
#  CONFIG — edit these to match your plan
# ======================================================================

# Session-window budget in COST-WEIGHTED token units (see WEIGHT_* and
# MODEL_COST below). Anthropic publishes no real numbers, so this is a rough
# Max-5x starting guess; calibration (manual via tray menu, or automatic
# whenever a live API sync succeeds) replaces it and is persisted in QSettings.
# It is sized for the cost-weighted scale (Opus tokens weigh ~5x), so an
# uncalibrated Opus-heavy window reads a plausible mid-range %, not a false
# >100% "limit".
MAX_TOKENS = 40_000_000            # placeholder default (Max 5x, cost-weighted units)
PLAN_NAME = "Max 5x"               # shown in the panel header
WINDOW_HOURS = 5                   # length of Anthropic's fixed session window
REPLAY_HOURS = 48                  # look-back to reconstruct the window chain
WEEK_REPLAY_HOURS = 192            # look-back covering the weekly limit window
SCAN_INTERVAL_MS = 2_000           # how often the logs are rescanned (feels live)
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Anthropic meters the plan limits by cost, not by raw token count. These
# weights mirror the public API price ratios (output = 5x input, cache write
# = 1.25x, cache read = 0.1x), so the local percentage scales like Claude's
# own display across changing usage mixes. Displayed token numbers stay raw
# input+output; only percentages and calibration use the weighted sum.
WEIGHT_INPUT = 1.0
WEIGHT_OUTPUT = 5.0
WEIGHT_CACHE_CREATION = 1.25
WEIGHT_CACHE_READ = 0.1

# Models also cost very differently against the plan. These multipliers mirror
# the public per-token price tiers (relative to Sonnet), so the estimate tracks
# Claude's percentage across changing model mixes — one calibration then holds
# whether you code with Opus or Fable, instead of drifting every window.
MODEL_COST = {"Opus": 5.0, "Sonnet": 1.0, "Haiku": 0.3, "Fable": 0.3}
WEIGHT_VERSION = 2      # bump to drop stale calibrations after changing the weights


def model_cost(name: str) -> float:
    return MODEL_COST.get(name, 1.0)

PET_HEIGHT = 132                   # on-screen pixel height of Clawd
PANEL_WIDTH = 392                  # width of the slide-out panel

# Animated GIF sprites (community pixel-art recreation of the official mascot,
# MIT-licensed: https://github.com/KebeliSamet0/clawd). If a file is missing,
# the built-in vector Clawd is drawn instead. The folder lives next to the
# clawd_pet.py entry point, one level above this package (also true in a
# PyInstaller onefile bundle, where _MEIPASS/clawdpet/config.py sits below
# the _MEIPASS/sprites payload).
SPRITE_DIR = Path(__file__).resolve().parent.parent / "sprites"
SPRITE_FILES = {
    "sleep": "clawd-sleeping.gif",       # no activity in the rolling window
    "chill": "clawd-idle.gif",           # budget left, no live activity
    "focus": "clawd-building.gif",       # running commands / 50-80 % quota
    "type": "clawd-typing.gif",          # editing / writing files
    "read": "clawd-idle-reading.gif",    # reading / searching / browsing
    "think": "clawd-thinking.gif",       # working with no tool (Claude thinking)
    "notify": "clawd-notification.gif",  # Claude is waiting for your input
    "happy": "clawd-happy.gif",          # turn finished
    "panic": "clawd-debugger.gif",       # 80-100 % quota / tool error
    "limit": "clawd-error.gif",          # over the limit
    "pet": "clawd-react-double-jump.gif",  # transient double-click reaction
    "annoyed": "clawd-react-annoyed.gif",  # over-petted
    # random idle flourishes, played occasionally while calm (see IDLE_FLOURISHES)
    "juggle": "clawd-juggling.gif",
    "conduct": "clawd-conducting.gif",
    "sweep": "clawd-sweeping.gif",
    "carry": "clawd-carrying.gif",
}

# --- Real-time activity (Stufe 1: log watcher, Stufe 2: opt-in hooks) -------
ACTIVITY_POLL_MS = 1500     # how often the newest session log is checked
ACTIVITY_IDLE_S = 240       # log untouched this long -> no activity
HOOK_UDP_PORT = 52741       # clawd_hook.py sends Claude Code events here
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOK_EVENTS = ["PreToolUse", "Notification", "Stop", "SessionStart"]
# Shared secret authenticating hook datagrams: any local process could send
# UDP to 127.0.0.1, so clawd_hook.py prefixes each event with this token and
# the pet drops datagrams that lack it (see hooks.ensure_hook_token).
HOOK_TOKEN_FILE = Path.home() / ".clawd" / "hook_token"

# --- Optional live sync with the Anthropic usage endpoint -------------------
# READ-ONLY, best effort: if ~/.claude/.credentials.json holds a *currently
# valid* OAuth token that Claude Code stored, the exact utilization percentages
# Claude's own /usage popup shows are fetched. Clawd never refreshes the token
# and never writes the credential store — a passive monitor must not touch the
# rotating login token Claude Code owns, or a failed write-back could lock the
# user out of Claude Code. When the token is expired the local log estimate is
# used; the last live reading is remembered as a calibration so it stays close.
USE_API_USAGE = True
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_OK_INTERVAL_S = 30.0           # hit the usage endpoint at most this often when healthy
API_RETRY_S = 5.0                  # base back-off after a failed usage fetch
API_MAX_BACKOFF_S = 120.0          # cap the exponential back-off on repeated failures
API_STALE_S = 180.0                # keep showing the last live % up to this long, then estimate

ORG_NAME = "ClawdPet"
APP_NAME = "Clawd"
APP_VERSION = "1.7.0"

# --- Burn-rate forecast + threshold notifications ----------------------------
BURN_LOOKBACK_S = 3600      # forecast fits over at most the last hour
BURN_MIN_SPAN_S = 300       # ... but needs at least 5 minutes of samples
NOTIFY_THRESHOLDS = (95.0, 80.0)   # toast on upward crossings, highest wins
WAIT_ALERT_MIN_S = 15       # only alert "your turn" on turns worked at least this long
ALERT_COOLDOWN_S = 20       # collapse near-simultaneous alerts (hook + log)

# --- Autostart (Windows Run key, toggled via the tray menu) ------------------
AUTOSTART_REG_PATH = (r"HKEY_CURRENT_USER\Software\Microsoft"
                      r"\Windows\CurrentVersion\Run")
AUTOSTART_REG_NAME = "ClawdPet"

# --- Update check (GitHub Releases, read-only, best effort) ------------------
GITHUB_REPO = "malzinger/clawd-pet"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK = True                # look for a newer release (once/launch + 6 h)
UPDATE_RECHECK_MS = 6 * 3600 * 1000

# --- Usage history (local sparkline in the panel) ----------------------------
HISTORY_FILE = Path.home() / ".clawd" / "history.json"
HISTORY_INTERVAL_S = 300           # store at most one point every 5 minutes
HISTORY_KEEP_DAYS = 7              # prune points older than this
HISTORY_WINDOW_H = 24             # span shown in the panel sparkline
HISTORY_GAP_S = 1800              # break the line across gaps longer than this

# --- Codex CLI detection (F6) ---------------------------------------------
# OpenAI Codex CLI writes *.jsonl session logs below ~/.codex/sessions. They
# are only consulted as a FALLBACK when no Claude session log is active, and
# activity is judged purely by mtime: the Codex log format is undocumented
# and may change at any time, so nothing in it is parsed strictly. A file
# younger than CODEX_ACTIVE_S means Codex is generating right now
# ("working"); between CODEX_ACTIVE_S and ACTIVITY_IDLE_S it counts as
# "waiting" (turn finished, session still warm); older logs are ignored.
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_ACTIVE_S = 20

