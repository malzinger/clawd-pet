"""Headless smoke test: scan logs, render every mood, build the panel."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtWidgets import QApplication, QLabel

from .activity import (
    SessionContext,
    _user_prompt_text,
    read_last_activity,
    read_session_context,
    tool_detail,
)
from .api import UsageBucket
from .app import ClawdApp
from .art import SpriteSet, make_clawd_icon
from .autostart import autostart_command, autostart_enabled, autostart_supported
from .bubble import SpeechBubble
from .config import (
    ACTIVITY_IDLE_S,
    CLAUDE_PROJECTS_DIR,
    HISTORY_KEEP_DAYS,
    MAX_TOKENS,
    SPRITE_DIR,
    SPRITE_FILES,
    model_cost,
)
from .history import HistoryChart, HistoryStore
from .hooks import (
    ensure_hook_token,
    hooks_registered,
    parse_hook_datagram,
    register_hooks,
    unregister_hooks,
)
from .i18n import _fmt_dur, fmt_de, language, set_language, tool_action, tool_bubble, tr
from .moods import PET_SPAM_COUNT, mood_for_pct
from .panel import PanelWidget
from .pet import PetWidget
from .update import is_trusted_update_url, parse_version, version_is_newer
from .usage import (
    _FILE_CACHE,
    UsageSnapshot,
    _current_window_start,
    _weekly_window,
    burn_eta,
    effective_max_tokens,
    is_calibrated,
    notify_decision,
    reset_auto_calibration,
    scan_usage,
    set_auto_calibration,
    set_max_tokens_override,
    weekly_budget_all,
)

def run_selftest() -> int:
    """Headless smoke test: scan logs, render every mood, build the panel."""
    app = QApplication(sys.argv)

    # a machine without Claude Code (e.g. CI) has no log directory yet; an
    # empty one makes the scan return a clean zero snapshot instead of an
    # error, so the log-mode panel assertions below hold everywhere
    CLAUDE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    snap = scan_usage()
    print(f"[selftest] dir={CLAUDE_PROJECTS_DIR}")
    print(f"[selftest] files_scanned={snap.files_scanned} entries={snap.entries}")
    print(f"[selftest] input={snap.input_tokens} output={snap.output_tokens} "
          f"cache_read={snap.cache_read} cache_creation={snap.cache_creation}")
    print(f"[selftest] total={snap.total} pct={snap.pct:.1f}% "
          f"mood={mood_for_pct(snap.pct)} oldest={snap.oldest} error={snap.error!r}")

    sprites = SpriteSet()
    frames = {m: len(s.pixmaps) for m, s in sprites.sprites.items()}
    print(f"[selftest] sprites loaded: {sorted(sprites.sprites)} frames={frames}")
    for m in ("type", "read", "think", "notify", "pet", "annoyed",
              "juggle", "conduct", "sweep", "carry"):
        # only require a mood when its gif is actually present, so an older
        # sprites/ folder still runs (MOOD_FALLBACK covers the missing ones)
        if (SPRITE_DIR / SPRITE_FILES[m]).is_file():
            assert m in sprites.sprites, f"sprite {m!r} not loaded"
    print(f"[selftest] by_model (5h): {snap.by_model}")

    pet = PetWidget(None)
    for pct in (10, 60, 90, 120):
        pet.set_pct(pct)
        pm = pet.grab()
        assert not pm.isNull(), f"render failed for pct={pct}"

    panel = PanelWidget()
    panel.update_snapshot(snap)
    panel.adjustSize()
    assert panel.height() > 100, "panel layout collapsed"

    api_snap = UsageSnapshot(updated_at=datetime.now(), source="api", pct=65.0)
    api_snap.buckets = [
        UsageBucket("five_hour", "5-Stunden-Limit", 65.0,
                    datetime.now(timezone.utc) + timedelta(hours=3, minutes=53)),
        UsageBucket("seven_day", "Wöchentlich · alle Modelle", 28.0,
                    datetime.now(timezone.utc) + timedelta(days=5)),
        UsageBucket("seven_day_fable", "Wöchentlich · Fable", 52.0,
                    datetime.now(timezone.utc) + timedelta(days=5)),
    ]
    panel.update_snapshot(api_snap)
    assert set(panel._rows) >= {"five_hour", "seven_day", "seven_day_fable"}
    print(f"[selftest] api-mode panel rows: {sorted(panel._rows)}")

    # Child geometry only exists once the widget has been shown — do it
    # far off-screen so the check runs unattended.
    panel.move(-4000, -4000)
    panel.show()
    app.processEvents()
    assert not panel._rows["estimate"]["bar"].isVisible(), \
        "log-mode row still visible in api mode"

    # let the progress-bar animations settle so the preview shows real bars
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        app.processEvents()

    # Verify no label is clipped: every label must fit its own width.
    clipped = []
    for w in panel.findChildren(QLabel):
        txt = w.text()
        if not txt or not w.isVisibleTo(panel):
            continue
        need = w.fontMetrics().boundingRect(
            QRect(0, 0, w.width(), 10_000),
            Qt.TextWordWrap if w.wordWrap() else 0, txt)
        if need.width() > w.width() + 1 or need.height() > w.height() + 1:
            clipped.append((txt[:40], need.width(), w.width(), need.height(), w.height()))
    print(f"[selftest] panel size: {panel.width()}x{panel.height()}, clipped labels: {clipped}")
    assert not clipped, f"clipped labels: {clipped}"

    assert panel.height() > 200, f"panel too short ({panel.height()}px) — rows squashed"

    here = Path(__file__).resolve().parent.parent   # repo root, not the package
    panel.grab().save(str(here / "panel_preview_api.png"))

    panel.update_snapshot(snap)          # back to log mode
    app.processEvents()
    assert not panel._rows["five_hour"]["bar"].isVisible(), \
        "api row still visible in log mode"
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        app.processEvents()
    panel.grab().save(str(here / "panel_preview_logs.png"))
    print(f"[selftest] log-mode panel: {panel.width()}x{panel.height()}")
    panel.hide()
    print("[selftest] previews written: panel_preview_api.png, panel_preview_logs.png")

    # calibration: budget derived from Claude's own percentage
    assert effective_max_tokens() == MAX_TOKENS and not is_calibrated()
    set_max_tokens_override(int(round(178_000 / 0.65)))
    assert is_calibrated() and effective_max_tokens() == 273_846
    probe = scan_usage()
    expected = probe.weighted / 273_846 * 100.0
    assert abs(probe.pct - expected) < 0.01, "calibrated budget not used by scan"
    print(f"[selftest] calibration: 178.000 Tokens @ 65 % -> "
          f"{effective_max_tokens()} Tokens Budget; live pct now {probe.pct:.1f}%")
    set_max_tokens_override(None)
    assert not is_calibrated()

    # weekly window from a known anchor + budget-derived percentages
    set_auto_calibration(
        weekly_anchor=datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc),
        weekly_budget=1_000_000, weekly_model_budgets={"Fable": 500_000})
    ws, wr = _weekly_window(datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc))
    assert wr == datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc)
    assert ws == wr - timedelta(days=7)
    panel.update_snapshot(scan_usage())
    assert "week_all" in panel._rows and "week:Fable" in panel._rows
    print(f"[selftest] weekly window OK, week_total={panel._snap.week_total} "
          f"week_by_model={panel._snap.week_by_model}")

    # per-model cost weighting: pricier models count more against the plan
    assert model_cost("Opus") == 5.0 and model_cost("Fable") == 0.3
    assert model_cost("Sonnet") == 1.0 and model_cost("Made-Up-Model") == 1.0
    # regression: the placeholder budget must be on the cost-weighted scale, so
    # a typical heavy all-Opus 5h window does not read a false >100% "limit"
    typical_opus_weighted = 24_000_000        # ~ observed heavy all-Opus window
    assert typical_opus_weighted / MAX_TOKENS * 100 < 100, \
        "placeholder budget too small for cost-weighted scale -> false limit"

    # weekly row shows no empty progress bar when the weekly budget is unknown
    reset_auto_calibration()
    assert weekly_budget_all() is None
    nb = scan_usage()
    nb.week_total = max(nb.week_total, 500_000)
    panel.update_snapshot(nb)
    app.processEvents()
    assert "week_all" in panel._rows
    assert panel._rows["week_all"]["bar"].isHidden(), "empty weekly bar still shown"
    assert panel._rows["week_all"]["pct"].isHidden(), "dash percentage still shown"
    assert panel._rows["week_all"]["reset"].text(), "weekly token count missing"
    print("[selftest] model cost + weekly-bar-when-unbudgeted OK")

    # fixed-window replay: chained windows and fresh starts after silence
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    chain = [base, base + 2 * h, base + 4 * h, base + 5 * h + h / 2, base + 6 * h]
    assert _current_window_start(chain, base + 6 * h) == base + 5 * h + h / 2
    fresh = [base, base + 12 * h]
    assert _current_window_start(fresh, base + 12 * h + h / 2) == base + 12 * h
    assert _current_window_start([base], base + 9 * h) is None
    print("[selftest] window replay OK")

    # real-time activity: tail parser + hook registration on scratch files
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "session.jsonl"
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}}) + "\n",
            encoding="utf-8")
        assert read_last_activity(log) == ("working", "Bash")
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "done"}]}}) + "\n",
            encoding="utf-8")
        assert read_last_activity(log) == ("waiting", None)
        future = datetime.now(timezone.utc) + timedelta(seconds=ACTIVITY_IDLE_S + 60)
        assert read_last_activity(log, now=future) is None

        sp = Path(td) / "settings.json"
        sp.write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "x", "hooks": []}]}}),
            encoding="utf-8")
        assert register_hooks(sp, 'py "clawd_hook.py"')
        assert hooks_registered(sp)
        data = json.loads(sp.read_text(encoding="utf-8"))
        assert len(data["hooks"]["PreToolUse"]) == 2
        assert "Notification" in data["hooks"]
        assert unregister_hooks(sp)
        assert not hooks_registered(sp)
        data = json.loads(sp.read_text(encoding="utf-8"))
        assert data["hooks"]["PreToolUse"] == [{"matcher": "x", "hooks": []}]
    print("[selftest] activity parser + hook registration OK")

    assert not sprites.sprites or "happy" in sprites.sprites, "happy sprite missing"

    bubble = SpeechBubble()
    bubble.show_text("führt Befehle aus …", pet)
    bubble.hide()

    pet.set_pct(10)
    pet.set_activity(("working", "Bash"))
    assert pet.mood == "focus"
    pet.set_activity(("working", "Edit"))      # tool picks the animation
    assert pet.mood == "type"
    pet.set_activity(("working", "Read"))
    assert pet.mood == "read"
    pet.set_activity(("working", None))        # thinking (no tool)
    assert pet.mood == "think"
    pet.set_activity(("needs_input", None))    # Claude asks you
    assert pet.mood == "notify"
    pet.set_activity(("waiting", None))
    assert pet.mood == "happy"
    pet.set_pct(90)                      # quota alarm overrides activity
    assert pet.mood == "panic"
    pet.set_pct(100)
    pet.set_activity(("working", "Edit"))
    assert pet.mood == "limit"           # over-limit overrides the tool mood
    pet.set_pct(10)
    pet.set_activity(None)
    assert pet.mood == "chill"
    # petting reaction briefly overrides the mood, then reverts
    if "pet" in pet._sprites.sprites:
        pet._play_reaction()
        assert pet._react_active and pet.mood == "pet"
        pet.set_activity(("working", "Edit"))  # ignored while reacting
        assert pet.mood == "pet"
        pet._end_reaction()
        assert not pet._react_active and pet.mood == "type"
    # over-petting makes Clawd annoyed instead of doing a happy jump
    if "annoyed" in pet._sprites.sprites:
        pet.set_pct(10)
        pet.set_activity(None)
        pet._end_reaction()
        pet._pet_times = []
        for _ in range(PET_SPAM_COUNT):
            pet._play_reaction()
        assert pet.mood == "annoyed", "spam petting should annoy Clawd"
        pet._end_reaction()
    # a random idle flourish shows only while calm, and is dropped when busy
    if pet._idle_pool:
        pet.set_pct(10)
        pet.set_activity(None)
        pet._quota_mood = "chill"
        pet._idle_variant = pet._idle_pool[0]
        pet._update_mood()
        assert pet.mood == pet._idle_pool[0], "idle flourish not shown while calm"
        pet.set_activity(("working", "Read"))   # busy -> flourish ignored AND cleared
        assert pet.mood == "read"
        assert pet._idle_variant is None, "stale flourish not cleared on interruption"
        pet.set_activity(None)                  # back to calm -> plain chill, no resume
        assert pet.mood == "chill"
    print("[selftest] activity mood combination OK")

    # language toggle: strings and number formatting switch together
    assert language() == "de" and fmt_de(1234567) == "1.234.567"
    set_language("en")
    assert fmt_de(1234567) == "1,234,567"
    assert tr("row_week_all") == "Weekly · all models"
    assert tr("reset_in_hm", h=3, m=7) == "Resets in 3 h 07 min"
    assert tool_bubble("Bash") == "running commands …"
    set_language("de")
    assert tr("row_week_all") == "Wöchentlich · alle Modelle"
    print("[selftest] language toggle OK")

    assert not make_clawd_icon().isNull(), "tray icon failed"
    assert fmt_de(1234567) == "1.234.567"
    assert mood_for_pct(49.9) == "chill" and mood_for_pct(50) == "focus"
    assert mood_for_pct(80) == "panic" and mood_for_pct(100) == "limit"

    # burn-rate forecast: linear projection to 100 %
    b0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    m = timedelta(minutes=1)
    eta = burn_eta([(b0, 40.0), (b0 + 20 * m, 50.0)])
    assert eta == b0 + 120 * m, f"burn eta wrong: {eta}"   # 0.5 %/min -> +100 min
    assert burn_eta([(b0, 40.0), (b0 + 2 * m, 50.0)]) is None    # span too short
    assert burn_eta([(b0, 50.0), (b0 + 20 * m, 45.0)]) is None   # usage falling
    assert burn_eta([(b0, 50.0)]) is None
    now_utc = datetime.now(timezone.utc)
    snap_fc = UsageSnapshot(updated_at=datetime.now(), pct=50.0,
                            oldest=now_utc - timedelta(hours=1),
                            burn_eta=now_utc + timedelta(hours=1))
    panel.update_snapshot(snap_fc)
    assert panel.forecast_label.text(), "forecast line missing"
    snap_fc.burn_eta = now_utc + timedelta(hours=9)   # past the window reset
    panel.update_snapshot(snap_fc)
    assert panel.forecast_label.text() == tr("forecast_ok")
    print("[selftest] burn-rate forecast OK")

    # notifications: threshold crossings and window-reset detection
    assert notify_decision(None, 85.0) is None       # no toast right at startup
    assert notify_decision(75.0, 85.0) == "warn80"
    assert notify_decision(85.0, 96.0) == "warn95"
    assert notify_decision(79.0, 96.0) == "warn95"   # highest threshold wins
    assert notify_decision(81.0, 82.0) is None
    assert notify_decision(76.0, 3.0) == "reset"
    assert notify_decision(30.0, 3.0) is None
    for kind in ("warn80", "warn95", "reset"):
        assert tr(f"notify_{kind}_title") and tr(f"notify_{kind}_text")
    print("[selftest] notification decisions OK")

    # per-source state: a transient api<->logs fallback must not wipe the
    # other source's history (else the forecast blanks and toasts never fire)
    capp = ClawdApp(app, with_tray=False)
    capp._notify_transition("api", 78.0)
    assert capp._prev_pct.get("api") == 78.0
    capp._notify_transition("logs", 40.0)          # transient fallback blip
    assert capp._prev_pct.get("api") == 78.0       # api lineage untouched
    assert capp._prev_pct.get("logs") == 40.0
    # so on return to api the crossing is judged 78 -> 82, not None -> 82
    assert notify_decision(capp._prev_pct.get("api"), 82.0) == "warn80"
    capp._burn_samples.setdefault("api", []).append(
        (datetime.now(timezone.utc), 78.0))
    capp._notify_transition("logs", 41.0)
    assert capp._burn_samples.get("api"), "api burn history wiped by logs blip"
    print("[selftest] per-source burn/notify state OK")

    # autostart: command resolvable, registry read only (no write in a test)
    if autostart_supported():
        assert autostart_command(), "no autostart runner found"
        assert isinstance(autostart_enabled(), bool)
    print("[selftest] autostart OK")

    # macOS autostart: LaunchAgent plist round-trip against a scratch path
    if sys.platform == "darwin":
        import plistlib
        from .autostart import _autostart_args, _set_autostart_darwin
        with tempfile.TemporaryDirectory() as td:
            pl = Path(td) / "com.clawdpet.clawd.plist"
            assert _autostart_args(), "no launch args found"
            assert _set_autostart_darwin(True, pl) and pl.is_file()
            data = plistlib.loads(pl.read_bytes())
            assert data["RunAtLoad"] is True and data["ProgramArguments"]
            assert data["ProgramArguments"][-1].endswith("clawd_pet.py")
            assert _set_autostart_darwin(False, pl) and not pl.exists()
            assert _set_autostart_darwin(False, pl), "double-disable failed"
        print("[selftest] macOS autostart plist OK")

    # update check: version parsing and strict-newer comparison
    assert parse_version("v1.2.0") == (1, 2, 0)
    assert parse_version("1.10") == (1, 10)
    assert version_is_newer("v1.2.0", "1.1.0")
    assert version_is_newer("v1.2", "1.1.9")
    assert version_is_newer("v1.10.0", "1.9.0")       # numeric, not lexical
    assert not version_is_newer("v1.1.0", "1.1.0")
    assert not version_is_newer("v1.0.0", "1.1.0")
    assert not version_is_newer("", "1.1.0")
    assert not version_is_newer("garbage", "1.1.0")
    # the toast-click handler only opens the browser for the update toast
    assert capp._last_toast_was_update is False
    print("[selftest] update version compare OK")

    # history store: throttled append, pruning, windowed series, reload
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "history.json"
        hs = HistoryStore(hp)
        hts0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        assert hs.add(hts0, 10.0) is True
        assert hs.add(hts0 + timedelta(seconds=30), 12.0) is False   # throttled
        assert hs.add(hts0 + timedelta(seconds=400), 20.0) is True
        hs._points.insert(0, (hts0 - timedelta(days=HISTORY_KEEP_DAYS + 1), 5.0))
        assert hs.add(hts0 + timedelta(seconds=800), 25.0) is True
        floor = hts0 + timedelta(seconds=800) - timedelta(days=HISTORY_KEEP_DAYS)
        assert all(p[0] >= floor for p in hs._points), "old point not pruned"
        hs2 = HistoryStore(hp)                       # reload from disk
        assert hs2._points and len(hs2._points) == len(hs._points)
        win = hs2.series(window_h=1, now=hts0 + timedelta(seconds=800))
        assert win and all(
            p[0] >= hts0 + timedelta(seconds=800) - timedelta(hours=1)
            for p in win)
        # throttle survives a restart: reload restores _last_write from disk
        hs3 = HistoryStore(hp)
        newest = max(p[0] for p in hs3._points)
        assert hs3.add(newest + timedelta(seconds=60), 99.0) is False
        assert hs3.add(newest + timedelta(seconds=400), 99.0) is True
    print("[selftest] history store OK")

    # history chart: renders standalone and shows up in the panel with data
    hseries = [(hts0 + timedelta(minutes=i * 5), 10.0 + i) for i in range(6)]
    chart = HistoryChart()
    chart.resize(320, 54)
    chart.set_series(hseries)
    assert not chart.grab().isNull(), "history chart render failed"
    chart.set_series(hseries[:1])
    assert not chart.isVisible() or chart.isHidden() is False
    panel.set_history(hseries)
    panel.update_snapshot(snap)
    app.processEvents()
    assert len(panel.history_chart._series) == 6, "panel chart series not applied"
    assert not panel.history_chart.isHidden(), "panel chart hidden with data"
    panel.set_history([])
    panel.update_snapshot(snap)
    assert panel.history_chart.isHidden(), "empty history chart still shown"
    print("[selftest] history chart OK")

    # session context: task + tool detail extracted from the tail
    assert tool_detail("Edit", {"file_path": "C:/x/clawd_pet.py"}) == "clawd_pet.py"
    assert tool_detail("Bash", {"command": "git push\nmore"}).startswith("git push")
    assert tool_detail("Grep", {"pattern": "def foo"}) == "def foo"
    assert tool_action("Edit", "foo.py") and "foo.py" in tool_action("Edit", "foo.py")
    assert _user_prompt_text("<command>/model</command>") == ""      # wrapper skipped
    assert _user_prompt_text("please refactor") == "please refactor"
    with tempfile.TemporaryDirectory() as td:
        slog = Path(td) / "s.jsonl"
        slog.write_text(
            json.dumps({"type": "user", "cwd": "C:/Users/x/Desktop/demo proj",
                        "message": {"content": "please refactor the parser"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "C:/a/foo.py"}}]}}) + "\n",
            encoding="utf-8")
        sctx = read_session_context(slog)
        assert sctx and sctx.kind == "working" and sctx.tool == "Edit"
        assert sctx.detail == "foo.py"
        assert sctx.task == "please refactor the parser"
        assert sctx.project == "demo proj"
        assert read_last_activity(slog) == ("working", "Edit")   # wrapper parity
    # a very long project name must wrap, not clip (fixed-width panel)
    long_ctx = SessionContext(
        kind="working", tool="Bash", detail="git push origin main",
        task=sctx.task,
        project="a-really-long-monorepo-folder-name-that-would-overflow-the-panel")
    panel.set_task(long_ctx)
    panel.move(-4000, -4000)
    panel.show()
    app.processEvents()
    for w in (panel.task_project, panel.task_prompt, panel.task_activity):
        if not w.text() or w.isHidden():
            continue
        need = w.fontMetrics().boundingRect(
            QRect(0, 0, w.width(), 10_000),
            Qt.TextWordWrap if w.wordWrap() else 0, w.text())
        assert need.width() <= w.width() + 1, f"task label clipped: {w.text()[:30]}"
    assert not panel.grab().isNull()
    panel.hide()
    panel.set_task(sctx)
    panel.update_snapshot(snap)
    app.processEvents()
    assert not panel.task_prompt.isHidden(), "task prompt hidden with data"
    assert "refactor" in panel.task_prompt.text()
    panel.set_task(None)
    panel.update_snapshot(snap)
    assert panel.task_title.isHidden(), "task section shown while idle"
    assert panel._task_action == "", "stale task action kept after idle"
    panel._render_task_activity()        # the 1 s countdown tick must not re-show it
    assert not panel.task_activity.text(), "orphaned activity line re-shown after idle"
    print("[selftest] session context / task view OK")

    # turn timer formatting + live rendering in the activity line
    assert _fmt_dur(5) == "0:05" and _fmt_dur(74) == "1:14"
    assert _fmt_dur(3661) == "1:01:01"
    work_ctx = SessionContext(kind="working", tool="Edit", detail="foo.py",
                              task="do the thing", project="demo")
    panel.set_task(work_ctx, work_since=time.monotonic() - 90)
    assert "· 1:" in panel.task_activity.text(), \
        f"turn timer missing: {panel.task_activity.text()!r}"
    panel.set_task(work_ctx, work_since=None)      # no timer without a start
    assert "·" not in panel.task_activity.text().split("foo.py")[-1]
    # the "your turn" alert is rate-limited and safe without a tray
    assert capp._work_kind is None and capp._last_alert_mono == 0.0
    assert isinstance(capp.notify_sound, bool)
    capp._fire_alert("t", "x")            # tray is None -> must not raise
    capp._alert_turn_done(5.0)            # below threshold -> no-op

    # cross-session guard: a newest-log switch from a working session to a
    # different waiting session must NOT fire a turn-done alert
    fired = []
    capp._alert_turn_done = lambda e: fired.append(e)
    with tempfile.TemporaryDirectory() as td:
        la, lb = Path(td) / "a.jsonl", Path(td) / "b.jsonl"
        la.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}}) + "\n",
            encoding="utf-8")
        lb.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}]}}) + "\n", encoding="utf-8")
        capp._work_kind = None
        capp._work_started_mono = None
        capp._work_log = None
        capp._newest_log = la
        capp._check_activity()            # session A is working
        assert capp._work_kind == "working" and capp._work_log == la
        capp._newest_log = lb
        capp._check_activity()            # switch to waiting session B -> no alert
        assert not fired, "spurious cross-session turn-done alert"
        capp._work_kind = "working"
        capp._work_log = lb
        capp._work_started_mono = time.monotonic() - 30
        capp._check_activity()            # B's own working->waiting -> alert
        assert fired and fired[-1] >= 20, "same-session turn-end not detected"
    print("[selftest] turn timer + your-turn alert OK")

    # hook datagram authentication: only token-prefixed events are accepted
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td) / "hook_token"
        tok = ensure_hook_token(tp)
        assert tok and len(tok) >= 32, "token too short"
        assert ensure_hook_token(tp) == tok, "token not stable across reads"
        if os.name == "posix":
            assert (tp.stat().st_mode & 0o777) == 0o600, "token file not 0600"
        raw = json.dumps({"hook_event_name": "Stop"}).encode("utf-8")
        assert parse_hook_datagram(tok.encode() + b"\n" + raw, tok) == \
            {"hook_event_name": "Stop"}
        assert parse_hook_datagram(b"deadbeef\n" + raw, tok) is None, \
            "wrong token accepted"
        assert parse_hook_datagram(raw, tok) is None, \
            "unauthenticated legacy datagram accepted"
        assert parse_hook_datagram(tok.encode() + b"\n{broken", tok) is None
        assert parse_hook_datagram(tok.encode() + b"\n[1, 2]", tok) is None
        assert parse_hook_datagram(b"", tok) is None
    assert capp._hook_token and len(capp._hook_token) >= 32
    print("[selftest] hook datagram auth OK")

    # file-cache pruning: entries of files outside the horizon are dropped
    _FILE_CACHE["/nonexistent/stale-session.jsonl"] = (0, 0, [])
    scan_usage()
    assert "/nonexistent/stale-session.jsonl" not in _FILE_CACHE, \
        "stale file-cache entry survived a full scan"
    print("[selftest] file-cache pruning OK")

    # update URLs: only this repo's release pages are ever opened
    assert is_trusted_update_url(
        "https://github.com/malzinger/clawd-pet/releases/tag/v9.9.9")
    assert not is_trusted_update_url("https://evil.example.com/clawd-pet")
    assert not is_trusted_update_url("https://github.com/someone-else/repo/x")
    assert not is_trusted_update_url("")
    print("[selftest] update-url whitelist OK")

    # --- W1-C: codex activity ---
    # Codex CLI fallback: mtime-based detection + best-effort task extraction
    from . import app as app_mod
    from .activity import newest_codex_log, read_codex_context
    from .config import CODEX_ACTIVE_S
    with tempfile.TemporaryDirectory() as td:
        cbase = Path(td) / "sessions"
        assert newest_codex_log(cbase) is None            # missing base dir
        cbase.mkdir(parents=True)
        assert newest_codex_log(cbase) is None            # empty base dir
        fresh = cbase / "2026" / "07" / "rollout-fresh.jsonl"
        fresh.parent.mkdir(parents=True)
        fresh.write_text('{"type":"user_message","content":"fix the tests"}\n',
                         encoding="utf-8")
        stale = cbase / "rollout-old.jsonl"
        stale.write_text("{}\n", encoding="utf-8")
        cnow = time.time()
        os.utime(fresh, (cnow, cnow))
        os.utime(stale, (cnow - 1000, cnow - 1000))
        assert newest_codex_log(cbase) == fresh           # fresh beats old
        os.utime(fresh, (cnow - ACTIVITY_IDLE_S - 60,) * 2)
        assert newest_codex_log(cbase) is None            # everything idle

        # read_codex_context: kind by mtime age, fixed project label, task tail
        os.utime(fresh, (cnow, cnow))
        cctx = read_codex_context(fresh)
        assert cctx and cctx.kind == "working" and cctx.tool is None
        assert cctx.project == "Codex CLI"
        assert "fix the tests" in cctx.task               # best-effort extraction
        assert CODEX_ACTIVE_S < 60 < ACTIVITY_IDLE_S
        os.utime(fresh, (cnow - 60, cnow - 60))
        cctx = read_codex_context(fresh)
        assert cctx and cctx.kind == "waiting"
        os.utime(fresh, (cnow - ACTIVITY_IDLE_S - 60,) * 2)
        assert read_codex_context(fresh) is None          # too old -> idle
        # binary garbage / broken JSON must not raise; task stays ""
        junk = cbase / "junk.jsonl"
        junk.write_bytes(b"\x00\xff\xfe not json\n{broken json\n")
        os.utime(junk, (cnow, cnow))
        jctx = read_codex_context(junk)
        assert jctx and jctx.kind == "working" and jctx.task == ""
        # payload-wrapped record shape (observed Codex rollout format)
        wrapped = cbase / "wrapped.jsonl"
        wrapped.write_text(json.dumps({"payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "refactor the scanner"}],
        }}) + "\n", encoding="utf-8")
        os.utime(wrapped, (cnow, cnow))
        wctx = read_codex_context(wrapped)
        assert wctx and "refactor the scanner" in wctx.task

        # app integration: without a Claude log the Codex fallback drives
        # set_task and takes over the work-log identity (turn timer runs)
        cnow2 = time.time()
        os.utime(fresh, (cnow2, cnow2))
        saved_state = (capp._work_kind, capp._work_log, capp._work_started_mono,
                       capp._newest_log, capp._session_ctx, capp._last_activity)
        orig_newest_codex = app_mod.newest_codex_log
        app_mod.newest_codex_log = lambda: fresh
        try:
            capp._newest_log = None                       # no active Claude log
            capp._work_kind = None
            capp._work_log = None
            capp._work_started_mono = None
            capp._check_activity()
            assert capp._session_ctx is not None
            assert capp._session_ctx.project == "Codex CLI"
            assert capp._work_kind == "working" and capp._work_log == fresh
            assert capp._work_started_mono is not None
        finally:
            app_mod.newest_codex_log = orig_newest_codex
            (capp._work_kind, capp._work_log, capp._work_started_mono,
             capp._newest_log, capp._session_ctx, capp._last_activity) = saved_state
    print("[selftest] codex activity OK")

    print("[selftest] OK")
    del app
    return 0
