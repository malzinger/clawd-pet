"""Slide-out usage panel (Claude-style)."""
import sys
import time
from datetime import timedelta
from typing import Optional

from PyQt5.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PyQt5.QtGui import QColor, QGuiApplication
from PyQt5.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .api import _fmt_reset
from .art import make_clawd_pixmap, sprite_pixmap
from .config import (
    PANEL_WIDTH,
    PLAN_NAME,
    SONNET_INPUT_USD_PER_MTOK,
    WINDOW_HOURS,
)
from .history import HistoryChart
from .i18n import _fmt_dur, fmt_de, fmt_pct_de, tool_action, tr
from .moods import MOOD_COLORS, mood_for_pct
from .usage import (
    UsageSnapshot,
    auto_budget_active,
    effective_max_tokens,
    is_calibrated,
    weekly_budget_all,
    weekly_model_budgets,
)

# ======================================================================
#  Slide-out usage panel
# ======================================================================

PANEL_QSS = """
QFrame#card {
    background-color: rgba(38, 37, 35, 250);
    border: 1px solid #3d3b38;
    border-radius: 12px;
}
QLabel {
    color: #eceae6;
    background: transparent;
    border: none;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
}
QLabel#h1       { font-size: 13px; font-weight: 600; }
QLabel#rowlabel { font-size: 12px; font-weight: 600; }
QLabel#reset    { font-size: 11px; color: #9b9892; }
QLabel#pct      { font-size: 12px; font-weight: 700; }
QLabel#sub      { font-size: 11px; color: #9b9892; }
QLabel#note     { font-size: 10px; color: #7d7a74; font-style: italic; }
QProgressBar {
    background: #3a3833;
    border: none;
    border-radius: 2px;
}
QProgressBar::chunk { border-radius: 2px; background: #6879f8; }
QFrame#divider { background: #3d3b38; border: none; }
"""


class PanelWidget(QWidget):
    SLIDE_PX = 16

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":   # Qt.Tool windows vanish on app deactivation
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFixedWidth(PANEL_WIDTH)
        self.setStyleSheet(PANEL_QSS)

        self.pinned = False
        self.on_leave = None            # callback set by ClawdApp
        self._snap: Optional[UsageSnapshot] = None
        self._anim: Optional[QParallelAnimationGroup] = None
        self._hiding = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame(self)
        card.setObjectName("card")
        outer.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 160))
        card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(8)
        self._lay = lay

        # ---- header (Claude style) -------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        avatar = QLabel()
        avatar.setPixmap(sprite_pixmap("chill", 24) or make_clawd_pixmap(24))
        header.addWidget(avatar)
        self._title = QLabel(tr("panel_title", plan=PLAN_NAME))
        self._title.setObjectName("h1")
        header.addWidget(self._title, 1)
        lay.addLayout(header)
        lay.addWidget(self._divider())

        # ---- "what Clawd is working on" (current task) -----------------
        self._task_ctx = None
        self._work_since = None
        self._task_action = ""
        self.task_title = QLabel(tr("task_title"))
        self.task_title.setObjectName("note")
        lay.addWidget(self.task_title)
        self.task_project = QLabel("")
        self.task_project.setObjectName("sub")
        self.task_project.setWordWrap(True)
        lay.addWidget(self.task_project)
        self.task_prompt = QLabel("")
        self.task_prompt.setObjectName("sub")
        self.task_prompt.setWordWrap(True)
        self.task_prompt.setStyleSheet("font-style: italic;")
        lay.addWidget(self.task_prompt)
        self.task_activity = QLabel("")
        self.task_activity.setObjectName("rowlabel")
        self.task_activity.setWordWrap(True)
        lay.addWidget(self.task_activity)
        self.task_div = self._divider()
        lay.addWidget(self.task_div)
        self._task_widgets = [self.task_title, self.task_project,
                              self.task_prompt, self.task_activity, self.task_div]
        for w in self._task_widgets:
            w.setVisible(False)

        # ---- usage rows, created on demand from the live buckets --------
        self._rows = {}

        # ---- footer ------------------------------------------------------
        self._footer_div = self._divider()
        lay.addWidget(self._footer_div)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("sub")
        self.detail_label.setWordWrap(True)
        lay.addWidget(self.detail_label)
        self.cost_label = QLabel("")
        self.cost_label.setObjectName("sub")
        self.cost_label.setWordWrap(True)
        lay.addWidget(self.cost_label)
        self.projects_label = QLabel("")
        self.projects_label.setObjectName("sub")
        self.projects_label.setWordWrap(True)
        lay.addWidget(self.projects_label)
        self.forecast_label = QLabel("")
        self.forecast_label.setObjectName("sub")
        self.forecast_label.setWordWrap(True)
        lay.addWidget(self.forecast_label)
        self._history = []
        self.history_title = QLabel(tr("history_title"))
        self.history_title.setObjectName("note")
        self.history_title.setVisible(False)
        lay.addWidget(self.history_title)
        self.history_chart = HistoryChart()
        self.history_chart.setVisible(False)
        lay.addWidget(self.history_chart)
        self.updated_label = QLabel("Zuletzt aktualisiert: –")
        self.updated_label.setObjectName("note")
        lay.addWidget(self.updated_label)

        # countdown refresher (only while visible)
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._refresh_countdown)

        self.adjustSize()

    # -------------------------------------------------- small builders

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def _ensure_row(self, key: str, label: str) -> dict:
        """Create (or fetch) a Claude-style usage row: label | reset | pct + bar."""
        row = self._rows.get(key)
        if row is not None:
            row["name"].setText(label)
            return row
        idx = self._lay.indexOf(self._footer_div)
        holder = QVBoxLayout()
        holder.setSpacing(3)

        # top line: name .......... percentage
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(label)
        name.setObjectName("rowlabel")
        pct = QLabel("–")
        pct.setObjectName("pct")
        top.addWidget(name, 1)
        top.addWidget(pct, 0, Qt.AlignRight)

        # second line: the reset hint gets its own full-width row, so long
        # German strings ("Zurücksetzung in 3 Std. 53 Min.") are never clipped
        reset = QLabel("")
        reset.setObjectName("reset")
        reset.setWordWrap(True)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)

        holder.addLayout(top)
        holder.addWidget(reset)
        holder.addWidget(bar)
        holder.addSpacing(6)
        self._lay.insertLayout(idx, holder)
        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(600)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        row = {"name": name, "reset": reset, "pct": pct, "bar": bar, "anim": anim}
        self._rows[key] = row
        return row

    def retranslate(self):
        self._title.setText(tr("panel_title", plan=PLAN_NAME))
        self.history_title.setText(tr("history_title"))
        self.task_title.setText(tr("task_title"))
        if self._snap is not None:    # re-render the cost/project lines in the
            self._update_extras(self._snap)   # new language right away
        self.set_task(self._task_ctx, self._work_since)

    def set_history(self, series):
        self._history = list(series)

    def set_task(self, ctx, work_since=None):
        """Update the 'what Clawd is working on' section from a SessionContext.

        work_since is a time.monotonic() stamp of when the current working
        phase began (or None); the panel derives the live '· M:SS' turn timer
        from it and ticks it every second while visible.
        """
        self._task_ctx = ctx
        self._work_since = work_since
        if ctx is None or not (ctx.task or ctx.kind):
            self._task_action = ""       # else _refresh_countdown re-shows a stale line
            for w in self._task_widgets:
                w.setVisible(False)
            self._relayout()
            return
        self.task_project.setText(
            tr("task_project", name=ctx.project) if ctx.project else "")
        self.task_project.setVisible(bool(ctx.project))
        self.task_prompt.setText(tr("task_quote", s=ctx.task) if ctx.task else "")
        self.task_prompt.setVisible(bool(ctx.task))
        if ctx.kind == "working":
            line = tool_action(ctx.tool, ctx.detail)
            self._task_action = "⚙ " + line if line else "⚙"
        elif ctx.kind == "waiting":
            self._task_action = "✓ " + tr("task_waiting")
        else:
            self._task_action = ""
        self._render_task_activity()
        self.task_title.setVisible(True)
        self.task_div.setVisible(True)
        self._relayout()

    def _render_task_activity(self):
        """Compose the activity line with the live turn timer (no relayout, so
        the per-second tick never resizes or jitters the card)."""
        text = self._task_action
        ctx = self._task_ctx
        if (text and ctx is not None and ctx.kind == "working"
                and self._work_since is not None):
            text += " · " + _fmt_dur(time.monotonic() - self._work_since)
        self.task_activity.setText(text)
        self.task_activity.setVisible(bool(text))

    def _relayout(self):
        # rows and the task section are shown/hidden dynamically — force a full
        # re-layout before resizing, otherwise sizeHint() is stale and the card
        # gets squashed
        self._lay.invalidate()
        self._lay.activate()
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    def _show_only(self, keys):
        """Hide rows that the current snapshot does not provide, so switching
        between API and log mode never leaves a stale duplicate row behind."""
        for key, row in self._rows.items():
            visible = key in keys
            for part in ("name", "reset", "pct", "bar"):
                row[part].setVisible(visible)

    @staticmethod
    def _animate_row(row: dict, pct: float):
        row["pct"].setText(f"{pct:.0f} %")
        color = MOOD_COLORS[mood_for_pct(pct)] if pct >= 80 else "#6879f8"
        row["pct"].setStyleSheet(f"color: {color};")
        row["bar"].setStyleSheet(
            "QProgressBar { background: #3a3833; border: none; border-radius: 2px; }"
            f"QProgressBar::chunk {{ border-radius: 2px; background: {color}; }}")
        target = max(0, min(100, int(round(pct))))
        anim = row["anim"]
        anim.stop()
        anim.setStartValue(row["bar"].value())
        anim.setEndValue(target)
        anim.start()

    # -------------------------------------------------- data updates

    def update_snapshot(self, snap: UsageSnapshot):
        self._snap = snap

        if snap.error:
            self.detail_label.setText(snap.error)
        elif snap.source == "api":
            for b in snap.buckets:
                self._animate_row(self._ensure_row(b.key, b.label), b.pct)
            self._show_only({b.key for b in snap.buckets})
            models = sorted(
                ((n, t) for n, t in snap.by_model.items() if n != "System" and t > 0),
                key=lambda kv: -kv[1])
            if models:
                parts = " · ".join(f"{n} {fmt_de(t)}" for n, t in models)
                self.detail_label.setText(tr("detail_local", parts=parts))
            else:
                self.detail_label.setText("")
        else:
            row = self._ensure_row("estimate", tr("row_5h"))
            self._animate_row(row, snap.pct)
            row["pct"].setText(fmt_pct_de(snap.pct))
            keys = {"estimate"}

            # per-model breakdown of the same 5-hour window (Fable, Opus, …)
            budget = effective_max_tokens()
            models = sorted(
                ((n, t) for n, t in snap.by_model.items() if n != "System" and t > 0),
                key=lambda kv: -kv[1])
            for name, tok in models:
                mkey = f"model:{name}"
                mrow = self._ensure_row(mkey, name)
                wtok = snap.by_model_weighted.get(name, 0.0)
                share = (wtok / budget * 100.0) if budget > 0 else 0.0
                self._animate_row(mrow, share)
                mrow["reset"].setText(tr("tokens_inout", n=fmt_de(tok)))
                keys.add(mkey)

            # weekly limits, estimated from the same logs
            wtail = ("  ·  " + _fmt_reset(snap.week_reset)
                     if snap.week_reset is not None else "  ·  " + tr("rolling7"))
            wk = self._ensure_row("week_all", tr("row_week_all"))
            wk["reset"].setText(tr("tokens_n", n=fmt_de(snap.week_total)) + wtail)
            wbudget = weekly_budget_all()
            if wbudget:
                self._animate_row(wk, snap.week_weighted / wbudget * 100.0)
            keys.add("week_all")
            for name, mb in weekly_model_budgets().items():
                tokens = snap.week_by_model.get(name, 0)
                wtok = snap.week_by_model_weighted.get(name, 0.0)
                mkey = f"week:{name}"
                mrow = self._ensure_row(mkey, tr("row_week_model", name=name))
                self._animate_row(mrow, wtok / mb * 100.0 if mb else 0.0)
                mrow["reset"].setText(tr("tokens_n", n=fmt_de(tokens)) + wtail)
                keys.add(mkey)
            self._show_only(keys)
            if not wbudget:
                # no learned weekly budget yet: show only the token count, not a
                # percentage bar that can never fill
                self._rows["week_all"]["pct"].setVisible(False)
                self._rows["week_all"]["bar"].setVisible(False)

            hint = (tr("hint_manual") if is_calibrated()
                    else tr("hint_auto") if auto_budget_active()
                    else tr("hint_placeholder"))
            self.detail_label.setText(
                tr("detail_used", n=fmt_de(snap.total), hint=hint))
        self.detail_label.setVisible(bool(self.detail_label.text()))
        self._update_extras(snap)
        self._update_forecast(snap)
        self.history_chart.set_series(self._history)
        self.history_title.setVisible(len(self._history) >= 2)
        if snap.updated_at:
            src = tr("src_live") if snap.source == "api" else tr("src_local")
            self.updated_label.setText(
                tr("updated", t=snap.updated_at.strftime("%H:%M:%S"), src=src))
        self._refresh_countdown()
        self._relayout()

    def _update_extras(self, snap: UsageSnapshot):
        """Cost-equivalent + per-project lines (shown in log AND api mode —
        the local log counting runs underneath the live sync either way)."""
        if snap.error or snap.weighted <= 0:
            self.cost_label.setText("")
        else:
            # weighted units are Sonnet-input-token equivalents, so the public
            # Sonnet input price converts them into an approximate API price.
            # USD amounts keep the US decimal format in both languages.
            c5 = f"${snap.weighted / 1e6 * SONNET_INPUT_USD_PER_MTOK:,.2f}"
            cw = f"${snap.week_weighted / 1e6 * SONNET_INPUT_USD_PER_MTOK:,.2f}"
            self.cost_label.setText(tr("cost_line", c5=c5, cw=cw))
        self.cost_label.setVisible(bool(self.cost_label.text()))

        parts = ""
        if not snap.error and snap.weighted > 0:
            # top 3 projects by weighted share of the current 5-hour window;
            # "?" (no cwd found) is noise, not a project — never shown
            top = sorted(((n, w) for n, w in snap.by_project_weighted.items()
                          if n != "?" and w > 0), key=lambda kv: -kv[1])[:3]
            parts = " · ".join(
                f"{n} {round(w / snap.weighted * 100)}%" for n, w in top)
        self.projects_label.setText(
            tr("projects_line", parts=parts) if parts else "")
        self.projects_label.setVisible(bool(self.projects_label.text()))

    def _update_forecast(self, snap: UsageSnapshot):
        """Burn-rate line: projected time of hitting the 5-hour limit."""
        reset_at = None
        if snap.source == "api":
            for b in snap.buckets:
                if b.key == "five_hour":
                    reset_at = b.resets_at
        elif snap.oldest is not None:
            reset_at = snap.oldest + timedelta(hours=WINDOW_HOURS)
        eta = snap.burn_eta
        if eta is None or snap.error or snap.pct >= 100.0:
            self.forecast_label.setText("")
        elif reset_at is not None and eta >= reset_at:
            self.forecast_label.setText(tr("forecast_ok"))
        else:
            self.forecast_label.setText(
                tr("forecast_eta", t=eta.astimezone().strftime("%H:%M")))
        self.forecast_label.setVisible(bool(self.forecast_label.text()))

    def _refresh_countdown(self):
        self._render_task_activity()     # tick the turn timer while visible
        snap = self._snap
        if snap is None:
            return
        if snap.source == "api":
            for b in snap.buckets:
                row = self._rows.get(b.key)
                if row is not None:
                    row["reset"].setText(_fmt_reset(b.resets_at))
        else:
            row = self._rows.get("estimate")
            if row is None:
                return
            if snap.oldest is None:
                row["reset"].setText("")
            else:
                row["reset"].setText(
                    _fmt_reset(snap.oldest + timedelta(hours=WINDOW_HOURS)))

    # -------------------------------------------------- show / hide

    def target_geometry(self, pet: QWidget):
        """Position next to the pet: right side preferred, left as fallback."""
        self.adjustSize()
        screen = QGuiApplication.screenAt(pet.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        pet_geo = pet.frameGeometry()

        x = pet_geo.right() + 6
        side = 1
        if x + self.width() > avail.right():
            x = pet_geo.left() - 6 - self.width()
            side = -1
        y = pet_geo.center().y() - self.height() // 2
        y = max(avail.top() + 8, min(y, avail.bottom() - self.height() - 8))
        return QPoint(x, y), side

    def show_for(self, pet: QWidget, pinned: bool):
        self.pinned = pinned or self.pinned
        target, side = self.target_geometry(pet)
        if self._anim is not None:
            self._anim.stop()   # a running fade-out would leave opacity at 0
            self._anim.deleteLater()
            self._anim = None
        self._hiding = False
        if self.isVisible():
            self.setWindowOpacity(1.0)
            self.move(target)
            return
        start = QPoint(target.x() - side * self.SLIDE_PX, target.y())
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()

        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(240)
        pos_anim.setStartValue(start)
        pos_anim.setEndValue(target)
        pos_anim.setEasingCurve(QEasingCurve.OutCubic)

        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(240)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)

        self._anim = QParallelAnimationGroup(self)
        self._anim.addAnimation(pos_anim)
        self._anim.addAnimation(fade)
        self._anim.start()

    def hide_animated(self):
        if not self.isVisible() or self._hiding:
            return
        if self._anim is not None:
            self._anim.stop()   # don't fight a still-running slide-in
            self._anim.deleteLater()
            self._anim = None
        self._hiding = True
        self.pinned = False
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(180)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.finished.connect(self._finish_hide)
        self._anim = QParallelAnimationGroup(self)
        self._anim.addAnimation(fade)
        self._anim.start()

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self.setWindowOpacity(1.0)
            self._hiding = False

    def reposition(self, pet: QWidget):
        if self.isVisible():
            target, _ = self.target_geometry(pet)
            self.move(target)

    # -------------------------------------------------- events

    def showEvent(self, event):
        self._countdown_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._countdown_timer.stop()
        super().hideEvent(event)

    def leaveEvent(self, event):
        if self.on_leave:
            self.on_leave()
        super().leaveEvent(event)
