"""Application controller + entry point — wires pet, panel, tray, scanner."""
import json
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import (
    QLockFile,
    QPoint,
    QSettings,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import QCursor, QGuiApplication
from PyQt5.QtNetwork import QHostAddress, QUdpSocket
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from .activity import read_session_context
from .api import collect_usage
from .art import make_app_icon
from .autostart import autostart_enabled, autostart_supported, set_autostart
from .bubble import SpeechBubble
from .config import (
    ACTIVITY_POLL_MS,
    ALERT_COOLDOWN_S,
    APP_NAME,
    APP_VERSION,
    BURN_LOOKBACK_S,
    CLAUDE_SETTINGS_FILE,
    HOOK_UDP_PORT,
    ORG_NAME,
    RELEASES_URL,
    SCAN_INTERVAL_MS,
    UPDATE_CHECK,
    UPDATE_RECHECK_MS,
    WAIT_ALERT_MIN_S,
    WEIGHT_VERSION,
)
from .history import HistoryStore
from .hooks import (
    ensure_hook_token,
    hook_command,
    hooks_registered,
    parse_hook_datagram,
    refresh_hook_copy,
    register_hooks,
    unregister_hooks,
)
from .i18n import (
    fmt_de,
    fmt_pct_de,
    language,
    set_language,
    tool_action,
    tool_bubble,
    tr,
)
from .moods import mood_for_pct
from .panel import PanelWidget
from .pet import PetWidget
from .update import UpdateThread, is_trusted_update_url, version_is_newer
from .usage import (
    UsageSnapshot,
    _parse_iso_ts,
    auto_calibration,
    burn_eta,
    is_calibrated,
    notify_decision,
    reset_auto_calibration,
    set_auto_calibration,
    set_max_tokens_override,
)

class ScanThread(QThread):
    """Runs scan_usage() off the GUI thread."""
    result = pyqtSignal(object)

    def run(self):
        try:
            snap = collect_usage(should_stop=self.isInterruptionRequested)
        except Exception as exc:  # never let the worker die silently
            snap = UsageSnapshot(error=tr("err_scan", e=exc), updated_at=datetime.now())
        self.result.emit(snap)
# ======================================================================
#  Application controller — wires pet, panel, tray and scanner together
# ======================================================================

class ClawdApp:
    def __init__(self, app: QApplication, with_tray: bool = True):
        self.app = app
        self.settings = QSettings(ORG_NAME, APP_NAME)
        set_language(str(self.settings.value("language", "de") or "de"))
        self.snapshot = UsageSnapshot()

        if self.settings.value("weight_version", 0, type=int) != WEIGHT_VERSION:
            # the cost model changed, so any stored calibration is in the wrong
            # scale — drop it so the user recalibrates once cleanly instead of
            # seeing a confidently-wrong number
            for _k in ("max_tokens", "auto_budget_5h", "weekly_budget_all",
                       "weekly_anchor", "weekly_budget_models"):
                self.settings.remove(_k)
            reset_auto_calibration()
            self.settings.setValue("weight_version", WEIGHT_VERSION)

        saved = self.settings.value("max_tokens")
        try:
            if saved and int(saved) > 0:
                set_max_tokens_override(int(saved))
        except (TypeError, ValueError):
            self.settings.remove("max_tokens")

        # restore auto-calibration learned from previous live API syncs
        try:
            anchor_raw = self.settings.value("weekly_anchor", "") or ""
            set_auto_calibration(
                budget_5h=int(self.settings.value("auto_budget_5h", 0) or 0) or None,
                weekly_anchor=_parse_iso_ts(anchor_raw) if anchor_raw else None,
                weekly_budget=int(self.settings.value("weekly_budget_all", 0) or 0) or None,
                weekly_model_budgets=json.loads(
                    self.settings.value("weekly_budget_models", "") or "{}") or None,
            )
        except (TypeError, ValueError):
            pass

        self.pet = PetWidget(self)
        self.panel = PanelWidget()
        self.panel.on_leave = self.schedule_panel_hide
        self.bubble = SpeechBubble()

        # real-time activity: log watcher (Stufe 1) + hook receiver (Stufe 2)
        self.quiet = self.settings.value("quiet", False, type=bool)
        self.notify_enabled = self.settings.value("notify", True, type=bool)
        self.notify_sound = self.settings.value("notify_sound", False, type=bool)
        # burn-rate history and last pct are kept PER SOURCE ("api"/"logs"):
        # the two modes report on different absolute scales, so cross-comparing
        # them would fake resets — but a transient api->logs->api fallback (an
        # expired OAuth token, a network blip) must not wipe either history, or
        # the forecast and the threshold toasts would go dark for minutes.
        self._burn_samples = {}          # source -> list[(utc time, pct)]
        self._prev_pct = {}              # source -> last pct seen
        self.history = HistoryStore()
        self.check_updates = self.settings.value(
            "check_updates", UPDATE_CHECK, type=bool)
        self._update_url = ""
        self._update_tag = ""
        self._update_thread: Optional[UpdateThread] = None
        self._last_toast_was_update = False   # gate messageClicked to the update toast
        self._newest_log: Optional[Path] = None
        self._last_activity = None
        self._session_ctx = None
        self._work_kind = None            # last log-derived activity kind
        self._work_started_mono = None    # start of the current working phase
        self._work_log = None             # which session log that phase belongs to
        self._last_alert_mono = 0.0       # rate-limit "your turn" alerts
        self._hook_hold_until = 0.0
        self._activity_timer = QTimer()
        self._activity_timer.setInterval(ACTIVITY_POLL_MS)
        self._activity_timer.timeout.connect(self._check_activity)
        self._hook_token = ensure_hook_token()   # authenticates hook datagrams
        self._udp = QUdpSocket()
        if self._udp.bind(QHostAddress.LocalHost, HOOK_UDP_PORT):
            self._udp.readyRead.connect(self._read_hook_datagrams)

        self._scan_thread: Optional[ScanThread] = None
        self._scan_timer = QTimer()
        self._scan_timer.setInterval(SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self.refresh)

        self._update_timer = QTimer()
        self._update_timer.setInterval(UPDATE_RECHECK_MS)
        self._update_timer.timeout.connect(self._begin_update_check)

        self._hide_check = QTimer()
        self._hide_check.setSingleShot(True)
        self._hide_check.setInterval(400)
        self._hide_check.timeout.connect(self._maybe_hide_panel)

        self.tray: Optional[QSystemTrayIcon] = None
        self._tray_menu: Optional[QMenu] = None
        if with_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._setup_tray()

        self._restore_position()

    # -------------------------------------------------- lifecycle

    def start(self):
        refresh_hook_copy()   # frozen exe: keep ~/.claude/clawd_hook.py current
        self.pet.show()
        self._scan_timer.start()
        self._activity_timer.start()
        self.refresh()
        if self.check_updates:
            self._begin_update_check()
            self._update_timer.start()

    def quit(self):
        self.save_position()
        self._scan_timer.stop()
        self._activity_timer.stop()
        self._update_timer.stop()
        self._udp.close()
        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)   # destroying a running QThread aborts the process
        upd = self._update_thread
        if upd is not None and upd.isRunning():
            upd.wait(7000)   # must exceed UpdateThread's 6 s network timeout
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # -------------------------------------------------- tray

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_app_icon(), self.app)
        self.tray.setToolTip(tr("tray_title"))
        self._tray_menu = self.build_menu(None)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._on_toast_clicked)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_pet_visible()

    def _rebuild_tray_menu(self):
        """The tray menu is cached, so it must be rebuilt when its items change."""
        if self.tray is None:
            return
        old = self._tray_menu
        self._tray_menu = self.build_menu(None)
        self.tray.setContextMenu(self._tray_menu)
        if old is not None:
            old.deleteLater()

    def build_menu(self, parent) -> QMenu:
        menu = QMenu(parent)
        if self._update_url:
            act_update = QAction(tr("menu_update", v=self._update_tag), menu)
            fnt = act_update.font()
            fnt.setBold(True)
            act_update.setFont(fnt)
            act_update.triggered.connect(self._open_update)
            menu.addAction(act_update)
            menu.addSeparator()
        act_refresh = QAction(tr("menu_refresh"), menu)
        act_refresh.triggered.connect(self.refresh)
        menu.addAction(act_refresh)

        act_panel = QAction(tr("menu_panel"), menu)
        act_panel.triggered.connect(self.toggle_panel)
        menu.addAction(act_panel)

        menu.addSeparator()
        act_quiet = QAction(tr("menu_quiet_on") if self.quiet
                            else tr("menu_quiet_off"), menu)
        act_quiet.triggered.connect(self.toggle_quiet)
        menu.addAction(act_quiet)

        act_notify = QAction(tr("menu_notify_off") if self.notify_enabled
                             else tr("menu_notify_on"), menu)
        act_notify.triggered.connect(self.toggle_notify)
        menu.addAction(act_notify)

        act_sound = QAction(tr("menu_sound_off") if self.notify_sound
                            else tr("menu_sound_on"), menu)
        act_sound.triggered.connect(self.toggle_notify_sound)
        menu.addAction(act_sound)

        if hooks_registered(CLAUDE_SETTINGS_FILE):
            act_hooks = QAction(tr("menu_hooks_off"), menu)
            act_hooks.triggered.connect(self.disable_hooks)
        else:
            act_hooks = QAction(tr("menu_hooks_on"), menu)
            act_hooks.triggered.connect(self.enable_hooks)
        menu.addAction(act_hooks)

        act_cal = QAction(tr("menu_cal"), menu)
        act_cal.triggered.connect(self.calibrate)
        menu.addAction(act_cal)
        if is_calibrated():
            act_reset = QAction(tr("menu_cal_reset"), menu)
            act_reset.triggered.connect(self.reset_calibration)
            menu.addAction(act_reset)

        act_lang = QAction(tr("menu_lang"), menu)
        act_lang.triggered.connect(self.toggle_language)
        menu.addAction(act_lang)

        if autostart_supported():
            act_auto = QAction(tr("menu_autostart"), menu)
            act_auto.setCheckable(True)
            act_auto.setChecked(autostart_enabled())
            act_auto.triggered.connect(self.toggle_autostart)
            menu.addAction(act_auto)

        act_upd = QAction(tr("menu_check_updates"), menu)
        act_upd.setCheckable(True)
        act_upd.setChecked(self.check_updates)
        act_upd.triggered.connect(self.toggle_update_check)
        menu.addAction(act_upd)
        menu.addSeparator()

        if self.tray is not None:   # without a tray there is no way to un-hide
            act_show = QAction(tr("menu_show"), menu)
            act_show.triggered.connect(self.toggle_pet_visible)
            menu.addAction(act_show)

        menu.addSeparator()
        act_quit = QAction(tr("menu_quit"), menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)
        return menu

    def toggle_language(self):
        set_language("en" if language() == "de" else "de")
        self.settings.setValue("language", language())
        self._rebuild_tray_menu()
        self.panel.retranslate()
        self.pet.set_snapshot(self.snapshot)
        self.panel.update_snapshot(self.snapshot)
        if self.tray:
            self.tray.setToolTip(tr("tray_title"))
        self.refresh()      # re-derive API bucket labels in the new language

    def toggle_pet_visible(self):
        if self.pet.isVisible():
            if self.tray is None:
                return   # hiding without a tray would leave no way back
            self.pet.hide()
            self.panel.hide_animated()
        else:
            self.pet.show()

    # -------------------------------------------------- scanning

    def refresh(self):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        self._scan_thread = ScanThread()
        self._scan_thread.result.connect(self._on_scan_result)
        self._scan_thread.start()

    def _on_scan_result(self, snap: UsageSnapshot):
        self.snapshot = snap
        self._newest_log = Path(snap.newest_file) if snap.newest_file else None
        if not snap.error:
            now = datetime.now(timezone.utc)
            samples = self._burn_samples.setdefault(snap.source, [])
            if samples and snap.pct < samples[-1][1] - 1.0:
                samples.clear()             # window reset — old rate is void
            samples.append((now, snap.pct))
            cutoff = now - timedelta(seconds=BURN_LOOKBACK_S)
            samples[:] = [s for s in samples if s[0] >= cutoff]
            snap.burn_eta = burn_eta(samples)
            self._notify_transition(snap.source, snap.pct)
            self.history.add(now, snap.pct)
        cal = auto_calibration()          # persist budgets learned from live syncs
        if cal["budget_5h"]:
            self.settings.setValue("auto_budget_5h", cal["budget_5h"])
        if cal["weekly_budget"]:
            self.settings.setValue("weekly_budget_all", cal["weekly_budget"])
        if cal["anchor"] is not None:
            self.settings.setValue("weekly_anchor", cal["anchor"].isoformat())
        if cal["models"]:
            self.settings.setValue("weekly_budget_models", json.dumps(cal["models"]))
        self.pet.set_snapshot(snap)
        self.panel.set_history(self.history.series())
        self.panel.update_snapshot(snap)
        if self.tray:
            if snap.error:
                self.tray.setToolTip(f"Clawd – {snap.error}")
            else:
                self.tray.setToolTip(tr("tray_tooltip", p=fmt_pct_de(snap.pct),
                                        n=fmt_de(snap.total)))
            self.tray.setIcon(make_app_icon(mood_for_pct(snap.pct)))

    # -------------------------------------------------- panel control

    def toggle_panel(self):
        if self.panel.isVisible() and self.panel.pinned:
            self.panel.hide_animated()
        else:
            self.panel.show_for(self.pet, pinned=True)

    def hover_panel(self):
        self._hide_check.stop()
        # unconditional: show_for() also rescues a panel that is mid-fade-out
        self.panel.show_for(self.pet, pinned=False)

    def schedule_panel_hide(self):
        self._hide_check.start()

    def _maybe_hide_panel(self):
        if not self.panel.isVisible() or self.panel.pinned:
            return
        cursor = QCursor.pos()
        pet_zone = self.pet.frameGeometry().adjusted(-8, -8, 8, 8)
        panel_zone = self.panel.frameGeometry().adjusted(-8, -8, 8, 8)
        if pet_zone.contains(cursor) or panel_zone.contains(cursor):
            self._hide_check.start()   # still hovering, check again later
            return
        self.panel.hide_animated()

    def pet_moved(self):
        self.panel.reposition(self.pet)
        if self.bubble.isVisible():
            self.bubble.follow(self.pet)

    # -------------------------------------------------- real-time activity

    def _check_activity(self):
        ctx = read_session_context(self._newest_log) if self._newest_log else None
        self._session_ctx = ctx
        # turn timer + "your turn" alert, keyed to the session log so a switch
        # between concurrent sessions never fakes a turn-end or a cross-session
        # timer (self._newest_log is the newest log across ALL projects)
        kind = ctx.kind if ctx else None
        log = self._newest_log
        if kind == "working" and self._work_kind == "working" and log == self._work_log:
            pass                                    # same working phase continues
        elif kind == "working":
            self._work_started_mono = time.monotonic()   # a new working phase
            self._work_log = log
        else:
            if (self._work_kind == "working" and kind == "waiting"
                    and log == self._work_log):     # same session finished its turn
                self._alert_turn_done(time.monotonic()
                                      - (self._work_started_mono or time.monotonic()))
            self._work_started_mono = None
        self._work_kind = kind
        self.panel.set_task(ctx, self._work_started_mono)  # live task view + timer
        if time.monotonic() < self._hook_hold_until:
            return                       # live hook events drive the mood
        act = (ctx.kind, ctx.tool) if ctx and ctx.kind else None
        prev = self._last_activity
        self._last_activity = act
        self.pet.set_activity(act)
        if act == prev or self.quiet or not self.pet.isVisible():
            return
        if act and act[0] == "working" and act[1]:
            text = (tool_action(ctx.tool, ctx.detail) if ctx else None) \
                or tool_bubble(act[1])
            if text and (not prev or prev[0] != "working" or prev[1] != act[1]):
                self.bubble.show_text(text, self.pet)
        elif act and act[0] == "waiting" and prev and prev[0] == "working":
            self.bubble.show_text(tr("bubble_done"), self.pet)

    def _read_hook_datagrams(self):
        while self._udp.hasPendingDatagrams():
            data, _host, _port = self._udp.readDatagram(65535)
            # only accept datagrams carrying the shared token — any local
            # process can send UDP to 127.0.0.1 (see hooks.parse_hook_datagram)
            event = parse_hook_datagram(bytes(data), self._hook_token)
            if event is not None:
                self._handle_hook_event(event)

    def _handle_hook_event(self, event: dict):
        name = event.get("hook_event_name") or ""
        act = None
        text = None
        if name == "PreToolUse":
            act = ("working", event.get("tool_name"))
            text = tool_bubble(event.get("tool_name"))
        elif name == "Notification":
            act = ("needs_input", None)
            text = tr("bubble_input")
            self._fire_alert(tr("notify_input_title"), tr("notify_input_text"))
        elif name in ("Stop", "TaskCompleted"):
            act = ("waiting", None)
        elif name == "PostToolUseFailure":
            act = ("error", None)
            QTimer.singleShot(5000, self._clear_error_state)
        elif name == "SessionStart":
            text = tr("bubble_session")
        else:
            return
        self._hook_hold_until = time.monotonic() + 15.0
        if act is not None:
            self._last_activity = act
            self.pet.set_activity(act)
        if text and not self.quiet and self.pet.isVisible():
            self.bubble.show_text(
                text, self.pet, 8000 if name == "Notification" else 4200)

    def _clear_error_state(self):
        if self.pet._activity and self.pet._activity[0] == "error":
            self._last_activity = None
            self.pet.set_activity(None)

    def toggle_quiet(self):
        self.quiet = not self.quiet
        self.settings.setValue("quiet", self.quiet)
        if self.quiet:
            self.bubble.hide()
        self._rebuild_tray_menu()

    def toggle_notify(self):
        self.notify_enabled = not self.notify_enabled
        self.settings.setValue("notify", self.notify_enabled)
        self._rebuild_tray_menu()

    def toggle_notify_sound(self):
        self.notify_sound = not self.notify_sound
        self.settings.setValue("notify_sound", self.notify_sound)
        self._rebuild_tray_menu()

    def _fire_alert(self, title: str, text: str):
        """A 'your turn' tray toast, rate-limited so near-simultaneous
        triggers (a hook and the log poll) do not double-fire."""
        now = time.monotonic()
        if (not self.notify_enabled or self.tray is None
                or now - self._last_alert_mono < ALERT_COOLDOWN_S):
            return
        self._last_alert_mono = now
        self._last_toast_was_update = False
        self.tray.showMessage(title, text, QSystemTrayIcon.Information, 7000)
        if self.notify_sound:
            QApplication.beep()

    def _alert_turn_done(self, elapsed: float):
        """Alert when a turn Claude actually spent time on has finished."""
        if elapsed >= WAIT_ALERT_MIN_S:
            self._fire_alert(tr("notify_done_title"), tr("notify_done_text"))

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self._rebuild_tray_menu()

    def toggle_update_check(self):
        self.check_updates = not self.check_updates
        self.settings.setValue("check_updates", self.check_updates)
        if self.check_updates:
            self._update_timer.start()
            self._begin_update_check()
        else:
            self._update_timer.stop()
        self._rebuild_tray_menu()

    # -------------------------------------------------- update check

    def _begin_update_check(self):
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        self._update_thread = UpdateThread()
        self._update_thread.result.connect(self._on_update_result)
        self._update_thread.start()

    def _on_update_result(self, tag: str, url: str):
        if not tag or not version_is_newer(tag, APP_VERSION):
            return
        self._update_tag = tag
        # never open an off-repo URL, whatever the API response claims
        self._update_url = url if is_trusted_update_url(url) else RELEASES_URL
        self._rebuild_tray_menu()
        if self.pet.isVisible() and not self.quiet:
            self.bubble.show_text(tr("update_available", v=tag), self.pet, 8000,
                                  on_click=self._open_update)
        if self.tray:
            self._last_toast_was_update = True
            self.tray.showMessage(tr("update_available", v=tag),
                                  tr("update_text"),
                                  QSystemTrayIcon.Information, 8000)

    def _on_toast_clicked(self):
        if self._last_toast_was_update:
            self._open_update()

    def _open_update(self):
        if self._update_url:
            webbrowser.open(self._update_url)

    def _notify_transition(self, source: str, pct: float):
        """Fire a tray toast when a scan crosses 80/95 % or the window resets.

        The previous pct is tracked per source so a transient api<->logs
        fallback neither drops a threshold toast nor fakes a reset from the
        level difference between the two modes.
        """
        prev = self._prev_pct.get(source)
        self._prev_pct[source] = pct
        kind = notify_decision(prev, pct)
        if kind is None or self.tray is None or not self.notify_enabled:
            return
        icon = (QSystemTrayIcon.Information if kind == "reset"
                else QSystemTrayIcon.Warning)
        self._last_toast_was_update = False   # this balloon is not the update one
        self.tray.showMessage(tr(f"notify_{kind}_title"),
                              tr(f"notify_{kind}_text"), icon, 6000)

    def enable_hooks(self):
        command = hook_command()
        if not command:
            QMessageBox.warning(
                None, tr("hooks_py_title"), tr("hooks_py_text"))
            return
        if register_hooks(CLAUDE_SETTINGS_FILE, command):
            QMessageBox.information(
                None, tr("hooks_on_title"),
                tr("hooks_on_text", f=f"{CLAUDE_SETTINGS_FILE.name}.clawd-bak"))
        self._rebuild_tray_menu()

    def disable_hooks(self):
        unregister_hooks(CLAUDE_SETTINGS_FILE)
        self._rebuild_tray_menu()

    # -------------------------------------------------- calibration

    def calibrate(self):
        """Derive the real token budget from the percentage Claude displays.

        Anthropic publishes no token quota, so the only ground truth is the
        number in Claude's own /usage popup. Given that percentage and the
        tokens we counted in the same window, the budget is a simple ratio.
        """
        snap = self.snapshot
        if snap.source == "api":
            QMessageBox.information(
                None, tr("cal_api_title"), tr("cal_api_text"))
            return
        if snap.total <= 0:
            QMessageBox.warning(
                None, tr("cal_nodata_title"), tr("cal_nodata_text"))
            return

        pct, ok = QInputDialog.getDouble(
            None, tr("cal_prompt_title"),
            tr("cal_prompt_text", n=fmt_de(snap.total)),
            value=65.0, min=0.5, max=100.0, decimals=1)
        if not ok:
            return

        budget = int(round(snap.weighted / (pct / 100.0)))
        self.settings.setValue("max_tokens", budget)
        set_max_tokens_override(budget)
        self._rebuild_tray_menu()
        QMessageBox.information(
            None, tr("cal_done_title"), tr("cal_done_text", n=fmt_de(budget)))
        self.refresh()

    def reset_calibration(self):
        self.settings.remove("max_tokens")
        set_max_tokens_override(None)
        self._rebuild_tray_menu()
        self.refresh()

    # -------------------------------------------------- position memory

    def save_position(self):
        self.settings.setValue("pet_pos", self.pet.pos())

    def _restore_position(self):
        pos = self.settings.value("pet_pos")
        if isinstance(pos, QPoint):
            center = QPoint(pos.x() + self.pet.width() // 2,
                            pos.y() + self.pet.height() // 2)
            screen = (QGuiApplication.screenAt(center)
                      or QGuiApplication.primaryScreen())
            avail = screen.availableGeometry()
            x = max(avail.left(), min(pos.x(), avail.right() - self.pet.width()))
            y = max(avail.top(), min(pos.y(), avail.bottom() - self.pet.height()))
            self.pet.move(x, y)
        else:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            self.pet.move(avail.right() - self.pet.width() - 24,
                          avail.bottom() - self.pet.height() - 48)
def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    if "--selftest" in sys.argv:
        from .selftest import run_selftest   # deferred: selftest imports app
        return run_selftest()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # lives in the tray
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(make_app_icon())

    # Single-instance guard: a second pet would fight over the hook UDP port
    # and keep the exe locked during updates. QLockFile stores the owner PID,
    # so a lock left behind by a crash is detected as stale and removed.
    set_language(str(QSettings(ORG_NAME, APP_NAME).value("language", "de") or "de"))
    # keep the lock in the user's own ~/.clawd: a shared temp dir would let
    # another local user pre-create the name and block the app from starting
    lock_dir = Path.home() / ".clawd"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        lock_dir = Path(tempfile.gettempdir())
    lock = QLockFile(str(lock_dir / "clawd_pet.lock"))
    lock.setStaleLockTime(0)               # the pet runs for days — never age out
    if not lock.tryLock(100):
        QMessageBox.information(None, tr("single_title"), tr("single_text"))
        return 0

    controller = ClawdApp(app)
    controller.start()
    return app.exec_()
