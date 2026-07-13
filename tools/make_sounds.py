#!/usr/bin/env python3
"""Generate the notification WAVs in sounds/ — stdlib only (wave + math).

Writes two short, quiet chimes next to the repo root:
  sounds/done.wav      (~0.25 s, two ascending sine tones)
  sounds/attention.wav (~0.35 s, three ascending sine tones)
44.1 kHz, mono, 16-bit, peak amplitude capped at 0.35 so the pet stays
polite on speakers. Re-run after tweaking the tone tables below.
"""
import math
import struct
import wave
from pathlib import Path

RATE = 44100
PEAK = 0.35          # absolute output ceiling (quiet on purpose)
ATTACK_S = 0.006     # short linear fade-in avoids a click at tone start
DECAY = 14.0         # exponential decay rate of each tone


def _render(tones, total_s: float) -> bytes:
    """Mix (freq_hz, start_s, dur_s) sine tones into 16-bit mono frames."""
    n = int(RATE * total_s)
    samples = [0.0] * n
    for freq, start, dur in tones:
        i0 = int(start * RATE)
        for i in range(i0, min(n, int((start + dur) * RATE))):
            t = (i - i0) / RATE
            env = min(1.0, t / ATTACK_S) * math.exp(-DECAY * t)
            samples[i] += math.sin(2.0 * math.pi * freq * t) * env
    top = max(abs(s) for s in samples) or 1.0
    gain = PEAK / top
    return struct.pack(
        "<%dh" % n, *(int(s * gain * 32767) for s in samples))


def _write(path: Path, tones, total_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(_render(tones, total_s))
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def main() -> None:
    sounds = Path(__file__).resolve().parent.parent / "sounds"
    # "done": a gentle two-note ascending chime (turn finished)
    _write(sounds / "done.wav",
           [(880.0, 0.00, 0.16),        # A5
            (1174.66, 0.09, 0.16)],     # D6
           0.25)
    # "attention": three ascending notes (Claude needs your input)
    _write(sounds / "attention.wav",
           [(740.0, 0.00, 0.14),        # F#5
            (932.33, 0.10, 0.14),       # A#5
            (1108.73, 0.20, 0.15)],     # C#6
           0.35)


if __name__ == "__main__":
    main()
