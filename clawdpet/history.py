"""Local usage history (JSON file) + sparkline chart."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

from .config import (
    HISTORY_FILE,
    HISTORY_GAP_S,
    HISTORY_INTERVAL_S,
    HISTORY_KEEP_DAYS,
    HISTORY_WINDOW_H,
)
from .usage import _parse_iso_ts

class HistoryStore:
    """Append-only local usage history for the panel sparkline (JSON file)."""

    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self._points = self._load()       # list[(datetime utc, pct)]
        # honour the throttle across restarts: the newest on-disk point counts
        self._last_write: Optional[datetime] = (
            self._points[-1][0] if self._points else None)

    def _load(self) -> list:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for it in raw if isinstance(raw, list) else []:
            if not isinstance(it, dict):
                continue
            ts = _parse_iso_ts(it.get("t"))
            if ts is None:
                continue
            try:
                out.append((ts, float(it.get("pct", 0.0))))
            except (TypeError, ValueError):
                pass
        out.sort(key=lambda p: p[0])
        return out

    def add(self, now: datetime, pct: float) -> bool:
        """Record a point, throttled to HISTORY_INTERVAL_S. True if stored."""
        if (self._last_write is not None and
                (now - self._last_write).total_seconds() < HISTORY_INTERVAL_S):
            return False
        self._points.append((now, pct))
        self._last_write = now
        cutoff = now - timedelta(days=HISTORY_KEEP_DAYS)
        self._points = [p for p in self._points if p[0] >= cutoff]
        self._save()
        return True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                [{"t": t.isoformat(), "pct": round(p, 2)}
                 for t, p in self._points]), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def series(self, window_h: int = HISTORY_WINDOW_H,
               now: Optional[datetime] = None) -> list:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=window_h)
        return [p for p in self._points if p[0] >= cutoff]


class HistoryChart(QWidget):
    """Compact area sparkline of the 5-hour usage pct over the last day."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self.setFixedHeight(54)

    def set_series(self, series) -> None:
        self._series = list(series)
        self.setVisible(len(self._series) >= 2)
        self.update()

    def paintEvent(self, _event):
        if len(self._series) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        pad = 2.0
        t0 = self._series[0][0]
        span = (self._series[-1][0] - t0).total_seconds() or 1.0

        def px(t):
            return pad + (t - t0).total_seconds() / span * (w - 2 * pad)

        def py(pct):
            v = max(0.0, min(100.0, pct))
            return h - pad - v / 100.0 * (h - 2 * pad)

        # 80 % warning guide
        p.setPen(QPen(QColor("#6b5836"), 1, Qt.DashLine))
        y80 = py(80.0)
        p.drawLine(int(pad), int(y80), int(w - pad), int(y80))

        # split into segments so long gaps (pet was off) are not bridged
        segments, prev_t = [[]], None
        for t, pct in self._series:
            if prev_t is not None and (t - prev_t).total_seconds() > HISTORY_GAP_S:
                segments.append([])
            segments[-1].append((px(t), py(pct)))
            prev_t = t

        line_pen = QPen(QColor("#6879f8"), 2)
        line_pen.setCapStyle(Qt.RoundCap)
        line_pen.setJoinStyle(Qt.RoundJoin)
        for seg in segments:
            if len(seg) < 2:
                if seg:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#6879f8"))
                    x, y = seg[0]
                    p.drawEllipse(QRectF(x - 1.6, y - 1.6, 3.2, 3.2))
                continue
            path = QPainterPath()
            path.moveTo(seg[0][0], seg[0][1])
            for x, y in seg[1:]:
                path.lineTo(x, y)
            fill = QPainterPath(path)
            fill.lineTo(seg[-1][0], h - pad)
            fill.lineTo(seg[0][0], h - pad)
            fill.closeSubpath()
            p.fillPath(fill, QColor(104, 121, 248, 40))
            p.strokePath(path, line_pen)
        p.end()
