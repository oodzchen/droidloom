"""Shared Qt widgets, theme tokens, background task runner, and formatting helpers."""

from __future__ import annotations

from typing import Any, Callable

import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets


_ACTIVE_THREADS: set["WorkerThread"] = set()


class WorkerThread(QtCore.QThread):
    """Run a callable off the GUI thread and emit its result or error."""

    finished_ok = QtCore.Signal(object)
    finished_err = QtCore.Signal(str)
    progress_msg = QtCore.Signal(str)

    def __init__(self, fn: Callable[..., Any], *args: Any, pass_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._pass_progress = pass_progress
        _ACTIVE_THREADS.add(self)
        self.finished.connect(self._on_thread_finished)

    def _on_thread_finished(self) -> None:
        _ACTIVE_THREADS.discard(self)

    def run(self) -> None:
        try:
            if self._pass_progress:
                res = self._fn(*self._args, on_progress=self.progress_msg.emit, **self._kwargs)
            else:
                res = self._fn(*self._args, **self._kwargs)
            self.finished_ok.emit(res)
        except Exception as exc:
            self.finished_err.emit(str(exc))


def wait_all_workers(timeout_ms: int = 5000) -> None:
    """Wait for active WorkerThread instances before application exit."""
    for th in list(_ACTIVE_THREADS):
        if th.isRunning():
            th.wait(timeout_ms)
    _ACTIVE_THREADS.clear()


def spawn_worker(
    owner: QtCore.QObject,
    pool: list[WorkerThread],
    fn: Callable[..., Any],
    on_ok: Callable[[Any], Any],
    on_err: Callable[[str], Any] | None = None,
) -> WorkerThread:
    """Run fn in WorkerThread and invoke callbacks only while owner is still alive."""
    w = WorkerThread(fn)
    pool.append(w)

    def _safe_ok(res: Any) -> None:
        if shiboken6.isValid(owner):
            on_ok(res)

    def _safe_err(msg: str) -> None:
        if on_err is not None and shiboken6.isValid(owner):
            on_err(msg)

    def _cleanup() -> None:
        if w in pool:
            pool.remove(w)

    w.finished_ok.connect(_safe_ok)
    if on_err is not None:
        w.finished_err.connect(_safe_err)
    w.finished.connect(_cleanup)
    w.start()
    return w


def format_bytes(num: int | float | None) -> str:
    if num is None:
        return "—"
    val = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(val) < 1024.0 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TiB"


def format_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    total_s = int(ms // 1000)
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{ms / 1000.0:.1f}s"


def _rel_luminance(color: QtGui.QColor) -> float:
    def _lin(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * _lin(color.redF())
        + 0.7152 * _lin(color.greenF())
        + 0.0722 * _lin(color.blueF())
    )


def _contrast_ratio(c1: QtGui.QColor, c2: QtGui.QColor) -> float:
    l1, l2 = _rel_luminance(c1), _rel_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def compute_theme_tokens(palette: QtGui.QPalette) -> dict[str, str]:
    """Compute palette-aware light/dark surface, border, text, and WCAG accent tokens."""
    win = palette.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.Window)
    base = palette.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.Base)
    is_dark = win.lightness() < 128

    hl_bg = palette.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.Highlight)
    hl_fg = palette.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.HighlightedText)
    if _contrast_ratio(hl_bg, hl_fg) < 4.5:
        white = QtGui.QColor("#ffffff")
        dark = QtGui.QColor("#111418")
        if _contrast_ratio(hl_bg, dark) >= 4.5:
            hl_fg = dark
        else:
            candidate = QtGui.QColor(hl_bg)
            for _ in range(8):
                if _contrast_ratio(candidate, white) >= 4.8:
                    break
                candidate = candidate.darker(115)
            hl_bg = candidate
            hl_fg = white

    accent_hover = hl_bg.lighter(110) if is_dark else hl_bg.darker(110)

    if is_dark:
        return {
            "is_dark": "1",
            "sidebar_bg": win.darker(108).name(),
            "card_bg": win.lighter(110).name(),
            "tile_bg": win.lighter(105).name(),
            "input_bg": base.name(),
            "border_subtle": "rgba(255, 255, 255, 26)",
            "border_strong": "rgba(255, 255, 255, 48)",
            "muted_text": "#9aa4b2",
            "hover_bg": "rgba(255, 255, 255, 18)",
            "accent_bg": hl_bg.name(),
            "accent_fg": hl_fg.name(),
            "accent_hover": accent_hover.name(),
            "danger_fg": "#f87171",
            "danger_border": "rgba(248, 113, 113, 110)",
            "danger_hover": "rgba(239, 68, 68, 36)",
        }
    return {
        "is_dark": "0",
        "sidebar_bg": win.darker(104).name(),
        "card_bg": base.name(),
        "tile_bg": win.darker(102).name(),
        "input_bg": base.name(),
        "border_subtle": "rgba(15, 23, 42, 26)",
        "border_strong": "rgba(15, 23, 42, 52)",
        "muted_text": "#5c6675",
        "hover_bg": "rgba(15, 23, 42, 14)",
        "accent_bg": hl_bg.name(),
        "accent_fg": hl_fg.name(),
        "accent_hover": accent_hover.name(),
        "danger_fg": "#b91c1c",
        "danger_border": "rgba(185, 28, 28, 95)",
        "danger_hover": "rgba(220, 38, 38, 22)",
    }


def apply_window_theme(widget: QtWidgets.QWidget) -> dict[str, str]:
    """Apply unified card, typography, button, table, and status-bar QSS on a top-level widget."""
    t = compute_theme_tokens(widget.palette())
    widget.setStyleSheet(
        f"""
        QFrame#sidebarPane {{
            background-color: {t['sidebar_bg']};
            border: none;
            border-right: 1px solid {t['border_subtle']};
        }}
        QFrame#contentHeader {{
            background-color: transparent;
            border: none;
            border-bottom: 1px solid {t['border_subtle']};
        }}
        QFrame#sectionCard {{
            background-color: {t['card_bg']};
            border: 1px solid {t['border_subtle']};
            border-radius: 8px;
        }}
        QFrame#subCard {{
            background-color: {t['tile_bg']};
            border: 1px solid {t['border_subtle']};
            border-radius: 6px;
        }}
        QFrame#rowDivider {{
            background-color: {t['border_subtle']};
            border: none;
        }}
        QFrame#resizableBox {{
            background-color: {t['input_bg']};
            border: 1px solid {t['border_strong']};
            border-radius: 6px;
        }}
        QFrame#statusDivider {{
            background-color: {t['border_strong']};
            border: none;
        }}
        QLabel#sectionTitle {{
            font-size: 13px;
            font-weight: 600;
            color: palette(text);
            background: transparent;
        }}
        QLabel#pageTitle {{
            font-size: 16px;
            font-weight: 700;
            color: palette(text);
            background: transparent;
        }}
        QLabel#brandTitle {{
            font-size: 14px;
            font-weight: 700;
            color: palette(text);
            background: transparent;
        }}
        QLabel[muted="true"] {{
            color: {t['muted_text']};
            background: transparent;
        }}
        QPushButton {{
            min-height: 28px;
            padding: 3px 14px;
            border-radius: 6px;
            border: 1px solid {t['border_strong']};
            background-color: {t['card_bg']};
        }}
        QPushButton:hover {{
            background-color: {t['hover_bg']};
        }}
        QPushButton:disabled {{
            opacity: 0.55;
        }}
        QPushButton[role="primary"] {{
            background-color: {t['accent_bg']};
            color: {t['accent_fg']};
            border: 1px solid {t['accent_bg']};
            font-weight: 600;
        }}
        QPushButton[role="primary"]:hover {{
            background-color: {t['accent_hover']};
        }}
        QPushButton[role="danger"] {{
            color: {t['danger_fg']};
            border: 1px solid {t['danger_border']};
            background-color: transparent;
        }}
        QPushButton[role="danger"]:hover {{
            background-color: {t['danger_hover']};
        }}
        QLineEdit {{
            min-height: 26px;
            padding: 2px 8px;
            border: 1px solid {t['border_strong']};
            border-radius: 5px;
            background-color: {t['input_bg']};
        }}
        QLineEdit:focus {{
            border-color: {t['accent_bg']};
        }}
        QComboBox, QSpinBox, QDoubleSpinBox {{
            min-height: 28px;
        }}
        QFrame#resizableBox > QTableWidget,
        QFrame#resizableBox > QPlainTextEdit {{
            border: none;
            background: transparent;
        }}
        QTableWidget, QTableView {{
            alternate-background-color: {t['tile_bg']};
            selection-background-color: {t['accent_bg']};
            selection-color: {t['accent_fg']};
            gridline-color: {t['border_subtle']};
        }}
        QHeaderView::section {{
            background-color: {t['tile_bg']};
            border: none;
            border-bottom: 1px solid {t['border_subtle']};
            border-right: 1px solid {t['border_subtle']};
            padding: 5px 8px;
            font-weight: 600;
        }}
        QStatusBar {{
            background-color: {t['sidebar_bg']};
            border-top: 1px solid {t['border_subtle']};
        }}
        QStatusBar::item {{
            border: none;
        }}
        """
    )
    return t


def set_button_role(btn: QtWidgets.QPushButton, role: str) -> QtWidgets.QPushButton:
    """Assign semantic role ('primary' or 'danger') to a button and polish its style."""
    btn.setProperty("role", role)
    btn.style().unpolish(btn)
    btn.style().polish(btn)
    return btn


def style_table(table: QtWidgets.QTableWidget) -> None:
    """Apply clean table defaults: hidden row numbers, alternating rows, row selection."""
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(30)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setShowGrid(False)


def make_form_layout() -> QtWidgets.QFormLayout:
    """Create a consistently aligned, expanding-field QFormLayout."""
    form = QtWidgets.QFormLayout()
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(10)
    form.setContentsMargins(0, 0, 0, 0)
    return form


class WrappingLabel(QtWidgets.QLabel):
    """QLabel subclass that accurately recalculates wrapped height across layout & resize passes."""

    def __init__(
        self,
        text: str = "",
        muted: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._updating_geom = False
        self.setWordWrap(True)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.setMinimumWidth(0)
        if muted:
            self.setProperty("muted", True)
        sp = QtWidgets.QSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _wrapped_height(self, width: int) -> int:
        txt = self.text()
        if not txt:
            return 0
        m = self.contentsMargins()
        avail = max(80, width - m.left() - m.right())
        fm = QtGui.QFontMetrics(self.font())
        rect = fm.boundingRect(
            QtCore.QRect(0, 0, avail, 10000),
            int(QtCore.Qt.TextFlag.TextWordWrap),
            txt,
        )
        return max(fm.height() + m.top() + m.bottom() + 2, rect.height() + m.top() + m.bottom() + 4)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._wrapped_height(width)

    def minimumSizeHint(self) -> QtCore.QSize:
        w = self.width() if self.width() > 40 else 280
        return QtCore.QSize(0, self._wrapped_height(w))

    def sizeHint(self) -> QtCore.QSize:
        w = self.width() if self.width() > 40 else 420
        return QtCore.QSize(w, self._wrapped_height(w))

    def _notify_geometry(self) -> None:
        if self._updating_geom:
            return
        self._updating_geom = True
        try:
            self.updateGeometry()
            p = self.parentWidget()
            depth = 0
            while p is not None and depth < 3:
                if p.layout() is not None:
                    p.layout().invalidate()
                p.updateGeometry()
                p = p.parentWidget()
                depth += 1
        finally:
            self._updating_geom = False

    def setText(self, text: str) -> None:
        super().setText(text)
        self._notify_geometry()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._notify_geometry()


class ElidingLabel(QtWidgets.QLabel):
    """Single-line label that elides overflow text on the right without forcing window width."""

    def __init__(
        self,
        text: str = "",
        muted: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        if muted:
            self.setProperty("muted", True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.setText(text)

    def full_text(self) -> str:
        return self._full_text

    def text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elision()

    def _apply_elision(self) -> None:
        avail = max(24, self.width() - 4) if self.width() > 12 else 480
        elided = self.fontMetrics().elidedText(
            self._full_text,
            QtCore.Qt.TextElideMode.ElideRight,
            avail,
        )
        super().setText(elided)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_elision()


def make_section_card(title: str, subtitle: str = "") -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    """Create a styled section card frame with consistent padding and typography."""
    box = QtWidgets.QFrame()
    box.setObjectName("sectionCard")
    box.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    title_lbl = QtWidgets.QLabel(title)
    title_lbl.setObjectName("sectionTitle")
    layout.addWidget(title_lbl)
    if subtitle:
        lbl = WrappingLabel(subtitle, muted=True)
        layout.addWidget(lbl)
    return box, layout


def wrap_scroll_page(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """Wrap a settings page widget inside an expanding, frameless QScrollArea."""
    if widget.layout() is not None:
        widget.layout().setContentsMargins(20, 16, 20, 20)
        widget.layout().setSpacing(14)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    scroll.setMinimumSize(200, 160)
    scroll.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Expanding,
    )
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(widget)
    return scroll


class GripSplitterHandle(QtWidgets.QSplitterHandle):
    """Minimal splitter handle with centered 3-dot grip indicator."""

    def __init__(self, orientation: QtCore.Qt.Orientation, parent: QtWidgets.QSplitter) -> None:
        super().__init__(orientation, parent)
        self._hovered = False
        self.setMouseTracking(True)

    def enterEvent(self, event: QtCore.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self._hovered:
            color = self.palette().highlight().color()
            color.setAlpha(220)
        else:
            color = self.palette().text().color()
            color.setAlpha(70)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(color)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = 1.4
        for offset in (-6.0, 0.0, 6.0):
            if self.orientation() == QtCore.Qt.Orientation.Horizontal:
                p.drawEllipse(QtCore.QPointF(cx, cy + offset), r, r)
            else:
                p.drawEllipse(QtCore.QPointF(cx + offset, cy), r, r)


class GripSplitter(QtWidgets.QSplitter):
    """QSplitter subclass using subtle 3-dot handles."""

    def createHandle(self) -> QtWidgets.QSplitterHandle:
        return GripSplitterHandle(self.orientation(), self)


def make_splitter(orientation: QtCore.Qt.Orientation) -> QtWidgets.QSplitter:
    """Create a non-collapsible GripSplitter."""
    splitter = GripSplitter(orientation)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(8)
    return splitter


class DragResizeHandle(QtWidgets.QWidget):
    """Bottom grip bar with textarea-style bottom-right diagonal stripes to drag-resize height."""

    def __init__(
        self,
        target: QtWidgets.QWidget,
        default_height: int = 160,
        min_height: int = 80,
        max_height: int = 1000,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._target = target
        self._default_height = default_height
        self._min_height = min_height
        self._max_height = max_height
        self._dragging = False
        self._hovered = False
        self._start_y = 0.0
        self._start_h = default_height
        self.setFixedHeight(16)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
        self.setToolTip("拖动右下角或底边调节高度（双击恢复默认）")
        target.setMinimumHeight(default_height)
        target.setMaximumHeight(default_height)

    def enterEvent(self, event: QtCore.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = True
            self._start_y = event.globalPosition().y()
            self._start_h = max(self._min_height, self._target.height())
            self.update()
            event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self._dragging:
            if event.position().x() >= max(0.0, self.width() - 22.0):
                self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
            else:
                self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
        if self._dragging:
            delta = int(round(event.globalPosition().y() - self._start_y))
            new_h = max(self._min_height, min(self._max_height, self._start_h + delta))
            self._target.setMinimumHeight(new_h)
            self._target.setMaximumHeight(new_h)
            self._target.updateGeometry()
            if self.parentWidget() is not None:
                self.parentWidget().updateGeometry()
            event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = False
            self.update()
            event.accept()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self._target.setMinimumHeight(self._default_height)
        self._target.setMaximumHeight(self._default_height)
        self._target.updateGeometry()
        if self.parentWidget() is not None:
            self.parentWidget().updateGeometry()
        event.accept()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Subtle top separator hairline
        line_col = self.palette().mid().color()
        line_col.setAlpha(110 if (self._hovered or self._dragging) else 55)
        p.setPen(QtGui.QPen(line_col, 1.0))
        p.drawLine(0, 0, w, 0)

        # Centered subtle pill indicator
        bar_col = self.palette().text().color()
        bar_col.setAlpha(110 if (self._hovered or self._dragging) else 45)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(bar_col)
        p.drawRoundedRect(QtCore.QRectF(w / 2.0 - 16, h / 2.0 - 1.0, 32, 2.0), 1.0, 1.0)

        # Bottom-right HTML <textarea>-style 3 diagonal resize stripes
        if self._hovered or self._dragging:
            grip_col = self.palette().highlight().color()
            grip_col.setAlpha(235)
        else:
            grip_col = self.palette().text().color()
            grip_col.setAlpha(125)
        pen = QtGui.QPen(grip_col, 1.35)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for span in (11, 7, 3):
            p.drawLine(
                QtCore.QPointF(w - 2 - span, h - 2),
                QtCore.QPointF(w - 2, h - 2 - span),
            )


def wrap_resizable(
    target: QtWidgets.QWidget,
    default_height: int = 160,
    min_height: int = 88,
    max_height: int = 1000,
) -> QtWidgets.QFrame:
    """Wrap a table, list, editor, or splitter in a bordered box with a textarea bottom-right resize grip."""
    box = QtWidgets.QFrame()
    box.setObjectName("resizableBox")
    lay = QtWidgets.QVBoxLayout(box)
    lay.setContentsMargins(1, 1, 1, 1)
    lay.setSpacing(0)
    if isinstance(target, QtWidgets.QFrame) and not isinstance(target, QtWidgets.QSplitter):
        target.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    lay.addWidget(target, 1)
    handle = DragResizeHandle(target, default_height=default_height, min_height=min_height, max_height=max_height)
    lay.addWidget(handle)
    box.resize_handle = handle  # type: ignore[attr-defined]
    return box


class _StatusDot(QtWidgets.QWidget):
    """Anti-aliased colored status dot with subtle halo."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QtGui.QColor("#94a3b8")
        self.setFixedSize(12, 12)

    def set_color(self, color: QtGui.QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        halo = QtGui.QColor(self._color)
        halo.setAlpha(42)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QtCore.QPointF(cx, cy), 5.0, 5.0)
        core = QtGui.QColor(self._color)
        core.setAlpha(255)
        p.setBrush(core)
        p.drawEllipse(QtCore.QPointF(cx, cy), 3.3, 3.3)


class StatusIndicator(QtWidgets.QWidget):
    """Borderless colored-dot + semantic text status indicator (no pill box background)."""

    _TONE_COLORS_DARK = {
        "ok": ("#22c55e", "#4ade80"),
        "warn": ("#f59e0b", "#fbbf24"),
        "err": ("#ef4444", "#f87171"),
        "info": ("#38bdf8", "#7dd3fc"),
        "neutral": ("#94a3b8", "#9aa4b2"),
    }
    _TONE_COLORS_LIGHT = {
        "ok": ("#16a34a", "#15803d"),
        "warn": ("#d97706", "#b45309"),
        "err": ("#dc2626", "#b91c1c"),
        "info": ("#0284c7", "#0369a1"),
        "neutral": ("#64748b", "#475569"),
    }

    def __init__(
        self,
        text: str = "",
        tone: str = "neutral",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tone = tone
        self._dot = _StatusDot(self)
        self._label = QtWidgets.QLabel(text, self)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(6)
        lay.addWidget(self._dot)
        lay.addWidget(self._label)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.set_status(text, tone)

    def text(self) -> str:
        return self._label.text()

    def tone(self) -> str:
        return self._tone

    def label(self) -> QtWidgets.QLabel:
        return self._label

    def set_status(self, text: str, tone: str = "neutral") -> None:
        self._tone = tone
        self._label.setText(text)
        self._apply_colors()

    def _apply_colors(self) -> None:
        win = self.palette().color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.Window)
        is_dark = win.lightness() < 128
        table = self._TONE_COLORS_DARK if is_dark else self._TONE_COLORS_LIGHT
        dot_hex, txt_hex = table.get(self._tone, table["neutral"])
        self._dot.set_color(QtGui.QColor(dot_hex))
        self._label.setStyleSheet(
            f"color: {txt_hex}; background: transparent; border: none; padding: 0; font-weight: 500;"
        )

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.PaletteChange:
            self._apply_colors()


def status_pill(text: str, tone: str = "neutral") -> StatusIndicator:
    """Create a colored-dot StatusIndicator (replaces legacy rounded pill badge)."""
    return StatusIndicator(text, tone)


def set_pill_tone(widget: StatusIndicator | QtWidgets.QLabel, text: str, tone: str) -> None:
    """Update a StatusIndicator (or QLabel fallback) with new text and semantic tone."""
    if isinstance(widget, StatusIndicator):
        widget.set_status(text, tone)
    else:
        widget.setText(text)


def pixmap_from_bytes(data: bytes | None, size: int = 48) -> QtGui.QPixmap:
    if data:
        pm = QtGui.QPixmap()
        if pm.loadFromData(data):
            return pm.scaled(
                size,
                size,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setBrush(QtGui.QColor("#3daee9"))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 10, 10)
    painter.setPen(QtGui.QColor("#ffffff"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(max(10, size // 3))
    painter.setFont(font)
    painter.drawText(pm.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "APK")
    painter.end()
    return pm


def show_message(parent: QtWidgets.QWidget, title: str, text: str, is_error: bool = False) -> None:
    if is_error:
        QtWidgets.QMessageBox.critical(parent, title, text)
    else:
        QtWidgets.QMessageBox.information(parent, title, text)
