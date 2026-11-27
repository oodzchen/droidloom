"""Display scaling, window policy, night mode, animations, and gravity/orientation simulation page."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..backend.android_bridge import (
    apply_android_rotation,
    apply_android_visual_tweaks,
    query_android_snapshot,
)
from ..backend.control_client import ControlClient
from ..backend.host_runtime import systemctl_user, systemctl_user_query
from ..backend.orientation_daemon import map_orientation_to_rotation, query_host_sensor_status
from ..backend.user_configs import (
    WindowPreference,
    list_drm_connectors,
    load_density_config,
    load_orientation_config,
    load_window_policy,
    save_density_config,
    save_orientation_config,
    save_window_policy,
)
from .widgets import (
    StatusIndicator,
    WorkerThread,
    make_form_layout,
    make_section_card,
    make_splitter,
    set_button_role,
    set_pill_tone,
    show_message,
    spawn_worker,
    style_table,
    wrap_resizable,
)

ROTATION_LABELS = {
    0: "0°",
    1: "90°",
    2: "180°",
    3: "270°",
}


class OrientationDialWidget(QtWidgets.QWidget):
    """Interactive visual orientation dial showing 0/90/180/270 device rotation."""

    rotation_clicked = QtCore.Signal(int)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._rotation = 0
        self.setMinimumSize(140, 140)
        self.setMaximumSize(160, 160)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def set_rotation(self, rot: int) -> None:
        self._rotation = rot % 4
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        cx, cy = self.width() / 2.0, self.height() / 2.0
        dx = event.position().x() - cx
        dy = event.position().y() - cy
        if abs(dy) >= abs(dx):
            rot = 0 if dy < 0 else 2
        else:
            rot = 1 if dx > 0 else 3
        self.rotation_clicked.emit(rot)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) / 2.0 - 8

        p.setPen(QtGui.QPen(self.palette().mid().color(), 1.5))
        p.setBrush(self.palette().base())
        p.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)

        accent = self.palette().highlight().color()
        accent_fill = QtGui.QColor(accent)
        accent_fill.setAlpha(42)

        p.save()
        p.translate(cx, cy)
        p.rotate(self._rotation * 90)
        pw, ph = radius * 0.68, radius * 1.15
        rect = QtCore.QRectF(-pw / 2, -ph / 2, pw, ph)
        p.setPen(QtGui.QPen(accent, 2.2))
        p.setBrush(accent_fill)
        p.drawRoundedRect(rect, 6, 6)
        # Top camera/speaker notch indicator
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QtCore.QPointF(0, -ph / 2 + 8), 3.5, 3.5)
        p.restore()

        p.setPen(self.palette().text().color())
        f = p.font()
        f.setBold(True)
        p.setFont(f)
        p.drawText(
            QtCore.QRectF(0, h - 22, w, 20),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            f"{self._rotation * 90}°",
        )


class DisplayOrientationPage(QtWidgets.QWidget):
    """Display scaling, DPI, night mode, animations, and gravity/orientation simulation."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = ControlClient()
        self._workers: list[WorkerThread] = []
        self._policy = load_window_policy()
        self._density = load_density_config()
        self._orient = load_orientation_config()

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. Gravity & Orientation Simulation
        ori_box, ori_lay = make_section_card("方向")
        sensor_row = QtWidgets.QHBoxLayout()
        sensor_row.setSpacing(18)
        self.host_sensor_pill = StatusIndicator("重力: 检测中", "neutral")
        self.android_sensor_pill = StatusIndicator("SensorService: 检测中", "neutral")
        self.sync_daemon_pill = StatusIndicator("同步服务: 检测中", "neutral")
        sensor_row.addWidget(self.host_sensor_pill)
        sensor_row.addWidget(self.android_sensor_pill)
        sensor_row.addWidget(self.sync_daemon_pill)
        sensor_row.addStretch(1)
        ori_lay.addLayout(sensor_row)

        ctrl_row = QtWidgets.QHBoxLayout()
        ctrl_row.setSpacing(16)
        self.dial = OrientationDialWidget()
        self.dial.rotation_clicked.connect(lambda r: self._apply_rotation("lock", r))
        ctrl_row.addWidget(self.dial)

        rot_col = QtWidgets.QVBoxLayout()
        rot_col.setSpacing(10)
        rot_btns = QtWidgets.QHBoxLayout()
        rot_btns.setSpacing(6)
        self.rot_buttons: dict[int, QtWidgets.QPushButton] = {}
        for idx, text in ROTATION_LABELS.items():
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, r=idx: self._apply_rotation("lock", r))
            self.rot_buttons[idx] = b
            rot_btns.addWidget(b)
        self.free_rot_btn = QtWidgets.QPushButton("自由")
        self.free_rot_btn.clicked.connect(lambda: self._apply_rotation("free", 0))
        rot_btns.addWidget(self.free_rot_btn)
        rot_btns.addStretch(1)
        rot_col.addLayout(rot_btns)
        if self._orient.last_manual_rotation is not None:
            self._highlight_rotation("lock", self._orient.last_manual_rotation)
        else:
            self._highlight_rotation("free", 0)

        self.fixed_rot_combo = QtWidgets.QComboBox()
        self.fixed_rot_combo.addItem("锁定 (enabled)", "enabled")
        self.fixed_rot_combo.addItem("默认 (default)", "default")
        self.fixed_rot_combo.addItem("禁用 (disabled)", "disabled")
        idx_f = self.fixed_rot_combo.findData(
            "enabled" if self._orient.force_fixed_to_user_rotation else "default"
        )
        if idx_f >= 0:
            self.fixed_rot_combo.setCurrentIndex(idx_f)

        self.follow_gravity_chk = QtWidgets.QCheckBox("跟随物理重力")
        self.follow_gravity_chk.setChecked(self._orient.follow_host_gravity)
        self.offset_combo = QtWidgets.QComboBox()
        for off in range(4):
            self.offset_combo.addItem(f"+{off * 90}°", off)
        self.offset_combo.setCurrentIndex(self._orient.rotation_offset % 4)

        o_form = make_form_layout()
        o_form.addRow("强制方向:", self.fixed_rot_combo)
        o_form.addRow("", self.follow_gravity_chk)
        o_form.addRow("角度偏移:", self.offset_combo)
        rot_col.addLayout(o_form)

        sync_btns = QtWidgets.QHBoxLayout()
        sync_btns.setSpacing(8)
        apply_ori_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        swap_win_btn = QtWidgets.QPushButton("宽高互换")
        apply_ori_btn.clicked.connect(self._save_orientation_settings)
        swap_win_btn.clicked.connect(self._swap_default_window_axes)
        sync_btns.addWidget(apply_ori_btn)
        sync_btns.addWidget(swap_win_btn)
        sync_btns.addStretch(1)
        rot_col.addLayout(sync_btns)

        ctrl_row.addLayout(rot_col, 1)
        ori_lay.addLayout(ctrl_row)
        root.addWidget(ori_box)

        # 2. Window Policy & Multi-Monitor Density
        win_splitter = make_splitter(QtCore.Qt.Orientation.Horizontal)

        pol_box, pol_lay = make_section_card("窗口")
        p_form = make_form_layout()
        self.win_mode_combo = QtWidgets.QComboBox()
        self.win_mode_combo.addItem("窗口", "windowed")
        self.win_mode_combo.addItem("铺满", "fit_output")
        self.win_mode_combo.setCurrentIndex(1 if self._policy.default.mode == "fit_output" else 0)

        self.win_w_spin = QtWidgets.QSpinBox()
        self.win_w_spin.setRange(100, 16384)
        self.win_w_spin.setValue(self._policy.default.width or 480)
        self.win_h_spin = QtWidgets.QSpinBox()
        self.win_h_spin.setRange(100, 16384)
        self.win_h_spin.setValue(self._policy.default.height or 800)

        preset_win = QtWidgets.QHBoxLayout()
        preset_win.setContentsMargins(0, 0, 0, 0)
        preset_win.setSpacing(6)
        for label, w, h in (
            ("480×800", 480, 800),
            ("960×680", 960, 680),
            ("1280×720", 1280, 720),
        ):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, ww=w, hh=h: self._set_win_preset(ww, hh))
            preset_win.addWidget(b)
        preset_win.addStretch(1)

        p_form.addRow("模式:", self.win_mode_combo)
        p_form.addRow("宽度:", self.win_w_spin)
        p_form.addRow("高度:", self.win_h_spin)
        p_form.addRow("预设:", preset_win)
        pol_lay.addLayout(p_form)
        pol_lay.addStretch(1)

        pol_btn_row = QtWidgets.QHBoxLayout()
        save_pol_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        save_pol_btn.clicked.connect(self._save_window_policy)
        pol_btn_row.addWidget(save_pol_btn)
        pol_btn_row.addStretch(1)
        pol_lay.addLayout(pol_btn_row)
        win_splitter.addWidget(pol_box)

        den_box, den_lay = make_section_card("缩放")
        d_form = make_form_layout()
        self.dps_spin = QtWidgets.QDoubleSpinBox()
        self.dps_spin.setRange(72.0, 640.0)
        self.dps_spin.setDecimals(1)
        self.dps_spin.setValue(self._density.dpi_per_scale or 230.0)
        d_form.addRow("全局密度:", self.dps_spin)
        den_lay.addLayout(d_form)

        self.outputs_table = QtWidgets.QTableWidget(0, 3)
        self.outputs_table.setHorizontalHeaderLabels(["接口", "状态", "独立密度"])
        self.outputs_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        style_table(self.outputs_table)
        self._populate_outputs_table()
        den_lay.addWidget(wrap_resizable(self.outputs_table, default_height=135, min_height=88))

        den_btn_row = QtWidgets.QHBoxLayout()
        save_den_btn = set_button_role(QtWidgets.QPushButton("应用"), "primary")
        save_den_btn.clicked.connect(self._save_density)
        den_btn_row.addWidget(save_den_btn)
        den_btn_row.addStretch(1)
        den_lay.addLayout(den_btn_row)
        win_splitter.addWidget(den_box)
        win_splitter.setSizes([420, 420])
        root.addWidget(win_splitter)

        # 3. Android Display DPI, Night Mode & Animations
        tweaks_box, tweaks_lay = make_section_card("显示")
        t_grid = QtWidgets.QGridLayout()
        t_grid.setHorizontalSpacing(12)
        t_grid.setVerticalSpacing(10)
        t_grid.setColumnStretch(1, 1)
        t_grid.setColumnStretch(3, 1)

        self.android_dpi_spin = QtWidgets.QSpinBox()
        self.android_dpi_spin.setRange(72, 1280)
        self.android_dpi_spin.setValue(160)
        apply_dpi_btn = QtWidgets.QPushButton("应用 DPI")
        apply_dpi_btn.clicked.connect(self._apply_android_dpi)

        night_on_btn = QtWidgets.QPushButton("深色")
        night_off_btn = QtWidgets.QPushButton("浅色")
        night_on_btn.clicked.connect(lambda: self._set_night_mode(True))
        night_off_btn.clicked.connect(lambda: self._set_night_mode(False))

        self.font_scale_spin = QtWidgets.QDoubleSpinBox()
        self.font_scale_spin.setRange(0.5, 2.0)
        self.font_scale_spin.setSingleStep(0.05)
        self.font_scale_spin.setValue(1.0)

        self.win_anim_spin = QtWidgets.QDoubleSpinBox()
        self.win_anim_spin.setRange(0.0, 5.0)
        self.win_anim_spin.setSingleStep(0.25)
        self.win_anim_spin.setValue(1.0)

        self.trans_anim_spin = QtWidgets.QDoubleSpinBox()
        self.trans_anim_spin.setRange(0.0, 5.0)
        self.trans_anim_spin.setSingleStep(0.25)
        self.trans_anim_spin.setValue(1.0)

        self.anim_dur_spin = QtWidgets.QDoubleSpinBox()
        self.anim_dur_spin.setRange(0.0, 5.0)
        self.anim_dur_spin.setSingleStep(0.25)
        self.anim_dur_spin.setValue(1.0)

        self.show_touches_chk = QtWidgets.QCheckBox("显示触摸")
        self.ptr_loc_chk = QtWidgets.QCheckBox("指针轨迹")

        align_r = QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter

        def _lbl(text: str) -> QtWidgets.QLabel:
            l = QtWidgets.QLabel(text)
            l.setAlignment(align_r)
            return l

        t_grid.addWidget(_lbl("DPI:"), 0, 0)
        t_grid.addWidget(self.android_dpi_spin, 0, 1)
        t_grid.addWidget(apply_dpi_btn, 0, 2)
        night_row = QtWidgets.QHBoxLayout()
        night_row.setSpacing(6)
        night_row.addWidget(night_on_btn)
        night_row.addWidget(night_off_btn)
        night_row.addStretch(1)
        t_grid.addLayout(night_row, 0, 3)

        t_grid.addWidget(_lbl("字体:"), 1, 0)
        t_grid.addWidget(self.font_scale_spin, 1, 1)
        t_grid.addWidget(_lbl("窗口动画:"), 1, 2)
        t_grid.addWidget(self.win_anim_spin, 1, 3)

        t_grid.addWidget(_lbl("过渡动画:"), 2, 0)
        t_grid.addWidget(self.trans_anim_spin, 2, 1)
        t_grid.addWidget(_lbl("时长动画:"), 2, 2)
        t_grid.addWidget(self.anim_dur_spin, 2, 3)

        chk_row = QtWidgets.QHBoxLayout()
        chk_row.setSpacing(16)
        chk_row.addWidget(self.show_touches_chk)
        chk_row.addWidget(self.ptr_loc_chk)
        chk_row.addStretch(1)
        t_grid.addLayout(chk_row, 3, 1, 1, 3)
        tweaks_lay.addLayout(t_grid)

        t_btns = QtWidgets.QHBoxLayout()
        t_btns.setSpacing(8)
        fast_anim_btn = QtWidgets.QPushButton("0.5x")
        no_anim_btn = QtWidgets.QPushButton("0x")
        apply_tweaks_btn = set_button_role(QtWidgets.QPushButton("应用"), "primary")
        fast_anim_btn.clicked.connect(lambda: self._set_anim_all(0.5))
        no_anim_btn.clicked.connect(lambda: self._set_anim_all(0.0))
        apply_tweaks_btn.clicked.connect(self._apply_visual_tweaks)
        t_btns.addWidget(fast_anim_btn)
        t_btns.addWidget(no_anim_btn)
        t_btns.addStretch(1)
        t_btns.addWidget(apply_tweaks_btn)
        tweaks_lay.addLayout(t_btns)
        root.addWidget(tweaks_box)

        root.addStretch(1)
        self._loaded = False

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh_state()

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _populate_outputs_table(self) -> None:
        conns = list_drm_connectors()
        known = {c["name"]: c["status"] for c in conns}
        for name in self._density.outputs:
            if name not in known:
                known[name] = "configured"
        items = sorted(known.items())
        self.outputs_table.setRowCount(len(items))
        for r, (name, st) in enumerate(items):
            n_it = QtWidgets.QTableWidgetItem(name)
            n_it.setFlags(n_it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            s_it = QtWidgets.QTableWidgetItem(st)
            s_it.setFlags(s_it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            val = self._density.outputs.get(name)
            v_it = QtWidgets.QTableWidgetItem(str(val) if val is not None else "")
            self.outputs_table.setItem(r, 0, n_it)
            self.outputs_table.setItem(r, 1, s_it)
            self.outputs_table.setItem(r, 2, v_it)

    def refresh_state(self) -> None:
        def fetch():
            sensor = query_host_sensor_status()
            sync_unit = systemctl_user_query("droidloom-orientation-sync.service")
            snap = query_android_snapshot()
            try:
                disp = self.client.display()
            except Exception:
                disp = {}
            return sensor, sync_unit, snap, disp

        def done(data):
            sensor, sync_unit, snap, disp = data
            if sensor.get("available"):
                ori = sensor.get("orientation", "undefined")
                mapped = map_orientation_to_rotation(ori, self.offset_combo.currentIndex())
                m_str = f" -> {mapped * 90}°" if mapped is not None else ""
                set_pill_tone(self.host_sensor_pill, f"重力: {ori}{m_str}", "ok")
            else:
                set_pill_tone(self.host_sensor_pill, "重力: 无", "warn")

            if sync_unit.get("ActiveState") == "active":
                set_pill_tone(self.sync_daemon_pill, "同步: 运行中", "ok")
            else:
                set_pill_tone(self.sync_daemon_pill, "同步: 未运行", "neutral")

            if snap.connected:
                set_pill_tone(
                    self.android_sensor_pill,
                    f"SensorService: {snap.sensor_status}",
                    "info",
                )
                self._highlight_rotation(snap.user_rotation_mode, snap.user_rotation_index)
                idx_f = self.fixed_rot_combo.findData(snap.fixed_to_user_rotation)
                if idx_f >= 0:
                    self.fixed_rot_combo.setCurrentIndex(idx_f)
                try:
                    self.font_scale_spin.setValue(float(snap.font_scale))
                    self.win_anim_spin.setValue(float(snap.window_anim_scale))
                    self.trans_anim_spin.setValue(float(snap.transition_anim_scale))
                    self.anim_dur_spin.setValue(float(snap.animator_scale))
                except ValueError:
                    pass
                self.show_touches_chk.setChecked(snap.show_touches)
                self.ptr_loc_chk.setChecked(snap.pointer_location)
            else:
                set_pill_tone(self.android_sensor_pill, "Android ADB 未连接", "warn")

            if disp.get("dpi"):
                self.android_dpi_spin.setValue(int(disp["dpi"]))

        self._spawn(fetch, done)

    def _highlight_rotation(self, mode: str, rotation: int) -> None:
        self._active_rotation_mode = mode
        self._active_rotation_index = rotation % 4
        self.dial.set_rotation(self._active_rotation_index)
        for idx, btn in self.rot_buttons.items():
            set_button_role(
                btn,
                "primary" if (mode == "lock" and idx == self._active_rotation_index) else "",
            )
        set_button_role(self.free_rot_btn, "primary" if mode == "free" else "")

    def _apply_rotation(self, mode: str, rotation: int) -> None:
        fixed = str(self.fixed_rot_combo.currentData())
        self._highlight_rotation(mode, rotation)
        self._spawn(
            lambda: apply_android_rotation(mode, rotation, fixed_mode=fixed),
            lambda r: (
                self.refresh_state(),
                show_message(self, "方向模拟", f"方向设置失败: {r[1]}", is_error=True)
                if not r[0]
                else None,
            ),
        )

    def _save_orientation_settings(self) -> None:
        self._orient.follow_host_gravity = self.follow_gravity_chk.isChecked()
        self._orient.rotation_offset = self.offset_combo.currentIndex()
        self._orient.force_fixed_to_user_rotation = self.fixed_rot_combo.currentData() == "enabled"
        save_orientation_config(self._orient)

        def task():
            if self._orient.follow_host_gravity:
                systemctl_user(["enable", "--now", "droidloom-orientation-sync.service"])
                return systemctl_user(["restart", "droidloom-orientation-sync.service"])
            return systemctl_user(["disable", "--now", "droidloom-orientation-sync.service"])

        self._spawn(
            task,
            lambda r: (
                self.refresh_state(),
                show_message(
                    self,
                    "重力同步设置",
                    "已保存并更新重力同步后台服务。" if r[0] else f"服务操作提示: {r[1]}",
                ),
            ),
        )

    def _swap_default_window_axes(self) -> None:
        w, h = self.win_w_spin.value(), self.win_h_spin.value()
        self.win_w_spin.setValue(h)
        self.win_h_spin.setValue(w)
        self._save_window_policy()

    def _set_win_preset(self, w: int, h: int) -> None:
        self.win_mode_combo.setCurrentIndex(0)
        self.win_w_spin.setValue(w)
        self.win_h_spin.setValue(h)

    def _save_window_policy(self) -> None:
        mode = str(self.win_mode_combo.currentData())
        if mode == "fit_output":
            self._policy.default = WindowPreference(mode="fit_output", width=None, height=None)
        else:
            self._policy.default = WindowPreference(
                mode="windowed", width=self.win_w_spin.value(), height=self.win_h_spin.value()
            )
        save_window_policy(self._policy)
        show_message(self, "窗口策略", "已保存到 ~/.config/droidloom/window-policy-v1.json")

    def _save_density(self) -> None:
        self._density.dpi_per_scale = self.dps_spin.value()
        outputs: dict[str, float] = {}
        for r in range(self.outputs_table.rowCount()):
            name = self.outputs_table.item(r, 0).text().strip()
            val_str = self.outputs_table.item(r, 2).text().strip()
            if val_str:
                try:
                    outputs[name] = float(val_str)
                except ValueError:
                    show_message(self, "输入错误", f"{name} 的缩放值必须是数字", is_error=True)
                    return
        self._density.outputs = outputs
        save_density_config(self._density)
        show_message(self, "显示器缩放", "已写入 density-v1.json，Droidloom 窗口将立即自动更新缩放。")

    def _apply_android_dpi(self) -> None:
        dpi = self.android_dpi_spin.value()
        self._spawn(
            lambda: self.client.set_dpi(dpi),
            lambda r: show_message(self, "显示 DPI", r.message),
            lambda e: show_message(self, "设置 DPI 失败", e, is_error=True),
        )

    def _set_night_mode(self, night: bool) -> None:
        self._spawn(
            lambda: self.client.set_night_mode(night),
            lambda r: show_message(self, "深色模式", r.message),
            lambda e: show_message(self, "切换失败", e, is_error=True),
        )

    def _set_anim_all(self, val: float) -> None:
        self.win_anim_spin.setValue(val)
        self.trans_anim_spin.setValue(val)
        self.anim_dur_spin.setValue(val)

    def _apply_visual_tweaks(self) -> None:
        self._spawn(
            lambda: apply_android_visual_tweaks(
                self.font_scale_spin.value(),
                self.win_anim_spin.value(),
                self.trans_anim_spin.value(),
                self.anim_dur_spin.value(),
                self.show_touches_chk.isChecked(),
                self.ptr_loc_chk.isChecked(),
            ),
            lambda r: show_message(
                self,
                "视觉与动画",
                "已应用到运行中的 Android" if r[0] else f"应用失败: {r[1]}",
                is_error=not r[0],
            ),
        )
