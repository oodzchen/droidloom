"""APK Installer, MIME file association, and installed Android application manager page."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..backend.control_client import ControlClient
from ..backend.mime_assoc import (
    is_apk_mime_icon_installed,
    is_droidloom_default_apk_handler,
    query_default_apk_handler,
    set_droidloom_default_apk_handler,
)
from ..backend.user_configs import (
    WindowPreference,
    clear_remembered_window_size,
    get_desktop_launch_resolution,
    load_remembered_window_sizes,
    load_window_policy,
    save_window_policy,
    set_desktop_launch_resolution,
)
from .apk_install_dialog import ApkInstallDialog
from .widgets import (
    StatusIndicator,
    WorkerThread,
    WrappingLabel,
    format_bytes,
    make_form_layout,
    make_section_card,
    make_splitter,
    pixmap_from_bytes,
    set_button_role,
    set_pill_tone,
    show_message,
    spawn_worker,
    wrap_resizable,
)


class DropApkFrame(QtWidgets.QFrame):
    """Drag-and-drop zone for standalone APK files."""

    files_dropped = QtCore.Signal(list)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("dropApkZone")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setStyleSheet(
            "QFrame#dropApkZone { border: 1.5px dashed palette(highlight); border-radius: 8px; "
            "padding: 18px; background: palette(alternate-base); }"
        )
        lay = QtWidgets.QVBoxLayout(self)
        lay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(4)
        lbl = QtWidgets.QLabel("拖入 .apk 文件，或点击选择")
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        sub = QtWidgets.QLabel("支持单个 APK 安装与保留数据升级")
        sub.setProperty("muted", True)
        sub.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        lay.addWidget(sub)

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
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.isLocalFile()
        ]
        if paths:
            self.files_dropped.emit(paths)


class AppsApkPage(QtWidgets.QWidget):
    """APK installation, default .apk MIME association, and per-app window/lifecycle management."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = ControlClient()
        self._workers: list[WorkerThread] = []
        self._apps: list[dict] = []
        self._icons: dict[str, bytes] = {}
        self._selected_app: dict | None = None

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. System .apk MIME Association
        mime_box, mime_lay = make_section_card("关联")
        m_row = QtWidgets.QHBoxLayout()
        m_row.setSpacing(16)
        self.mime_pill = StatusIndicator("检测中...", "neutral")
        self.mime_detail_lbl = WrappingLabel("", muted=True)
        self.set_mime_btn = QtWidgets.QPushButton("设为默认")
        self.set_mime_btn.clicked.connect(self._set_default_mime)
        m_row.addWidget(self.mime_pill)
        m_row.addWidget(self.mime_detail_lbl, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
        m_row.addWidget(self.set_mime_btn)
        mime_lay.addLayout(m_row)
        root.addWidget(mime_box)

        # 2. Graphical APK Installer
        apk_box, apk_lay = make_section_card("安装")
        self.drop_zone = DropApkFrame()
        self.drop_zone.files_dropped.connect(self.open_apk_installer_dialog)
        apk_lay.addWidget(self.drop_zone)

        a_btns = QtWidgets.QHBoxLayout()
        choose_btn = set_button_role(QtWidgets.QPushButton("选择 APK..."), "primary")
        choose_btn.clicked.connect(self._choose_apk_files)
        a_btns.addWidget(choose_btn)
        a_btns.addStretch(1)
        apk_lay.addLayout(a_btns)
        root.addWidget(apk_box)

        # 3. Installed Android Apps & Per-App Policy
        apps_box, apps_lay = make_section_card("应用")
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("搜索应用或包名...")
        self.search_edit.textChanged.connect(self._filter_apps)
        refresh_btn = QtWidgets.QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_apps)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(refresh_btn)
        apps_lay.addLayout(bar)

        split = make_splitter(QtCore.Qt.Orientation.Horizontal)
        self.app_list = QtWidgets.QListWidget()
        self.app_list.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.app_list.setIconSize(QtCore.QSize(36, 36))
        self.app_list.currentItemChanged.connect(self._on_app_selected)
        split.addWidget(self.app_list)

        detail_panel = QtWidgets.QFrame()
        detail_panel.setObjectName("subCard")
        detail_panel.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        dp_lay = QtWidgets.QVBoxLayout(detail_panel)
        dp_lay.setContentsMargins(12, 10, 12, 10)
        dp_lay.setSpacing(10)

        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(10)
        self.det_icon = QtWidgets.QLabel()
        self.det_icon.setFixedSize(48, 48)
        self.det_title = QtWidgets.QLabel("选择左侧应用")
        f = self.det_title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.det_title.setFont(f)
        hdr.addWidget(self.det_icon)
        hdr.addWidget(self.det_title, 1)
        dp_lay.addLayout(hdr)

        self.det_info = WrappingLabel("包名 / 版本 / 存储", muted=True)
        dp_lay.addWidget(self.det_info)

        act_grid = QtWidgets.QGridLayout()
        act_grid.setSpacing(6)
        self.btn_launch = set_button_role(QtWidgets.QPushButton("启动"), "primary")
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_cache = QtWidgets.QPushButton("清缓存")
        self.btn_data = set_button_role(QtWidgets.QPushButton("清数据"), "danger")
        self.btn_sysinfo = QtWidgets.QPushButton("详情")
        self.btn_uninst = set_button_role(QtWidgets.QPushButton("卸载"), "danger")

        self.btn_launch.clicked.connect(self._launch_selected)
        self.btn_stop.clicked.connect(lambda: self._app_action("stop"))
        self.btn_cache.clicked.connect(lambda: self._app_action("clear_cache"))
        self.btn_data.clicked.connect(lambda: self._app_action("clear_data", confirm=True))
        self.btn_sysinfo.clicked.connect(lambda: self._app_action("settings"))
        self.btn_uninst.clicked.connect(lambda: self._app_action("uninstall", confirm=True))

        for i, b in enumerate(
            (
                self.btn_launch,
                self.btn_stop,
                self.btn_cache,
                self.btn_data,
                self.btn_sysinfo,
                self.btn_uninst,
            )
        ):
            act_grid.addWidget(b, i // 3, i % 3)
        dp_lay.addLayout(act_grid)

        # Per-app window & resolution controls
        per_app_card = QtWidgets.QFrame()
        per_app_card.setObjectName("sectionCard")
        per_app_card.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        pa_card_lay = QtWidgets.QVBoxLayout(per_app_card)
        pa_card_lay.setContentsMargins(12, 10, 12, 10)
        pa_card_lay.setSpacing(8)
        pa_title = QtWidgets.QLabel("窗口")
        pa_title.setObjectName("sectionTitle")
        pa_card_lay.addWidget(pa_title)

        pa_form = make_form_layout()
        self.pa_mode_combo = QtWidgets.QComboBox()
        self.pa_mode_combo.addItem("默认", "default")
        self.pa_mode_combo.addItem("窗口", "windowed")
        self.pa_mode_combo.addItem("铺满", "fit_output")

        self.pa_w_spin = QtWidgets.QSpinBox()
        self.pa_w_spin.setRange(100, 16384)
        self.pa_w_spin.setValue(960)
        self.pa_h_spin = QtWidgets.QSpinBox()
        self.pa_h_spin.setRange(100, 16384)
        self.pa_h_spin.setValue(680)
        self.pa_remembered_lbl = QtWidgets.QLabel("—")
        self.pa_remembered_lbl.setProperty("muted", True)
        self.pa_res_edit = QtWidgets.QLineEdit()
        self.pa_res_edit.setPlaceholderText("例如 2560x1440")

        pa_form.addRow("模式:", self.pa_mode_combo)
        pa_form.addRow("尺寸:", self._hbox(self.pa_w_spin, self.pa_h_spin))
        pa_form.addRow("记忆尺寸:", self.pa_remembered_lbl)
        pa_form.addRow("分辨率:", self.pa_res_edit)
        pa_card_lay.addLayout(pa_form)

        pa_btns = QtWidgets.QHBoxLayout()
        pa_btns.setSpacing(8)
        save_pa_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        reset_mem_btn = QtWidgets.QPushButton("清除记忆")
        save_pa_btn.clicked.connect(self._save_per_app_policy)
        reset_mem_btn.clicked.connect(self._clear_remembered_size)
        pa_btns.addWidget(save_pa_btn)
        pa_btns.addWidget(reset_mem_btn)
        pa_btns.addStretch(1)
        pa_card_lay.addLayout(pa_btns)
        dp_lay.addWidget(per_app_card)
        dp_lay.addStretch(1)

        split.addWidget(detail_panel)
        split.setSizes([280, 460])
        apps_lay.addWidget(wrap_resizable(split, default_height=360, min_height=220))
        root.addWidget(apps_box)
        root.addStretch(1)

        self._loaded = False
        self._pending_package: str | None = None
        self.refresh_mime_status()

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh_apps()

    def select_package(self, package: str) -> None:
        self._pending_package = package.strip() or None
        if self._pending_package is None:
            return
        for row in range(self.app_list.count()):
            item = self.app_list.item(row)
            app = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
            if isinstance(app, dict) and app.get("package") == self._pending_package:
                self.app_list.setCurrentRow(row)
                self._pending_package = None
                return

    def _hbox(self, w1: QtWidgets.QWidget, w2: QtWidgets.QWidget) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        l = QtWidgets.QHBoxLayout(box)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(w1)
        l.addWidget(QtWidgets.QLabel("×"))
        l.addWidget(w2)
        return box

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def refresh_mime_status(self) -> None:
        handler = query_default_apk_handler()
        is_default = is_droidloom_default_apk_handler()
        icon_ok = is_apk_mime_icon_installed()
        if is_default and icon_ok:
            set_pill_tone(self.mime_pill, "已设为默认 .apk 打开程序", "ok")
            self.mime_detail_lbl.setText(f"当前默认关联: {handler}（APK 文件图标已同步为 Droidloom）")
        elif is_default and not icon_ok:
            set_pill_tone(self.mime_pill, "已设为默认，但 APK 图标未同步", "warn")
            self.mime_detail_lbl.setText(
                f"当前默认关联: {handler}（点击“设为默认”按桌面标准同步 APK 图标）"
            )
        else:
            set_pill_tone(self.mime_pill, "未关联为默认打开程序", "warn")
            self.mime_detail_lbl.setText(f"当前默认关联: {handler or '(未设置)'}")

    def _set_default_mime(self) -> None:
        ok, msg = set_droidloom_default_apk_handler()
        self.refresh_mime_status()
        show_message(self, "文件关联", msg, is_error=not ok)

    def _choose_apk_files(self) -> None:
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择一个或多个 APK 安装包",
            str(Path.home() / "Downloads"),
            "Android Package (*.apk);;All Files (*)",
        )
        if files:
            self.open_apk_installer_dialog([Path(f) for f in files])

    def open_apk_installer_dialog(self, paths: list[Path]) -> None:
        dlg = ApkInstallDialog(paths, parent=self)
        dlg.settings_btn.setVisible(False)
        dlg.exec()
        self.refresh_apps()

    def refresh_apps(self) -> None:
        def fetch():
            apps = self.client.list_applications()
            comps = [a["component"] for a in apps if a.get("component")]
            icons = self.client.application_icons(comps, size=64) if comps else {}
            return apps, icons

        def done(data):
            self._apps, self._icons = data
            self._filter_apps()

        def err(_msg):
            self._apps = []
            self._icons = {}
            self.app_list.clear()
            self.det_title.setText("Droidloom 未运行或未就绪，无法读取应用列表")

        self._spawn(fetch, done, err)

    def _filter_apps(self) -> None:
        q = self.search_edit.text().strip().lower()
        self.app_list.clear()
        for app in sorted(self._apps, key=lambda a: a.get("name", "").lower()):
            name = app.get("name", "")
            pkg = app.get("package", "")
            if q and q not in name.lower() and q not in pkg.lower():
                continue
            item = QtWidgets.QListWidgetItem(f"{name}\n{pkg}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, app)
            pm = pixmap_from_bytes(self._icons.get(app.get("component", "")), 36)
            item.setIcon(QtGui.QIcon(pm))
            self.app_list.addItem(item)
        if self._pending_package:
            for row in range(self.app_list.count()):
                item = self.app_list.item(row)
                app = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
                if isinstance(app, dict) and app.get("package") == self._pending_package:
                    self.app_list.setCurrentRow(row)
                    self._pending_package = None
                    break

    def _on_app_selected(self, item: QtWidgets.QListWidgetItem | None) -> None:
        if item is None:
            return
        app = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self._selected_app = app
        pkg = app.get("package", "")
        comp = app.get("component", "")
        self.det_title.setText(app.get("name") or pkg)
        self.det_icon.setPixmap(pixmap_from_bytes(self._icons.get(comp), 48))
        self.det_info.setText(f"包名: {pkg}\n组件: {comp}\n加载详情中...")

        policy = load_window_policy()
        pref = policy.applications.get(pkg)
        if pref is None:
            self.pa_mode_combo.setCurrentIndex(0)
        elif pref.mode == "fit_output":
            self.pa_mode_combo.setCurrentIndex(2)
        else:
            self.pa_mode_combo.setCurrentIndex(1)
            self.pa_w_spin.setValue(pref.width or 960)
            self.pa_h_spin.setValue(pref.height or 680)

        remembered = load_remembered_window_sizes().get(pkg)
        self.pa_remembered_lbl.setText(
            f"{remembered[0]}×{remembered[1]}" if remembered else "（未记录）"
        )
        self.pa_res_edit.setText(get_desktop_launch_resolution(pkg) or "")

        self._spawn(
            lambda: self.client.application_details(pkg),
            lambda det: self.det_info.setText(
                f"包名: {pkg}  |  版本: {det.get('version', '—')}  |  "
                f"{'系统应用' if det.get('system') else '用户安装应用'}\n"
                f"应用代码: {format_bytes(det.get('app_bytes'))}  |  "
                f"数据: {format_bytes(det.get('data_bytes'))}  |  "
                f"缓存: {format_bytes(det.get('cache_bytes'))}"
            ),
        )

    def _launch_selected(self) -> None:
        if not self._selected_app:
            return
        pkg = self._selected_app["package"]
        comp = self._selected_app.get("component")
        res = self.pa_res_edit.text().strip() or None
        self._spawn(
            lambda: self.client.launch(pkg, component=comp, resolution=res),
            lambda _: None,
            lambda e: show_message(self, "启动失败", e, is_error=True),
        )

    def _app_action(self, action: str, confirm: bool = False) -> None:
        if not self._selected_app:
            return
        pkg = self._selected_app["package"]
        if confirm:
            ans = QtWidgets.QMessageBox.warning(
                self,
                "确认操作",
                f"确定对 {pkg} 执行 {action} 吗？此操作不可撤销。",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._spawn(
            lambda: self.client.application_action(pkg, action),
            lambda r: (
                show_message(self, "应用操作", r.message),
                self.refresh_apps() if action == "uninstall" else None,
            ),
            lambda e: show_message(self, "操作失败", e, is_error=True),
        )

    def _save_per_app_policy(self) -> None:
        if not self._selected_app:
            return
        pkg = self._selected_app["package"]
        policy = load_window_policy()
        mode = self.pa_mode_combo.currentData()
        if mode == "default":
            policy.applications.pop(pkg, None)
        elif mode == "fit_output":
            policy.applications[pkg] = WindowPreference(mode="fit_output", width=None, height=None)
        else:
            policy.applications[pkg] = WindowPreference(
                mode="windowed", width=self.pa_w_spin.value(), height=self.pa_h_spin.value()
            )
        save_window_policy(policy)

        res_txt = self.pa_res_edit.text().strip() or None
        try:
            updated_desktop = set_desktop_launch_resolution(pkg, res_txt)
        except ValueError as exc:
            show_message(self, "分辨率格式错误", str(exc), is_error=True)
            return

        msg = f"已保存 {pkg} 的窗口策略。"
        if res_txt and not updated_desktop:
            msg += "\n（未找到该应用的 .desktop 文件，启动分辨率仅在生成桌面快捷方式后可写入。）"
        elif updated_desktop:
            msg += "\n已同步更新桌面启动器中的 X-Droidloom-Resolution。"
        show_message(self, "应用窗口设置", msg)

    def _clear_remembered_size(self) -> None:
        if not self._selected_app:
            return
        pkg = self._selected_app["package"]
        if clear_remembered_window_size(pkg):
            self.pa_remembered_lbl.setText("（已清除）")
            show_message(self, "已清除", f"已清除 {pkg} 记忆的窗口尺寸")
        else:
            show_message(self, "提示", "该应用没有已记忆的窗口尺寸")
