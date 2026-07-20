"""Personality quips — context-aware speech-bubble one-liners.

Pure logic, no Qt: the app decides when to ask for a quip (a QTimer it
owns, gated through QuipScheduler) and pushes the result into the speech
bubble. Everything random flows through an INJECTED random.Random so the
selftest is fully deterministic (the Date/randomness lesson of this repo).
"""
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import QUIP_JITTER_S, QUIP_MIN_INTERVAL_S


@dataclass
class QuipContext:
    """Everything a quip rule may look at.

    Defaults are deliberately NEUTRAL (midday, mid-usage, midweek): an
    empty context matches only the fallback smalltalk rule, so callers can
    fill in just the facts they actually know.
    """
    hour: int = 12                # local wall-clock hour, 0-23
    pct: float = 50.0             # 5-hour usage window, 0-100
    level: int = 0                # gamification level (progress.py)
    title: str = ""               # evolution title for the level
    codex_active: bool = False    # the OTHER agent has a warm session
    session_minutes: float = 0.0  # length of the current Claude session
    tool_counts: Dict[str, int] = field(default_factory=dict)  # this session
    weekday: int = 2              # 0 = Monday ... 6 = Sunday


@dataclass(frozen=True)
class QuipRule:
    name: str
    predicate: Callable[[QuipContext], bool]
    templates: Dict[str, List[str]]   # language -> template strings
    fallback: bool = False            # only used when nothing else matches


def template_values(ctx: QuipContext) -> Dict[str, object]:
    """The full placeholder namespace every template may draw from."""
    return {
        "hour": int(ctx.hour),
        "pct": int(round(ctx.pct)),
        "level": int(ctx.level),
        "title": ctx.title or "Clawd",
        "n": int(ctx.tool_counts.get("Bash", 0)),
        "minutes": int(ctx.session_minutes),
        "hours": int(ctx.session_minutes // 60),
    }


QUIP_RULES: List[QuipRule] = [
    QuipRule(
        "late_night",
        lambda c: 0 <= c.hour < 5,
        {
            "de": [
                "{hour} Uhr nachts und du promptest immer noch?",
                "Der Schlaf kompiliert sich nicht von selbst.",
                "Wir Nachtkrebse müssen zusammenhalten.",
            ],
            "en": [
                "{hour} a.m. and you're still prompting?",
                "Sleep doesn't compile itself.",
                "Us night crabs have to stick together.",
            ],
        },
    ),
    QuipRule(
        "early_bird",
        lambda c: 5 <= c.hour < 7,
        {
            "de": [
                "Der frühe Krebs fängt den Bug.",
                "Schon wach? Ich hab noch Sand in den Scheren.",
                "Erst Kaffee, dann Commits.",
            ],
            "en": [
                "The early crab catches the bug.",
                "Up already? I still have sand in my claws.",
                "Coffee first, commits second.",
            ],
        },
    ),
    QuipRule(
        "high_usage",
        lambda c: c.pct >= 85.0,
        {
            "de": [
                "Wir fliegen auf Reserve — {pct} % verbraucht.",
                "Das Budget schmilzt wie Eis in der Sonne.",
                "Vielleicht eine kleine Prompt-Pause? Nur so eine Idee.",
            ],
            "en": [
                "Flying on fumes — {pct} % used.",
                "The budget is melting like ice in the sun.",
                "Maybe a tiny prompt break? Just a thought.",
            ],
        },
    ),
    QuipRule(
        "weekend",
        lambda c: c.weekday >= 5,
        {
            "de": [
                "Wochenende und wir coden? Ich sag ja nichts.",
                "Wochenend-Deploys sind mutig. Respekt.",
                "Der Strand wäre auch schön gewesen …",
            ],
            "en": [
                "Coding on the weekend? Not judging.",
                "Weekend deploys are brave. Respect.",
                "The beach would have been nice too ...",
            ],
        },
    ),
    QuipRule(
        "bash_heavy",
        lambda c: c.tool_counts.get("Bash", 0) >= 25,
        {
            "de": [
                "Der {n}. Shell-Befehl heute. Respekt.",
                "Shell-Befehl Nummer {n} — die Tastatur glüht.",
                "{n} Bash-Befehle. Ich zähle mit, keine Sorge.",
            ],
            "en": [
                "Shell command number {n} today. Respect.",
                "{n} Bash commands — your keyboard is glowing.",
                "{n} shell commands. I'm keeping count, don't worry.",
            ],
        },
    ),
    QuipRule(
        "long_session",
        lambda c: c.session_minutes >= 180.0,
        {
            "de": [
                "Über {hours} Stunden am Stück — Strecken nicht vergessen!",
                "Marathon-Session! Ich hol schon mal Wasser.",
                "{minutes} Minuten dabei. Deine Augen hätten gern Meerblick.",
            ],
            "en": [
                "Over {hours} hours straight — remember to stretch!",
                "Marathon session! I'll go fetch some water.",
                "{minutes} minutes in. Your eyes deserve an ocean view too.",
            ],
        },
    ),
    QuipRule(
        "codex_rivalry",
        lambda c: c.codex_active,
        {
            "de": [
                "Der andere Agent schon wieder … ich bin trotzdem der Süßere.",
                "Codex arbeitet auch? Na gut. Möge der bessere Krebs gewinnen.",
                "Zwei Agenten, ein Ruhm. Ich teile ungern.",
            ],
            "en": [
                "That other agent again ... I'm still the cuter one.",
                "Codex is working too? Fine. May the best crab win.",
                "Two agents, one glory. I don't like sharing.",
            ],
        },
    ),
    QuipRule(
        "high_level",
        lambda c: c.level >= 10,
        {
            "de": [
                "Level {level}, {title} — ich bin quasi Adel.",
                "Ein {title} wischt den Boden nicht selbst. Außer heute.",
                "Verbeugung bitte: {title}, Level {level}.",
            ],
            "en": [
                "Level {level}, {title} — basically royalty.",
                "A {title} doesn't sweep floors. Except today.",
                "Bow, please: {title}, level {level}.",
            ],
        },
    ),
    QuipRule(
        "fresh_budget",
        lambda c: c.pct < 10.0,
        {
            "de": [
                "Frisches Budget! Ich rieche Möglichkeiten.",
                "Volle Scheren, leeres Limit — los geht's.",
                "Noch fast alles übrig. Träum ruhig groß.",
            ],
            "en": [
                "Fresh budget! I smell possibilities.",
                "Full claws, empty meter — let's go.",
                "Almost everything left. Dream big.",
            ],
        },
    ),
    QuipRule(
        "smalltalk",
        lambda c: True,
        {
            "de": [
                "Seitwärts ist auch eine Richtung.",
                "Ich hätte gern Daumen. Nur so.",
                "Code ist wie Sand: er ist einfach überall.",
                "Scheren poliert, bereit für Bugs.",
            ],
            "en": [
                "Sideways is a direction too.",
                "I wish I had thumbs. Just saying.",
                "Code is like sand: it gets everywhere.",
                "Claws polished, ready for bugs.",
            ],
        },
        fallback=True,
    ),
]


def choose_quip(ctx: QuipContext, lang: str,
                rng: random.Random) -> Optional[str]:
    """Pick one filled-in quip for the context, or None if nothing matches.

    All matching rules contribute their templates; specific (non-fallback)
    rules push the fallback smalltalk out of the pool. The pick is uniform
    over the pooled templates via the injected rng.
    """
    if lang not in ("de", "en"):
        lang = "de"
    matched = [r for r in QUIP_RULES if r.predicate(ctx)]
    specific = [r for r in matched if not r.fallback]
    pool = specific or matched
    templates = [t for r in pool for t in r.templates.get(lang, ())]
    if not templates:
        return None
    return rng.choice(templates).format(**template_values(ctx))


class QuipScheduler:
    """Pure timing state machine: WHEN the next quip may fire.

    No timers inside — the app owns the QTimer and merely asks
    should_fire(now) on each tick, calling mark_fired(now) after showing a
    quip. now is a monotonic clock value (time.monotonic()); the jitter
    keeps the pet from quipping on a metronome.
    """

    def __init__(self, rng: random.Random,
                 min_interval_s: float = QUIP_MIN_INTERVAL_S,
                 jitter_s: float = QUIP_JITTER_S) -> None:
        self._rng = rng
        self._min_interval_s = float(min_interval_s)
        self._jitter_s = float(jitter_s)
        self._next_at: Optional[float] = None   # None -> free to fire

    def should_fire(self, now_mono: float) -> bool:
        return self._next_at is None or now_mono >= self._next_at

    def mark_fired(self, now_mono: float) -> None:
        self._next_at = (now_mono + self._min_interval_s
                         + self._rng.uniform(0.0, self._jitter_s))
