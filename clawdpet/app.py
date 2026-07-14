"""Application controller + entry point — wires pet, panel, tray, scanner."""
import json
import os
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Qt on macOS activates the WHOLE app inside QWidget.raise_() when it is
# inactive ([NSApp activateIgnoringOtherApps:]) — so every reaction bubble
# stole the keyboard focus from whatever the user was typing in. The escape
# hatch is documented in Qt's cocoa plugin and must be set before the
# QApplication exists, hence at import time. setdefault: a user override wins.
os.environ.setdefault("QT_MAC_SET_RAISE_PROCESS", "0")

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
    QActionGroup,
    QApplication,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import sounds
from .activity import newest_codex_log, read_codex_context, read_session_context
from .api import (
    clawd_build_authorize_url,
    clawd_exchange_code,
    collect_usage,
    force_live_refetch,
)
from .art import import_sprite_pack, make_app_icon
from .autostart import autostart_enabled, autostart_supported, set_autostart
from .bubble import SpeechBubble
from .codex import (
    codex_available,
    codex_usage,
    notify_command as codex_notify_command,
    notify_registered as codex_notify_registered,
    register_notify,
    unregister_notify,
)
from .config import (
    ACTIVITY_POLL_MS,
    ALERT_COOLDOWN_S,
    APP_NAME,
    APP_VERSION,
    BURN_LOOKBACK_S,
    CLAUDE_SETTINGS_FILE,
    HOOK_UDP_PORT,
    ORG_NAME,
    PET_HEIGHT,
    PET_SIZE_FACTORS,
    RELEASES_URL,
    SCAN_INTERVAL_MS,
    SPRITE_FILES,
    UPDATE_CHECK,
    UPDATE_RECHECK_MS,
    WAIT_ALERT_MIN_S,
    WEIGHT_VERSION,
)
from .focus import focus_terminal
from .history import HistoryStore
from .macdock import hide_dock_icon
from .hooks import (
    ensure_hook_token,
    hook_command,
    hooks_registered,
    parse_hook_datagram,
    permission_hook_registered,
    refresh_hook_copy,
    register_hooks,
    register_permission_hook,
    unregister_hooks,
    unregister_permission_hook,
)
from .permission_bubble import DECIDE_S, PermissionBubble
from .telegram_approval import (
    REMOTE_WINDOW_S,
    load_config as tg_load_config,
    remote_watch as tg_remote_watch,
    remove_config as tg_remove_config,
    save_config as tg_save_config,
    telegram_configured,
)
from .status_check import anthropic_sick
from .statusline import (
    register_statusline,
    statusline_registered,
    unregister_statusline,
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
from .notify import post_notification
from . import progress
from .panel import PanelWidget
from .pet import PetWidget
from .update import UpdateThread, is_trusted_update_url, version_is_newer
from .usage import (
    UsageSnapshot,
    _parse_iso_ts,
    auto_budget_active,
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
        try:
            # X1: Codex rate limits ride along — throttled internally, and the
            # subprocess spawn may block, which is exactly why it happens here
            snap.codex_buckets = codex_usage()
        except Exception:
            snap.codex_buckets = None
        try:
            snap.anthropic_sick = anthropic_sick()   # 10-min throttle inside
        except Exception:
            snap.anthropic_sick = False
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

        # Auto-calibration lives in ~/.clawd/calibration.json ONLY (loaded
        # lazily by clawdpet.usage). It used to be mirrored in QSettings too,
        # and the startup restore-from-QSettings clobbered a fresher file
        # with stale values after every pet restart — two persistence layers
        # fighting each other. One-time migration: seed the file from any
        # leftover QSettings values, then delete those keys for good.
        if not auto_budget_active():
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
        for _k in ("auto_budget_5h", "weekly_budget_all",
                   "weekly_anchor", "weekly_budget_models"):
            self.settings.remove(_k)

        self.pet = PetWidget(self)
        self.panel = PanelWidget()
        self.panel.on_leave = self.schedule_panel_hide
        self.bubble = SpeechBubble()
        self.perm_bubble = PermissionBubble()   # F11 Allow/Deny callout

        # real-time activity: log watcher (Stufe 1) + hook receiver (Stufe 2)
        self.quiet = self.settings.value("quiet", False, type=bool)
        self.notify_enabled = self.settings.value("notify", True, type=bool)
        self.notify_sound = self.settings.value("notify_sound", False, type=bool)
        self.dnd = self.settings.value("dnd", False, type=bool)   # master mute
        self.wander = self.settings.value("wander", False, type=bool)
        self.click_through = self.settings.value("click_through", False, type=bool)
        self.cursor_chase = self.settings.value("cursor_chase", False, type=bool)
        self._was_sick = False           # Anthropic incident edge detection
        # burn-rate history and last pct are kept PER SOURCE ("api"/"logs"):
        # the two modes report on different absolute scales, so cross-comparing
        # them would fake resets — but a transient api->logs->api fallback (an
        # expired OAuth token, a network blip) must not wipe either history, or
        # the forecast and the threshold toasts would go dark for minutes.
        self._burn_samples = {}          # source -> list[(utc time, pct)]
        self._prev_pct = {}              # source -> last pct seen
        # G: gamification — previous scan's weekly weighted counter. The
        # weekly counter is monotonic within a week and survives 5h-window
        # resets, so per-scan deltas feed the pet's XP; a shrinking value
        # means a new weekly window and only re-arms the baseline.
        self._prev_week_weighted: Optional[float] = None
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
        # X2: hook-driven state — running subagents (juggle while > 0), an
        # active context compaction (sweep), and the latest context-window
        # fill reported by the statusline sender (clawd_statusline.py)
        self._subagent_count = 0
        self._compacting = False
        self._context_pct: Optional[float] = None
        # remote approval (Telegram): worker thread results land in a queue
        # that this GUI-side timer drains while a request is pending
        self._remote_watch = None
        self._remote_timer = QTimer()
        self._remote_timer.setInterval(250)
        self._remote_timer.timeout.connect(self._drain_remote_decisions)
        self._activity_timer = QTimer()
        self._activity_timer.setInterval(ACTIVITY_POLL_MS)
        self._activity_timer.timeout.connect(self._check_activity)
        self._hook_token = ensure_hook_token()   # authenticates hook datagrams
        self._udp = QUdpSocket()
        if self._udp.bind(QHostAddress.LocalHost, HOOK_UDP_PORT):
            self._udp.readyRead.connect(self._read_hook_datagrams)
        # Replies (permission ack/decision) go through their own ephemeral
        # socket: Qt refuses writeDatagram on an unbound socket, and the main
        # one stays unbound when another instance already holds the hook port.
        self._udp_reply = QUdpSocket()
        self._udp_reply.bind(QHostAddress.LocalHost, 0)

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

        # F2/F13: apply the saved size preset + custom sprite pack (one
        # rebuild). Must happen BEFORE the tray menu is built — build_menu
        # reads self.pet_size / self.sprite_dir for the checked entries.
        size_key = str(self.settings.value("pet_size", "M") or "M")
        self.pet_size = size_key if size_key in PET_SIZE_FACTORS else "M"
        self.sprite_dir: Optional[Path] = None
        sdir_raw = str(self.settings.value("sprite_dir", "") or "")
        if sdir_raw:
            if Path(sdir_raw).is_dir():
                self.sprite_dir = Path(sdir_raw)
            else:
                self.settings.remove("sprite_dir")   # folder gone -> default
        if self.pet_size != "M" or self.sprite_dir is not None:
            self.pet.rebuild(height=self._pet_height(),
                             sprite_dir=self.sprite_dir)

        self.tray: Optional[QSystemTrayIcon] = None
        self._tray_menu: Optional[QMenu] = None
        if with_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._setup_tray()

        self._restore_position()
        self.pet.enable_wander(self.wander)          # F5 (opt-in)
        self.pet.set_click_through(self.click_through)   # F8 (opt-in)
        self.pet.enable_cursor_chase(self.cursor_chase)  # Y (opt-in)

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
        self.perm_bubble.decide("pass")   # unblock a waiting hook fast
        self.save_position()
        self._scan_timer.stop()
        self._activity_timer.stop()
        self._update_timer.stop()
        self._udp.close()
        self._udp_reply.close()
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
        act_dnd = QAction(tr("menu_dnd_off") if self.dnd
                          else tr("menu_dnd_on"), menu)
        act_dnd.triggered.connect(self.toggle_dnd)
        menu.addAction(act_dnd)

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

        act_sound_test = QAction(tr("menu_sound_test"), menu)
        act_sound_test.triggered.connect(self.test_sound)
        menu.addAction(act_sound_test)

        if telegram_configured():
            act_tg = QAction(tr("menu_tg_off"), menu)
            act_tg.triggered.connect(self.remove_telegram)
        else:
            act_tg = QAction(tr("menu_tg_on"), menu)
            act_tg.triggered.connect(self.setup_telegram)
        menu.addAction(act_tg)

        if hooks_registered(CLAUDE_SETTINGS_FILE):
            act_hooks = QAction(tr("menu_hooks_off"), menu)
            act_hooks.triggered.connect(self.disable_hooks)
        else:
            act_hooks = QAction(tr("menu_hooks_on"), menu)
            act_hooks.triggered.connect(self.enable_hooks)
        menu.addAction(act_hooks)

        if permission_hook_registered(CLAUDE_SETTINGS_FILE):
            act_perm = QAction(tr("menu_perm_off"), menu)
            act_perm.triggered.connect(self.disable_permission_bubble)
        else:
            act_perm = QAction(tr("menu_perm_on"), menu)
            act_perm.triggered.connect(self.enable_permission_bubble)
        menu.addAction(act_perm)

        if statusline_registered(CLAUDE_SETTINGS_FILE):
            act_sline = QAction(tr("menu_statusline_off"), menu)
            act_sline.triggered.connect(self.disable_statusline)
        else:
            act_sline = QAction(tr("menu_statusline_on"), menu)
            act_sline.triggered.connect(self.enable_statusline)
        menu.addAction(act_sline)

        if codex_available():
            if codex_notify_registered():
                act_cdx = QAction(tr("menu_codex_notify_off"), menu)
                act_cdx.triggered.connect(self.disable_codex_notify)
            else:
                act_cdx = QAction(tr("menu_codex_notify_on"), menu)
                act_cdx.triggered.connect(self.enable_codex_notify)
            menu.addAction(act_cdx)

        act_cal = QAction(tr("menu_cal"), menu)
        act_cal.triggered.connect(self.calibrate)
        menu.addAction(act_cal)
        if is_calibrated():
            act_reset = QAction(tr("menu_cal_reset"), menu)
            act_reset.triggered.connect(self.reset_calibration)
            menu.addAction(act_reset)

        act_login = QAction(tr("menu_clawd_login"), menu)
        act_login.triggered.connect(self.setup_clawd_login)
        menu.addAction(act_login)

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

        act_wander = QAction(tr("menu_wander"), menu)
        act_wander.setCheckable(True)
        act_wander.setChecked(self.wander)
        act_wander.triggered.connect(self.toggle_wander)
        menu.addAction(act_wander)

        act_click = QAction(tr("menu_clickthrough"), menu)
        act_click.setCheckable(True)
        act_click.setChecked(self.click_through)
        act_click.triggered.connect(self.toggle_click_through)
        menu.addAction(act_click)

        act_chase = QAction(tr("menu_chase"), menu)     # Y: oneko mode
        act_chase.setCheckable(True)
        act_chase.setChecked(self.cursor_chase)
        act_chase.triggered.connect(self.toggle_cursor_chase)
        menu.addAction(act_chase)

        size_menu = menu.addMenu(tr("menu_size"))    # F2: S / M / L presets
        size_group = QActionGroup(size_menu)
        size_group.setExclusive(True)
        for key in PET_SIZE_FACTORS:
            act_size = QAction(key, size_menu)
            act_size.setCheckable(True)
            act_size.setChecked(key == self.pet_size)
            act_size.triggered.connect(
                lambda _checked=False, k=key: self.set_pet_size(k))
            size_group.addAction(act_size)
            size_menu.addAction(act_size)

        act_sprites = QAction(tr("menu_sprites_choose"), menu)   # F13
        act_sprites.triggered.connect(self.choose_sprite_dir)
        menu.addAction(act_sprites)
        act_pack = QAction(tr("menu_pack_import"), menu)   # Y: petdex import
        act_pack.triggered.connect(self.import_pack_dialog)
        menu.addAction(act_pack)
        if self.sprite_dir is not None:
            act_spr_reset = QAction(tr("menu_sprites_reset"), menu)
            act_spr_reset.triggered.connect(self.reset_sprite_dir)
            menu.addAction(act_spr_reset)
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
            # G: gamification — feed the weekly-counter growth to the pet.
            # Only while the same weekly window is still active: a shrunken
            # counter means a new window, so just re-arm the baseline.
            prev_ww = self._prev_week_weighted
            self._prev_week_weighted = snap.week_weighted
            if prev_ww is not None and snap.week_weighted >= prev_ww:
                event = progress.add_usage(snap.week_weighted - prev_ww)
                if event is not None:
                    self._on_level_up(event)
        sick = bool(getattr(snap, "anthropic_sick", False))
        if sick != self._was_sick:
            self._was_sick = sick
            self.panel.set_incident(sick)
            if sick and not self.dnd and not self.quiet and self.pet.isVisible():
                self.bubble.show_text(tr("bubble_incident"), self.pet, 8000)
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
        log = self._newest_log
        if ctx is None:
            # Codex CLI fallback (F6): only when no Claude session is active —
            # Claude logs always win. The codex log then carries the work-log
            # identity below, so the turn timer and the cross-session guard
            # keep working across both worlds.
            codex_log = newest_codex_log()
            if codex_log is not None:
                ctx = read_codex_context(codex_log)
                if ctx is not None:
                    log = codex_log
        self._session_ctx = ctx
        # turn timer + "your turn" alert, keyed to the session log so a switch
        # between concurrent sessions never fakes a turn-end or a cross-session
        # timer (self._newest_log is the newest log across ALL projects)
        kind = ctx.kind if ctx else None
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
        self.pet.set_generating(bool(act and act[0] == "working"))
        if act == prev or self.quiet or self.dnd or not self.pet.isVisible():
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
            data, host, port = self._udp.readDatagram(65535)
            # only accept datagrams carrying the shared token — any local
            # process can send UDP to 127.0.0.1 (see hooks.parse_hook_datagram)
            event = parse_hook_datagram(bytes(data), self._hook_token)
            if event is None:
                continue
            query = event.get("clawd_permission")
            status = event.get("clawd_statusline")
            codex_turn = event.get("codex_turn")
            if isinstance(query, dict):
                self._handle_permission_query(query, host, port)
            elif isinstance(status, dict):
                self._handle_statusline(status)
            elif isinstance(codex_turn, dict):
                self._handle_codex_turn(codex_turn)
            else:
                self._handle_hook_event(event)

    # ------------------------------------------- permission bubble (F11)

    def _reply_perm(self, host, port, payload: dict):
        """Token-prefixed reply straight back to the waiting hook process."""
        data = (self._hook_token.encode("utf-8") + b"\n"
                + json.dumps(payload).encode("utf-8"))
        self._udp_reply.writeDatagram(data, host, port)

    def _handle_permission_query(self, query: dict, host, port):
        """A clawd_permission_hook.py process is blocked on a permission
        prompt and asks us to decide. We only engage (ack) when we can show
        the bubble — everything else stays silent so the hook falls back to
        the normal terminal prompt within half a second."""
        qid = query.get("id")
        if not isinstance(qid, str) or not qid:
            return
        if (self.dnd                     # do-not-disturb: terminal decides
                or not self.pet.isVisible()
                or self.perm_bubble.active):   # one question at a time
            return
        tool = str(query.get("tool_name") or "?")
        detail = str(query.get("detail") or "")
        tg_cfg = tg_load_config()
        window = REMOTE_WINDOW_S if tg_cfg else DECIDE_S + 1.0
        self._reply_perm(host, port, {"id": qid, "type": "ack",
                                      "window_s": window})

        def _decide(decision: str, _qid=qid, _host=host, _port=port):
            self._reply_perm(_host, _port, {"id": _qid, "type": "decision",
                                            "decision": decision})
            self._stop_remote_watch(answered_locally=(decision != "pass"))

        self.perm_bubble.ask(tool, detail, self.pet, on_decide=_decide,
                             window_s=max(DECIDE_S, window - 2.0))
        if tg_cfg:
            self._start_remote_watch(tg_cfg, qid, tool, detail, window)

    # ---------------------------------------- remote approval (Telegram)

    def _start_remote_watch(self, cfg, qid, tool, detail, window):
        """Ask the phone too — whichever channel answers first wins."""
        import queue as _queue
        import threading as _threading
        self._stop_remote_watch(answered_locally=False)
        stop = _threading.Event()
        results: "_queue.Queue" = _queue.Queue()
        deadline = time.monotonic() + window - 1.0
        t = _threading.Thread(
            target=tg_remote_watch, daemon=True,
            args=(cfg, qid, tool, detail, stop, deadline, results.put))
        self._remote_watch = {"stop": stop, "queue": results, "qid": qid}
        t.start()
        self._remote_timer.start()

    def _stop_remote_watch(self, answered_locally: bool):
        watch = getattr(self, "_remote_watch", None)
        if watch is None:
            return
        if answered_locally:
            watch["stop"].set()        # thread edits the card to 'answered'
        self._remote_watch = None
        self._remote_timer.stop()

    def _drain_remote_decisions(self):
        """GUI-thread side of the phone channel: apply the first decision."""
        watch = getattr(self, "_remote_watch", None)
        if watch is None:
            self._remote_timer.stop()
            return
        try:
            decision = watch["queue"].get_nowait()
        except Exception:
            return
        if decision in ("allow", "deny") and self.perm_bubble.active:
            self.perm_bubble.decide(decision)   # routes through _decide
        self._remote_watch = None
        self._remote_timer.stop()

    def setup_telegram(self):
        """Two-step dialog: bot token (from @BotFather), then the chat id."""
        token, ok = QInputDialog.getText(
            None, tr("tg_title"), tr("tg_token_prompt"))
        if not ok or not token.strip():
            return
        chat, ok = QInputDialog.getText(
            None, tr("tg_title"), tr("tg_chat_prompt"))
        if not ok or not chat.strip():
            return
        if tg_save_config(token, chat):
            QMessageBox.information(None, tr("tg_title"), tr("tg_saved"))
        self._rebuild_tray_menu()

    def remove_telegram(self):
        tg_remove_config()
        self._rebuild_tray_menu()

    def _handle_hook_event(self, event: dict):
        name = event.get("hook_event_name") or ""
        act = None
        clear_act = False       # explicit "back to idle" (act stays None)
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
            self._subagent_count = 0
            self._compacting = False
        elif name in ("SubagentStart", "SubagentStop"):
            # X2: while subagents run, Clawd juggles them ("Task" maps to the
            # juggle mood via moods.TOOL_MOODS); the count never goes below 0
            if name == "SubagentStart":
                self._subagent_count += 1
            else:
                self._subagent_count = max(0, self._subagent_count - 1)
            if self._subagent_count > 0:
                act = ("working", "Task")
            else:
                clear_act = True
        elif name in ("PostToolUseFailure", "StopFailure"):
            # X2: a failure startles the pet — no toast (too noisy), and under
            # do-not-disturb nothing happens at all
            if self.dnd:
                return
            self.pet._startle()          # 30 s cooldown handled by the pet
            act = ("error", None)        # brief grumpy/panic look
            QTimer.singleShot(5000, self._clear_error_state)
        elif name == "PreCompact":
            # X2: sweeping up while Claude Code compacts the context
            self._compacting = True
            act = ("working", "Compact")
            text = tr("bubble_compact")
        elif name == "PostCompact":
            self._compacting = False
            if self._subagent_count > 0:
                act = ("working", "Task")   # subagents still running -> juggle
            else:
                clear_act = True
        elif name == "SessionStart":
            self._subagent_count = 0
            self._compacting = False
            text = tr("bubble_session")
        elif name == "SessionEnd":
            # X2: session gone — clear any hook-driven activity state (like
            # Stop does) and release the mood hold for other live sessions
            self._subagent_count = 0
            self._compacting = False
            clear_act = True
        else:
            return                       # unknown/old events: ignore gracefully
        self._hook_hold_until = (0.0 if name == "SessionEnd"
                                 else time.monotonic() + 15.0)
        if act is not None or clear_act:
            self._last_activity = act
            self.pet.set_activity(act)
            self.pet.set_generating(bool(act and act[0] == "working"))
        if text and not self.quiet and not self.dnd and self.pet.isVisible():
            # a "needs you" bubble jumps to the terminal when clicked
            self.bubble.show_text(
                text, self.pet, 8000 if name == "Notification" else 4200,
                on_click=focus_terminal if name == "Notification" else None)

    def _clear_error_state(self):
        if self.pet._activity and self.pet._activity[0] == "error":
            self._last_activity = None
            self.pet.set_activity(None)
            self.pet.set_generating(False)

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

    def test_sound(self):
        """Play the notification chime on demand, ignoring the sound setting —
        this exists precisely so the user can hear it before enabling it."""
        if not sounds.play("attention"):
            QApplication.beep()

    def _fire_alert(self, title: str, text: str):
        """A 'your turn' tray toast, rate-limited so near-simultaneous
        triggers (a hook and the log poll) do not double-fire."""
        now = time.monotonic()
        if (not self.notify_enabled or self.dnd or self.tray is None
                or now - self._last_alert_mono < ALERT_COOLDOWN_S):
            return
        self._last_alert_mono = now
        self._last_toast_was_update = False
        # native first: Qt's fallback balloon steals focus on macOS
        if not post_notification(title, text):
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 7000)
        # F9: real chime when available; beep stays the offscreen/CI fallback
        if self.notify_sound and not sounds.play("attention"):
            QApplication.beep()

    def _alert_turn_done(self, elapsed: float):
        """Alert when a turn Claude actually spent time on has finished."""
        if elapsed >= WAIT_ALERT_MIN_S:
            self._fire_alert(tr("notify_done_title"), tr("notify_done_text"))

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self._rebuild_tray_menu()

    def toggle_wander(self):
        self.wander = not self.wander
        self.settings.setValue("wander", self.wander)
        self.pet.enable_wander(self.wander)
        self._rebuild_tray_menu()

    def toggle_cursor_chase(self):
        self.cursor_chase = not self.cursor_chase
        self.settings.setValue("cursor_chase", self.cursor_chase)
        self.pet.enable_cursor_chase(self.cursor_chase)

    def toggle_click_through(self):
        self.click_through = not self.click_through
        self.settings.setValue("click_through", self.click_through)
        self.pet.set_click_through(self.click_through)
        self._rebuild_tray_menu()

    # -------------------------------------------------- customization (F2/F13)

    def _pet_height(self) -> int:
        return int(PET_HEIGHT * PET_SIZE_FACTORS[self.pet_size])

    def _rebuild_pet(self):
        """Apply the current size preset + sprite pack to the pet."""
        self.pet.rebuild(height=self._pet_height(), sprite_dir=self.sprite_dir)
        self.pet_moved()             # panel/bubble follow the new geometry
        self._rebuild_tray_menu()

    def set_pet_size(self, key: str):
        if key not in PET_SIZE_FACTORS:
            return
        self.pet_size = key
        self.settings.setValue("pet_size", key)
        self._rebuild_pet()

    @staticmethod
    def _dir_has_sprites(path: Path) -> bool:
        """True when the folder holds at least one known Clawd gif."""
        return any((path / f).is_file() for f in SPRITE_FILES.values())

    def choose_sprite_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            None, tr("menu_sprites_choose"))
        if not chosen:
            return
        if not self._set_sprite_dir(Path(chosen)):
            QMessageBox.warning(None, tr("sprites_invalid_title"),
                                tr("sprites_invalid_text"))

    def import_pack_dialog(self):
        """Import a petdex/'Codex pet' community pack (.zip or folder)."""
        chosen, _ = QFileDialog.getOpenFileName(
            None, tr("menu_pack_import"), "",
            "Sprite-Pack (*.zip);;Alle Dateien (*)")
        if not chosen:
            return
        dest = import_sprite_pack(Path(chosen))
        if dest is None or not self._set_sprite_dir(dest):
            QMessageBox.warning(None, tr("sprites_invalid_title"),
                                tr("pack_invalid_text"))

    def _set_sprite_dir(self, path: Path) -> bool:
        """Activate a sprite pack; False (setting untouched) when invalid."""
        if not self._dir_has_sprites(path):
            return False
        self.sprite_dir = path
        self.settings.setValue("sprite_dir", str(path))
        self._rebuild_pet()
        return True

    def reset_sprite_dir(self):
        self.sprite_dir = None
        self.settings.remove("sprite_dir")
        self._rebuild_pet()

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
        if kind == "reset" and not self.dnd:
            self.pet.celebrate()          # quota refreshed — party hop (Y)
        if (kind is None or self.tray is None or not self.notify_enabled
                or self.dnd):
            return
        icon = (QSystemTrayIcon.Information if kind == "reset"
                else QSystemTrayIcon.Warning)
        self._last_toast_was_update = False   # this balloon is not the update one
        if not post_notification(tr(f"notify_{kind}_title"),
                                 tr(f"notify_{kind}_text")):
            self.tray.showMessage(tr(f"notify_{kind}_title"),
                                  tr(f"notify_{kind}_text"), icon, 6000)

    def _on_level_up(self, event: dict):
        """G: the pet crossed a level — party hop + a toast with its new title.

        Same etiquette as the reset celebration: DND mutes everything, the
        notification toggle gates the toast, native notification first."""
        if self.dnd:
            return
        self.pet.celebrate()
        if self.tray is None or not self.notify_enabled:
            return
        title = tr("levelup_title", n=event["level"])
        text = tr("levelup_text", title=event["title"])
        self._last_toast_was_update = False   # this balloon is not the update one
        if not post_notification(title, text):
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 6000)

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

    def toggle_dnd(self):
        """Master mute: bubbles, toasts, chimes and permission engagement."""
        self.dnd = not self.dnd
        self.settings.setValue("dnd", self.dnd)
        if self.dnd:
            self.bubble.hide()
            self.perm_bubble.decide("pass")   # open question -> terminal
        self._rebuild_tray_menu()

    def enable_permission_bubble(self):
        command = hook_command("clawd_permission_hook.py")
        if not command:
            QMessageBox.warning(
                None, tr("hooks_py_title"), tr("hooks_py_text"))
            return
        if register_permission_hook(CLAUDE_SETTINGS_FILE, command):
            QMessageBox.information(
                None, tr("perm_on_title"),
                tr("perm_on_text", f=f"{CLAUDE_SETTINGS_FILE.name}.clawd-bak"))
        self._rebuild_tray_menu()

    def disable_permission_bubble(self):
        unregister_permission_hook(CLAUDE_SETTINGS_FILE)
        self.perm_bubble.decide("pass")
        self._rebuild_tray_menu()

    # ------------------------------------------- statusline (X2)

    def enable_statusline(self):
        command = hook_command("clawd_statusline.py")
        if not command:
            QMessageBox.warning(
                None, tr("hooks_py_title"), tr("hooks_py_text"))
            return
        if register_statusline(CLAUDE_SETTINGS_FILE, command):
            QMessageBox.information(
                None, tr("statusline_on_title"),
                tr("statusline_on_text",
                   f=f"{CLAUDE_SETTINGS_FILE.name}.clawd-bak"))
        elif not statusline_registered(CLAUDE_SETTINGS_FILE):
            # a statusline the user configured themselves — never overwritten
            QMessageBox.warning(
                None, tr("statusline_foreign_title"),
                tr("statusline_foreign_text"))
        self._rebuild_tray_menu()

    def disable_statusline(self):
        unregister_statusline(CLAUDE_SETTINGS_FILE)
        self._rebuild_tray_menu()

    def _handle_statusline(self, payload: dict):
        """Latest context-window fill from clawd_statusline.py via UDP."""
        try:
            pct = float(payload.get("context_pct"))
        except (TypeError, ValueError):
            return
        pct = max(0.0, min(100.0, pct))
        model = payload.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        self._context_pct = pct
        self.panel.set_context(pct, model)

    # -------------------------------------------------- Codex (X1)

    def _handle_codex_turn(self, payload: dict):
        """codex_notify.py forwarded an agent-turn-complete event: same
        'your turn' treatment as a finished Claude turn, Codex-labeled."""
        if self.dnd or not self.notify_enabled:
            return
        self._fire_alert(tr("notify_codex_title"), tr("notify_codex_text"))
        if self.pet.isVisible() and not self.quiet:
            self.bubble.show_text(tr("notify_codex_text"), self.pet,
                                  on_click=focus_terminal)

    def enable_codex_notify(self):
        from .hooks import _hook_runner
        runner = _hook_runner()
        script = Path(__file__).resolve().parent.parent / "codex_notify.py"
        if getattr(sys, "frozen", False):
            # the bundle payload dir is temporary — config.toml needs a path
            # that survives, so the sender is copied next to our other state
            src = Path(getattr(sys, "_MEIPASS", "")) / "codex_notify.py"
            script = Path.home() / ".clawd" / "codex_notify.py"
            try:
                script.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _sh
                _sh.copy2(src, script)
            except OSError:
                return
        if not runner or not script.is_file():
            return
        line = codex_notify_command(runner, script)
        if not register_notify(line):
            if not codex_notify_registered():   # a foreign entry blocked us
                QMessageBox.information(None, "Codex",
                                        tr("codex_notify_foreign"))
        self._rebuild_tray_menu()

    def disable_codex_notify(self):
        unregister_notify()
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

    def setup_clawd_login(self):
        """One-time login that gives Clawd its OWN independent usage token, so
        live values keep working (auto-refreshed) without touching Claude Code's
        credential store. Opens the browser, then takes the pasted code."""
        url, verifier, redirect = clawd_build_authorize_url()
        try:
            webbrowser.open(url)
        except Exception:
            pass
        raw, ok = QInputDialog.getText(
            None, tr("clawd_login_title"),
            tr("clawd_login_prompt") + "\n\n" + url)
        if not ok:
            return
        if not raw.strip():
            QMessageBox.warning(None, tr("clawd_login_title"),
                                tr("clawd_login_nocode"))
            return
        try:
            clawd_exchange_code(raw, verifier, redirect)
        except Exception as e:      # noqa: BLE001 — surface any failure to the user
            QMessageBox.warning(
                None, tr("clawd_login_title"),
                tr("clawd_login_fail", e=str(e)[:200]))
            return
        force_live_refetch()        # immediate live fetch with the new token
        self.refresh()
        QMessageBox.information(None, tr("clawd_login_title"),
                                tr("clawd_login_ok"))

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
    hide_dock_icon()   # macOS: menu-bar (tray) app only — no Dock, no Cmd-Tab
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
