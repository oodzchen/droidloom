"""Hardware, GPU, device identity spoofing, audio, camera/peripherals, and cgroup limits page."""

from __future__ import annotations

import copy
import os

from PySide6 import QtCore, QtWidgets

from ..backend.android_bridge import apply_hardware_spoof_props, query_android_snapshot
from ..backend.cell_config import (
    build_runtime_env_for_gpu,
    detect_supervisor_extensions_support,
    list_host_gpus,
    load_cell_json,
    load_extensions_config,
    save_cell_spec_privileged,
    save_extensions_sidecar,
)
from ..backend.control_client import ControlClient
from ..backend.host_runtime import (
    ResourceLimits,
    control_system_service_privileged,
    list_braun_cameras,
    list_pipewire_sinks,
    load_resource_limits,
    parse_effective_resource_limits,
    query_droidloomd_effective_limits,
    save_resource_limits,
    systemctl_system_query,
    systemctl_user,
    systemctl_user_query,
)
from ..backend.user_configs import AudioSettings, load_audio_settings, save_audio_settings
from .widgets import (
    StatusIndicator,
    WorkerThread,
    WrappingLabel,
    format_bytes,
    make_form_layout,
    make_section_card,
    make_splitter,
    set_button_role,
    show_message,
    spawn_worker,
    style_table,
    wrap_resizable,
)


class HardwarePage(QtWidgets.QWidget):
    """GPU, OpenGL driver, hardware spoofing, audio, peripherals, and CPU/RAM cgroups."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = ControlClient()
        self._workers: list[WorkerThread] = []
        self._cell = load_cell_json()
        self._ext = load_extensions_config(self._cell)

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. GPU & Graphics Backend
        gpu_box, gpu_lay = make_section_card("图形")
        self.gpu_list_lbl = WrappingLabel("正在扫描 GPU...", muted=True)
        gpu_lay.addWidget(self.gpu_list_lbl)

        g_form = make_form_layout()
        self.gpu_choice_combo = QtWidgets.QComboBox()
        self.gpu_choice_combo.setEditable(True)
        for opt in ("auto", "amd", "intel", "nvidia", "qualcomm", "mali"):
            self.gpu_choice_combo.addItem(opt, opt)

        self.backend_combo = QtWidgets.QComboBox()
        for val, label in (
            ("drm", "drm (原生)"),
            ("drm_dma_heap", "drm_dma_heap"),
            ("kgsl_dma_heap", "kgsl_dma_heap"),
            ("forwarding", "forwarding (转发)"),
        ):
            self.backend_combo.addItem(label, val)

        self.render_node_edit = QtWidgets.QLineEdit(str(self._cell.get("render_node") or "/dev/dri/renderD128"))

        self.gl_driver_combo = QtWidgets.QComboBox()
        self.gl_driver_combo.addItem("ANGLE", "angle")
        self.gl_driver_combo.addItem("Mesa", "mesa")

        self.minigbm_lbl = WrappingLabel("—", muted=True)

        g_form.addRow("GPU:", self.gpu_choice_combo)
        g_form.addRow("后端:", self.backend_combo)
        g_form.addRow("节点:", self.render_node_edit)
        g_form.addRow("游戏 GL:", self.gl_driver_combo)
        g_form.addRow("UBWC:", self.minigbm_lbl)
        gpu_lay.addLayout(g_form)

        g_btns = QtWidgets.QHBoxLayout()
        g_btns.setSpacing(8)
        apply_gl_btn = QtWidgets.QPushButton("应用 GL")
        save_gpu_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        save_gpu_btn.clicked.connect(self._save_gpu_config)
        apply_gl_btn.clicked.connect(self._apply_games_driver)
        g_btns.addWidget(apply_gl_btn)
        g_btns.addWidget(save_gpu_btn)
        g_btns.addStretch(1)
        gpu_lay.addLayout(g_btns)
        root.addWidget(gpu_box)

        # 2. Hardware Spoofing & Device Identity
        spoof_box, spoof_lay = make_section_card("伪装")
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.setSpacing(6)
        preset_lbl = QtWidgets.QLabel("预设:")
        preset_lbl.setProperty("muted", True)
        preset_row.addWidget(preset_lbl)
        for title, soc, ren, ver, model, brand, maker in (
            (
                "骁龙 8 Elite / Adreno 830",
                "SM8750",
                "Adreno (TM) 830",
                "OpenGL ES 3.2 ANGLE",
                "OnePlus Pad 2 Pro",
                "OnePlus",
                "OnePlus",
            ),
            (
                "骁龙 8 Gen 3 / Adreno 750",
                "SM8650",
                "Adreno (TM) 750",
                "OpenGL ES 3.2 ANGLE",
                "Pixel 9 Pro",
                "google",
                "Google",
            ),
            (
                "清空",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
        ):
            b = QtWidgets.QPushButton(title)
            b.clicked.connect(
                lambda _=False, s=soc, r=ren, v=ver, m=model, br=brand, mk=maker: self._fill_spoof_preset(
                    s, r, v, m, br, mk
                )
            )
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        spoof_lay.addLayout(preset_row)

        sp_form = make_form_layout()
        self.soc_edit = QtWidgets.QLineEdit()
        self.soc_edit.setPlaceholderText("persist.vendor.droidloom.soc_model")
        self.angle_ren_edit = QtWidgets.QLineEdit()
        self.angle_ren_edit.setPlaceholderText("persist.vendor.droidloom.angle.gl_renderer")
        self.angle_ver_edit = QtWidgets.QLineEdit()
        self.angle_ver_edit.setPlaceholderText("persist.vendor.droidloom.angle.gl_version")
        self.live_ident_lbl = WrappingLabel("—", muted=True)

        sp_form.addRow("SoC:", self.soc_edit)
        sp_form.addRow("渲染器:", self.angle_ren_edit)
        sp_form.addRow("GL 版本:", self.angle_ver_edit)
        sp_form.addRow("运行时:", self.live_ident_lbl)
        spoof_lay.addLayout(sp_form)

        prop_btn_row = QtWidgets.QHBoxLayout()
        apply_prop_btn = QtWidgets.QPushButton("写入属性")
        apply_prop_btn.clicked.connect(self._apply_spoof_props)
        prop_btn_row.addWidget(apply_prop_btn)
        prop_btn_row.addStretch(1)
        spoof_lay.addLayout(prop_btn_row)

        # CellSpec.device_identity section
        id_form = make_form_layout()
        ident = self._ext.device_identity or {}
        self.id_model_edit = QtWidgets.QLineEdit(str(ident.get("model") or ""))
        self.id_model_edit.setPlaceholderText("留空使用宿主机型号")
        self.id_brand_edit = QtWidgets.QLineEdit(str(ident.get("brand") or ""))
        self.id_maker_edit = QtWidgets.QLineEdit(str(ident.get("manufacturer") or ""))
        self.id_name_edit = QtWidgets.QLineEdit(str(ident.get("device_name") or ""))
        id_form.addRow("型号:", self.id_model_edit)
        id_form.addRow("品牌:", self.id_brand_edit)
        id_form.addRow("厂商:", self.id_maker_edit)
        id_form.addRow("设备名:", self.id_name_edit)
        spoof_lay.addLayout(id_form)

        self.extra_props_table = QtWidgets.QTableWidget(0, 2)
        self.extra_props_table.setHorizontalHeaderLabels(["属性键", "值"])
        self.extra_props_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        style_table(self.extra_props_table)
        for k, v in sorted((ident.get("extra_properties") or {}).items()):
            r = self.extra_props_table.rowCount()
            self.extra_props_table.insertRow(r)
            self.extra_props_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(k)))
            self.extra_props_table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(v)))
        spoof_lay.addWidget(wrap_resizable(self.extra_props_table, default_height=135, min_height=88))

        id_btns = QtWidgets.QHBoxLayout()
        id_btns.setSpacing(8)
        add_prop_btn = QtWidgets.QPushButton("添加")
        del_prop_btn = QtWidgets.QPushButton("删除")
        save_ident_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        add_prop_btn.clicked.connect(
            lambda: self.extra_props_table.insertRow(self.extra_props_table.rowCount())
        )
        del_prop_btn.clicked.connect(
            lambda: (
                self.extra_props_table.removeRow(self.extra_props_table.currentRow())
                if self.extra_props_table.currentRow() >= 0
                else None
            )
        )
        save_ident_btn.clicked.connect(self._save_device_identity)
        id_btns.addWidget(add_prop_btn)
        id_btns.addWidget(del_prop_btn)
        id_btns.addStretch(1)
        id_btns.addWidget(save_ident_btn)
        spoof_lay.addLayout(id_btns)
        root.addWidget(spoof_box)

        # 3. Audio & Peripherals
        io_splitter = make_splitter(QtCore.Qt.Orientation.Horizontal)

        aud_box, aud_lay = make_section_card("音频")
        aud_cfg = load_audio_settings()
        self.mic_chk = QtWidgets.QCheckBox("允许麦克风")
        self.mic_chk.setChecked(aud_cfg.allow_capture)
        self.loopback_chk = QtWidgets.QCheckBox("环回采集")
        self.loopback_chk.setChecked(aud_cfg.capture_sink)
        self.pw_target_combo = QtWidgets.QComboBox()
        self.pw_target_combo.setEditable(True)
        self.pw_target_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.pw_target_combo.setMinimumContentsLength(16)
        self.pw_target_combo.addItem("默认", "")
        if aud_cfg.target:
            self.pw_target_combo.setCurrentText(aud_cfg.target)
        self.pw_remote_edit = QtWidgets.QLineEdit(aud_cfg.remote)
        self.pw_remote_edit.setPlaceholderText("可选 remote")
        self.audio_volume_mode_combo = QtWidgets.QComboBox()
        self.audio_volume_mode_combo.addItem("跟随运行模式（桌面独立流 / 移动主音量）", "auto")
        self.audio_volume_mode_combo.addItem("独立应用流音量 (stream，推荐)", "stream")
        self.audio_volume_mode_combo.addItem("联动宿主机主音量 (host)", "host")
        self.audio_volume_mode_combo.addItem("禁用音量同步 (off)", "off")
        idx_vol = self.audio_volume_mode_combo.findData(aud_cfg.volume_mode)
        if idx_vol >= 0:
            self.audio_volume_mode_combo.setCurrentIndex(idx_vol)

        aud_form = make_form_layout()
        aud_form.addRow("", self.mic_chk)
        aud_form.addRow("", self.loopback_chk)
        aud_form.addRow("音量模式:", self.audio_volume_mode_combo)
        aud_form.addRow("输出:", self.pw_target_combo)
        aud_form.addRow("Remote:", self.pw_remote_edit)
        aud_lay.addLayout(aud_form)

        aud_btn_row = QtWidgets.QHBoxLayout()
        save_aud_btn = set_button_role(QtWidgets.QPushButton("保存并重启"), "primary")
        save_aud_btn.clicked.connect(self._save_audio)
        aud_btn_row.addWidget(save_aud_btn)
        aud_btn_row.addStretch(1)
        aud_lay.addLayout(aud_btn_row)
        aud_lay.addStretch(1)
        io_splitter.addWidget(aud_box)

        per_box, per_lay = make_section_card("外设")
        self.braun_indicator = StatusIndicator("摄像头: 检测中...", "neutral")
        self.braun_detail_lbl = WrappingLabel("", muted=True)
        self.braun_detail_lbl.setVisible(False)
        self.devices_indicator = StatusIndicator("外设桥接: 检测中...", "neutral")
        per_lay.addWidget(self.braun_indicator)
        per_lay.addWidget(self.braun_detail_lbl)
        per_lay.addWidget(self.devices_indicator)
        per_lay.addStretch(1)

        per_btns = QtWidgets.QHBoxLayout()
        per_btns.setSpacing(8)
        braun_restart_btn = QtWidgets.QPushButton("重启摄像头")
        dev_restart_btn = QtWidgets.QPushButton("重启外设")
        braun_restart_btn.clicked.connect(
            lambda: self._spawn(
                lambda: control_system_service_privileged("droidloom-braun.service", "restart"),
                lambda r: (
                    show_message(self, "摄像头服务", r[1], is_error=not r[0]),
                    self.refresh_state(),
                ),
            )
        )
        dev_restart_btn.clicked.connect(
            lambda: self._spawn(
                lambda: systemctl_user(["restart", "droidloom-devices.service"]),
                lambda r: (
                    show_message(self, "外设服务", r[1] or "已重启", is_error=not r[0]),
                    self.refresh_state(),
                ),
            )
        )
        per_btns.addWidget(braun_restart_btn)
        per_btns.addWidget(dev_restart_btn)
        per_btns.addStretch(1)
        per_lay.addLayout(per_btns)
        io_splitter.addWidget(per_box)
        io_splitter.setSizes([420, 420])
        root.addWidget(io_splitter)

        # 4. Systemd cgroup v2 resource limits
        res_box, res_lay = make_section_card("限额")
        self.res_effective_lbl = WrappingLabel(
            "正在读取当前生效限额...",
            muted=True,
        )
        res_lay.addWidget(self.res_effective_lbl)
        limits = load_resource_limits()
        self._saved_limits = limits
        self._effective_defaults = ResourceLimits(cpu_weight=100, io_weight=100)
        self._limits_dirty = False
        r_grid = QtWidgets.QGridLayout()
        r_grid.setHorizontalSpacing(14)
        r_grid.setVerticalSpacing(10)
        r_grid.setColumnStretch(1, 1)
        r_grid.setColumnStretch(3, 1)

        self.cpu_quota_spin = QtWidgets.QSpinBox()
        self.cpu_quota_spin.setRange(0, 6400)
        self.cpu_quota_spin.setSpecialValueText("不限")
        self.cpu_quota_spin.setSuffix(" %")
        self.cpu_quota_spin.setValue(limits.cpu_quota_percent or 0)

        self.allowed_cpus_edit = QtWidgets.QLineEdit(limits.allowed_cpus)
        self.allowed_cpus_edit.setPlaceholderText(f"不限 (0-{max(0, (os.cpu_count() or 1) - 1)})")

        self.cpu_weight_spin = QtWidgets.QSpinBox()
        self.cpu_weight_spin.setRange(0, 10000)
        self.cpu_weight_spin.setSpecialValueText("默认 (100)")
        self.cpu_weight_spin.setValue(limits.cpu_weight or 100)

        self.mem_high_spin = QtWidgets.QSpinBox()
        self.mem_high_spin.setRange(0, 262144)
        self.mem_high_spin.setSpecialValueText("默认 (40%)")
        self.mem_high_spin.setSuffix(" MiB")
        self.mem_high_spin.setValue(limits.memory_high_mb or 0)

        self.mem_max_spin = QtWidgets.QSpinBox()
        self.mem_max_spin.setRange(0, 262144)
        self.mem_max_spin.setSpecialValueText("默认 (50%)")
        self.mem_max_spin.setSuffix(" MiB")
        self.mem_max_spin.setValue(limits.memory_max_mb or 0)

        self.swap_max_spin = QtWidgets.QSpinBox()
        self.swap_max_spin.setRange(0, 262144)
        self.swap_max_spin.setSpecialValueText("不限")
        self.swap_max_spin.setSuffix(" MiB")
        self.swap_max_spin.setValue(limits.memory_swap_max_mb or 0)

        self.tasks_max_spin = QtWidgets.QSpinBox()
        self.tasks_max_spin.setRange(0, 100000)
        self.tasks_max_spin.setSpecialValueText("默认 (8192)")
        self.tasks_max_spin.setValue(limits.tasks_max or 0)

        self.io_weight_spin = QtWidgets.QSpinBox()
        self.io_weight_spin.setRange(0, 10000)
        self.io_weight_spin.setSpecialValueText("默认 (100)")
        self.io_weight_spin.setValue(limits.io_weight or 100)

        for w in (
            self.cpu_quota_spin,
            self.cpu_weight_spin,
            self.mem_high_spin,
            self.mem_max_spin,
            self.swap_max_spin,
            self.tasks_max_spin,
            self.io_weight_spin,
        ):
            w.valueChanged.connect(lambda _v: setattr(self, "_limits_dirty", True))
        self.allowed_cpus_edit.textEdited.connect(
            lambda _t: setattr(self, "_limits_dirty", True)
        )

        align_r = QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        rows_cfg = [
            ("CPU 配额:", self.cpu_quota_spin, "CPU 核:", self.allowed_cpus_edit),
            ("CPU 权重:", self.cpu_weight_spin, "IO 权重:", self.io_weight_spin),
            ("内存软限:", self.mem_high_spin, "内存硬限:", self.mem_max_spin),
            ("Swap 上限:", self.swap_max_spin, "线程上限:", self.tasks_max_spin),
        ]
        for row_idx, (l1, w1, l2, w2) in enumerate(rows_cfg):
            lbl1 = QtWidgets.QLabel(l1)
            lbl1.setAlignment(align_r)
            lbl2 = QtWidgets.QLabel(l2)
            lbl2.setAlignment(align_r)
            r_grid.addWidget(lbl1, row_idx, 0)
            r_grid.addWidget(w1, row_idx, 1)
            r_grid.addWidget(lbl2, row_idx, 2)
            r_grid.addWidget(w2, row_idx, 3)
        res_lay.addLayout(r_grid)

        r_btns = QtWidgets.QHBoxLayout()
        r_btns.setSpacing(8)
        apply_res_btn = set_button_role(QtWidgets.QPushButton("应用"), "primary")
        reset_res_btn = QtWidgets.QPushButton("重置")
        apply_res_btn.clicked.connect(self._apply_resources)
        reset_res_btn.clicked.connect(self._reset_resources)
        r_btns.addWidget(apply_res_btn)
        r_btns.addWidget(reset_res_btn)
        r_btns.addStretch(1)
        res_lay.addLayout(r_btns)
        root.addWidget(res_box)

        root.addStretch(1)
        self._loaded = False
        self._load_cell_gpu_fields()

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh_state()

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _load_cell_gpu_fields(self) -> None:
        gpu_val = str(self._cell.get("gpu") or "auto")
        self.gpu_choice_combo.setCurrentText(gpu_val)
        backend = str(self._cell.get("graphics_backend") or "drm")
        idx = self.backend_combo.findData(backend)
        if idx >= 0:
            self.backend_combo.setCurrentIndex(idx)

    def refresh_state(self) -> None:
        def fetch():
            gpus = list_host_gpus()
            sinks = list_pipewire_sinks()
            braun_unit = systemctl_system_query("droidloom-braun.service")
            cams = list_braun_cameras()
            dev_unit = systemctl_user_query("droidloom-devices.service")
            try:
                gl_drv = self.client.games_driver()
            except Exception:
                gl_drv = ""
            eff_limits = query_droidloomd_effective_limits()
            snap = query_android_snapshot()
            return gpus, sinks, braun_unit, cams, dev_unit, gl_drv, eff_limits, snap

        def done(data):
            gpus, sinks, braun_unit, cams, dev_unit, gl_drv, eff_limits, snap = data
            if gpus:
                self.gpu_list_lbl.setText(
                    "已检测到宿主机 GPU:\n" + "\n".join(f"• {g.label}" for g in gpus)
                )
                for g in gpus:
                    if self.gpu_choice_combo.findText(g.device) < 0 and ":" in g.device:
                        self.gpu_choice_combo.addItem(g.device, g.device)
            else:
                self.gpu_list_lbl.setText("未在 /sys/class/drm 检测到可用 renderD* 节点")

            cur_target = self.pw_target_combo.currentText()
            self.pw_target_combo.clear()
            self.pw_target_combo.addItem("默认", "")
            for s in sinks:
                self.pw_target_combo.addItem(f"{s['description']} ({s['name']})", s["name"])
            if cur_target:
                self.pw_target_combo.setCurrentText(cur_target)

            b_state = braun_unit.get("ActiveState", "unknown")
            b_tone = "ok" if b_state == "active" else ("neutral" if b_state == "inactive" else "warn")
            self.braun_indicator.set_status(f"摄像头服务: {b_state}", b_tone)
            cam_desc = (
                ", ".join(
                    f"{c.get('name', '?')} ({c.get('facing', '?')}, {c.get('width', '?')}x{c.get('height', '?')})"
                    for c in cams
                )
                if cams
                else "无可用摄像头"
            )
            self.braun_detail_lbl.setText(cam_desc)
            self.braun_detail_lbl.setVisible(True)

            d_state = dev_unit.get("ActiveState", "unknown")
            d_sub = dev_unit.get("SubState", "")
            d_tone = "ok" if d_state == "active" else ("neutral" if d_state == "inactive" else "warn")
            suffix = f" ({d_sub})" if d_sub else ""
            self.devices_indicator.set_status(f"外设桥接: {d_state}{suffix}", d_tone)

            if gl_drv:
                idx = self.gl_driver_combo.findData(gl_drv)
                if idx >= 0:
                    self.gl_driver_combo.setCurrentIndex(idx)

            if eff_limits:
                self._apply_effective_limits_to_ui(eff_limits)

            if snap.connected:
                self.soc_edit.setText(snap.soc_model_persist)
                self.angle_ren_edit.setText(snap.angle_renderer_persist)
                self.angle_ver_edit.setText(snap.angle_version_persist)
                self.minigbm_lbl.setText(snap.minigbm_debug or "(未设置)")
                self.live_ident_lbl.setText(
                    f"Model={snap.ro_product_model}, Brand={snap.ro_product_brand}, "
                    f"Manufacturer={snap.ro_product_manufacturer}, Vulkan={snap.ro_hardware_vulkan}, "
                    f"SoC={snap.soc_model_live or '(默认)'}"
                )

        self._spawn(fetch, done)

    def _fill_spoof_preset(
        self, soc: str, ren: str, ver: str, model: str, brand: str, maker: str
    ) -> None:
        self.soc_edit.setText(soc)
        self.angle_ren_edit.setText(ren)
        self.angle_ver_edit.setText(ver)
        self.id_model_edit.setText(model)
        self.id_brand_edit.setText(brand)
        self.id_maker_edit.setText(maker)

    def _save_gpu_config(self) -> None:
        if not self._cell:
            show_message(self, "错误", "未能加载 /etc/droidloom/cell.json", is_error=True)
            return
        updated = copy.deepcopy(self._cell)
        choice = self.gpu_choice_combo.currentText().strip() or "auto"
        if choice == "auto":
            updated.pop("gpu", None)
        else:
            updated["gpu"] = choice
        updated["graphics_backend"] = self.backend_combo.currentData()
        updated["render_node"] = self.render_node_edit.text().strip() or "/dev/dri/renderD128"
        env_lines = build_runtime_env_for_gpu(choice)
        self._spawn(
            lambda: save_cell_spec_privileged(updated, runtime_env_lines=env_lines),
            lambda r: (
                setattr(self, "_cell", updated) if r[0] else None,
                show_message(self, "GPU 配置", r[1], is_error=not r[0]),
            ),
        )

    def _apply_games_driver(self) -> None:
        drv = str(self.gl_driver_combo.currentData())
        self._spawn(
            lambda: self.client.set_games_driver(drv),
            lambda r: show_message(self, "游戏 OpenGL 驱动", r.message),
            lambda e: show_message(self, "设置失败", e, is_error=True),
        )

    def _apply_spoof_props(self) -> None:
        self._spawn(
            lambda: apply_hardware_spoof_props(
                self.soc_edit.text(), self.angle_ren_edit.text(), self.angle_ver_edit.text()
            ),
            lambda r: show_message(
                self,
                "硬件属性伪装",
                "已写入持久化属性，下次启动 Droidloom 后生效。" if r[0] else f"写入失败: {r[1]}",
                is_error=not r[0],
            ),
        )

    def _save_device_identity(self) -> None:
        ident: dict = {}
        if self.id_model_edit.text().strip():
            ident["model"] = self.id_model_edit.text().strip()
        if self.id_brand_edit.text().strip():
            ident["brand"] = self.id_brand_edit.text().strip()
        if self.id_maker_edit.text().strip():
            ident["manufacturer"] = self.id_maker_edit.text().strip()
        if self.id_name_edit.text().strip():
            ident["device_name"] = self.id_name_edit.text().strip()
        extra = {}
        for r in range(self.extra_props_table.rowCount()):
            k_it = self.extra_props_table.item(r, 0)
            v_it = self.extra_props_table.item(r, 1)
            k = k_it.text().strip() if k_it else ""
            v = v_it.text().strip() if v_it else ""
            if k:
                extra[k] = v
        if extra:
            ident["extra_properties"] = extra

        self._ext.device_identity = ident
        save_extensions_sidecar(self._ext)

        if detect_supervisor_extensions_support(base_spec=self._cell) and self._cell:
            updated = copy.deepcopy(self._cell)
            if ident:
                updated["device_identity"] = ident
            else:
                updated.pop("device_identity", None)
            self._spawn(
                lambda: save_cell_spec_privileged(updated),
                lambda r: (
                    setattr(self, "_cell", updated) if r[0] else None,
                    show_message(self, "设备身份配置", r[1], is_error=not r[0]),
                ),
            )
        else:
            show_message(
                self,
                "设备身份配置",
                "已保存到 ~/.config/droidloom/extensions-v1.json（升级/安装扩展版 supervisor 后自动写入 cell.json）。",
            )

    def _save_audio(self) -> None:
        target_data = self.pw_target_combo.currentData()
        target = target_data if isinstance(target_data, str) and target_data else ""
        if not target:
            txt = self.pw_target_combo.currentText().strip()
            if txt and txt not in ("默认", "系统默认策略"):
                target = txt
        cfg = AudioSettings(
            allow_capture=self.mic_chk.isChecked(),
            target=target,
            remote=self.pw_remote_edit.text().strip(),
            capture_sink=self.loopback_chk.isChecked(),
            volume_mode=str(self.audio_volume_mode_combo.currentData() or "auto"),
        )
        save_audio_settings(cfg)
        self._spawn(
            lambda: (
                systemctl_user(["daemon-reload"]),
                systemctl_user(["restart", "droidloom-audio.service"]),
            )[1],
            lambda r: show_message(
                self,
                "音频设置",
                "已保存并重启 droidloom-audio.service" if r[0] else f"重启失败: {r[1]}",
                is_error=not r[0],
            ),
        )

    def _apply_effective_limits_to_ui(self, eff_limits: dict[str, str]) -> None:
        eff = parse_effective_resource_limits(eff_limits)
        self._effective_defaults = eff
        if not self._limits_dirty:
            pairs = (
                (self.cpu_quota_spin, self._saved_limits.cpu_quota_percent, eff.cpu_quota_percent),
                (self.cpu_weight_spin, self._saved_limits.cpu_weight, eff.cpu_weight),
                (self.mem_high_spin, self._saved_limits.memory_high_mb, eff.memory_high_mb),
                (self.mem_max_spin, self._saved_limits.memory_max_mb, eff.memory_max_mb),
                (self.swap_max_spin, self._saved_limits.memory_swap_max_mb, eff.memory_swap_max_mb),
                (self.tasks_max_spin, self._saved_limits.tasks_max, eff.tasks_max),
                (self.io_weight_spin, self._saved_limits.io_weight, eff.io_weight),
            )
            for spin, saved_val, eff_val in pairs:
                if saved_val is None:
                    spin.blockSignals(True)
                    spin.setValue(eff_val or 0)
                    spin.blockSignals(False)
            if not self._saved_limits.allowed_cpus:
                self.allowed_cpus_edit.blockSignals(True)
                self.allowed_cpus_edit.setText(eff.allowed_cpus)
                self.allowed_cpus_edit.blockSignals(False)

        def _fmt_mem(raw: str | None) -> str:
            if not raw or raw in ("infinity", "[not set]"):
                return "无上限"
            return format_bytes(int(raw)) if raw.isdigit() else raw

        mh = _fmt_mem(eff_limits.get("MemoryHigh"))
        mm = _fmt_mem(eff_limits.get("MemoryMax"))
        tm = eff_limits.get("TasksMax") or "—"
        self.res_effective_lbl.setText(
            f"当前生效：内存软限 {mh}，硬限 {mm}，线程上限 {tm}（未修改项保持单元默认策略，点击「重置」恢复默认）"
        )

    def _apply_resources(self) -> None:
        def _resolve_num(
            spin: QtWidgets.QSpinBox,
            saved_val: int | None,
            eff_default: int | None,
        ) -> int | None:
            v = spin.value()
            if v <= 0:
                return None
            if saved_val is None and eff_default is not None and v == eff_default:
                return None
            return v

        eff = self._effective_defaults
        limits = ResourceLimits(
            cpu_quota_percent=_resolve_num(
                self.cpu_quota_spin, self._saved_limits.cpu_quota_percent, eff.cpu_quota_percent
            ),
            allowed_cpus=(
                ""
                if not self._saved_limits.allowed_cpus
                and self.allowed_cpus_edit.text().strip() == eff.allowed_cpus
                else self.allowed_cpus_edit.text().strip()
            ),
            cpu_weight=_resolve_num(
                self.cpu_weight_spin, self._saved_limits.cpu_weight, eff.cpu_weight
            ),
            memory_high_mb=_resolve_num(
                self.mem_high_spin, self._saved_limits.memory_high_mb, eff.memory_high_mb
            ),
            memory_max_mb=_resolve_num(
                self.mem_max_spin, self._saved_limits.memory_max_mb, eff.memory_max_mb
            ),
            memory_swap_max_mb=_resolve_num(
                self.swap_max_spin, self._saved_limits.memory_swap_max_mb, eff.memory_swap_max_mb
            ),
            tasks_max=_resolve_num(
                self.tasks_max_spin, self._saved_limits.tasks_max, eff.tasks_max
            ),
            io_weight=_resolve_num(
                self.io_weight_spin, self._saved_limits.io_weight, eff.io_weight
            ),
        )
        self._spawn(
            lambda: save_resource_limits(limits),
            lambda r: (
                show_message(self, "资源限制", r[1], is_error=not r[0]),
                setattr(self, "_saved_limits", load_resource_limits()) if r[0] else None,
                setattr(self, "_limits_dirty", False) if r[0] else None,
                self.refresh_state() if r[0] else None,
            ),
        )

    def _reset_resources(self) -> None:
        self._spawn(
            lambda: save_resource_limits(ResourceLimits()),
            lambda r: (
                show_message(self, "资源限制", r[1], is_error=not r[0]),
                setattr(self, "_saved_limits", ResourceLimits()) if r[0] else None,
                setattr(self, "_limits_dirty", False) if r[0] else None,
                self.refresh_state() if r[0] else None,
            ),
        )
