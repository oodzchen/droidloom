"""Main settings window with sidebar rail, content header, and bottom status bar."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .icons import load_brand_pixmap
from .page_apps_apk import AppsApkPage
from .page_diagnostics import DiagnosticsPage
from .page_display_orientation import DisplayOrientationPage
from .page_hardware import HardwarePage
from .page_network import NetworkPage
from .page_overview import OverviewPage
from .page_storage import StoragePage
from .widgets import (
    ElidingLabel,
    StatusIndicator,
    apply_window_theme,
    format_bytes,
    make_splitter,
    wait_all_workers,
    wrap_scroll_page,
)

PAGE_KEYS = [
    ("overview", "概览"),
    ("network", "网络"),
    ("storage", "存储"),
    ("hardware", "硬件"),
    ("display", "显示"),
    ("apps", "应用"),
    ("diagnostics", "诊断"),
]


class MainWindow(QtWidgets.QMainWindow):
    """Droidloom Settings primary GUI window."""

    def __init__(
        self, initial_page: str = "overview", initial_package: str | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle("Droidloom 设置")
        self.setMinimumSize(560, 420)
        self.resize(1100, 760)
        self.setAcceptDrops(True)

        self._tokens = apply_window_theme(self)

        # Left navigation rail pane
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebarPane")
        sidebar.setMinimumWidth(116)
        sidebar.setMaximumWidth(230)
        sb_lay = QtWidgets.QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(10, 14, 10, 12)
        sb_lay.setSpacing(10)

        brand_row = QtWidgets.QHBoxLayout()
        brand_row.setContentsMargins(4, 2, 4, 6)
        brand_row.setSpacing(8)
        brand_pm = load_brand_pixmap(20, self.devicePixelRatioF())
        if not brand_pm.isNull():
            icon_lbl = QtWidgets.QLabel()
            icon_lbl.setPixmap(brand_pm)
            icon_lbl.setFixedSize(20, 20)
            brand_row.addWidget(icon_lbl)
        brand_lbl = QtWidgets.QLabel("Droidloom")
        brand_lbl.setObjectName("brandTitle")
        brand_row.addWidget(brand_lbl)
        brand_row.addStretch(1)
        sb_lay.addLayout(brand_row)

        self.nav = QtWidgets.QListWidget()
        self.nav.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._apply_nav_style()
        for _key, label in PAGE_KEYS:
            self.nav.addItem(label)
        sb_lay.addWidget(self.nav, 1)

        # Right content pane (compact header + scrollable page stack)
        content_pane = QtWidgets.QWidget()
        content_pane.setMinimumWidth(360)
        cp_lay = QtWidgets.QVBoxLayout(content_pane)
        cp_lay.setContentsMargins(0, 0, 0, 0)
        cp_lay.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("contentHeader")
        hdr_lay = QtWidgets.QHBoxLayout(header)
        hdr_lay.setContentsMargins(20, 12, 20, 10)
        self.page_title_lbl = QtWidgets.QLabel("概览")
        self.page_title_lbl.setObjectName("pageTitle")
        quick_apk_btn = QtWidgets.QPushButton("安装 APK")
        quick_apk_btn.clicked.connect(self._quick_install_apk)
        hdr_lay.addWidget(self.page_title_lbl)
        hdr_lay.addStretch(1)
        hdr_lay.addWidget(quick_apk_btn)
        cp_lay.addWidget(header)

        self.stack = QtWidgets.QStackedWidget()
        self.page_overview = OverviewPage()
        self.page_network = NetworkPage()
        self.page_storage = StoragePage()
        self.page_hardware = HardwarePage()
        self.page_display = DisplayOrientationPage()
        self.page_apps = AppsApkPage()
        self.page_diagnostics = DiagnosticsPage()

        self._pages = [
            self.page_overview,
            self.page_network,
            self.page_storage,
            self.page_hardware,
            self.page_display,
            self.page_apps,
            self.page_diagnostics,
        ]
        for p in self._pages:
            self.stack.addWidget(wrap_scroll_page(p))
        cp_lay.addWidget(self.stack, 1)

        splitter = make_splitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        splitter.addWidget(content_pane)
        splitter.setSizes([148, 952])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # Clean bottom status bar
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        sb.setContentsMargins(10, 2, 10, 2)

        self.status_indicator = StatusIndicator("检查中...", "neutral")
        sb.addWidget(self.status_indicator)

        div = QtWidgets.QFrame()
        div.setObjectName("statusDivider")
        div.setFixedSize(1, 14)
        sb.addWidget(div)

        self.status_detail_lbl = ElidingLabel("正在获取运行状态...", muted=True)
        sb.addWidget(self.status_detail_lbl, 1)

        self.status_hint_lbl = QtWidgets.QLabel("")
        self.status_hint_lbl.setProperty("muted", True)
        sb.addPermanentWidget(self.status_hint_lbl)

        self.nav.currentRowChanged.connect(self._on_page_changed)
        self.page_overview.status_updated.connect(self._on_status_updated)
        self.page_overview.status_hint.connect(self._on_status_hint)
        self.page_network.status_hint.connect(self._on_status_hint)

        idx = next((i for i, (k, _) in enumerate(PAGE_KEYS) if k == initial_page), 0)
        self.nav.setCurrentRow(idx)
        if initial_package:
            self.page_apps.select_package(initial_package)

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(3000)
        self._poll_timer.timeout.connect(self.page_overview.refresh_all)
        self._poll_timer.start()

    def _apply_nav_style(self) -> None:
        t = self._tokens
        self.nav.setStyleSheet(
            f"""
            QListWidget {{
                border: none;
                background: transparent;
                padding: 0px;
                outline: 0;
            }}
            QListWidget::item {{
                padding: 9px 12px;
                border-radius: 6px;
                margin-bottom: 3px;
                color: palette(text);
                background: transparent;
            }}
            QListWidget::item:hover:!selected {{
                background: {t['hover_bg']};
                color: palette(text);
            }}
            QListWidget::item:selected,
            QListWidget::item:selected:active,
            QListWidget::item:selected:!active {{
                background: {t['accent_bg']};
                color: {t['accent_fg']};
                font-weight: 600;
            }}
            """
        )

    def _on_page_changed(self, row: int) -> None:
        if 0 <= row < len(self._pages):
            self.stack.setCurrentIndex(row)
            self.page_title_lbl.setText(PAGE_KEYS[row][1])
            page = self._pages[row]
            if hasattr(page, "ensure_loaded"):
                page.ensure_loaded()

    def _on_status_hint(self, text: str) -> None:
        self.status_hint_lbl.setText(text)
        self.status_hint_lbl.setVisible(bool(text))

    def _on_status_updated(self, state: str, stats: dict) -> None:
        adb_ok = stats.get("adb_ok")
        adb_str = ""
        if adb_ok is True:
            adb_str = "ADB 已连接"
        elif adb_ok is False:
            adb_str = "ADB 未连接"

        if state == "running":
            ready = bool(stats.get("ready"))
            self.status_indicator.set_status("运行中" if ready else "启动中", "ok" if ready else "warn")
            parts: list[str] = []
            ver = stats.get("android_version")
            if ver:
                parts.append(f"Android {ver}")
            parts.append("框架就绪" if ready else "框架启动中")
            mem = format_bytes(stats.get("memory_bytes"))
            if mem != "—":
                parts.append(f"内存 {mem}")
            procs = stats.get("processes")
            if procs is not None:
                apps = stats.get("app_processes", "?")
                parts.append(f"进程 {procs} (应用 {apps})")
            if adb_str:
                parts.append(adb_str)
            self.status_detail_lbl.setText("  ·  ".join(parts))
        else:
            self.status_indicator.set_status("已停止", "neutral")
            tail = f"  ·  {adb_str}" if adb_str else ""
            self.status_detail_lbl.setText(f"容器未运行{tail}")

    def _quick_install_apk(self) -> None:
        idx = next((i for i, (k, _) in enumerate(PAGE_KEYS) if k == "apps"), 5)
        self.nav.setCurrentRow(idx)
        self.page_apps._choose_apk_files()

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(
                    (".apk", ".apks", ".xapk", ".apkm")
                ):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.page_apps.open_apk_installer_dialog(paths)

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.PaletteChange:
            self._tokens = apply_window_theme(self)
            self._apply_nav_style()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._poll_timer.stop()
        wait_all_workers(4000)
        super().closeEvent(event)
