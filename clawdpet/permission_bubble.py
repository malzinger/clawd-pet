"""Permission bubble — Allow/Deny callout for Claude Code permission prompts.

Shown when the (opt-in) PermissionRequest hook asks the pet whether a tool
call may run. The user clicks Allow or Deny; no click within DECIDE_S sends
"pass", which makes the hook fall back to Claude Code's normal terminal
prompt. Exactly one question is shown at a time — a second query arriving
while one is open is not acknowledged, so it too falls back to the terminal.
"""
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr

DECIDE_S = 14.0            # auto-"pass" before the hook's own 15 s runs out

_QSS = """
QFrame#permcard {
    background-color: rgba(38, 37, 35, 250);
    border: 1px solid #3d3b38;
    border-radius: 10px;
}
QLabel { color: #eceae6; background: transparent; border: none;
         font-size: 12px; }
QLabel#detail { color: #9b9892; font-size: 11px; }
QPushButton {
    border: 1px solid #3d3b38; border-radius: 6px;
    padding: 4px 14px; font-size: 12px; font-weight: 600;
    background: #3a3833; color: #eceae6;
}
QPushButton#allow { background: #1f4d2e; }
QPushButton#allow:hover { background: #2a6b3f; }
QPushButton#deny:hover { background: #6b2a2a; }
"""


class PermissionBubble(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setStyleSheet(_QSS)

        self.active = False
        self._on_decide = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        card = QFrame(self)
        card.setObjectName("permcard")
        outer.addWidget(card)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        self._title = QLabel("")
        lay.addWidget(self._title)
        self._detail = QLabel("")
        self._detail.setObjectName("detail")
        self._detail.setWordWrap(True)
        lay.addWidget(self._detail)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_allow = QPushButton(tr("perm_allow"))
        self._btn_allow.setObjectName("allow")
        self._btn_allow.clicked.connect(lambda: self.decide("allow"))
        self._btn_deny = QPushButton(tr("perm_deny"))
        self._btn_deny.setObjectName("deny")
        self._btn_deny.clicked.connect(lambda: self.decide("deny"))
        row.addWidget(self._btn_allow)
        row.addWidget(self._btn_deny)
        lay.addLayout(row)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self.decide("pass"))

    def ask(self, tool: str, detail: str, pet: QWidget, on_decide,
            window_s: float = DECIDE_S) -> None:
        """Show the question above the pet; on_decide('allow'|'deny'|'pass')
        fires exactly once. window_s widens the auto-'pass' when a remote
        approval channel also has to have a chance to answer."""
        self._on_decide = on_decide
        self.active = True
        self._btn_allow.setText(tr("perm_allow"))
        self._btn_deny.setText(tr("perm_deny"))
        self._title.setText(tr("perm_question", tool=tool or "?"))
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
        self.adjustSize()
        self._follow(pet)
        self.show()
        if sys.platform != "darwin":
            # raise_() can activate the whole app on macOS (focus steal);
            # WindowStaysOnTopHint already keeps the callout above everything
            self.raise_()
        self._timeout.start(int(max(1.0, window_s) * 1000))

    def _follow(self, pet: QWidget) -> None:
        from PyQt5.QtGui import QGuiApplication
        geo = pet.frameGeometry()
        screen = (QGuiApplication.screenAt(geo.center())
                  or QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        x = geo.center().x() - self.width() // 2
        x = max(avail.left() + 4, min(x, avail.right() - self.width() - 4))
        y = max(avail.top() + 4, geo.top() - self.height() - 4)
        self.move(x, y)

    def decide(self, decision: str) -> None:
        """Resolve the open question (idempotent; later calls are no-ops)."""
        if not self.active:
            return
        self.active = False
        self._timeout.stop()
        self.hide()
        cb, self._on_decide = self._on_decide, None
        if cb is not None:
            cb(decision)
