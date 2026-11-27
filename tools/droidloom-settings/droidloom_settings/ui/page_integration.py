"""Desktop integration, clipboard isolation, host backlight, and MPRIS artwork settings page."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..backend.host_runtime import restart_droidloom, systemctl_user, systemctl_user_query
from ..backend.user_configs import (
    ClipboardSettings,
    DevicesSettings,
    PanelEnvSettings,
    load_clipboard_settings,
    load_devices_settings,
    load_panel_env_settings,
    save_clipboard_settings,
    save_devices_settings,
    save_panel_env_settings,
)
from .widgets import (
    StatusIndicator,
    WorkerThread,
    WrappingLabel,
    make_form_layout,
    make_section_card,
    set_button_role,
    show_message,
    spawn_worker,
)


class IntegrationPage(QtWidgets.QWidget):
    """Clipboard policy, host brightness decoupling, and MPRIS artwork isolation."""

    status_hint = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._workers: list[WorkerThread] = []
        self._loaded = False

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. Clipboard isolation & sync policy
        clip_box, clip_lay = make_section_card("剪贴板")

        clip_status_row = QtWidgets.QHBoxLayout()
        clip_status_row.setSpacing(18)
        self.droidloom_indicator = StatusIndicator("运行环境: 检测中...", "neutral")
        clip_status_row.addWidget(self.droidloom_indicator)
        clip_status_row.addStretch(1)
        clip_lay.addLayout(clip_status_row)

        clip_cfg = load_clipboard_settings()
        c_form = make_form_layout()

        self.clipboard_sync_combo = QtWidgets.QComboBox()
        self.clipboard_sync_combo.addItem("跟随运行模式（桌面按需 / 移动即时）", "auto")
        self.clipboard_sync_combo.addItem("聚焦时按需同步 (focus，推荐)", "focus")
        self.clipboard_sync_combo.addItem("后台即时同步 (eager)", "eager")
        idx_sync = self.clipboard_sync_combo.findData(clip_cfg.sync_mode)
        if idx_sync >= 0:
            self.clipboard_sync_combo.setCurrentIndex(idx_sync)
        self.clipboard_sync_combo.currentIndexChanged.connect(self._update_clipboard_hint)

        self.clipboard_allow_secrets_chk = QtWidgets.QCheckBox(
            "允许同步密码管理器等敏感剪贴板内容"
        )
        self.clipboard_allow_secrets_chk.setChecked(clip_cfg.allow_secrets)

        self.clipboard_cache_dir_edit = QtWidgets.QLineEdit(clip_cfg.cache_dir)
        self.clipboard_cache_dir_edit.setPlaceholderText(
            "留空默认使用 $XDG_CACHE_HOME/droidloom/clipboard"
        )
        cache_row = QtWidgets.QHBoxLayout()
        cache_row.setSpacing(8)
        cache_row.addWidget(self.clipboard_cache_dir_edit, 1)
        browse_cache_btn = QtWidgets.QPushButton("浏览...")
        browse_cache_btn.clicked.connect(self._browse_cache_dir)
        cache_row.addWidget(browse_cache_btn)

        c_form.addRow("同步策略:", self.clipboard_sync_combo)
        c_form.addRow("", self.clipboard_allow_secrets_chk)
        c_form.addRow("缓存目录:", cache_row)
        clip_lay.addLayout(c_form)

        self.clipboard_hint_lbl = WrappingLabel("", muted=True)
        clip_lay.addWidget(self.clipboard_hint_lbl)
        self._update_clipboard_hint()

        c_btns = QtWidgets.QHBoxLayout()
        c_btns.setSpacing(8)
        self.save_clip_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        self.save_restart_clip_btn = QtWidgets.QPushButton("保存并重启 Droidloom")
        self.save_clip_btn.clicked.connect(lambda: self._save_clipboard(restart=False))
        self.save_restart_clip_btn.clicked.connect(lambda: self._save_clipboard(restart=True))
        c_btns.addWidget(self.save_clip_btn)
        c_btns.addWidget(self.save_restart_clip_btn)
        c_btns.addStretch(1)
        clip_lay.addLayout(c_btns)
        root.addWidget(clip_box)

        # 2. Desktop & peripheral bridge isolation
        bridge_box, bridge_lay = make_section_card("桌面与外设桥接")

        bridge_status_row = QtWidgets.QHBoxLayout()
        bridge_status_row.setSpacing(18)
        self.devices_indicator = StatusIndicator("外设桥接: 检测中...", "neutral")
        self.panel_indicator = StatusIndicator("面板桥接: 检测中...", "neutral")
        bridge_status_row.addWidget(self.devices_indicator)
        bridge_status_row.addWidget(self.panel_indicator)
        bridge_status_row.addStretch(1)
        bridge_lay.addLayout(bridge_status_row)

        dev_cfg = load_devices_settings()
        panel_cfg = load_panel_env_settings()

        b_form = make_form_layout()
        self.host_brightness_combo = QtWidgets.QComboBox()
        self.host_brightness_combo.addItem("跟随运行模式（桌面禁用 / 移动启用）", "auto")
        self.host_brightness_combo.addItem("禁用宿主机背光控制 (推荐)", "0")
        self.host_brightness_combo.addItem("允许 Android 调节宿主机背光", "1")
        idx_br = self.host_brightness_combo.findData(dev_cfg.host_brightness)
        if idx_br >= 0:
            self.host_brightness_combo.setCurrentIndex(idx_br)

        self.mpris_remote_art_chk = QtWidgets.QCheckBox(
            "允许宿主机下载远程 HTTP/HTTPS 媒体封面（含防 SSRF 校验）"
        )
        self.mpris_remote_art_chk.setChecked(panel_cfg.mpris_fetch_remote_art)

        b_form.addRow("宿主机背光:", self.host_brightness_combo)
        b_form.addRow("MPRIS 封面:", self.mpris_remote_art_chk)
        bridge_lay.addLayout(b_form)

        self.bridge_hint_lbl = WrappingLabel(
            "桌面模式下默认禁止 Android 应用修改宿主机屏幕背光（droidloom-devices.service），并阻止 MPRIS 桥接传递本地 file:// 路径或主动发起远程 HTTP 封面抓取（droidloom-panel.service）。保存后将平滑重启这两个用户服务，无需重启 Android 容器。",
            muted=True,
        )
        bridge_lay.addWidget(self.bridge_hint_lbl)

        b_btns = QtWidgets.QHBoxLayout()
        b_btns.setSpacing(8)
        self.save_bridge_btn = set_button_role(
            QtWidgets.QPushButton("保存并重启服务"), "primary"
        )
        self.save_bridge_btn.clicked.connect(self._save_bridge)
        b_btns.addWidget(self.save_bridge_btn)
        b_btns.addStretch(1)
        bridge_lay.addLayout(b_btns)
        root.addWidget(bridge_box)

        root.addStretch(1)

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh_state()

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _update_clipboard_hint(self) -> None:
        mode = str(self.clipboard_sync_combo.currentData() or "auto")
        if mode == "focus":
            desc = "按需同步（推荐）：仅当切换焦点到 Android 窗口时才读取宿主机剪贴板内容，避免后台频繁抓取或导致宿主应用（如浏览器）管道阻塞；关闭密码管理器敏感内容过滤后仍会拦截 x-kde-passwordManagerHint。"
        elif mode == "eager":
            desc = "后台即时同步：宿主机复制后立即拉取剪贴板到缓存目录；若复制后立即关闭源 Linux 应用，Android 仍可粘贴，但会增加后台管道读取。"
        else:
            desc = "跟随运行模式：桌面模式默认启用按需同步（focus），移动模式默认启用即时同步（eager）。未勾选敏感内容选项时自动跳过密码管理器条目。修改需重启 droidloom.service 后生效。"
        self.clipboard_hint_lbl.setText(desc)

    def _browse_cache_dir(self) -> None:
        cur = self.clipboard_cache_dir_edit.text().strip()
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择剪贴板临时缓存目录", cur)
        if d:
            self.clipboard_cache_dir_edit.setText(d)

    def refresh_state(self) -> None:
        def fetch():
            dl = systemctl_user_query("droidloom.service")
            dev = systemctl_user_query("droidloom-devices.service")
            panel = systemctl_user_query("droidloom-panel.service")
            return dl, dev, panel

        def done(data):
            dl, dev, panel = data
            for ind, prefix, info in (
                (self.droidloom_indicator, "运行环境", dl),
                (self.devices_indicator, "外设桥接", dev),
                (self.panel_indicator, "面板桥接", panel),
            ):
                state = info.get("ActiveState", "unknown")
                tone = (
                    "ok"
                    if state == "active"
                    else ("neutral" if state == "inactive" else "warn")
                )
                ind.set_status(f"{prefix}: {state}", tone)

        self._spawn(fetch, done)

    def _save_clipboard(self, restart: bool) -> None:
        cfg = ClipboardSettings(
            sync_mode=str(self.clipboard_sync_combo.currentData() or "auto"),
            allow_secrets=self.clipboard_allow_secrets_chk.isChecked(),
            cache_dir=self.clipboard_cache_dir_edit.text().strip(),
        )
        try:
            save_clipboard_settings(cfg)
        except ValueError as exc:
            show_message(self, "剪贴板配置无效", str(exc), is_error=True)
            return

        if not restart:
            self.status_hint.emit("已保存剪贴板配置到 ~/.config/droidloom/clipboard.env")
            show_message(
                self,
                "剪贴板设置",
                "已保存到 ~/.config/droidloom/clipboard.env，下次启动或重启 Droidloom 后生效。",
            )
            return

        ans = QtWidgets.QMessageBox.question(
            self,
            "保存并重启 Droidloom",
            "剪贴板配置由 droidloom-wayland 读取，重启 droidloom.service 会重启当前运行中的 Android 应用。是否立即重启？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.save_clip_btn.setEnabled(False)
        self.save_restart_clip_btn.setEnabled(False)
        self.status_hint.emit("正在重启 Droidloom 以应用剪贴板配置...")

        def task():
            systemctl_user(["daemon-reload"])
            return restart_droidloom()

        def done(res):
            self.save_clip_btn.setEnabled(True)
            self.save_restart_clip_btn.setEnabled(True)
            ok, msg = res
            self.refresh_state()
            self.status_hint.emit("已重启 Droidloom" if ok else f"重启失败: {msg}")
            show_message(
                self,
                "剪贴板设置",
                "已保存剪贴板配置并重启 Droidloom。" if ok else f"重启失败: {msg}",
                is_error=not ok,
            )

        self._spawn(
            task,
            done,
            lambda e: (
                self.save_clip_btn.setEnabled(True),
                self.save_restart_clip_btn.setEnabled(True),
                show_message(self, "剪贴板设置", f"重启失败: {e}", is_error=True),
            ),
        )

    def _save_bridge(self) -> None:
        dev_cfg = DevicesSettings(
            host_brightness=str(self.host_brightness_combo.currentData() or "auto")
        )
        panel_cfg = PanelEnvSettings(
            mpris_fetch_remote_art=self.mpris_remote_art_chk.isChecked()
        )
        try:
            save_devices_settings(dev_cfg)
            save_panel_env_settings(panel_cfg)
        except ValueError as exc:
            show_message(self, "桥接配置无效", str(exc), is_error=True)
            return

        self.save_bridge_btn.setEnabled(False)
        self.status_hint.emit("正在保存并重启外设与面板桥接服务...")

        def task():
            systemctl_user(["daemon-reload"])
            ok1, msg1 = systemctl_user(["restart", "droidloom-devices.service"])
            ok2, msg2 = systemctl_user(["restart", "droidloom-panel.service"])
            if ok1 and ok2:
                return True, ""
            errs = [m for ok, m in ((ok1, msg1), (ok2, msg2)) if not ok and m]
            return False, "；".join(errs) or "部分服务重启失败"

        def done(res):
            self.save_bridge_btn.setEnabled(True)
            ok, msg = res
            self.refresh_state()
            self.status_hint.emit(
                "已应用桌面与外设桥接配置" if ok else f"服务重启异常: {msg}"
            )
            show_message(
                self,
                "桌面与外设桥接",
                "已保存配置并平滑重启 droidloom-devices.service 与 droidloom-panel.service。"
                if ok
                else f"配置已写入，但服务重启异常: {msg}",
                is_error=not ok,
            )

        self._spawn(
            task,
            done,
            lambda e: (
                self.save_bridge_btn.setEnabled(True),
                show_message(self, "桌面与外设桥接", f"操作失败: {e}", is_error=True),
            ),
        )
