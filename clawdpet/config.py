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

# --- Live sync with the Anthropic usage endpoint ----------------------------
# Two token sources, in order of preference:
#  1. Clawd's OWN independent OAuth token in ~/.clawd/auth.json (set up via the
#     one-time login, "Clawd-Login einrichten"). This is a separate grant — like
#     a third device — so Clawd may auto-refresh it freely: rotating it only ever
#     affects this file and can never lock the user out of Claude Code.
#  2. Fallback: the token Claude Code itself stored in ~/.claude/.credentials.json,
#     READ-ONLY. Clawd never refreshes or writes that shared file, because a
#     failed write-back of Claude Code's rotating token could break its login.
# If neither token is valid, the local log estimate is used; the last live
# reading is remembered as a calibration so the estimate stays close.
USE_API_USAGE = True
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAWD_AUTH_FILE = Path.home() / ".clawd" / "auth.json"     # Clawd's own token (writable)
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public client id
REFRESH_COOLDOWN_S = 300.0         # don't retry a failing own-token refresh more than every 5 min
# The usage endpoint rate-limits background pollers HARD (observed: repeated
# one-hour lockouts at a 30-60 s cadence). Between live syncs the anchored,
# auto-calibrated local estimate carries the display, so 15 min is plenty —
# each sync re-anchors the windows and re-learns the budgets.
API_OK_INTERVAL_S = 900.0          # hit the usage endpoint at most this often when healthy
RATE_LIMIT_BASE_S = 300.0          # a 429 pauses live polling at least this long
RATE_LIMIT_MAX_S = 3600.0          # cap for Retry-After and the 429 back-off
API_RETRY_S = 5.0                  # base back-off after a failed usage fetch
API_MAX_BACKOFF_S = 120.0          # cap the exponential back-off on repeated failures
API_STALE_S = 180.0                # legacy (pre-projection); kept for external scripts

ORG_NAME = "ClawdPet"
APP_NAME = "Clawd"
APP_VERSION = "1.8.0"

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

# --- cost estimate (F4) ---
# Public API price for Sonnet input, USD per million tokens. The cost-weighted
# usage sum (WEIGHT_* x MODEL_COST) is expressed in Sonnet-input-token
# equivalents, so weighted units x this price = the approximate API price the
# same usage would have cost on pay-as-you-go.
SONNET_INPUT_USD_PER_MTOK = 3.0

# --- motion (F5/F12/F8) ---
# F5: autonomous wandering (opt-in via the tray menu, default off). Clawd
# alternates between random-length walks and pauses while he is idle.
WANDER_TICK_MS = 33                 # movement timer tick (~30 fps)
WANDER_SPEED_PX = 2.2               # horizontal pixels walked per tick
WANDER_PAUSE_RANGE_S = (3.0, 10.0)  # random pause length between walks
WANDER_WALK_RANGE_S = (2.0, 6.0)    # random length of one walking stretch

# F12: throw physics — drag Clawd fast and let go to fling him.
THROW_MIN_SPEED = 900.0    # px/s release speed required to start a throw
THROW_GRAVITY = 2800.0     # px/s^2 downward acceleration during the flight
THROW_BOUNCE = 0.55        # velocity kept on each floor/ceiling/wall bounce
THROW_FRICTION = 0.82      # horizontal damping applied on floor/ceiling hits
THROW_STOP_SPEED = 60.0    # px/s below which a grounded throw comes to rest

# --- customization (F2/F9/F13) ---
# F2: pet size presets, chosen via the tray "Size" submenu. Each value is a
# factor applied to PET_HEIGHT (the sprites rescale, the panel/tray art not).
PET_SIZE_FACTORS = {"S": 0.7, "M": 1.0, "L": 1.4}

# --- X2: hook events + statusline ---
# Newer Claude Code hook events the activity hook also registers for.
# Events arriving from an old clawd_hook.py copy (or unknown ones) are
# ignored gracefully by the receiver, so this list can only grow.
HOOK_EVENTS += [
    "SubagentStart", "SubagentStop",     # Clawd juggles running subagents
    "PostToolUseFailure", "StopFailure",  # a failure startles the pet
    "PreCompact", "PostCompact",          # sweeping up while compacting
    "SessionEnd",                         # clears hook-driven activity state
]
# Context-window fill from the Claude Code statusline (clawd_statusline.py):
# the panel hides its context row when no update arrived for this long.
CONTEXT_STALE_S = 120

# --- Y: pet behaviors (idle throttle, cursor chase, typing bob, celebrate) ---
THROTTLE_IDLE_S = 60.0      # fully idle this long -> slow the animation timer
THROTTLE_TICK_MS = 250      # throttled frame interval (vs. ANIM_TICK_MS 33)
CHASE_TICK_MS = 50          # cursor-chase state machine tick
CHASE_SPEED_PX = 3.2        # px per tick while chasing (a bit faster than wander)
CHASE_WAIT_RANGE_S = (30.0, 90.0)   # pause between chase attempts
CHASE_STOP_SHORT_PX = 30    # never park ON the cursor — stop this short of it
CHASE_RELEASE_PX = 120      # cursor moved this far -> wake up and let go
TYPING_BOB_PERIOD_MS = 125  # ~8 Hz typing-along bob while Claude generates
TYPING_BOB_PX = 2
CELEBRATE_MS = 3000         # length of the one-shot celebration
CELEBRATE_HOP_V = 260.0     # upward hop speed (reuses the throw physics)


# --- X1: Codex CLI integration (rate limits via app-server, notify hook) -----
CODEX_CONFIG_FILE = Path.home() / ".codex" / "config.toml"
CODEX_RPC_TIMEOUT_S = 12.0     # budget for one app-server rate-limit read
CODEX_POLL_INTERVAL_S = 300.0  # spawn the subprocess at most every 5 min
CODEX_RETRY_S = 600.0          # after a failure wait even longer

# --- W: window sitting ---
# Shimeji-style perch on the frontmost window (opt-in, macOS via Quartz).
WINDOW_SIT_POLL_MS = 500    # frontmost-window poll cadence while enabled
WINDOW_SIT_MIN_W = 200      # ignore windows narrower than this (palettes etc.)
WINDOW_SIT_MIN_H = 100      # ignore windows flatter than this (toolbars etc.)

# --- Ball fetch + shell drops + mischief (inline wave) -----------------------
BALL_SIZE = 18                 # px, the fetch ball
FETCH_SPEED_PX = 4.2           # pet speed per 50 ms tick while fetching
SHELL_SIZE = 26                # px, collectible shell
SHELL_XP_RANGE = (40, 160)     # bonus XP per collected shell
SHELL_LIFETIME_S = 90.0        # unclaimed shells fade away
SHELL_MIN_INTERVAL_S = 480.0   # spawn cadence while an agent works
SHELL_MAX_INTERVAL_S = 1200.0
MISCHIEF_MIN_INTERVAL_S = 600.0   # opt-in cursor pinch, rare by design
MISCHIEF_MAX_INTERVAL_S = 1800.0
MISCHIEF_CURSOR_STILL_S = 20.0    # cursor must rest this long first
MISCHIEF_PUSH_PX = 60             # how far the pinch drags the cursor
# --- M: mini pets ---
# One small crab per running subagent (clawdpet/minipets.py). Minis reuse the
# idle sprite at a fraction of the main pet's height and are hard-capped so a
# subagent swarm cannot flood the desktop with windows/timers.
MINIPET_MAX = 5               # never show more mini crabs than this
MINIPET_HEIGHT_FACTOR = 0.45  # mini height = PET_HEIGHT * this factor
# --- Q: quips ---
# Personality one-liners (quips.py): a quip may fire at most every
# QUIP_MIN_INTERVAL_S seconds, plus a random 0..QUIP_JITTER_S so the pet
# never quips on a metronome. The app owns the timer; the scheduler only
# answers should_fire().
QUIP_MIN_INTERVAL_S = 600.0
QUIP_JITTER_S = 300.0
