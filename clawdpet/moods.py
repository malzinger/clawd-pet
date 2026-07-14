"""Mood mapping: quota level + running tool -> animation."""

def mood_for_pct(pct: float) -> str:
    if pct >= 100.0:
        return "limit"
    if pct >= 80.0:
        return "panic"
    if pct >= 50.0:
        return "focus"
    return "chill"


MOOD_COLORS = {
    "sleep": "#3fb950",
    "chill": "#3fb950",
    "read": "#3fb950",
    "think": "#3fb950",
    "type": "#d29922",
    "focus": "#d29922",
    "notify": "#d29922",
    "happy": "#3fb950",
    "panic": "#f0883e",
    "limit": "#f85149",
    "pet": "#3fb950",
    "annoyed": "#d29922",
    "juggle": "#3fb950", "conduct": "#3fb950", "sweep": "#3fb950",
    "carry": "#3fb950",
}

# Random idle animations played now and then while Clawd is calm (mood "chill").
IDLE_FLOURISHES = ("juggle", "conduct", "sweep", "carry")
IDLE_SWITCH_MS = 6000       # how often the idle animation may change
IDLE_FLOURISH_PROB = 0.45   # chance an idle tick starts a flourish (else idle)
PET_SPAM_WINDOW_S = 3.0     # over-petting window
PET_SPAM_COUNT = 3          # this many pets within the window -> annoyed

# Which animation each running tool maps to (used only when quota is calm).
# Task/Agent = Claude delegates to subagents -> Clawd juggles them.
TOOL_MOODS = {
    "Read": "read", "Grep": "read", "Glob": "read",
    "WebFetch": "read", "WebSearch": "read",
    "Edit": "type", "Write": "type", "MultiEdit": "type", "NotebookEdit": "type",
    "Bash": "focus", "PowerShell": "focus", "Task": "juggle", "Agent": "juggle",
    # X2: pseudo-tool used by the hook receiver while Claude Code compacts the
    # context (PreCompact..PostCompact) — Clawd sweeps up the mess.
    "Compact": "sweep",
}

# If a mapped animation is missing (older sprites/ folder), fall back sensibly.
# "juggle" falls back to "focus": Task/Agent map to it, so a missing gif should
# still read as "working", not "chilling". Idle flourishes are unaffected by
# this — they are only picked from sprites that actually loaded (see
# PetWidget._idle_pool).
MOOD_FALLBACK = {"type": "focus", "read": "chill", "think": "focus",
                 "notify": "happy", "pet": "happy", "annoyed": "happy",
                 "juggle": "focus", "conduct": "chill", "sweep": "chill",
                 "carry": "chill"}
