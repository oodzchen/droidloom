"""Standalone compact APK Installer dialog used when opening .apk files from desktop/file manager."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ..backend.apk_parser import ApkMetadata, inspect_apk
from ..backend.control_client import ControlClient, ControlError
from ..backend.host_runtime import start_droidloom
from .widgets import (
    StatusIndicator,
    WorkerThread,
    WrappingLabel,
    apply_window_theme,
    format_bytes,
    make_form_layout,
    make_section_card,
    pixmap_from_bytes,
    set_button_role,
    set_pill_tone,
    wait_all_workers,
)


class ApkInstallDialog(QtWidgets.QDialog):
    """Compact GUI installer for one or more APK files."""

    open_settings_requested = QtCore.Signal()

    def __init__(self, apk_paths: list[Path], parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.apk_paths = apk_paths
        self.current_index = 0
        self.client = ControlClient()
        self.worker: WorkerThread | None = None
        self.current_meta: ApkMetadata | None = None

        self.setWindowTitle("安装 APK")
        self.setSizeGripEnabled(True)
        self.setMinimumSize(420, 360)
        self.resize(540, 480)
        apply_window_theme(self)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # Header with icon & basic metadata
        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(12)
        self.icon_label = QtWidgets.QLabel()
        self.icon_label.setFixedSize(64, 64)
        hdr.addWidget(self.icon_label)

        info_col = QtWidgets.QVBoxLayout()
        info_col.setSpacing(3)
        self.title_label = QtWidgets.QLabel("正在解析 APK...")
        f = self.title_label.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self.title_label.setFont(f)
        info_col.addWidget(self.title_label)

        self.pkg_label = QtWidgets.QLabel("")
        self.pkg_label.setProperty("muted", True)
        self.pkg_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        info_col.addWidget(self.pkg_label)

        self.ver_label = QtWidgets.QLabel("")
        self.ver_label.setProperty("muted", True)
        info_col.addWidget(self.ver_label)
        hdr.addLayout(info_col, 1)
        root.addLayout(hdr)

        # Compatibility & runtime status indicators (colored dots, no pill boxes)
        badges = QtWidgets.QHBoxLayout()
        badges.setSpacing(16)
        self.compat_pill = StatusIndicator("检查架构...", "neutral")
        self.runtime_pill = StatusIndicator("检查 Droidloom 状态...", "neutral")
        badges.addWidget(self.compat_pill)
        badges.addWidget(self.runtime_pill)
        badges.addStretch(1)
        root.addLayout(badges)

        # Details / permissions
        form = make_form_layout()
        self.path_label = WrappingLabel("", muted=True)
        self.sdk_label = WrappingLabel("—", muted=True)
        self.installed_label = WrappingLabel("检查中...", muted=True)
        self.user_spin = QtWidgets.QSpinBox()
        self.user_spin.setRange(0, 999)
        self.user_spin.setValue(0)
        form.addRow("路径:", self.path_label)
        form.addRow("架构:", self.sdk_label)
        form.addRow("状态:", self.installed_label)
        form.addRow("用户:", self.user_spin)
        root.addLayout(form)

        perm_card, perm_lay = make_section_card("权限")
        self.perm_group = perm_card
        self.perm_title_lbl = perm_card.findChild(QtWidgets.QLabel, "sectionTitle")
        self.perm_text = QtWidgets.QPlainTextEdit()
        self.perm_text.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.perm_text.setReadOnly(True)
        self.perm_text.setMinimumHeight(80)
        perm_lay.addWidget(self.perm_text)
        root.addWidget(perm_card, 1)

        # Progress & status
        self.auto_start_chk = QtWidgets.QCheckBox("未运行时自动启动")
        self.auto_start_chk.setChecked(True)
        root.addWidget(self.auto_start_chk)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = WrappingLabel("", muted=True)
        self.status_label.setVisible(False)
        root.addWidget(self.status_label)

        # Buttons
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        self.settings_btn = QtWidgets.QPushButton("设置")
        self.settings_btn.clicked.connect(self._on_open_settings)
        btns.addWidget(self.settings_btn)
        btns.addStretch(1)

        self.launch_btn = QtWidgets.QPushButton("打开")
        self.launch_btn.setVisible(False)
        self.launch_btn.clicked.connect(self._on_launch)
        btns.addWidget(self.launch_btn)

        self.install_btn = set_button_role(QtWidgets.QPushButton("安装"), "primary")
        self.install_btn.setDefault(True)
        self.install_btn.clicked.connect(self._on_install)
        btns.addWidget(self.install_btn)

        self.close_btn = QtWidgets.QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

        self._load_current()

    def _set_perm_title(self, text: str) -> None:
        if self.perm_title_lbl is not None:
            self.perm_title_lbl.setText(text)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _on_open_settings(self) -> None:
        self.open_settings_requested.emit()

    def _load_current(self) -> None:
        if not self.apk_paths:
            self.title_label.setText("未提供 APK 文件")
            self.install_btn.setEnabled(False)
            return
        path = self.apk_paths[self.current_index]
        meta = inspect_apk(path)
        self.current_meta = meta
        self.icon_label.setPixmap(pixmap_from_bytes(meta.icon_bytes, 64))
        self.path_label.setText(f"{path} ({format_bytes(meta.file_size)})")

        if len(self.apk_paths) > 1:
            self.setWindowTitle(
                f"安装 APK ({self.current_index + 1}/{len(self.apk_paths)})"
            )

        if meta.is_split_bundle:
            self.title_label.setText(path.name)
            self.pkg_label.setText(meta.bundle_reason)
            set_pill_tone(self.compat_pill, "不支持的拆分包", "err")
            self.install_btn.setEnabled(False)
            self.perm_text.setPlainText(meta.bundle_reason)
        elif meta.parse_error:
            self.title_label.setText(path.name)
            self.pkg_label.setText(meta.parse_error)
            set_pill_tone(self.compat_pill, "解析失败", "err")
            self.install_btn.setEnabled(False)
        else:
            self.title_label.setText(meta.label or meta.package or path.name)
            self.pkg_label.setText(f"包名: {meta.package or '未知'}")
            ver = meta.version_name or "—"
            if meta.version_code is not None:
                ver = f"{ver} (code {meta.version_code})"
            self.ver_label.setText(f"版本: {ver}")
            sdk = f"minSdk {meta.min_sdk or '?'} / targetSdk {meta.target_sdk or '?'}"
            abis = ", ".join(meta.abis) if meta.abis else "无原生库 (Pure DEX)"
            self.sdk_label.setText(f"{sdk} | ABI: {abis}")
            set_pill_tone(
                self.compat_pill,
                meta.abi_status_message,
                "ok" if meta.abi_compatible else "err",
            )
            self.install_btn.setEnabled(True)
            if not meta.abi_compatible:
                self.install_btn.setText("仍尝试安装")
            if meta.permissions:
                self._set_perm_title(f"权限 ({len(meta.permissions)})")
                self.perm_text.setPlainText("\n".join(meta.permissions))
            else:
                self._set_perm_title("权限 (0)")
                self.perm_text.setPlainText("无特殊权限。")

        self._refresh_runtime_state()

    def _refresh_runtime_state(self) -> None:
        def probe():
            st = self.client.status()
            existing = None
            if st.state == "running" and self.current_meta and self.current_meta.package:
                try:
                    existing = self.client.application_details(
                        self.current_meta.package, user=self.user_spin.value()
                    )
                except ControlError:
                    existing = None
            return st, existing

        def done(res):
            st, existing = res
            if st.state == "running":
                set_pill_tone(self.runtime_pill, "Droidloom 运行中", "ok")
            else:
                set_pill_tone(self.runtime_pill, "Droidloom 未运行", "warn")
            if existing:
                self.installed_label.setText(
                    f"已安装版本 {existing.get('version', '未知')}（将保留数据更新）"
                )
            elif st.state == "running":
                self.installed_label.setText("全新安装")
            else:
                self.installed_label.setText("需启动后检查")

        self._probe_worker = WorkerThread(probe)
        self._probe_worker.finished_ok.connect(done)
        self._probe_worker.finished_err.connect(
            lambda err: set_pill_tone(self.runtime_pill, f"状态未知: {err}", "warn")
        )
        self._probe_worker.start()

    def _on_install(self) -> None:
        if not self.current_meta:
            return
        path = self.current_meta.path
        user = self.user_spin.value()
        auto_start = self.auto_start_chk.isChecked()

        self.install_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._set_status("准备安装...")

        def task(on_progress=None):
            st = self.client.status()
            if st.state != "running":
                if not auto_start:
                    raise ControlError("Droidloom 未运行；请先启动或勾选自动启动。")
                if on_progress:
                    on_progress("Droidloom 未运行，正在启动 Android 运行时...")
                ok, msg = start_droidloom()
                if not ok:
                    raise ControlError(f"启动 Droidloom 失败：{msg}")
            return self.client.install_apk(path, user=user, on_progress=on_progress)

        self.worker = WorkerThread(task, pass_progress=True)
        self.worker.progress_msg.connect(self._set_status)
        self.worker.finished_ok.connect(self._on_install_ok)
        self.worker.finished_err.connect(self._on_install_err)
        self.worker.start()

    def _on_install_ok(self, resp) -> None:
        self.progress_bar.setVisible(False)
        self._set_status(f"安装成功：{resp.message}")
        set_pill_tone(self.runtime_pill, "安装完成", "ok")
        if self.current_meta and self.current_meta.package:
            self.launch_btn.setVisible(True)
        if self.current_index + 1 < len(self.apk_paths):
            self.current_index += 1
            self.install_btn.setEnabled(True)
            self.install_btn.setText("继续安装下一个")
            self._load_current()
        else:
            self.install_btn.setText("重新安装")
            self.install_btn.setEnabled(True)

    def _on_install_err(self, err: str) -> None:
        self.progress_bar.setVisible(False)
        self._set_status(f"安装失败：{err}")
        set_pill_tone(self.runtime_pill, "安装失败", "err")
        self.install_btn.setEnabled(True)

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.PaletteChange:
            apply_window_theme(self)

    def done(self, result: int) -> None:
        for attr in ("worker", "_probe_worker"):
            th = getattr(self, attr, None)
            if th is not None and th.isRunning():
                th.wait(3000)
        wait_all_workers(3000)
        super().done(result)

    def _on_launch(self) -> None:
        if not self.current_meta or not self.current_meta.package:
            return
        pkg = self.current_meta.package
        user = self.user_spin.value()

        def do_launch():
            return self.client.launch(pkg, user=user)

        self.worker = WorkerThread(do_launch)
        self.worker.finished_ok.connect(lambda r: self._set_status(f"已启动 {pkg}"))
        self.worker.finished_err.connect(lambda e: self._set_status(f"启动失败: {e}"))
        self.worker.start()
