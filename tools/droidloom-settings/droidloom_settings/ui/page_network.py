"""Network translation, host proxy bridge, subnet/DNS, port forwarding, and Android Private DNS page."""

from __future__ import annotations

import copy
import os

from PySide6 import QtCore, QtWidgets

from ..backend.android_bridge import (
    apply_android_private_dns,
    apply_android_proxy,
    query_android_snapshot,
)
from ..backend.cell_config import (
    detect_supervisor_extensions_support,
    load_cell_json,
    load_extensions_config,
    save_cell_spec_privileged,
    save_extensions_sidecar,
)
from ..backend.host_runtime import (
    apply_live_nftables_rules,
    check_firewalld_droidloom_status,
    check_host_ipv4_forwarding,
    detect_host_proxy_candidates,
    list_host_network_interfaces,
)
from .widgets import (
    WorkerThread,
    WrappingLabel,
    make_form_layout,
    make_section_card,
    make_splitter,
    set_button_role,
    show_message,
    spawn_worker,
    style_table,
    wrap_resizable,
)


class NetworkPage(QtWidgets.QWidget):
    """Network translation, proxy bridge, firewall, and DNS settings."""

    status_hint = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._workers: list[WorkerThread] = []
        self._cell = load_cell_json()
        self._ext = load_extensions_config(self._cell)

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. One-click Host Proxy Bridge
        proxy_box, proxy_lay = make_section_card("代理")
        p_form = make_form_layout()
        detect_row = QtWidgets.QHBoxLayout()
        detect_row.setSpacing(8)
        self.detected_combo = QtWidgets.QComboBox()
        self.detected_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.detected_combo.setMinimumContentsLength(18)
        self.detected_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.scan_proxy_btn = QtWidgets.QPushButton("扫描")
        self.scan_proxy_btn.clicked.connect(self._scan_proxies)
        detect_row.addWidget(self.detected_combo, 1)
        detect_row.addWidget(self.scan_proxy_btn)

        self.bridge_enable_chk = QtWidgets.QCheckBox("启用代理转发")
        self.bridge_port_spin = QtWidgets.QSpinBox()
        self.bridge_port_spin.setRange(1, 65535)
        self.bridge_port_spin.setValue(self._ext.proxy_bridge_port)
        self.bridge_bypass_edit = QtWidgets.QLineEdit(self._ext.proxy_bridge_bypass)
        hint_lbl = WrappingLabel("请开启宿主机代理客户端的「允许局域网连接」", muted=True)

        p_form.addRow("探测代理:", detect_row)
        p_form.addRow("", self.bridge_enable_chk)
        p_form.addRow("端口:", self.bridge_port_spin)
        p_form.addRow("绕过:", self.bridge_bypass_edit)
        p_form.addRow("", hint_lbl)
        proxy_lay.addLayout(p_form)

        self.detected_combo.currentIndexChanged.connect(self._on_detected_proxy_changed)

        p_btn_row = QtWidgets.QHBoxLayout()
        self.apply_proxy_btn = set_button_role(QtWidgets.QPushButton("应用"), "primary")
        self.apply_proxy_btn.clicked.connect(self._apply_proxy_bridge)
        p_btn_row.addWidget(self.apply_proxy_btn)
        p_btn_row.addStretch(1)
        proxy_lay.addLayout(p_btn_row)
        root.addWidget(proxy_box)

        # 2. Network translation & DHCP subnet (CellSpec.network)
        net_box, net_lay = make_section_card("网卡")
        n_form = make_form_layout()
        self.net_mode_combo = QtWidgets.QComboBox()
        self.net_mode_combo.addItem("NAT", "nat")
        self.net_mode_combo.addItem("桥接 (Bridge)", "bridge")
        self.net_mode_combo.addItem("宿主机 (Host)", "host")
        self.net_mode_combo.addItem("离线", "offline")
        self.net_mode_combo.currentIndexChanged.connect(self._update_mode_fields)

        self.bridge_combo = QtWidgets.QComboBox()
        self.bridge_combo.setEditable(False)
        self.bridge_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.bridge_combo.setMinimumContentsLength(22)
        self.bridge_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.bridge_combo.currentIndexChanged.connect(lambda _idx: self._update_bridge_notice())

        self.refresh_bridge_btn = QtWidgets.QPushButton("刷新")
        self.refresh_bridge_btn.clicked.connect(self._refresh_bridge_interfaces)

        self._bridge_row_widget = QtWidgets.QWidget()
        br_row = QtWidgets.QHBoxLayout(self._bridge_row_widget)
        br_row.setContentsMargins(0, 0, 0, 0)
        br_row.setSpacing(8)
        br_row.addWidget(self.bridge_combo, 1)
        br_row.addWidget(self.refresh_bridge_btn)

        self.mode_hint_lbl = WrappingLabel("", muted=True)
        self.bridge_notice_lbl = WrappingLabel("", muted=True)
        self.bridge_dhcp_lbl = WrappingLabel("自动获取 (DHCP)", muted=False)
        self.bridge_static_ip_chk = QtWidgets.QCheckBox("指定已知静态 IP（仅用于 ADB 连接）")
        self.bridge_static_ip_chk.toggled.connect(lambda _chk: self._update_mode_fields())
        self.bridge_adb_ip_edit = QtWidgets.QLineEdit()
        self.bridge_adb_ip_edit.setPlaceholderText("可选，例如 192.168.1.50")

        self.host_ip_edit = QtWidgets.QLineEdit("10.177.0.1")
        self.cell_ip_edit = QtWidgets.QLineEdit("10.177.0.2")
        self.prefix_spin = QtWidgets.QSpinBox()
        self.prefix_spin.setRange(8, 30)
        self.prefix_spin.setValue(30)
        self.mac_edit = QtWidgets.QLineEdit("02:64:6c:00:00:02")
        self.dns_edit = QtWidgets.QLineEdit("1.1.1.1, 8.8.8.8")

        dns_presets = QtWidgets.QHBoxLayout()
        dns_presets.setContentsMargins(0, 0, 0, 0)
        dns_presets.setSpacing(6)
        for label, val in (
            ("Cloudflare + Google", "1.1.1.1, 8.8.8.8"),
            ("阿里 + 腾讯", "223.5.5.5, 119.29.29.29"),
            ("Quad9 + 1.1.1.1", "9.9.9.9, 1.1.1.1"),
        ):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, v=val: self.dns_edit.setText(v))
            dns_presets.addWidget(b)
        dns_presets.addStretch(1)
        self._dns_preset_widget = QtWidgets.QWidget()
        self._dns_preset_widget.setLayout(dns_presets)

        self._net_form = n_form
        n_form.addRow("模式:", self.net_mode_combo)
        n_form.addRow("说明:", self.mode_hint_lbl)
        n_form.addRow("网卡:", self._bridge_row_widget)
        n_form.addRow("", self.bridge_notice_lbl)
        n_form.addRow("IP 分配:", self.bridge_dhcp_lbl)
        n_form.addRow("", self.bridge_static_ip_chk)
        n_form.addRow("ADB IP:", self.bridge_adb_ip_edit)
        n_form.addRow("网关:", self.host_ip_edit)
        n_form.addRow("容器 IP:", self.cell_ip_edit)
        n_form.addRow("掩码:", self.prefix_spin)
        n_form.addRow("MAC:", self.mac_edit)
        n_form.addRow("DNS:", self.dns_edit)
        n_form.addRow("预设:", self._dns_preset_widget)
        net_lay.addLayout(n_form)

        self.cap_label = WrappingLabel("", muted=True)
        self.cap_label.setVisible(False)
        net_lay.addWidget(self.cap_label)
        root.addWidget(net_box)

        # 3. Allow Host Ports & Port Forwarding tables (resizable splitter + textarea grips)
        rules_splitter = make_splitter(QtCore.Qt.Orientation.Horizontal)
        self._rules_splitter = rules_splitter

        allow_box, allow_lay = make_section_card("放行")
        self.allow_table = QtWidgets.QTableWidget(0, 2)
        self.allow_table.setHorizontalHeaderLabels(["协议", "端口"])
        self.allow_table.horizontalHeader().setStretchLastSection(True)
        style_table(self.allow_table)
        allow_lay.addWidget(wrap_resizable(self.allow_table, default_height=150, min_height=88))
        ab = QtWidgets.QHBoxLayout()
        ab.setSpacing(8)
        add_allow_btn = QtWidgets.QPushButton("添加")
        del_allow_btn = QtWidgets.QPushButton("移除")
        add_allow_btn.clicked.connect(self._add_allow_port)
        del_allow_btn.clicked.connect(lambda: self._remove_selected_row(self.allow_table))
        ab.addWidget(add_allow_btn)
        ab.addWidget(del_allow_btn)
        ab.addStretch(1)
        allow_lay.addLayout(ab)
        rules_splitter.addWidget(allow_box)

        fwd_box, fwd_lay = make_section_card("转发")
        self.fwd_table = QtWidgets.QTableWidget(0, 4)
        self.fwd_table.setHorizontalHeaderLabels(["协议", "监听地址", "宿主端口", "容器端口"])
        self.fwd_table.horizontalHeader().setStretchLastSection(True)
        style_table(self.fwd_table)
        fwd_lay.addWidget(wrap_resizable(self.fwd_table, default_height=150, min_height=88))
        fb = QtWidgets.QHBoxLayout()
        fb.setSpacing(8)
        add_fwd_btn = QtWidgets.QPushButton("添加")
        del_fwd_btn = QtWidgets.QPushButton("移除")
        add_fwd_btn.clicked.connect(self._add_port_forward)
        del_fwd_btn.clicked.connect(lambda: self._remove_selected_row(self.fwd_table))
        fb.addWidget(add_fwd_btn)
        fb.addWidget(del_fwd_btn)
        fb.addStretch(1)
        fwd_lay.addLayout(fb)
        rules_splitter.addWidget(fwd_box)
        rules_splitter.setSizes([280, 460])
        root.addWidget(rules_splitter)

        save_net_row = QtWidgets.QHBoxLayout()
        save_net_row.setSpacing(8)
        self.apply_live_rules_btn = QtWidgets.QPushButton("热应用")
        self.save_net_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        self.apply_live_rules_btn.clicked.connect(self._apply_live_nft)
        self.save_net_btn.clicked.connect(self._save_network_config)
        save_net_row.addWidget(self.apply_live_rules_btn)
        save_net_row.addWidget(self.save_net_btn)
        save_net_row.addStretch(1)
        root.addLayout(save_net_row)

        # 4. Android Private DNS & Live host diagnostics
        adns_box, adns_lay = make_section_card("DNS")
        adns_form = make_form_layout()
        self.pdns_mode_combo = QtWidgets.QComboBox()
        self.pdns_mode_combo.addItem("自动", "opportunistic")
        self.pdns_mode_combo.addItem("关闭", "off")
        self.pdns_mode_combo.addItem("主机名", "hostname")
        self.pdns_host_edit = QtWidgets.QLineEdit()
        self.pdns_host_edit.setPlaceholderText("例如 dns.alidns.com 或 one.one.one.one")
        self.host_fwd_lbl = WrappingLabel("检测中...", muted=True)
        self.firewalld_lbl = WrappingLabel("检测中...", muted=True)

        adns_form.addRow("Private DNS:", self.pdns_mode_combo)
        adns_form.addRow("主机名:", self.pdns_host_edit)
        adns_form.addRow("IPv4 转发:", self.host_fwd_lbl)
        adns_form.addRow("Firewalld:", self.firewalld_lbl)
        adns_lay.addLayout(adns_form)

        adns_btn_row = QtWidgets.QHBoxLayout()
        adns_btn = set_button_role(QtWidgets.QPushButton("应用 DNS"), "primary")
        adns_btn.clicked.connect(self._apply_private_dns)
        adns_btn_row.addWidget(adns_btn)
        adns_btn_row.addStretch(1)
        adns_lay.addLayout(adns_btn_row)
        root.addWidget(adns_box)

        root.addStretch(1)
        self._loaded = False
        self._load_into_ui()

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self._scan_proxies()
            self.refresh_live_state()

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _populate_bridge_combo(
        self,
        interfaces: list[dict] | None = None,
        preferred_name: str | None = None,
    ) -> None:
        if interfaces is None:
            interfaces = list_host_network_interfaces()
        cur_data = self.bridge_combo.currentData()
        cur_name = cur_data.get("name") if isinstance(cur_data, dict) else None
        target_name = preferred_name if preferred_name is not None else cur_name

        self.bridge_combo.blockSignals(True)
        self.bridge_combo.clear()
        seen: set[str] = set()
        for item in interfaces:
            name = str(item.get("name") or "")
            if not name:
                continue
            seen.add(name)
            kind = str(item.get("kind") or "other")
            state = str(item.get("state") or "unknown")
            if kind == "bridge":
                label = f"{name}  (Linux 网桥 · {state})"
            elif kind == "ethernet":
                label = f"{name}  (有线网卡 · {state} · 未建网桥)"
            elif kind == "wifi":
                label = f"{name}  (Wi-Fi · {state} · 不支持直接桥接)"
            else:
                label = f"{name}  ({state})"
            self.bridge_combo.addItem(label, item)

        if target_name and target_name not in seen:
            self.bridge_combo.addItem(
                f"{target_name}  (已保存配置)",
                {"name": target_name, "kind": "bridge", "state": "configured", "bridge_ready": True},
            )

        if self.bridge_combo.count() == 0:
            self.bridge_combo.addItem("未检测到宿主机网卡或网桥", None)
        else:
            sel_idx = 0
            if target_name:
                for i in range(self.bridge_combo.count()):
                    d = self.bridge_combo.itemData(i)
                    if isinstance(d, dict) and d.get("name") == target_name:
                        sel_idx = i
                        break
            self.bridge_combo.setCurrentIndex(sel_idx)
        self.bridge_combo.blockSignals(False)
        self._update_bridge_notice()

    def _refresh_bridge_interfaces(self) -> None:
        self._populate_bridge_combo(list_host_network_interfaces())

    def _update_bridge_notice(self) -> None:
        data = self.bridge_combo.currentData()
        if not isinstance(data, dict) or not data.get("name"):
            self.bridge_notice_lbl.setText(
                "未检测到可用 Linux 网桥，请先在宿主机创建网桥，或改用「宿主机 (Host)」模式。"
            )
            return
        name = str(data.get("name"))
        kind = str(data.get("kind") or "other")
        if data.get("bridge_ready"):
            self.bridge_notice_lbl.setText(
                f"容器将接入网桥 {name}，由上游 DHCP 自动分配局域网 IP。"
            )
        elif kind == "wifi":
            self.bridge_notice_lbl.setText(
                f"当前所选 {name} 为 Wi-Fi 网卡，Linux 内核不支持直接二层桥接普通无线网卡；建议改用「宿主机 (Host)」或「NAT」模式。"
            )
        else:
            self.bridge_notice_lbl.setText(
                f"{name} 尚未加入 Linux 网桥；请先在 NetworkManager 中创建桥接接口（如 br0）绑定该网卡，或改用「宿主机 (Host)」模式。"
            )

    def _load_into_ui(self) -> None:
        net = self._ext.network or {}
        mode = str(net.get("mode") or "nat")
        idx = self.net_mode_combo.findData(mode)
        self.net_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        saved_br = str(net.get("bridge_interface") or "").strip()
        self._populate_bridge_combo(preferred_name=saved_br or None)

        if mode == "bridge":
            saved_ip = str(net.get("cell_ipv4") or "").strip()
            has_custom_ip = bool(saved_ip and saved_ip != "10.177.0.2")
            self.bridge_static_ip_chk.setChecked(has_custom_ip)
            self.bridge_adb_ip_edit.setText(saved_ip if has_custom_ip else "")
        else:
            self.bridge_static_ip_chk.setChecked(False)
            self.bridge_adb_ip_edit.clear()

        self.host_ip_edit.setText(str(net.get("host_ipv4") or "10.177.0.1"))
        self.cell_ip_edit.setText(str(net.get("cell_ipv4") or "10.177.0.2"))
        self.prefix_spin.setValue(int(net.get("prefix_len") or 30))
        self.mac_edit.setText(str(net.get("cell_mac") or "02:64:6c:00:00:02"))
        dns = net.get("dns") or ["1.1.1.1", "8.8.8.8"]
        self.dns_edit.setText(", ".join(str(x) for x in dns))
        self._update_mode_fields()

        self.bridge_enable_chk.setChecked(self._ext.proxy_bridge_enabled)
        self.bridge_port_spin.setValue(self._ext.proxy_bridge_port)
        self.bridge_bypass_edit.setText(self._ext.proxy_bridge_bypass)

        self.allow_table.setRowCount(0)
        for item in net.get("allow_host_ports") or []:
            self._insert_allow_row(str(item.get("protocol", "tcp")), int(item.get("port", 7890)))

        self.fwd_table.setRowCount(0)
        for item in net.get("port_forwards") or []:
            self._insert_fwd_row(
                str(item.get("protocol", "tcp")),
                str(item.get("bind_address") or "127.0.0.1"),
                int(item.get("host_port", 8080)),
                int(item.get("cell_port", 8080)),
            )

    def _insert_allow_row(self, proto: str, port: int) -> None:
        row = self.allow_table.rowCount()
        self.allow_table.insertRow(row)
        cb = QtWidgets.QComboBox()
        cb.addItems(["tcp", "udp"])
        cb.setCurrentText(proto.lower())
        self.allow_table.setCellWidget(row, 0, cb)
        sp = QtWidgets.QSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(port)
        self.allow_table.setCellWidget(row, 1, sp)

    def _insert_fwd_row(self, proto: str, bind: str, hp: int, cp: int) -> None:
        row = self.fwd_table.rowCount()
        self.fwd_table.insertRow(row)
        cb = QtWidgets.QComboBox()
        cb.addItems(["tcp", "udp"])
        cb.setCurrentText(proto.lower())
        self.fwd_table.setCellWidget(row, 0, cb)
        self.fwd_table.setItem(row, 1, QtWidgets.QTableWidgetItem(bind))
        sp_h = QtWidgets.QSpinBox()
        sp_h.setRange(1, 65535)
        sp_h.setValue(hp)
        self.fwd_table.setCellWidget(row, 2, sp_h)
        sp_c = QtWidgets.QSpinBox()
        sp_c.setRange(1, 65535)
        sp_c.setValue(cp)
        self.fwd_table.setCellWidget(row, 3, sp_c)

    def _add_allow_port(self) -> None:
        self._insert_allow_row("tcp", self.bridge_port_spin.value())

    def _add_port_forward(self) -> None:
        self._insert_fwd_row("tcp", "127.0.0.1", 18080, 8080)

    def _remove_selected_row(self, table: QtWidgets.QTableWidget) -> None:
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)

    def _scan_proxies(self) -> None:
        self._spawn(detect_host_proxy_candidates, self._on_proxies_scanned)

    def _on_proxies_scanned(self, items: list[dict]) -> None:
        self.detected_combo.blockSignals(True)
        self.detected_combo.clear()
        if not items:
            self.detected_combo.addItem("未检测到常见本地代理端口（可手动输入端口）", None)
        else:
            for item in items:
                self.detected_combo.addItem(
                    f"{item['label']} — 端口 {item['port']} ({item['addr']})",
                    item["port"],
                )
        self.detected_combo.blockSignals(False)

    def _on_detected_proxy_changed(self, idx: int) -> None:
        port = self.detected_combo.itemData(idx)
        if isinstance(port, int):
            self.bridge_port_spin.setValue(port)

    def _update_mode_fields(self) -> None:
        mode = str(self.net_mode_combo.currentData() or "nat")
        hints = {
            "nat": "独立命名空间 + 私有 veth + NAT 隔离出站（默认推荐）",
            "bridge": "接入宿主机网桥（如 br0/virbr0），由上游 DHCP 自动分配同局域网 IP（Wi-Fi 通常不支持直接桥接）",
            "host": "共享宿主机网络命名空间：直接复用宿主机 IPv4/公网 IPv6/Wi-Fi/VPN/TUN 与 127.0.0.1（无 NAT），隔离保护宿主机路由表，ADB 仅监听 127.0.0.1:5555",
            "offline": "禁止外网出站转发，仅保留容器与宿主机之间的私有通信",
        }
        self.mode_hint_lbl.setText(hints.get(mode, ""))
        is_bridge = mode == "bridge"
        is_veth_nat = mode in ("nat", "offline")

        self._net_form.setRowVisible(self._bridge_row_widget, is_bridge)
        self._net_form.setRowVisible(self.bridge_notice_lbl, is_bridge)
        self._net_form.setRowVisible(self.bridge_dhcp_lbl, is_bridge)
        self._net_form.setRowVisible(self.bridge_static_ip_chk, is_bridge)
        self._net_form.setRowVisible(
            self.bridge_adb_ip_edit,
            is_bridge and self.bridge_static_ip_chk.isChecked(),
        )
        for w in (
            self.host_ip_edit,
            self.cell_ip_edit,
            self.prefix_spin,
            self.dns_edit,
            self._dns_preset_widget,
        ):
            self._net_form.setRowVisible(w, is_veth_nat)
        self._net_form.setRowVisible(self.mac_edit, mode != "host")
        self._rules_splitter.setVisible(is_veth_nat)
        self.apply_live_rules_btn.setVisible(is_veth_nat)
        self.apply_live_rules_btn.setEnabled(is_veth_nat)
        if is_bridge:
            self._update_bridge_notice()

    def refresh_live_state(self) -> None:
        def fetch():
            ext_ok = detect_supervisor_extensions_support(base_spec=self._cell)
            fwd = check_host_ipv4_forwarding()
            fw = check_firewalld_droidloom_status()
            interfaces = list_host_network_interfaces()
            mode = str(self.net_mode_combo.currentData() or "nat")
            if mode == "host":
                adb_ip = "127.0.0.1"
            elif mode == "bridge":
                adb_ip = (
                    self.bridge_adb_ip_edit.text().strip()
                    if self.bridge_static_ip_chk.isChecked()
                    else ""
                ) or "10.177.0.2"
            else:
                adb_ip = self.cell_ip_edit.text().strip() or "10.177.0.2"
            snap = query_android_snapshot(adb_ip)
            return ext_ok, fwd, fw, interfaces, snap

        def done(data):
            ext_ok, fwd, fw, interfaces, snap = data
            self._populate_bridge_combo(interfaces=interfaces)
            if ext_ok:
                self.cap_label.setText("当前 supervisor 已原生支持扩展网络字段")
                self.status_hint.emit("Supervisor 原生扩展已启用")
            else:
                self.cap_label.setText(
                    "当前 supervisor (0.1.0-42) 通过热规则支持代理与转发；模式/子网/MAC 暂存于用户配置"
                )
                self.status_hint.emit("Supervisor 0.1.0-42 兼容模式")
            self.cap_label.setVisible(True)

            self.host_fwd_lbl.setText(
                "已开启 (1)" if fwd is True else ("未开启 (0)" if fwd is False else "未知")
            )
            self.firewalld_lbl.setText(fw)
            if snap.connected:
                idx = self.pdns_mode_combo.findData(snap.private_dns_mode)
                if idx >= 0:
                    self.pdns_mode_combo.setCurrentIndex(idx)
                self.pdns_host_edit.setText(snap.private_dns_specifier)
                if snap.http_proxy:
                    self.bridge_enable_chk.setChecked(True)
                    if ":" in snap.http_proxy:
                        p = snap.http_proxy.rsplit(":", 1)[1]
                        if p.isdigit():
                            self.bridge_port_spin.setValue(int(p))
                    if snap.proxy_exclusion:
                        self.bridge_bypass_edit.setText(snap.proxy_exclusion)

        self._spawn(fetch, done)

    def _collect_network_dict(self) -> dict:
        allow_ports = []
        for r in range(self.allow_table.rowCount()):
            cb = self.allow_table.cellWidget(r, 0)
            sp = self.allow_table.cellWidget(r, 1)
            if isinstance(cb, QtWidgets.QComboBox) and isinstance(sp, QtWidgets.QSpinBox):
                allow_ports.append({"protocol": cb.currentText(), "port": sp.value()})

        forwards = []
        for r in range(self.fwd_table.rowCount()):
            cb = self.fwd_table.cellWidget(r, 0)
            bind_item = self.fwd_table.item(r, 1)
            sp_h = self.fwd_table.cellWidget(r, 2)
            sp_c = self.fwd_table.cellWidget(r, 3)
            if (
                isinstance(cb, QtWidgets.QComboBox)
                and isinstance(sp_h, QtWidgets.QSpinBox)
                and isinstance(sp_c, QtWidgets.QSpinBox)
            ):
                entry = {
                    "protocol": cb.currentText(),
                    "host_port": sp_h.value(),
                    "cell_port": sp_c.value(),
                }
                bind_txt = bind_item.text().strip() if bind_item else ""
                if bind_txt and bind_txt != "127.0.0.1":
                    entry["bind_address"] = bind_txt
                forwards.append(entry)

        mode = str(self.net_mode_combo.currentData() or "nat")
        if mode == "host":
            return {"mode": "host"}
        if mode == "bridge":
            out_br: dict = {"mode": "bridge"}
            data = self.bridge_combo.currentData()
            if isinstance(data, dict) and data.get("name"):
                out_br["bridge_interface"] = str(data["name"])
            mac = self.mac_edit.text().strip()
            if mac:
                out_br["cell_mac"] = mac
            if self.bridge_static_ip_chk.isChecked():
                adb_ip = self.bridge_adb_ip_edit.text().strip()
                if adb_ip:
                    out_br["cell_ipv4"] = adb_ip
            return out_br

        dns = [s.strip() for s in self.dns_edit.text().split(",") if s.strip()][:4]
        out: dict = {
            "mode": mode,
            "host_ipv4": self.host_ip_edit.text().strip() or "10.177.0.1",
            "cell_ipv4": self.cell_ip_edit.text().strip() or "10.177.0.2",
            "prefix_len": self.prefix_spin.value(),
            "cell_mac": self.mac_edit.text().strip() or "02:64:6c:00:00:02",
        }
        if dns:
            out["dns"] = dns
        if allow_ports:
            out["allow_host_ports"] = allow_ports
        if forwards:
            out["port_forwards"] = forwards
        return out

    def _apply_proxy_bridge(self) -> None:
        enabled = self.bridge_enable_chk.isChecked()
        port = self.bridge_port_spin.value()
        bypass = self.bridge_bypass_edit.text().strip()
        host_ip = self.host_ip_edit.text().strip() or "10.177.0.1"
        cell_ip = self.cell_ip_edit.text().strip() or "10.177.0.2"
        uid = int(self._cell.get("host_uid") or os.getuid())

        net = self._collect_network_dict()
        allow = list(net.get("allow_host_ports") or [])
        if enabled:
            if not any(x["port"] == port and x["protocol"] == "tcp" for x in allow):
                allow.append({"protocol": "tcp", "port": port})
                self._insert_allow_row("tcp", port)
        else:
            allow = [x for x in allow if not (x["port"] == port and x["protocol"] == "tcp")]
            net["allow_host_ports"] = allow

        self._ext.proxy_bridge_enabled = enabled
        self._ext.proxy_bridge_port = port
        self._ext.proxy_bridge_bypass = bypass
        net["allow_host_ports"] = allow
        self._ext.network = net
        save_extensions_sidecar(self._ext)

        def task():
            ok_nft, msg_nft = apply_live_nftables_rules(
                uid=uid,
                host_ip=host_ip,
                cell_ip=cell_ip,
                offline_mode=(net.get("mode") == "offline"),
                allow_host_ports=allow,
                port_forwards=net.get("port_forwards") or [],
            )
            if not ok_nft:
                return False, f"放行宿主机端口失败: {msg_nft}"
            if enabled:
                ok_adb, msg_adb = apply_android_proxy(host_ip, port, bypass)
            else:
                ok_adb, msg_adb = apply_android_proxy("", None, "")
            if not ok_adb:
                return False, f"防火墙已更新，但写入 Android 代理失败: {msg_adb}"
            return True, (
                f"已放行 {host_ip}:{port} 并配置 Android 全局代理！"
                if enabled
                else "已关闭代理桥接并清理规则。"
            )

        self._spawn(
            task,
            lambda r: show_message(self, "代理桥接", r[1], is_error=not r[0]),
            lambda e: show_message(self, "代理桥接错误", e, is_error=True),
        )

    def _apply_live_nft(self) -> None:
        net = self._collect_network_dict()
        uid = int(self._cell.get("host_uid") or os.getuid())
        self._spawn(
            lambda: apply_live_nftables_rules(
                uid=uid,
                host_ip=net["host_ipv4"],
                cell_ip=net["cell_ipv4"],
                offline_mode=(net["mode"] == "offline"),
                allow_host_ports=net.get("allow_host_ports") or [],
                port_forwards=net.get("port_forwards") or [],
            ),
            lambda r: show_message(self, "实时防火墙规则", r[1], is_error=not r[0]),
            lambda e: show_message(self, "规则应用失败", e, is_error=True),
        )

    def _save_network_config(self) -> None:
        mode = str(self.net_mode_combo.currentData() or "nat")
        if mode == "bridge":
            data = self.bridge_combo.currentData()
            if not isinstance(data, dict) or not data.get("name") or not data.get("bridge_ready"):
                self._update_bridge_notice()
                show_message(self, "无法保存桥接配置", self.bridge_notice_lbl.text(), is_error=True)
                return

        net = self._collect_network_dict()
        self._ext.network = net
        save_extensions_sidecar(self._ext)

        if detect_supervisor_extensions_support(base_spec=self._cell) and self._cell:
            updated = copy.deepcopy(self._cell)
            updated["network"] = net
            self._spawn(
                lambda: save_cell_spec_privileged(updated),
                lambda r: (
                    setattr(self, "_cell", updated) if r[0] else None,
                    show_message(
                        self,
                        "保存网络配置",
                        f"{r[1]}\n网络模式变更需重启 Droidloom 后生效。" if r[0] else r[1],
                        is_error=not r[0],
                    ),
                ),
                lambda e: show_message(self, "保存失败", e, is_error=True),
            )
        else:
            if mode in ("host", "bridge"):
                mode_label = "宿主机 (Host)" if mode == "host" else "桥接 (Bridge)"
                show_message(
                    self,
                    "已保存配置",
                    f"已保存「{mode_label}」模式配置到 ~/.config/droidloom/extensions-v1.json。\n"
                    "切换网络模式无法通过防火墙规则热应用；当前安装的 supervisor (0.1.0-42) 尚未原生支持该模式，"
                    "需安装本仓库新版 runtime 并重启 Droidloom 后生效。",
                )
            else:
                show_message(
                    self,
                    "已保存配置",
                    "已保存到 ~/.config/droidloom/extensions-v1.json。\n"
                    "如需立即生效放行端口、离线隔离或端口映射，请点击「热应用」；子网与 MAC 变更需升级新版 runtime 并重启 Droidloom 生效。",
                )

    def _apply_private_dns(self) -> None:
        mode = str(self.pdns_mode_combo.currentData())
        host = self.pdns_host_edit.text().strip()
        self._spawn(
            lambda: apply_android_private_dns(mode, host),
            lambda r: show_message(
                self,
                "Private DNS",
                "已应用到 Android" if r[0] else f"应用失败: {r[1]}",
                is_error=not r[0],
            ),
        )
