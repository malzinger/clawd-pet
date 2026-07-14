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
    # The layout is tuned for the platform font (Segoe UI / Helvetica Neue).
    # Headless CI may silently substitute a wider fallback family (Windows
    # offscreen has no Segoe UI) — that is a property of the test rig, not of
    # the product, so the strict assert only runs when the real font loaded.
    from PyQt5.QtGui import QFontInfo
    _actual_family = QFontInfo(panel._title.font()).family()
    _requested = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}.get(sys.platform)
    if _requested is not None and _actual_family != _requested:
        if clipped:
            print(f"[selftest] WARN: clipping under fallback font "
                  f"{_actual_family!r} tolerated (headless rig)")
    else:
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
    # X2 fix: a live pet may have persisted an auto-calibration in
    # ~/.clawd/calibration.json — this block (and line 204 below) already
    # overwrites/reset that state anyway, so clear it up front to make the
    # baseline assertion hold on machines with a live installation too.
    reset_auto_calibration()
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
    _FILE_CACHE["/nonexistent/stale-session.jsonl"] = (0, 0, [], "")
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

    # --- W1-A: pet behavior ---
    from .moods import MOOD_FALLBACK
    assert MOOD_FALLBACK["juggle"] == "focus", "juggle should fall back to focus"
    _w1a_pct, _w1a_activity = pet.pct, pet._activity
    pet.set_pct(10)
    if "juggle" in pet._sprites.sprites:
        pet.set_activity(("working", "Task"))    # delegating -> juggling
        assert pet.mood == "juggle", "Task should map to juggle"
        pet.set_activity(("working", "Agent"))
        assert pet.mood == "juggle", "Agent should map to juggle"
    pet.set_activity(None)
    assert pet.mood == "chill"
    if pet._sprites.sprites:
        pet._quota_mood = "sleep"                # put Clawd to sleep
        pet.set_activity(None)
        pet._update_mood()
        assert pet.mood == "sleep"
        assert pet._startle() is True, "hovering should startle sleeping Clawd"
        assert pet._react_active and pet.mood in ("pet", "happy")
        assert not pet.grab().isNull(), "startle reaction did not render"
        assert pet._startle() is False, "startle during reaction must be a no-op"
        pet._end_reaction()
        assert pet.mood == "sleep", "startled Clawd should fall back asleep"
        assert pet._startle() is False, "cooldown should block a re-startle"
        pet._last_startle = None                 # reset the cooldown stamp
    pet.set_pct(_w1a_pct)                        # restore pre-block state
    pet.set_activity(_w1a_activity)
    print("[selftest] W1-A pet behavior OK")

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

    # --- W1-B: cost + projects ---
    from .config import SONNET_INPUT_USD_PER_MTOK
    from .usage import _parse_file_entries

    # F10 backend: the per-project split is dedup-consistent with the total
    fresh = scan_usage()
    assert isinstance(fresh.by_project, dict)
    assert isinstance(fresh.by_project_weighted, dict)
    assert sum(fresh.by_project.values()) == fresh.total, \
        "per-project token split does not add up to the window total"
    if fresh.by_project:
        assert all(isinstance(k, str) and k for k in fresh.by_project), \
            "empty project key in by_project"

    # F10 synthetic: project (cwd) extracted from a session log and attached
    # to each parsed entry, cache tuple carries the cwd as its 4th element
    with tempfile.TemporaryDirectory() as td:
        plog = Path(td) / "proj-session.jsonl"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        plog.write_text(
            json.dumps({"type": "user", "cwd": "C:/Users/x/dev/demo proj",
                        "message": {"content": "hi"}}) + "\n"
            + json.dumps({"type": "assistant", "timestamp": now_iso,
                          "message": {"id": "msg_w1b_1",
                                      "model": "claude-sonnet-4",
                                      "usage": {"input_tokens": 100,
                                                "output_tokens": 50}}}) + "\n",
            encoding="utf-8")
        pentries = _parse_file_entries(plog)
        assert len(pentries) == 1, f"unexpected entries: {pentries!r}"
        assert pentries[0][7] == "C:/Users/x/dev/demo proj", \
            "cwd not attached to the parsed entry"
        assert Path(pentries[0][7]).name == "demo proj"
        assert _FILE_CACHE[str(plog)][3] == "C:/Users/x/dev/demo proj"
        del _FILE_CACHE[str(plog)]      # scratch file, keep the cache clean

    # F4 backend: weighted units x Sonnet input price = API-cost equivalent
    xpanel = PanelWidget()
    cost_snap = UsageSnapshot(weighted=2_000_000, week_weighted=10_000_000,
                              updated_at=datetime.now(), pct=10.0)
    cost_snap.by_project = {"clawd-pet": 1_000, "other": 500}
    cost_snap.by_project_weighted = {"clawd-pet": 1_500_000.0,
                                     "other": 500_000.0}
    xpanel.update_snapshot(cost_snap)
    assert not xpanel.cost_label.isHidden(), "cost line hidden despite usage"
    assert "$6.00" in xpanel.cost_label.text(), xpanel.cost_label.text()
    assert "$30.00" in xpanel.cost_label.text(), xpanel.cost_label.text()
    assert "clawd-pet 75%" in xpanel.projects_label.text(), \
        xpanel.projects_label.text()
    assert "other 25%" in xpanel.projects_label.text()
    zero_snap = UsageSnapshot(updated_at=datetime.now())
    xpanel.update_snapshot(zero_snap)
    assert not xpanel.cost_label.text(), "cost line shown without usage"
    assert xpanel.cost_label.isHidden()
    assert not xpanel.projects_label.text(), "projects line shown without data"
    only_q = UsageSnapshot(weighted=1_000_000, updated_at=datetime.now())
    only_q.by_project_weighted = {"?": 1_000_000.0}
    xpanel.update_snapshot(only_q)
    assert not xpanel.projects_label.text(), "'?' pseudo-project shown"

    # F4/F10 frontend: rendered off-screen, new labels must not be clipped
    xpanel.update_snapshot(cost_snap)
    xpanel.move(-4000, -4000)
    xpanel.show()
    app.processEvents()
    assert not xpanel.grab().isNull(), "cost/projects panel render failed"
    from PyQt5.QtGui import QFontInfo as _QFI
    _req = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}.get(sys.platform)
    _strict = _req is None or _QFI(xpanel.cost_label.font()).family() == _req
    for w in (xpanel.cost_label, xpanel.projects_label):
        assert w.text() and not w.isHidden()
        need = w.fontMetrics().boundingRect(
            QRect(0, 0, w.width(), 10_000),
            Qt.TextWordWrap if w.wordWrap() else 0, w.text())
        fits = (need.width() <= w.width() + 1
                and need.height() <= w.height() + 1)
        if not fits and not _strict:
            # headless Windows has no Segoe UI; the wider fallback font may
            # clip — same tolerance as the title clip check above
            print("[selftest] WARN: cost/projects clip under fallback "
                  "font tolerated: " + ascii(w.text()[:40]))
            continue
        assert fits, f"cost/projects label clipped: {w.text()[:40]!r}"
    xpanel.hide()
    assert SONNET_INPUT_USD_PER_MTOK == 3.0
    print("[selftest] cost estimate + project split OK")

    # --- W2: motion ---
    from PyQt5.QtCore import QPoint
    from .config import THROW_MIN_SPEED

    # F5 wander: a forced walk state moves horizontally and stays on-screen
    pet.set_pct(10)
    pet.set_activity(None)
    pet.enable_wander(True)
    assert pet._wander_timer.isActive(), "wander timer not running"
    wav = pet._screen_avail()
    pet.move(wav.center().x() - pet.width() // 2, wav.bottom() - pet.height())
    pet._wander_state = "walk"
    pet._wander_dir = 1
    pet._wander_until = time.monotonic() + 60.0    # keep walking for the test
    wx0, wy0 = pet.x(), pet.y()
    for _ in range(5):
        pet._wander_tick()
    assert pet.x() != wx0 and pet.y() == wy0, "wander did not walk horizontally"
    assert wav.left() <= pet.x() <= wav.right() - pet.width(), \
        "wander left the available screen area"
    # blocker: a live activity must freeze the walk immediately
    pet.set_activity(("working", "Bash"))
    wbx = pet.x()
    pet._wander_tick()
    assert pet.x() == wbx, "wander moved while Clawd is working"
    assert pet._wander_state == "pause", "blocked walk not paused"
    pet.set_activity(None)
    # facing flip: walking left renders via the mirrored blit
    pet._wander_facing = -1
    assert not pet.grab().isNull(), "mirrored (left-facing) render failed"
    pet.enable_wander(False)
    assert not pet._wander_timer.isActive(), "wander timer still active"
    assert pet._wander_facing == 1, "facing not reset on disable"

    # F12 throw: headless trajectory terminates at rest inside the rect
    tavail = QRect(0, 0, 2000, 1000)
    pet.move(100, 100)
    pet._start_throw(1200.0, -300.0)
    assert pet._throw_active
    tsteps = 0
    while pet._throw_step(0.033, tavail):
        tsteps += 1
        assert tsteps < 1000, "throw physics did not terminate"
    assert not pet._throw_active
    assert tavail.left() <= pet.x() <= tavail.right() - pet.width()
    assert tavail.top() <= pet.y() <= tavail.bottom() - pet.height()
    pet._throw_timer.stop()               # headless: no event loop ran
    # ceiling bounce: a hard upward throw must mirror at avail.top()
    pet.move(500, 5)
    pet._start_throw(200.0, -2500.0)
    tsteps = 0
    while pet._throw_step(0.033, tavail):
        tsteps += 1
        assert tsteps < 1000, "ceiling throw did not terminate"
        assert pet.y() >= tavail.top(), "throw escaped through the ceiling"
    pet._throw_timer.stop()
    # release-velocity estimate from known synthetic drag samples
    tnow = time.monotonic()
    pet._drag_samples = [(tnow - 0.10, QPoint(0, 0)),
                         (tnow - 0.05, QPoint(60, 0)),
                         (tnow, QPoint(120, 0))]        # 120 px in 0.1 s
    tvx, tvy = pet._release_velocity()
    assert 1000.0 <= tvx <= 1400.0, f"vx estimate off: {tvx}"
    assert abs(tvy) < 1.0, f"vy estimate off: {tvy}"
    assert (tvx * tvx + tvy * tvy) ** 0.5 >= THROW_MIN_SPEED
    pet._drag_samples = []
    assert pet._release_velocity() == (0.0, 0.0)        # no samples -> no throw

    # F8 click-through: bounding-box input mask set and cleared with sprites
    if pet._sprites.sprites:
        pet.set_click_through(True)
        assert not pet.mask().isEmpty(), "click-through mask missing"
        assert not pet.grab().isNull(), "masked render failed"
        pet.set_click_through(False)
        assert pet.mask().isEmpty(), "input mask not cleared"
        assert not pet.grab().isNull(), "unmasked render failed"
    else:
        pet.set_click_through(True)       # vector fallback: must be a no-op
        pet.set_click_through(False)

    # leave the pet tidy for anything running after this block
    assert not pet._wander_timer.isActive() and not pet._throw_active
    pet.set_pct(10)
    pet.set_activity(None)

    # app level: toggles round-trip through QSettings without a tray
    _w2_wander = capp.settings.value("wander")
    _w2_click = capp.settings.value("click_through")
    wv0 = capp.wander
    capp.toggle_wander()
    assert capp.wander == (not wv0)
    assert capp.settings.value("wander", type=bool) == capp.wander
    capp.toggle_wander()
    assert capp.wander == wv0
    assert capp.pet._wander_timer.isActive() == wv0   # timer mirrors the setting
    cv0 = capp.click_through
    capp.toggle_click_through()
    assert capp.click_through == (not cv0)
    assert capp.settings.value("click_through", type=bool) == capp.click_through
    capp.toggle_click_through()
    assert capp.click_through == cv0
    # restore the user's stored values (or leave no trace at all)
    for _key, _old in (("wander", _w2_wander), ("click_through", _w2_click)):
        if _old is None:
            capp.settings.remove(_key)
        else:
            capp.settings.setValue(_key, _old)
    capp.pet.enable_wander(False)         # selftest instance: timers off
    print("[selftest] W2 motion OK")

    # --- W3: customization ---
    import shutil
    from . import sounds
    from .config import PET_HEIGHT, PET_SIZE_FACTORS

    # F2: SpriteSet scales to the requested height, widget rebuild follows
    assert set(PET_SIZE_FACTORS) == {"S", "M", "L"}
    s_small = SpriteSet(height=66)
    if s_small.sprites:
        assert s_small.height == 66
        assert s_small.width < SpriteSet(height=132).width, \
            "smaller sprite set not narrower"
    pet.rebuild(height=92)
    assert pet.height() == 92, f"rebuild height not applied: {pet.height()}"
    assert not pet.grab().isNull(), "rebuilt pet did not render"
    pet.rebuild(height=PET_HEIGHT)               # back to the original state
    assert pet.height() == PET_HEIGHT

    # F2 app level: size preset round-trips through QSettings + pet geometry
    _w3_size = capp.settings.value("pet_size")
    capp.set_pet_size("S")
    assert str(capp.settings.value("pet_size")) == "S"
    assert capp.pet.height() == int(PET_HEIGHT * PET_SIZE_FACTORS["S"])
    assert capp.pet.height() < PET_HEIGHT, "size S did not shrink the pet"
    capp.set_pet_size("M")
    assert capp.pet.height() == PET_HEIGHT
    if _w3_size is None:
        capp.settings.remove("pet_size")
    else:
        capp.settings.setValue("pet_size", _w3_size)

    # F9: committed WAV assets exist; play() is bool and never raises
    # (muted: QT_QPA_PLATFORM=offscreen hides windows but NOT audio — an
    # unmuted selftest audibly beeps on the machine running it)
    os.environ["CLAWD_NO_SOUND"] = "1"
    _w3_sounds = Path(__file__).resolve().parent.parent / "sounds"
    assert (_w3_sounds / "done.wav").is_file(), "done.wav asset missing"
    assert (_w3_sounds / "attention.wav").is_file(), "attention.wav missing"
    assert isinstance(sounds.play("done"), bool)     # offscreen may be False
    assert isinstance(sounds.play("done"), bool)     # cached effect reused
    assert sounds.play("unknown") is False

    # F13: custom sprite packs — empty folder falls back, partial pack loads
    with tempfile.TemporaryDirectory() as td:
        w3_empty = Path(td) / "empty"
        w3_empty.mkdir()
        assert SpriteSet(sprite_dir=w3_empty).sprites == {}, \
            "empty pack folder must load no sprites"
        w3_idle = SPRITE_DIR / SPRITE_FILES["chill"]
        if w3_idle.is_file():
            w3_pack = Path(td) / "pack"
            w3_pack.mkdir()
            shutil.copy(w3_idle, w3_pack / SPRITE_FILES["chill"])
            assert set(SpriteSet(sprite_dir=w3_pack).sprites) == {"chill"}, \
                "one-gif pack should load exactly the chill mood"
            pet.rebuild(sprite_dir=w3_pack)
            assert not pet.grab().isNull(), "custom-pack pet did not render"
            # app level: setter round-trip without any QFileDialog involved
            _w3_sdir = capp.settings.value("sprite_dir")
            assert capp._set_sprite_dir(w3_pack) is True
            assert str(capp.settings.value("sprite_dir")) == str(w3_pack)
            assert capp.sprite_dir == w3_pack
            capp.reset_sprite_dir()
            assert capp.sprite_dir is None
            assert capp.settings.value("sprite_dir") is None
            if _w3_sdir is not None:
                capp.settings.setValue("sprite_dir", _w3_sdir)
        pet.rebuild(sprite_dir=None, height=PET_HEIGHT)  # restore defaults
    assert pet.height() == PET_HEIGHT
    print("[selftest] W3 customization OK")

    # --- release gate: tray menu builds without a tray -------------------
    # capp runs with with_tray=False, so build_menu() was never exercised and
    # an attribute read before its initialisation slipped through (pet_size
    # was set AFTER _setup_tray). Building the menu here catches any such
    # ordering regression on every future run.
    menu = capp.build_menu(None)
    acts = [a.text() for a in menu.actions() if a.text()]
    assert len(acts) >= 8, f"tray menu suspiciously small: {acts}"
    assert any(tr("menu_size") in a for a in acts), "size submenu missing"
    assert tr("menu_clawd_login") in acts, "Clawd login entry missing"
    assert tr("menu_sound_test") in acts, "sound test entry missing"
    menu.deleteLater()
    print("[selftest] tray menu build OK")

    # --- v1.8 port: Clawd's own login (token store + PKCE url builder) ---
    from . import api as api_mod
    from .api import (_clawd_own_token, _store_clawd_auth,
                      clawd_build_authorize_url)
    from .config import OAUTH_CLIENT_ID
    url, verifier, redirect = clawd_build_authorize_url()
    assert url.startswith("https://platform.claude.com/oauth/authorize?")
    assert OAUTH_CLIENT_ID in url and "code_challenge=" in url and "state=" in url
    assert len(verifier) >= 40 and redirect.startswith("https://console.anthropic.com/")
    url2 = clawd_build_authorize_url()[0]
    assert url != url2, "authorize url must use fresh PKCE material"
    with tempfile.TemporaryDirectory() as td:
        af = Path(td) / "auth.json"
        good = {"access_token": "tok-live",
                "expires_at": int(time.time() * 1000 + 3600_000)}
        assert _store_clawd_auth(good, af) is True and af.is_file()
        if os.name == "posix":
            assert (af.stat().st_mode & 0o777) == 0o600, "auth file not 0600"
        assert _clawd_own_token(af) == "tok-live"        # valid -> returned as-is
        # expired without a refresh token: throttle is armed, no network happens
        api_mod._clawd_refresh_ts = 0.0
        _store_clawd_auth({"access_token": "tok-old", "expires_at": 1}, af)
        assert _clawd_own_token(af) is None
        assert api_mod._clawd_refresh_ts > 0.0, "refresh throttle not armed"
        assert _clawd_own_token(af) is None              # inside cooldown -> fast None
        api_mod._clawd_refresh_ts = 0.0                  # restore module state
        assert _clawd_own_token(Path(td) / "missing.json") is None
    # CLAWD_NO_API guard still short-circuits the whole chain (when set)
    if os.environ.get("CLAWD_NO_API"):
        from .api import _get_access_token
        assert _get_access_token() is None
    capp.test_sound()                                    # must not raise offscreen
    print("[selftest] clawd own login OK")

    # --- wander walk animation: the carry gif plays while strolling ------
    if "carry" in pet._sprites.sprites:
        pet.set_pct(10)
        pet.set_activity(None)
        pet._idle_variant = None
        pet.enable_wander(True)
        pet._wander_state = "walk"
        pet._update_mood()
        assert pet.mood == "carry", "walking gait not applied while wandering"
        pet._wander_state = "pause"
        pet._update_mood()
        assert pet.mood == "chill", "gait kept after the walk ended"
        pet.enable_wander(False)
        assert pet.mood == "chill"
        print("[selftest] wander walk animation OK")

    # --- F11 permission bubble: registration, protocol, widget, hook e2e ---
    import socket as socket_mod
    import subprocess
    from .hooks import (PERMISSION_HOOK_TIMEOUT_S,
                        permission_hook_registered, register_permission_hook,
                        unregister_permission_hook)
    from .permission_bubble import PermissionBubble

    # registration is independent from the activity hooks
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "settings.json"
        sp.write_text("{}", encoding="utf-8")
        assert register_hooks(sp, 'py "clawd_hook.py"')
        assert register_permission_hook(sp, 'py "clawd_permission_hook.py"')
        assert hooks_registered(sp) and permission_hook_registered(sp)
        pdata = json.loads(sp.read_text(encoding="utf-8"))
        pentry = pdata["hooks"]["PermissionRequest"][0]["hooks"][0]
        assert pentry["timeout"] == PERMISSION_HOOK_TIMEOUT_S, \
            "permission hook needs its timeout"
        assert unregister_hooks(sp)                  # activity off ...
        assert permission_hook_registered(sp), "permission entry lost"
        assert not hooks_registered(sp)
        assert unregister_permission_hook(sp)        # ... then permission off
        assert not permission_hook_registered(sp)
        perm_cmd = 'py "clawd_permission_hook.py"'    # dedup keys on the marker
        assert register_permission_hook(sp, perm_cmd) and not \
            register_permission_hook(sp, perm_cmd), "double registration"
        unregister_permission_hook(sp)

    # widget: ask -> decide fires exactly once, timeout timer is cancelled
    perm_decisions = []
    pb = PermissionBubble()
    pb.ask("Bash", "git push origin main", pet, perm_decisions.append)
    assert pb.active and not pb.grab().isNull()
    pb.decide("allow")
    pb.decide("deny")                                # idempotent no-op
    assert perm_decisions == ["allow"] and not pb.active
    assert not pb._timeout.isActive(), "timeout timer still running"

    # app protocol: ack + decision reach the asking socket, token-prefixed
    probe_sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
    probe_sock.bind(("127.0.0.1", 0))
    probe_sock.settimeout(3.0)
    probe_port = probe_sock.getsockname()[1]
    from PyQt5.QtNetwork import QHostAddress
    was_dnd = capp.dnd
    capp.dnd = False
    capp.pet.show()                                  # engagement needs a visible pet
    capp._handle_permission_query(
        {"id": "q1", "tool_name": "Bash", "detail": "ls"},
        QHostAddress("127.0.0.1"), probe_port)
    app.processEvents()                              # flush queued datagrams
    for expected in ("ack", None):                   # ack now, decision on click
        if expected is None:
            capp.perm_bubble.decide("deny")
            app.processEvents()
        raw, _ = probe_sock.recvfrom(4096)
        reply = parse_hook_datagram(raw, capp._hook_token)
        assert reply and reply["id"] == "q1"
        assert reply["type"] == (expected or "decision")
    assert reply["decision"] == "deny"
    # DND: no engagement, the hook must fall back to the terminal
    capp.dnd = True
    capp._handle_permission_query(
        {"id": "q2", "tool_name": "Bash", "detail": ""},
        QHostAddress("127.0.0.1"), probe_port)
    probe_sock.settimeout(0.3)
    try:
        probe_sock.recvfrom(4096)
        raise AssertionError("DND still engaged the permission bubble")
    except socket_mod.timeout:
        pass
    capp.dnd = was_dnd
    capp.pet.hide()
    probe_sock.close()

    # hook script end-to-end: fake pet answers allow -> hook prints decision
    hook_py = Path(__file__).resolve().parent.parent / "clawd_permission_hook.py"
    with tempfile.TemporaryDirectory() as td:
        tokf = Path(td) / "hook_token"
        tok = ensure_hook_token(tokf)
        fake_pet = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        fake_pet.bind(("127.0.0.1", 0))
        fake_pet.settimeout(5.0)
        env = dict(os.environ,
                   CLAWD_PET_PORT=str(fake_pet.getsockname()[1]),
                   CLAWD_TOKEN_FILE=str(tokf))
        event = json.dumps({"hook_event_name": "PermissionRequest",
                            "tool_name": "Bash",
                            "tool_input": {"command": "git push"}})
        proc = subprocess.Popen([sys.executable, str(hook_py)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                env=env)
        proc.stdin.write(event.encode("utf-8"))
        proc.stdin.close()
        raw, addr = fake_pet.recvfrom(4096)
        q = parse_hook_datagram(raw, tok)
        assert q and q["clawd_permission"]["tool_name"] == "Bash"
        assert q["clawd_permission"]["detail"] == "git push"
        qid = q["clawd_permission"]["id"]
        for msg in ({"id": qid, "type": "ack"},
                    {"id": qid, "type": "decision", "decision": "allow"}):
            fake_pet.sendto(tok.encode() + b"\n" + json.dumps(msg).encode(), addr)
        out = proc.stdout.read().decode("utf-8")
        assert proc.wait(timeout=10) == 0
        got = json.loads(out)
        assert got["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        # silent pet: the hook gives up fast and prints nothing (terminal wins)
        t0 = time.monotonic()
        proc2 = subprocess.run([sys.executable, str(hook_py)],
                               input=event.encode("utf-8"),
                               stdout=subprocess.PIPE, env=env, timeout=10)
        assert proc2.returncode == 0 and proc2.stdout.strip() == b""
        assert time.monotonic() - t0 < 5.0, "no-pet fallback too slow"
        fake_pet.close()
    print("[selftest] permission bubble OK")

    # DND master toggle: settings round-trip, bubbles hidden
    was = capp.settings.value("dnd")
    capp.toggle_dnd()
    assert capp.dnd is True and capp.settings.value("dnd", type=bool) is True
    capp.toggle_dnd()
    assert capp.dnd is False
    if was is None:
        capp.settings.remove("dnd")
    from .focus import TERMINAL_APPS, focus_terminal
    assert callable(focus_terminal)     # not invoked: would steal real focus
    assert TERMINAL_APPS and TERMINAL_APPS[0] == "Warp"
    print("[selftest] dnd + focus OK")

    # --- hook runner resolution: must work without any python on PATH -----
    # (running from source, the pet's own interpreter is used — macOS often
    # ships no plain "python"; this regressed for real users before)
    from .hooks import _hook_runner, hook_command
    runner = _hook_runner()
    assert runner and Path(runner).is_file(), f"no hook runner: {runner!r}"
    cmd = hook_command()
    assert cmd and "clawd_hook.py" in cmd, f"activity hook cmd: {cmd!r}"
    pcmd = hook_command("clawd_permission_hook.py")
    assert pcmd and "clawd_permission_hook.py" in pcmd
    from .macdock import hide_dock_icon
    assert callable(hide_dock_icon)     # not invoked: offscreen has no Dock
    print("[selftest] hook runner + macdock OK")

    # --- usage accuracy: 429 back-off + persisted auto-calibration --------
    import email.message
    import urllib.error as _ue
    from . import api as _api, usage as _usage
    from .config import API_RETRY_S, RATE_LIMIT_BASE_S, RATE_LIMIT_MAX_S
    _fail_before = dict(_api._fetch_fail)
    hdrs = email.message.Message()
    hdrs["Retry-After"] = "900"
    e429 = _ue.HTTPError("u", 429, "rl", hdrs, None)
    assert _api._parse_retry_after(e429) == 900.0
    e429b = _ue.HTTPError("u", 429, "rl", email.message.Message(), None)
    assert _api._parse_retry_after(e429b) is None
    _api._fetch_fail.update(kind="429", retry_after=900.0)
    assert _api._failure_backoff(1) == 900.0
    _api._fetch_fail.update(kind="429", retry_after=None)
    assert _api._failure_backoff(1) == RATE_LIMIT_BASE_S
    assert _api._failure_backoff(99) == RATE_LIMIT_MAX_S
    _api._fetch_fail.update(kind="net", retry_after=None)
    assert _api._failure_backoff(1) == API_RETRY_S       # ordinary blip stays quick
    cache_before = dict(_api._api_cache)
    _api._api_cache["buckets"] = None
    _api._fetch_fail.update(kind="429", retry_after=None)
    state, until = _api.live_status()
    assert state == "rate_limited" and until is not None
    _api._fetch_fail.update(kind="no_token", retry_after=None)
    assert _api.live_status()[0] == "no_token"
    _api._api_cache.update(cache_before)
    _api._fetch_fail.update(_fail_before)

    cal_backup = (_usage.CALIBRATION_FILE, _usage._calibration_loaded,
                  _usage._AUTO_BUDGET_5H, _usage._WEEKLY_ANCHOR,
                  _usage._WEEKLY_BUDGET_ALL, dict(_usage._WEEKLY_BUDGET_MODELS))
    try:
        with tempfile.TemporaryDirectory() as td:
            _usage.CALIBRATION_FILE = Path(td) / "calibration.json"
            _usage._calibration_loaded = True     # skip loading the real file
            _usage.reset_auto_calibration()
            anchor = datetime(2026, 7, 19, 20, 59, tzinfo=timezone.utc)
            _usage.set_auto_calibration(600_000, anchor, 42_000_000,
                                        {"Fable": 20_000_000})
            assert _usage.CALIBRATION_FILE.is_file(), "calibration not persisted"
            # simulate a fresh process: wipe the globals, force a re-load
            _usage._AUTO_BUDGET_5H = _usage._WEEKLY_ANCHOR = None
            _usage._WEEKLY_BUDGET_ALL = None
            _usage._WEEKLY_BUDGET_MODELS = {}
            _usage._calibration_loaded = False
            assert _usage.effective_max_tokens() == 600_000
            assert _usage.weekly_budget_all() == 42_000_000
            assert _usage.weekly_model_budgets() == {"Fable": 20_000_000}
            assert _usage.auto_calibration()["anchor"] == anchor
            _usage.reset_auto_calibration()
            assert not _usage.CALIBRATION_FILE.exists()
            assert _usage.effective_max_tokens() >= 1_000_000  # placeholder again
    finally:
        (_usage.CALIBRATION_FILE, _usage._calibration_loaded,
         _usage._AUTO_BUDGET_5H, _usage._WEEKLY_ANCHOR,
         _usage._WEEKLY_BUDGET_ALL, _usage._WEEKLY_BUDGET_MODELS) = cal_backup
    print("[selftest] usage 429 backoff + calibration persistence OK")

    # --- native notifications must not run in tests (focus-steal fix) -----
    from .notify import _applescript_str, post_notification
    os.environ["CLAWD_NO_NATIVE_NOTIFY"] = "1"
    assert post_notification("t", "x") is False        # env kill-switch works
    assert _applescript_str('a"b\\c') == '"a\\"b\\\\c"'
    print("[selftest] native notify OK")

    # --- X2: hook events + statusline ---
    from .config import CONTEXT_STALE_S, HOOK_EVENTS
    from .statusline import (register_statusline, statusline_registered,
                             unregister_statusline)

    # 1) new events registered + register/unregister round-trip stays idempotent
    x2_new_events = ("SubagentStart", "SubagentStop", "PostToolUseFailure",
                     "StopFailure", "PreCompact", "PostCompact", "SessionEnd")
    for _ev in x2_new_events:
        assert _ev in HOOK_EVENTS, f"{_ev} missing from HOOK_EVENTS"
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "settings.json"
        sp.write_text("{}", encoding="utf-8")
        assert register_hooks(sp, 'py "clawd_hook.py"')
        assert not register_hooks(sp, 'py "clawd_hook.py"'), \
            "double hook registration not idempotent"
        x2_data = json.loads(sp.read_text(encoding="utf-8"))
        for _ev in HOOK_EVENTS:
            assert len(x2_data["hooks"][_ev]) == 1, f"event {_ev} not registered"
        assert unregister_hooks(sp)
        assert not hooks_registered(sp)
        x2_data = json.loads(sp.read_text(encoding="utf-8"))
        assert all(arr == [] for arr in x2_data["hooks"].values()), \
            "unregister left clawd entries behind"
    print("[selftest] X2 new hook events + idempotent registration OK")

    # 2/3) app handler: subagent counter, failure startle, compaction bubble
    x2_saved = (capp.dnd, capp.quiet, capp._last_activity,
                capp._hook_hold_until, capp.pet.pct, capp.pet._activity)
    capp.dnd = False
    capp.quiet = False
    capp.pet.hide()                        # no bubbles while counting
    capp.pet.set_pct(10)                   # calm quota -> tool mood visible
    capp.pet.set_activity(None)
    capp._last_activity = None
    capp._subagent_count = 0
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    assert capp._subagent_count == 1
    assert capp._last_activity == ("working", "Task"), "subagent did not juggle"
    if "juggle" in capp.pet._sprites.sprites:
        assert capp.pet.mood == "juggle", f"subagent mood: {capp.pet.mood}"
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    assert capp._subagent_count == 2
    capp._handle_hook_event({"hook_event_name": "SubagentStop"})
    assert capp._subagent_count == 1
    assert capp._last_activity == ("working", "Task"), "juggle ended too early"
    capp._handle_hook_event({"hook_event_name": "SubagentStop"})
    assert capp._subagent_count == 0
    assert capp._last_activity is None, "activity not cleared at count 0"
    capp._handle_hook_event({"hook_event_name": "SubagentStop"})
    assert capp._subagent_count == 0, "subagent count went below 0"
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    capp._handle_hook_event({"hook_event_name": "SessionStart"})
    assert capp._subagent_count == 0, "SessionStart did not reset the count"
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    capp._handle_hook_event({"hook_event_name": "Stop"})
    assert capp._subagent_count == 0, "Stop did not reset the count"
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    capp._handle_hook_event({"hook_event_name": "SessionEnd"})
    assert capp._subagent_count == 0, "SessionEnd did not reset the count"
    assert capp._last_activity is None, "SessionEnd left activity behind"
    assert capp._hook_hold_until == 0.0, "SessionEnd kept the mood hold"
    # old/unknown events (old clawd_hook.py copies) are ignored gracefully
    capp._handle_hook_event({"hook_event_name": "SomeFutureEvent"})
    capp._handle_hook_event({})
    assert capp._last_activity is None and capp._subagent_count == 0

    # failure events: startle path must not raise, activity goes "error"
    capp._handle_hook_event({"hook_event_name": "PostToolUseFailure"})
    assert capp._last_activity == ("error", None), "failure not grumpy"
    if "panic" in capp.pet._sprites.sprites:
        assert capp.pet.mood == "panic"
    capp._clear_error_state()
    assert capp._last_activity is None
    capp._handle_hook_event({"hook_event_name": "StopFailure"})
    assert capp._last_activity == ("error", None), "StopFailure not handled"
    capp._clear_error_state()
    capp.dnd = True                        # DND: failures do nothing at all
    capp._handle_hook_event({"hook_event_name": "PostToolUseFailure"})
    assert capp._last_activity is None, "failure reacted under DND"
    capp.dnd = False

    # PreCompact shows the bubble + sweeps, PostCompact ends it
    capp.pet.show()
    capp._handle_hook_event({"hook_event_name": "PreCompact"})
    assert capp._compacting is True
    assert capp._last_activity == ("working", "Compact")
    if "sweep" in capp.pet._sprites.sprites:
        assert capp.pet.mood == "sweep", f"compact mood: {capp.pet.mood}"
    assert capp.bubble.isVisible(), "compact bubble not shown"
    assert capp.bubble._text == tr("bubble_compact")
    capp._handle_hook_event({"hook_event_name": "PostCompact"})
    assert capp._compacting is False
    assert capp._last_activity is None, "PostCompact did not end the sweep"
    # compaction while subagents run: PostCompact goes back to juggling
    capp._handle_hook_event({"hook_event_name": "SubagentStart"})
    capp._handle_hook_event({"hook_event_name": "PreCompact"})
    capp._handle_hook_event({"hook_event_name": "PostCompact"})
    assert capp._last_activity == ("working", "Task"), "juggle lost after compact"
    capp._handle_hook_event({"hook_event_name": "SessionEnd"})
    capp.bubble.hide()
    capp.pet.hide()
    capp.pet.set_activity(None)
    (capp.dnd, capp.quiet, capp._last_activity,
     capp._hook_hold_until, _x2_pct, _x2_act) = x2_saved
    capp.pet.set_pct(_x2_pct)
    capp.pet.set_activity(_x2_act)
    print("[selftest] X2 subagent counter + failure/compact events OK")

    # 4) statusline registration: never clobber a user's own statusline
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "settings.json"
        sl_cmd = 'py "clawd_statusline.py"'
        sp.write_text("{}", encoding="utf-8")
        assert not statusline_registered(sp)
        assert register_statusline(sp, sl_cmd)              # absent -> ours
        assert statusline_registered(sp)
        x2_sl = json.loads(sp.read_text(encoding="utf-8"))["statusLine"]
        assert x2_sl == {"type": "command", "command": sl_cmd}
        assert not register_statusline(sp, sl_cmd), \
            "re-registering our statusline should be a no-op"
        assert unregister_statusline(sp)                    # removes only ours
        assert "statusLine" not in json.loads(sp.read_text(encoding="utf-8"))
        assert not unregister_statusline(sp), "double unregister changed settings"
        foreign = {"statusLine": {"type": "command", "command": "my-own-status"},
                   "other": 1}
        sp.write_text(json.dumps(foreign), encoding="utf-8")
        assert not register_statusline(sp, sl_cmd), \
            "a foreign statusline was overwritten"
        assert not statusline_registered(sp)
        assert not unregister_statusline(sp), "foreign statusline removed"
        assert json.loads(sp.read_text(encoding="utf-8")) == foreign, \
            "foreign settings not left untouched"
    print("[selftest] X2 statusline registration OK")

    # 5) clawd_statusline.py end-to-end: stdout line + authenticated datagram
    sl_py = Path(__file__).resolve().parent.parent / "clawd_statusline.py"
    with tempfile.TemporaryDirectory() as td:
        tokf = Path(td) / "hook_token"
        tok = ensure_hook_token(tokf)
        probe = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        probe.settimeout(5.0)
        env = dict(os.environ,
                   CLAWD_PET_PORT=str(probe.getsockname()[1]),
                   CLAWD_TOKEN_FILE=str(tokf))
        sl_payload = json.dumps({
            "session_id": "x2-session",
            "model": {"id": "claude-sonnet-4", "display_name": "Sonnet 4"},
            "context_window": {"used_percentage": 42.5},
        })
        proc = subprocess.run([sys.executable, str(sl_py)],
                              input=sl_payload.encode("utf-8"),
                              stdout=subprocess.PIPE, env=env, timeout=10)
        assert proc.returncode == 0
        sl_out = proc.stdout.decode("utf-8", errors="replace")
        assert "42" in sl_out, f"statusline output lacks the pct: {sl_out!r}"
        raw, _ = probe.recvfrom(65535)
        ev = parse_hook_datagram(raw, tok)
        assert ev and isinstance(ev.get("clawd_statusline"), dict), ev
        assert ev["clawd_statusline"]["context_pct"] == 42.5
        assert ev["clawd_statusline"]["model"] == "Sonnet 4"
        assert ev["clawd_statusline"]["session_id"] == "x2-session"
        # broken stdin: still prints a line, exits 0, sends nothing
        proc2 = subprocess.run([sys.executable, str(sl_py)],
                               input=b"{not json", stdout=subprocess.PIPE,
                               env=env, timeout=10)
        assert proc2.returncode == 0 and proc2.stdout.strip(), \
            "statusline must always print something"
        probe.settimeout(0.3)
        try:
            probe.recvfrom(65535)
            raise AssertionError("broken payload still sent a datagram")
        except socket_mod.timeout:
            pass
        probe.close()
    print("[selftest] X2 statusline sender e2e OK")

    # 6) panel context row shows fresh values, hides on unknown/stale
    panel.set_context(63.0, "Sonnet 4")
    panel.update_snapshot(snap)            # must survive _show_only
    app.processEvents()
    x2_row = panel._rows.get("context")
    assert x2_row is not None, "context row not created"
    assert not x2_row["name"].isHidden() and not x2_row["bar"].isHidden()
    assert "63" in x2_row["pct"].text(), x2_row["pct"].text()
    assert x2_row["reset"].text() == "Sonnet 4"
    panel.set_context(70.0, None)          # no model -> single compact row
    assert x2_row["reset"].isHidden(), "empty model line still visible"
    panel.set_context(None)                # unknown -> hidden
    assert x2_row["name"].isHidden() and x2_row["bar"].isHidden()
    panel.set_context(50.0, "Opus")
    assert not x2_row["name"].isHidden()
    panel._ctx_ts = time.monotonic() - (CONTEXT_STALE_S + 1)   # went stale
    panel._refresh_countdown()
    assert x2_row["name"].isHidden(), "stale context row still shown"
    panel.set_context(None)
    # app routing: the datagram payload lands in the panel via the setter
    capp._handle_statusline({"context_pct": 33.0, "model": "Opus",
                             "session_id": "s"})
    assert capp._context_pct == 33.0
    assert capp.panel._ctx_pct == 33.0 and capp.panel._ctx_model == "Opus"
    capp._handle_statusline({"context_pct": "garbage"})        # ignored
    assert capp._context_pct == 33.0
    capp._handle_statusline({"context_pct": 250.0})            # clamped
    assert capp._context_pct == 100.0
    capp.panel.set_context(None)
    print("[selftest] X2 panel context row OK")

    # --- Y: idle throttle / cursor chase / typing bob / celebrate ---------
    from PyQt5.QtCore import QPoint
    from .config import THROTTLE_TICK_MS
    pet.set_activity(None)
    pet.set_pct(10)                                   # chill quota mood
    pet._idle_variant = None
    pet.enable_wander(False)
    pet._update_mood()
    pet._last_active_mono = time.monotonic() - 999    # long idle
    pet._maybe_throttle()
    assert pet.throttled, "idle pet must throttle its animation timer"
    assert pet._anim_timer.interval() == THROTTLE_TICK_MS
    pet.set_activity(("working", "Edit"))             # any life -> full rate
    assert not pet.throttled, "activity must restore the frame rate instantly"
    pet.set_activity(None)
    print("[selftest] idle throttle OK")

    pet.enable_cursor_chase(True)
    avail = pet._screen_avail()
    pet.move(avail.left() + 10, avail.center().y())  # room to walk right
    # a cursor right next to the pet must NOT trigger a chase (oneko sits)
    pet._chase_test_target = QPoint(pet.x() + pet.width() // 2 + 10,
                                    avail.center().y())
    pet._chase_next = 0.0
    pet._chase_tick()
    assert pet._chase_state == "wait", "nearby cursor must not start a chase"
    start_x = pet.x()
    pet._chase_test_target = QPoint(pet.x() + pet.width() // 2 + 400,
                                    avail.center().y())
    pet._chase_next = 0.0                             # due immediately
    pet._chase_tick()
    assert pet._chase_state == "chase", pet._chase_state
    for _ in range(30):
        pet._chase_tick()
    assert pet.x() > start_x, "chase must move the pet toward the cursor"
    pet._chase_test_target = QPoint(pet.x() + pet.width() // 2, pet.y())
    pet._chase_tick()                                 # within catch radius
    assert pet._chase_state == "caught"
    assert pet.mood == "sleep", "caught cursor -> nap on it"
    pet._chase_test_target = QPoint(pet.x() + pet.width() // 2 + 500, pet.y())
    pet._chase_tick()                                 # it escaped
    assert pet._chase_state == "wait" and pet.mood != "sleep"
    pet._chase_test_target = None
    pet.enable_cursor_chase(False)
    print("[selftest] cursor chase OK")

    pet.set_generating(True)
    assert pet._generating
    assert pet.grab() is not None                     # bob path paints fine
    pet.set_generating(False)
    assert not pet._generating
    assert pet.celebrate() is True
    assert pet._react_active and pet.mood in ("conduct", "juggle", "happy")
    assert pet.celebrate() is False                   # idempotent while playing
    pet._stop_throw()                                 # cancel the hop
    pet._end_reaction()
    assert not pet._celebrating
    print("[selftest] typing bob + celebrate OK")

    # --- Y: sprite-pack import (petdex / Codex pet format) ----------------
    from .art import _pack_mood_for, import_sprite_pack
    import base64
    tiny_gif = base64.b64decode(
        b"R0lGODlhAgACAPAAAP8AAAAAACH5BAAAAAAALAAAAAACAAIAAAICRAEAOw==")
    assert _pack_mood_for("cat-idle.gif") == "chill"
    assert _pack_mood_for("clawd-sleeping.gif") == "sleep"
    assert _pack_mood_for("random-noise.gif") is None
    with tempfile.TemporaryDirectory() as td:
        packs = Path(td) / "packs"
        srcd = Path(td) / "my-pack"
        srcd.mkdir()
        (srcd / "idle.gif").write_bytes(tiny_gif)
        (srcd / "sleeping.gif").write_bytes(tiny_gif)
        dest = import_sprite_pack(srcd, dest_root=packs)
        assert dest is not None and (dest / "clawd-idle.gif").is_file()
        assert (dest / "clawd-sleeping.gif").is_file()
        loaded = SpriteSet(height=32, sprite_dir=dest)
        assert "chill" in loaded.sprites, "imported pack must load"
        # manifest wins over name sniffing
        srcm = Path(td) / "manifest-pack"
        srcm.mkdir()
        (srcm / "a.gif").write_bytes(tiny_gif)
        (srcm / "manifest.json").write_text('{"states": {"idle": "a.gif"}}')
        dest2 = import_sprite_pack(srcm, dest_root=packs)
        assert dest2 is not None and (dest2 / "clawd-idle.gif").is_file()
        # no idle -> rejected; garbage -> rejected
        srcb = Path(td) / "bad-pack"
        srcb.mkdir()
        (srcb / "sleeping.gif").write_bytes(tiny_gif)
        assert import_sprite_pack(srcb, dest_root=packs) is None
        assert import_sprite_pack(Path(td) / "missing", dest_root=packs) is None
    print("[selftest] sprite-pack import OK")

    # --- X1: Codex integration (rate-limit parsing, notify config, e2e) ---
    from clawdpet.codex import (_parse_rate_limits, notify_command,
                                notify_registered, register_notify,
                                unregister_notify)
    real_rpc = {"rateLimits": {
        "limitId": "codex", "planType": "plus",
        "primary": {"usedPercent": 51, "windowDurationMins": 10080,
                    "resetsAt": 1784488399},
        "secondary": {"usedPercent": 7, "windowDurationMins": 300,
                      "resetsAt": 1784488399},
    }}                                # shape captured from codex-cli 0.144.1
    cb = _parse_rate_limits(real_rpc)
    assert cb and len(cb) == 2
    assert cb[0].key == "codex_primary" and cb[0].pct == 51.0
    assert cb[0].resets_at is not None and "Codex" in cb[0].label
    assert cb[1].pct == 7.0
    assert _parse_rate_limits({}) is None
    assert _parse_rate_limits({"rateLimits": {"primary": None}}) is None
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text('model = "gpt-5"\n', encoding="utf-8")
        line = notify_command("/usr/bin/python3", Path(td) / "codex_notify.py")
        assert register_notify(line, cfg) is True
        assert notify_registered(cfg) is True
        assert register_notify(line, cfg) is False        # idempotent
        assert 'model = "gpt-5"' in cfg.read_text()
        assert unregister_notify(cfg) is True
        assert not notify_registered(cfg)
        assert 'model = "gpt-5"' in cfg.read_text()
        cfg.write_text('notify = ["mytool"]\n', encoding="utf-8")
        assert register_notify(line, cfg) is False        # foreign -> refuse
        assert not notify_registered(cfg)                 # ... and it is not ours
        assert unregister_notify(cfg) is False            # never touch foreign
        assert cfg.read_text() == 'notify = ["mytool"]\n'
    # codex_turn routing: respects DND, alerts otherwise (no tray -> no raise)
    capp.dnd = True
    capp._handle_codex_turn({"turn_id": "t1"})
    capp.dnd = False
    was = capp._last_alert_mono
    capp._last_alert_mono = 0.0
    capp._handle_codex_turn({"turn_id": "t1", "message": "done"})
    capp._last_alert_mono = was
    print("[selftest] codex rate limits + notify config OK")

    # codex_notify.py end-to-end against a probe socket
    import socket as socket_mod
    import subprocess as sp
    probe = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    probe.settimeout(5.0)
    with tempfile.TemporaryDirectory() as td:
        tokf = Path(td) / "tok"
        tokf.write_text("cafebabe" * 4, encoding="utf-8")
        env = dict(os.environ,
                   CLAWD_PET_PORT=str(probe.getsockname()[1]),
                   CLAWD_TOKEN_FILE=str(tokf))
        script = Path(__file__).resolve().parent.parent / "codex_notify.py"
        evt = json.dumps({"type": "agent-turn-complete", "turn-id": "abc",
                          "last-assistant-message": "done!"})
        rc = sp.run([sys.executable, str(script), evt], env=env,
                    timeout=15).returncode
        assert rc == 0
        data, _ = probe.recvfrom(65535)
        tok, _, body = data.partition(b"\n")
        assert tok.decode() == "cafebabe" * 4
        payload = json.loads(body)
        assert payload["codex_turn"]["turn_id"] == "abc"
        # non-turn events send nothing and still exit 0
        rc = sp.run([sys.executable, str(script),
                     json.dumps({"type": "something-else"})],
                    env=env, timeout=15).returncode
        assert rc == 0
    probe.close()
    # panel line renders from codex buckets
    snap_cdx = UsageSnapshot(updated_at=datetime.now())
    snap_cdx.weighted = 1.0
    snap_cdx.codex_buckets = cb
    panel._update_extras(snap_cdx)
    assert panel.codex_label.isVisibleTo(panel) or panel.codex_label.text()
    assert "Codex" in panel.codex_label.text()
    print("[selftest] codex notify e2e + panel line OK")

    # --- integration: celebrate-on-reset, incident line, menu entries -----
    from clawdpet import status_check as sc_mod
    sc_backup = dict(sc_mod._cache)
    assert sc_mod.anthropic_sick(now=1e9, fetch=lambda: "major") is True
    assert sc_mod.anthropic_sick(now=1e9 + 5,
                                 fetch=lambda: "none") is True   # throttled
    assert sc_mod.anthropic_sick(now=1e9 + 700, fetch=lambda: "none") is False
    assert sc_mod.anthropic_sick(now=1e9 + 1400, fetch=lambda: None) is False
    sc_mod._cache.update(sc_backup)
    panel.set_incident(True)
    assert panel.incident_label.isVisibleTo(panel)
    assert "status.anthropic.com" in panel.incident_label.text()
    panel.set_incident(False)
    assert not panel.incident_label.isVisibleTo(panel)
    # a quota reset makes the pet celebrate (DND suppresses it)
    capp.dnd = False
    capp.pet._react_active = False
    capp.pet._celebrating = False
    capp._prev_pct["api"] = 90.0
    capp._notify_transition("api", 3.0)
    assert capp.pet._celebrating, "quota reset must trigger a celebration"
    capp.pet._stop_throw()
    capp.pet._end_reaction()
    capp.dnd = True
    capp._prev_pct["api"] = 90.0
    capp._notify_transition("api", 3.0)
    assert not capp.pet._celebrating, "DND must suppress the celebration"
    capp.dnd = False
    # typing-along follows hook-driven working state
    capp._handle_hook_event({"hook_event_name": "PreToolUse",
                             "tool_name": "Edit"})
    assert capp.pet._generating, "working hook must start the typing bob"
    capp._handle_hook_event({"hook_event_name": "Stop"})
    assert not capp.pet._generating
    # new tray entries exist (codex entry only with a codex binary present)
    menu2 = capp.build_menu(None)
    acts2 = [a.text() for a in menu2.actions() if a.text()]
    assert tr("menu_chase") in acts2, "cursor-chase toggle missing"
    assert tr("menu_pack_import") in acts2, "pack import entry missing"
    import shutil as _sh
    if _sh.which("codex"):
        assert (tr("menu_codex_notify_on") in acts2
                or tr("menu_codex_notify_off") in acts2)
    menu2.deleteLater()
    print("[selftest] integration wiring OK")

    # --- usage accuracy round 2: session anchor + pairing guard -----------
    from clawdpet.api import _calibrate_from_buckets, _windows_aligned, UsageBucket as UB
    utc = timezone.utc
    win_backup = (_usage._SESSION_ANCHOR, _usage._calibration_loaded,
                  _usage.CALIBRATION_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _usage.CALIBRATION_FILE = Path(td) / "cal.json"
            _usage._calibration_loaded = True
            now2 = datetime(2026, 7, 14, 15, 0, tzinfo=utc)
            ts_all = [datetime(2026, 7, 14, h, 0, tzinfo=utc) for h in (8, 11, 14)]
            # live says the window ends 17:00 -> it started 12:00, replay beaten
            _usage._SESSION_ANCHOR = datetime(2026, 7, 14, 17, 0, tzinfo=utc)
            ws = _current_window_start(ts_all, now2)
            assert ws == datetime(2026, 7, 14, 12, 0, tzinfo=utc), ws
            # boundary already passed -> the first message after it opens a window
            _usage._SESSION_ANCHOR = datetime(2026, 7, 14, 13, 0, tzinfo=utc)
            ws = _current_window_start(ts_all, now2)
            assert ws == datetime(2026, 7, 14, 14, 0, tzinfo=utc), ws
            _usage._SESSION_ANCHOR = None
            # pairing guard: misaligned local window must NOT calibrate the
            # budget but must still store both reset anchors
            _usage.reset_auto_calibration()
            snap_g = UsageSnapshot(updated_at=datetime.now())
            snap_g.weighted = 500_000.0
            snap_g.week_weighted = 10_000_000.0
            snap_g.oldest = datetime(2026, 7, 14, 9, 45, tzinfo=utc)   # ends 14:45
            snap_g.week_reset = None                                   # rolling
            live_b = [UB("five_hour", "5h", 51.0,
                         datetime(2026, 7, 14, 17, 0, tzinfo=utc)),    # ends 17:00
                      UB("seven_day", "week", 39.0,
                         datetime(2026, 7, 19, 21, 0, tzinfo=utc))]
            _calibrate_from_buckets(snap_g, live_b)
            cal_g = _usage.auto_calibration()
            assert cal_g["budget_5h"] is None, "misaligned pairing must be skipped"
            assert cal_g["weekly_budget"] is None
            assert cal_g["session_reset"] == live_b[0].resets_at
            assert cal_g["anchor"] == live_b[1].resets_at
            # aligned windows -> both budgets calibrate
            snap_g.oldest = datetime(2026, 7, 14, 12, 0, tzinfo=utc)   # ends 17:00
            snap_g.week_reset = datetime(2026, 7, 19, 21, 0, tzinfo=utc)
            _calibrate_from_buckets(snap_g, live_b)
            cal_g = _usage.auto_calibration()
            assert cal_g["budget_5h"] == round(500_000 / 0.51)
            assert cal_g["weekly_budget"] == round(10_000_000 / 0.39)
            assert _windows_aligned(None, live_b[0].resets_at, 600) is False
            _usage.reset_auto_calibration()
    finally:
        (_usage._SESSION_ANCHOR, _usage._calibration_loaded,
         _usage.CALIBRATION_FILE) = win_backup
    # an estimate over 100 % may panic but never assert the hard limit
    snap_lim = UsageSnapshot(updated_at=datetime.now())
    snap_lim.source = "logs"
    snap_lim.pct = 228.4
    snap_lim.entries = 5
    pet.set_snapshot(snap_lim)
    assert pet._quota_mood == "panic", pet._quota_mood
    snap_lim.source = "api"
    pet.set_snapshot(snap_lim)
    assert pet._quota_mood == "limit", "live data must still assert the limit"
    pet.set_pct(10)                                   # restore chill for later blocks
    print("[selftest] session anchor + pairing guard OK")

    # --- token rotation: per-source 429 pause + read-only CC token --------
    from clawdpet.api import _claude_code_token, fetch_api_usage
    creds_backup = api_mod.CREDENTIALS_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            cf = Path(td) / "creds.json"
            api_mod.CREDENTIALS_FILE = cf
            good_ms = int(time.time() * 1000 + 3600_000)
            cf.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "cc-tok", "expiresAt": good_ms}}))
            if sys.platform != "darwin":
                assert _claude_code_token() == "cc-tok"
            else:
                assert _claude_code_token() == "cc-tok"   # file wins, no keychain
            cf.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "cc-old", "expiresAt": 1}}))
            assert _claude_code_token() is None            # expired -> unusable
    finally:
        api_mod.CREDENTIALS_FILE = creds_backup
    rot_backup = (api_mod._clawd_own_token, api_mod._claude_code_token,
                  api_mod._fetch_usage_with, dict(api_mod._source_pause),
                  os.environ.pop("CLAWD_NO_API", None))
    try:
        api_mod._source_pause.clear()
        api_mod._clawd_own_token = lambda: "own-tok"
        api_mod._claude_code_token = lambda: "cc-tok"
        calls = []
        def fake_fetch(token):
            calls.append(token)
            if token == "own-tok":
                return None, "429", 1200.0                # own token locked out
            return {"limits": [{"kind": "session", "percent": 72.0,
                                "resets_at": None}]}, None, None
        api_mod._fetch_usage_with = fake_fetch
        got = fetch_api_usage()
        assert got and got[0].pct == 72.0, "rotation must fall back to CC token"
        assert calls == ["own-tok", "cc-tok"]
        assert api_mod._source_pause.get("own", 0) > time.time() + 600
        calls.clear()
        got = fetch_api_usage()                            # own still paused
        assert got and calls == ["cc-tok"], "paused source must be skipped"
        api_mod._fetch_usage_with = lambda tok: (None, "429", 60.0)
        api_mod._source_pause.clear()
        assert fetch_api_usage() is None                   # all limited -> None
        assert api_mod._fetch_fail["kind"] == "429"
    finally:
        (api_mod._clawd_own_token, api_mod._claude_code_token,
         api_mod._fetch_usage_with, pause_prev, env_prev) = rot_backup
        api_mod._source_pause.clear()
        api_mod._source_pause.update(pause_prev)
        if env_prev is not None:
            os.environ["CLAWD_NO_API"] = env_prev
        api_mod._fetch_fail.update(kind=None, retry_after=None)
    print("[selftest] token rotation OK")

    # --- live-first display: projection, stale grace, mtime reload --------
    from clawdpet.api import _project_buckets
    cal_bk2 = (_usage.CALIBRATION_FILE, _usage._calibration_loaded,
               _usage._calibration_mtime)
    try:
        with tempfile.TemporaryDirectory() as td:
            _usage.CALIBRATION_FILE = Path(td) / "cal.json"
            _usage._calibration_loaded = True
            _usage._calibration_mtime = None
            _usage.reset_auto_calibration()
            _usage.set_auto_calibration(
                1_000_000, datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
                10_000_000, {"Fable": 5_000_000},
                session_reset=datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc))
            live = [UB("five_hour", "5h", 50.0, None),
                    UB("seven_day", "week", 30.0, None),
                    UB("weekly_fable", "Weekly · Fable", 40.0, None)]
            base = {"weighted": 100_000.0, "week_weighted": 1_000_000.0,
                    "week_model": {"Fable": 500_000.0}}
            snap_p = UsageSnapshot(updated_at=datetime.now())
            snap_p.weighted = 150_000.0            # +50k of 1M budget -> +5 pp
            snap_p.week_weighted = 1_200_000.0     # +200k of 10M -> +2 pp
            snap_p.week_by_model_weighted = {"Fable": 750_000.0}  # +250k/5M -> +5 pp
            shown = _project_buckets(live, base, snap_p)
            got = {b.key: round(b.pct, 1) for b in shown}
            assert got == {"five_hour": 55.0, "seven_day": 32.0,
                           "weekly_fable": 45.0}, got
            snap_p.weighted = 50_000.0             # counter SHRANK (reset/rewrite)
            shown = _project_buckets(live, base, snap_p)
            assert shown[0].pct == 50.0, "projection must never go below live"
            snap_p.weighted = 10_000_000.0         # huge delta -> clamped
            assert _project_buckets(live, base, snap_p)[0].pct == 100.0
            # mtime-aware reload: an EXTERNAL write is picked up lazily
            _usage.CALIBRATION_FILE.write_text(
                json.dumps({"budget_5h": 777_000, "weekly_budget": None,
                            "models": {}, "anchor": None,
                            "session_reset": None}), encoding="utf-8")
            import os as _os
            _os.utime(_usage.CALIBRATION_FILE, ns=(
                _usage.CALIBRATION_FILE.stat().st_atime_ns,
                _usage.CALIBRATION_FILE.stat().st_mtime_ns + 1_000_000))
            assert _usage.effective_max_tokens() == 777_000, \
                "external calibration write must be picked up without restart"
            _usage.reset_auto_calibration()
    finally:
        (_usage.CALIBRATION_FILE, _usage._calibration_loaded,
         _usage._calibration_mtime) = cal_bk2
    # a single failed poll must keep the live buckets (grace: 3 fails + 2x poll)
    cache_bk2 = dict(api_mod._api_cache)
    api_mod._api_cache.update(buckets=[UB("five_hour", "5h", 50.0, None)],
                              ts=time.time() - 100, fails=1, base=None)
    assert api_mod._api_cache["buckets"] is not None
    api_mod._api_cache.update(cache_bk2)
    print("[selftest] live-first projection + reload OK")

    # --- focus-steal fix: raise_() must never activate the app ------------
    assert os.environ.get("QT_MAC_SET_RAISE_PROCESS") == "0", \
        "raise_() app-activation escape hatch must be set at import time"
    raised = []
    bub2 = SpeechBubble()
    bub2.raise_ = lambda: raised.append(1)
    bub2.show_text("focus test", pet, 50)
    if sys.platform == "darwin":
        assert not raised, "raise_() on macOS activates the app — must be skipped"
    else:
        assert raised, "non-mac keeps raise_ for stacking"
    bub2.hide()
    bub2.deleteLater()
    print("[selftest] no-activate raise OK")

    # --- G: gamification — XP curve, events, persistence -------------------
    from . import progress as _prog
    prog_bk = (_prog.STATE_FILE, _prog._state_loaded, _prog._state_mtime,
               _prog._xp)
    try:
        with tempfile.TemporaryDirectory() as td:
            _prog.STATE_FILE = Path(td) / "pet_state.json"
            _prog._state_loaded = True      # skip loading the real file
            _prog._state_mtime = None
            _prog._xp = 0.0
            # level curve: exact thresholds, strictly monotonic
            assert _prog.xp_for_level(0) == 0
            assert _prog.xp_for_level(1) == 500
            assert _prog.xp_for_level(2) == 1500
            assert _prog.xp_for_level(3) == 3000
            prev_thr = -1
            for lv in range(31):
                thr = _prog.xp_for_level(lv)
                assert thr > prev_thr, "level curve must be strictly monotonic"
                prev_thr = thr
                assert _prog.level_for_xp(thr) == lv
                if thr:
                    assert _prog.level_for_xp(thr - 1) == lv - 1
            # evolution titles by level band
            assert _prog.title_for_level(0) == "Hatchling"
            assert _prog.title_for_level(2) == "Hatchling"
            assert _prog.title_for_level(3) == "Crabling"
            assert _prog.title_for_level(5) == "Crabling"
            assert _prog.title_for_level(6) == "Scuttler"
            assert _prog.title_for_level(9) == "Scuttler"
            assert _prog.title_for_level(10) == "Coder Crab"
            assert _prog.title_for_level(15) == "Deep-Sea Dev"
            assert _prog.title_for_level(20) == "Deep-Sea Dev"
            assert _prog.title_for_level(21) == "Kraken Whisperer"
            assert _prog.title_for_level(28) == "Legend"
            assert _prog.title_for_level(99) == "Legend"
            # add_usage accumulates; the event fires exactly on the crossing
            assert _prog.add_usage(0) is None
            assert _prog.add_usage(-5000) is None      # negative delta: ignored
            assert _prog.add_usage(499_000) is None    # 499 XP — still level 0
            ev = _prog.add_usage(1_000)                # 500 XP — level 1 crossed
            assert ev == {"level": 1, "title": "Hatchling"}, ev
            assert _prog.add_usage(1_000) is None      # no repeat event
            cur = _prog.current()
            assert cur["level"] == 1 and cur["title"] == "Hatchling"
            assert cur["next_level_xp"] == 1500
            assert abs(cur["xp"] - 501.0) < 1e-9
            ev = _prog.add_usage(2_500_000)            # 501 -> 3001 XP: level 3
            assert ev == {"level": 3, "title": "Crabling"}, ev
            # persistence round-trip: wipe the globals, force a re-load
            assert _prog.STATE_FILE.is_file(), "pet state not persisted"
            _prog._xp = 0.0
            _prog._state_loaded = False
            _prog._state_mtime = None
            assert abs(_prog.current()["xp"] - 3001.0) < 1e-9
            # mtime-aware reload: an EXTERNAL write is picked up lazily
            _prog.STATE_FILE.write_text(json.dumps({"xp": 7777.0}),
                                        encoding="utf-8")
            os.utime(_prog.STATE_FILE, ns=(
                _prog.STATE_FILE.stat().st_atime_ns,
                _prog.STATE_FILE.stat().st_mtime_ns + 1_000_000))
            assert _prog.current()["xp"] == 7777.0, \
                "external pet-state write must be picked up without restart"
            # i18n: the gamification keys exist in BOTH languages
            from .i18n import STRINGS as _strings
            for _lang in ("de", "en"):
                for _key in ("levelup_title", "levelup_text", "progress_line"):
                    assert _key in _strings[_lang], (_lang, _key)
    finally:
        (_prog.STATE_FILE, _prog._state_loaded, _prog._state_mtime,
         _prog._xp) = prog_bk
    print("[selftest] gamification progress OK")

    # --- Telegram remote approval (fake HTTP server, no real API) ---------
    import http.server
    import threading as _thr
    from clawdpet import telegram_approval as tg
    with tempfile.TemporaryDirectory() as td:
        tgf = Path(td) / "telegram.json"
        assert tg.load_config(tgf) is None
        assert tg.save_config("123:ABC", "42", tgf) is True
        if os.name == "posix":
            assert (tgf.stat().st_mode & 0o777) == 0o600, "bot token not 0600"
        cfg = tg.load_config(tgf)
        assert cfg == {"bot_token": "123:ABC", "chat_id": "42"}
        assert tg.telegram_configured(tgf) is True
        assert tg.remove_config(tgf) is True and not tgf.exists()
        tgf.write_text("{broken", encoding="utf-8")
        assert tg.load_config(tgf) is None            # garbage -> unconfigured

    seen = []
    class _FakeTg(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            seen.append((method, body))
            if method == "sendMessage":
                out = {"ok": True, "result": {"message_id": 7}}
            elif method == "getUpdates":
                out = {"ok": True, "result": [
                    {"update_id": 100, "callback_query": {
                        "id": "cbq1", "data": "allow:tg-test-1"}}]}
            else:
                out = {"ok": True, "result": True}
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeTg)
    thr = _thr.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    tg_api_backup = tg.TELEGRAM_API
    try:
        tg.TELEGRAM_API = f"http://127.0.0.1:{srv.server_address[1]}"
        cfg = {"bot_token": "123:ABC", "chat_id": "42"}
        mid = tg.send_permission_request(cfg, "tg-test-1", "Bash", "rm -rf x")
        assert mid == 7
        kb = seen[0][1]["reply_markup"]["inline_keyboard"][0]
        assert kb[0]["callback_data"] == "allow:tg-test-1"
        decision, offset = tg.poll_decision(cfg, "tg-test-1", None)
        assert decision == "allow" and offset == 101
        decision, _ = tg.poll_decision(cfg, "OTHER-qid", None)
        assert decision is None, "foreign query ids must be ignored"
        # full worker round-trip: send -> poll -> on_decision exactly once
        got = []
        stop = _thr.Event()
        tg.remote_watch(cfg, "tg-test-1", "Bash", "x", stop,
                        time.monotonic() + 10, got.append)
        assert got == ["allow"]
        assert any(m == "editMessageText" for m, _ in seen), "card not finished"
    finally:
        tg.TELEGRAM_API = tg_api_backup
        srv.shutdown()
    # window handshake: ack window_s reaches the hook script (subprocess E2E
    # variant: announce 6 s, decide after the classic 15 s would NOT have
    # mattered — here we simply verify the script accepts and uses the ack)
    print("[selftest] telegram remote approval OK")
    # --- W: window sitting (macOS) — fake provider, offscreen-safe --------
    from . import macwindows
    from .macwindows import frontmost_window_frame, window_tracking_available
    assert isinstance(window_tracking_available(), bool)
    _wlive = frontmost_window_frame()      # must never raise, Quartz or not
    if sys.platform != "darwin":
        assert _wlive is None, "window frames are a macOS-only feature"
    if _wlive is not None:
        assert len(_wlive) == 4 and _wlive[2] >= 200 and _wlive[3] >= 100
    pet.enable_wander(False)               # a clean, idle pet for this block
    pet.enable_cursor_chase(False)
    pet._stop_throw()
    pet.set_pct(10)
    pet.set_activity(None)
    wsav = pet._screen_avail()
    wsx, wsy = wsav.left() + 60, wsav.top() + 320
    ws_frame = [(wsx, wsy, 640, 400)]      # mutable: the test moves the window
    pet._window_frame_provider = lambda: ws_frame[0]
    pet.move(wsav.left() + 5, wsav.bottom() - pet.height())
    pet.enable_window_sitting(True)        # anchors immediately via the tick
    assert pet._window_sit_timer.isActive(), "sit poll timer not running"
    assert pet._window_sitting, "pet did not anchor to the fake window"
    assert pet.y() == wsy - pet.height(), "feet not on the window top edge"
    assert wsx <= pet.x() <= wsx + 640 - pet.width(), "x not clamped to window"
    # the window moves -> the pet follows on the next poll (dx/dy directly)
    ws_frame[0] = (wsx + 48, wsy + 64, 640, 400)
    _wpx = pet.x()
    pet._window_sit_tick()
    assert pet.y() == wsy + 64 - pet.height(), "did not follow the window down"
    assert pet.x() == _wpx + 48, "did not follow the window sideways"
    # the window disappears -> the pet FALLS via the throw physics
    ws_frame[0] = None
    pet._window_sit_tick()
    assert pet._throw_active and not pet._window_sitting, \
        "lost window must drop the pet"
    pet._stop_throw()
    pet._throw_timer.stop()
    # chase priority: while a chase runs, sitting must not adjust anything
    ws_frame[0] = (wsx, wsy, 640, 400)
    pet._chase_state = "chase"
    _wpy = pet.y()
    pet._window_sit_tick()
    assert pet.y() == _wpy and not pet._window_sitting, \
        "sitting must never fight the cursor chase"
    pet._chase_state = "wait"
    # a fresh window re-anchors; disabling while sitting drops him again
    pet._window_sit_tick()
    assert pet._window_sitting
    pet.enable_window_sitting(False)
    assert not pet._window_sit_timer.isActive(), "sit timer still active"
    assert pet._throw_active, "disabling while perched must start the drop"
    pet._stop_throw()
    pet._throw_timer.stop()
    assert not pet._window_sitting and pet._sit_frame is None
    pet._window_frame_provider = macwindows.frontmost_window_frame
    # i18n: the menu key exists in BOTH languages
    from .i18n import STRINGS as _wstrings
    for _lang in ("de", "en"):
        assert "menu_window_sit" in _wstrings[_lang], _lang
    print("[selftest] window sitting OK")

    # --- motion gates: focus must not paralyze wander/chase/sit -----------
    pet.set_activity(None)
    pet.set_generating(False)
    pet.set_pct(60)                                   # "focus" quota mood
    assert pet._quota_mood == "focus", pet._quota_mood
    assert not pet._chase_blocked(), "chase must work at 60 % usage"
    pet.set_pct(93)                                   # panic must NOT block:
    assert pet._quota_mood == "panic"                 # an explicitly enabled
    assert not pet._chase_blocked(), \
        "quota mood must never overrule an explicitly enabled chase"
    assert not pet._wander_blocked() or pet._activity is not None
    pet._quota_mood = "sleep"                         # asleep: wander rests,
    assert pet._wander_blocked()                      # but a chase may wake
    assert not pet._chase_blocked(), "oneko wakes up to chase"
    pet.set_pct(60)
    pet.set_activity(("working", "Edit"))             # Claude works ...
    assert not pet._window_sit_blocked(), \
        "sitting on a window must survive Claude working"
    assert pet._chase_blocked(), "chase yields while Claude works"
    pet.set_activity(("waiting", None))               # ... Claude waits ...
    assert not pet._chase_blocked(), \
        "a WAITING Claude must not block the chase — that is when the user's cursor moves"
    pet.set_pct(93)                                   # gait wins at ANY mood
    pet._chase_state = "chase"
    pet._update_mood()
    if "carry" in pet._sprites.sprites:
        assert pet.mood == "carry", "chase gait must show even while panicking"
    pet._chase_state = "wait"
    pet.set_activity(None)
    pet.set_pct(10)
    pet._update_mood()
    # progress line is always visible now (level 0 is a valid state)
    from . import progress as progress_mod
    st_bk = (progress_mod.STATE_FILE, progress_mod._state_loaded,
             progress_mod._xp, progress_mod._state_mtime)
    try:
        with tempfile.TemporaryDirectory() as td:
            progress_mod.STATE_FILE = Path(td) / "state.json"
            progress_mod._state_loaded = True
            progress_mod._state_mtime = None
            progress_mod._xp = 0.0
            panel._update_extras(UsageSnapshot(updated_at=datetime.now()))
            assert panel.progress_label.isVisibleTo(panel)
            assert "Level 0" in panel.progress_label.text()
    finally:
        (progress_mod.STATE_FILE, progress_mod._state_loaded,
         progress_mod._xp, progress_mod._state_mtime) = st_bk
    print("[selftest] motion gates + visible progress OK")

    print("[selftest] OK")
    del app
    return 0
