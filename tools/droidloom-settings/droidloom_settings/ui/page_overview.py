"""Overview, telemetry, lifecycle control, and backup/restore page."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..backend.android_bridge import is_adb_connected, reconnect_adb
from ..backend.control_client import ControlClient
from ..backend.host_runtime import (
    backup_android_storage,
    restart_droidloom,
    restore_android_storage,
    set_droidloom_autostart,
    start_droidloom,
    stop_droidloom,
    systemctl_user_query,
)
from ..backend.user_configs import load_session_mode
from .widgets import (
    StatusIndicator,
    WorkerThread,
    WrappingLabel,
    format_bytes,
    format_duration_ms,
    make_section_card,
    make_splitter,
    set_button_role,
    set_pill_tone,
    show_message,
    spawn_worker,
)


class OverviewPage(QtWidgets.QWidget):
    """Runtime overview, lifecycle, ADB, and backup/restore."""

    status_updated = QtCore.Signal(str, dict)
    status_hint = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = ControlClient()
        self._workers: list[WorkerThread] = []

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. Lifecycle control card
        life_box, life_lay = make_section_card("运行")
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(18)
        self.state_pill = StatusIndicator("检测中...", "neutral")
        self.ready_pill = StatusIndicator("未知", "neutral")
        top_row.addWidget(self.state_pill)
        top_row.addWidget(self.ready_pill)
        top_row.addStretch(1)

        top_row.addWidget(QtWidgets.QLabel("模式:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("桌面模式", "desktop")
        self.mode_combo.addItem("移动模式", "mobile")
        cur_mode = load_session_mode()
        self.mode_combo.setCurrentIndex(1 if cur_mode == "mobile" else 0)
        top_row.addWidget(self.mode_combo)
        life_lay.addLayout(top_row)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        self.start_btn = set_button_role(QtWidgets.QPushButton("启动"), "primary")
        self.restart_btn = QtWidgets.QPushButton("重启")
        self.stop_btn = set_button_role(QtWidgets.QPushButton("停止"), "danger")
        self.open_android_settings_btn = QtWidgets.QPushButton("系统设置")
        self.autostart_chk = QtWidgets.QCheckBox("开机自启")

        self.start_btn.clicked.connect(lambda: self._run_lifecycle("start"))
        self.stop_btn.clicked.connect(lambda: self._run_lifecycle("stop"))
        self.restart_btn.clicked.connect(lambda: self._run_lifecycle("restart"))
        self.open_android_settings_btn.clicked.connect(self._open_android_settings)
        self.autostart_chk.toggled.connect(self._toggle_autostart)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.restart_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.open_android_settings_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.autostart_chk)
        life_lay.addLayout(btn_row)

        self.life_progress = WrappingLabel("", muted=True)
        self.life_progress.setVisible(False)
        life_lay.addWidget(self.life_progress)
        root.addWidget(life_box)

        # 2. Telemetry metrics (clean borderless 3-column typographic layout)
        stats_box, stats_lay = make_section_card("状态")
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        self.stat_labels: dict[str, QtWidgets.QLabel] = {}
        fields = [
            ("android_version", "Android"),
            ("model", "型号"),
            ("device_name", "设备名"),
            ("uptime", "运行时长"),
            ("ready_ms", "就绪耗时"),
            ("memory", "内存"),
            ("swap", "Swap"),
            ("cpu", "CPU"),
            ("procs", "进程 / 线程 / 应用"),
        ]
        for idx, (key, title) in enumerate(fields):
            r, c = divmod(idx, 3)
            grid_row = r * 2
            cell = QtWidgets.QWidget()
            cl = QtWidgets.QVBoxLayout(cell)
            cl.setContentsMargins(2, 4, 8, 4)
            cl.setSpacing(2)
            t_lbl = QtWidgets.QLabel(title)
            t_lbl.setProperty("muted", True)
            tf = t_lbl.font()
            tf.setPointSize(max(9, tf.pointSize() - 1))
            t_lbl.setFont(tf)
            v_lbl = QtWidgets.QLabel("—")
            vf = v_lbl.font()
            vf.setBold(True)
            vf.setPointSize(vf.pointSize() + 1)
            v_lbl.setFont(vf)
            cl.addWidget(t_lbl)
            cl.addWidget(v_lbl)
            grid.addWidget(cell, grid_row, c)
            self.stat_labels[key] = v_lbl
            if r < 2 and c == 0:
                divider = QtWidgets.QFrame()
                divider.setObjectName("rowDivider")
                divider.setFixedHeight(1)
                grid.addWidget(divider, grid_row + 1, 0, 1, 3)
        stats_lay.addLayout(grid)
        root.addWidget(stats_box)

        # 3. ADB & Backup row
        bottom_splitter = make_splitter(QtCore.Qt.Orientation.Horizontal)

        adb_box, adb_lay = make_section_card("ADB")
        self.adb_indicator = StatusIndicator("检测中...", "neutral")
        self.adb_detail_lbl = WrappingLabel("", muted=True)
        self.adb_detail_lbl.setVisible(False)
        adb_lay.addWidget(self.adb_indicator)
        adb_lay.addWidget(self.adb_detail_lbl)
        adb_btns = QtWidgets.QHBoxLayout()
        adb_btns.setSpacing(8)
        self.adb_reconn_btn = QtWidgets.QPushButton("重连")
        self.adb_copy_btn = QtWidgets.QPushButton("复制命令")
        self.adb_reconn_btn.clicked.connect(self._reconnect_adb)
        self.adb_copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText("adb -s device:droidloom shell")
        )
        adb_btns.addWidget(self.adb_reconn_btn)
        adb_btns.addWidget(self.adb_copy_btn)
        adb_btns.addStretch(1)
        adb_lay.addLayout(adb_btns)
        adb_lay.addStretch(1)
        bottom_splitter.addWidget(adb_box)

        bak_box, bak_lay = make_section_card("备份")
        bak_btns = QtWidgets.QHBoxLayout()
        bak_btns.setSpacing(8)
        self.backup_btn = QtWidgets.QPushButton("导出备份...")
        self.restore_btn = QtWidgets.QPushButton("恢复备份...")
        self.backup_btn.clicked.connect(self._on_backup)
        self.restore_btn.clicked.connect(self._on_restore)
        bak_btns.addWidget(self.backup_btn)
        bak_btns.addWidget(self.restore_btn)
        bak_btns.addStretch(1)
        bak_lay.addLayout(bak_btns)
        self.bak_status = WrappingLabel("", muted=True)
        self.bak_status.setVisible(False)
        bak_lay.addWidget(self.bak_status)
        bak_lay.addStretch(1)
        bottom_splitter.addWidget(bak_box)
        bottom_splitter.setSizes([420, 420])

        root.addWidget(bottom_splitter)
        root.addStretch(1)

        self._autostart_updating = False
        self._loaded = False

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh_all()

    def _set_life_progress(self, text: str) -> None:
        self.life_progress.setText(text)
        self.life_progress.setVisible(bool(text))
        if text:
            self.status_hint.emit(text)

    def _set_bak_status(self, text: str) -> None:
        self.bak_status.setText(text)
        self.bak_status.setVisible(bool(text))

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def refresh_all(self) -> None:
        def fetch():
            resp = self.client.stats()
            unit_info = systemctl_user_query("droidloom.service")
            adb_ok, adb_msg = is_adb_connected()
            return resp, unit_info, adb_ok, adb_msg

        def done(data):
            resp, unit_info, adb_ok, adb_msg = data
            self._autostart_updating = True
            self.autostart_chk.setChecked(unit_info.get("UnitFileState") == "enabled")
            self._autostart_updating = False

            stats = resp.stats or {}
            emitted = dict(stats)
            emitted["adb_ok"] = adb_ok
            emitted["adb_msg"] = adb_msg
            self.status_updated.emit(resp.state, emitted)

            if resp.state == "running":
                set_pill_tone(self.state_pill, "运行中", "ok")
                if stats.get("ready"):
                    set_pill_tone(self.ready_pill, "Android 框架就绪", "ok")
                else:
                    set_pill_tone(self.ready_pill, "Android 启动中...", "warn")
            else:
                set_pill_tone(self.state_pill, "已停止", "neutral")
                set_pill_tone(self.ready_pill, "未启动", "neutral")

            self.stat_labels["android_version"].setText(str(stats.get("android_version") or "—"))
            self.stat_labels["model"].setText(str(stats.get("model") or "—"))
            self.stat_labels["device_name"].setText(str(stats.get("device_name") or "—"))
            self.stat_labels["uptime"].setText(
                format_duration_ms(stats.get("uptime_ms")) if resp.state == "running" else "—"
            )
            self.stat_labels["ready_ms"].setText(format_duration_ms(stats.get("ready_ms")))
            self.stat_labels["memory"].setText(format_bytes(stats.get("memory_bytes")))
            self.stat_labels["swap"].setText(format_bytes(stats.get("swap_bytes")))
            cpu_us = stats.get("cpu_usec")
            self.stat_labels["cpu"].setText(
                f"{cpu_us / 1_000_000:.1f} s" if cpu_us is not None else "—"
            )
            if stats.get("processes") is not None:
                self.stat_labels["procs"].setText(
                    f"{stats.get('processes')} / {stats.get('threads', '?')} / {stats.get('app_processes', '?')}"
                )
            else:
                self.stat_labels["procs"].setText("—")

            self.adb_indicator.set_status(
                "已连接" if adb_ok else "未连接",
                "ok" if adb_ok else "warn",
            )
            self.adb_detail_lbl.setText(adb_msg or "")
            self.adb_detail_lbl.setVisible(bool(adb_msg))

        self._spawn(fetch, done)

    def _run_lifecycle(self, action: str) -> None:
        mode = self.mode_combo.currentData()
        self._set_life_progress(f"正在执行 {action}...")
        for b in (self.start_btn, self.stop_btn, self.restart_btn):
            b.setEnabled(False)

        def task():
            if action == "start":
                return start_droidloom(mode)
            if action == "stop":
                return stop_droidloom()
            return restart_droidloom(mode)

        def done(res):
            for b in (self.start_btn, self.stop_btn, self.restart_btn):
                b.setEnabled(True)
            ok, msg = res
            self._set_life_progress(msg or ("完成" if ok else "失败"))
            self.refresh_all()

        def err(msg):
            for b in (self.start_btn, self.stop_btn, self.restart_btn):
                b.setEnabled(True)
            self._set_life_progress(f"操作失败: {msg}")

        self._spawn(task, done, err)

    def _toggle_autostart(self, checked: bool) -> None:
        if self._autostart_updating:
            return
        self._spawn(
            lambda: set_droidloom_autostart(checked),
            lambda res: self._set_life_progress(
                "已开启开机自启" if (res[0] and checked) else ("已关闭开机自启" if res[0] else res[1])
            ),
        )

    def _open_android_settings(self) -> None:
        self._spawn(
            lambda: self.client.launch("com.android.settings"),
            lambda _: self._set_life_progress("已请求打开 Android 系统设置"),
            lambda e: show_message(self, "打开失败", e, is_error=True),
        )

    def _reconnect_adb(self) -> None:
        self._spawn(
            reconnect_adb,
            lambda res: (
                self.adb_indicator.set_status(
                    "已连接" if res[0] else "未连接",
                    "ok" if res[0] else "warn",
                ),
                self.adb_detail_lbl.setText(res[1] or ""),
                self.adb_detail_lbl.setVisible(bool(res[1])),
                self.refresh_all(),
            ),
        )

    def _on_backup(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存 Android 存储备份",
            "droidloom-backup.tar.zst",
            "Zstd Tar Archive (*.tar.zst)",
        )
        if not path:
            return
        self._set_bak_status("正在备份 Android 存储，请勿关闭电源...")
        self.backup_btn.setEnabled(False)
        self._spawn(
            lambda: backup_android_storage(path),
            lambda res: (
                self.backup_btn.setEnabled(True),
                self._set_bak_status(res[1] if res[0] else f"备份失败: {res[1]}"),
            ),
            lambda e: (self.backup_btn.setEnabled(True), self._set_bak_status(f"备份失败: {e}")),
        )

    def _on_restore(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择 Android 备份文件",
            "",
            "Zstd Tar Archive (*.tar.zst);;All Files (*)",
        )
        if not path:
            return
        ans = QtWidgets.QMessageBox.warning(
            self,
            "确认恢复存储",
            f"将使用 {path} 替换当前 Android 存储（原存储会被重命名保留在旁）。是否继续？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._set_bak_status("正在校验并恢复存储...")
        self.restore_btn.setEnabled(False)
        self._spawn(
            lambda: restore_android_storage(path),
            lambda res: (
                self.restore_btn.setEnabled(True),
                self._set_bak_status(res[1] if res[0] else f"恢复失败: {res[1]}"),
            ),
            lambda e: (self.restore_btn.setEnabled(True), self._set_bak_status(f"恢复失败: {e}")),
        )
