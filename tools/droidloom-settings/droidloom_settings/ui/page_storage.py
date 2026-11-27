"""Storage mapping, shared folders, disk usage, and overrides page."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..backend.android_bridge import trigger_media_rescan
from ..backend.cell_config import (
    directory_disk_usage_bytes,
    load_cell_json,
    save_cell_spec_privileged,
    validate_shared_directory_entry,
)
from .widgets import (
    WorkerThread,
    WrappingLabel,
    format_bytes,
    make_form_layout,
    make_section_card,
    set_button_role,
    show_message,
    spawn_worker,
    style_table,
    wrap_resizable,
)


class StoragePage(QtWidgets.QWidget):
    """Shared host storage mappings and Android storage configuration."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._workers: list[WorkerThread] = []
        self._cell = load_cell_json()
        self._uid = int(self._cell.get("host_uid") or os.getuid())
        self._gid = os.getgid()

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. Explicit shared_storage_directories
        custom_box, custom_lay = make_section_card("映射")
        custom_lay.addWidget(
            WrappingLabel(
                "仅将下方显式添加的目录映射到 Android 的 /storage/emulated/0/<名称>（已禁用自动共享整个用户目录以保护宿主机文件；推荐使用 ~/Downloads/Droidloom）。",
                muted=True,
            )
        )
        self.custom_table = QtWidgets.QTableWidget(0, 3)
        self.custom_table.setHorizontalHeaderLabels(["宿主机路径", "Android 名称", "GID"])
        self.custom_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        style_table(self.custom_table)
        custom_lay.addWidget(wrap_resizable(self.custom_table, default_height=165, min_height=88))

        c_btns = QtWidgets.QHBoxLayout()
        c_btns.setSpacing(8)
        add_btn = QtWidgets.QPushButton("添加...")
        self.add_recommended_btn = QtWidgets.QPushButton("添加推荐目录")
        remove_btn = QtWidgets.QPushButton("移除")
        rescan_btn = QtWidgets.QPushButton("刷新媒体库")
        add_btn.clicked.connect(self._add_custom_dir)
        self.add_recommended_btn.clicked.connect(self._add_recommended_dir)
        remove_btn.clicked.connect(self._remove_custom_dir)
        rescan_btn.clicked.connect(self._rescan_media)
        c_btns.addWidget(add_btn)
        c_btns.addWidget(self.add_recommended_btn)
        c_btns.addWidget(remove_btn)
        c_btns.addStretch(1)
        c_btns.addWidget(rescan_btn)
        custom_lay.addLayout(c_btns)
        root.addWidget(custom_box)

        # 3. Storage & image paths
        paths_box, paths_lay = make_section_card("路径")
        p_form = make_form_layout()
        self.data_dir_edit = QtWidgets.QLineEdit(str(self._cell.get("data_dir") or ""))
        self.data_usage_lbl = WrappingLabel("计算中...", muted=True)
        data_row = QtWidgets.QHBoxLayout()
        data_row.setSpacing(8)
        data_row.addWidget(self.data_dir_edit, 1)
        open_data_btn = QtWidgets.QPushButton("打开")
        open_data_btn.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(self.data_dir_edit.text().strip())
            )
        )
        data_row.addWidget(open_data_btn)

        self.image_dir_lbl = WrappingLabel(str(self._cell.get("image_dir") or "—"), muted=True)
        self.vendor_img_lbl = WrappingLabel(str(self._cell.get("vendor_image") or "—"), muted=True)
        self.gapps_edit = QtWidgets.QLineEdit(str(self._cell.get("gapps_dir") or ""))
        self.gapps_edit.setPlaceholderText("留空不启用")
        gapps_row = QtWidgets.QHBoxLayout()
        gapps_row.setSpacing(8)
        gapps_row.addWidget(self.gapps_edit, 1)
        gapps_browse = QtWidgets.QPushButton("浏览...")
        gapps_browse.clicked.connect(self._browse_gapps)
        gapps_row.addWidget(gapps_browse)

        p_form.addRow("数据目录:", data_row)
        p_form.addRow("占用:", self.data_usage_lbl)
        p_form.addRow("系统镜像:", self.image_dir_lbl)
        p_form.addRow("Vendor:", self.vendor_img_lbl)
        p_form.addRow("GApps:", gapps_row)
        paths_lay.addLayout(p_form)
        root.addWidget(paths_box)

        # 4. Advanced overrides (collapsible)
        adv_box, adv_lay = make_section_card("覆盖")
        self.adv_toggle = QtWidgets.QCheckBox("编辑 JSON")
        self.adv_edit = QtWidgets.QPlainTextEdit()
        self.adv_edit.setStyleSheet("font-family: monospace; font-size: 12px;")
        self.adv_wrap = wrap_resizable(self.adv_edit, default_height=160, min_height=90)
        self.adv_wrap.setVisible(False)
        self.adv_toggle.toggled.connect(self.adv_wrap.setVisible)
        adv_lay.addWidget(self.adv_toggle)
        adv_lay.addWidget(self.adv_wrap)
        root.addWidget(adv_box)

        save_row = QtWidgets.QHBoxLayout()
        self.save_btn = set_button_role(QtWidgets.QPushButton("保存"), "primary")
        self.save_btn.clicked.connect(self._save)
        save_row.addStretch(1)
        save_row.addWidget(self.save_btn)
        root.addLayout(save_row)
        root.addStretch(1)

        self._populate()

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _populate(self) -> None:
        custom = self._cell.get("shared_storage_directories") or []
        self.custom_table.setRowCount(0)
        for entry in custom:
            self._insert_custom_row(
                str(entry.get("source", "")),
                str(entry.get("android_name", "")),
                int(entry.get("host_gid", self._gid)),
            )

        adv = {
            "android_file_overrides": self._cell.get("android_file_overrides") or [],
            "android_runtime_directories": self._cell.get("android_runtime_directories") or [],
        }
        self.adv_edit.setPlainText(json.dumps(adv, indent=2, ensure_ascii=False))

        data_dir = Path(self.data_dir_edit.text().strip() or "/")
        self._spawn(
            lambda: directory_disk_usage_bytes(data_dir),
            lambda b: self.data_usage_lbl.setText(
                format_bytes(b) if b is not None else "无法读取（需管理员权限或目录未创建）"
            ),
        )

    def _insert_custom_row(self, source: str, android_name: str, gid: int) -> None:
        r = self.custom_table.rowCount()
        self.custom_table.insertRow(r)
        self.custom_table.setItem(r, 0, QtWidgets.QTableWidgetItem(source))
        self.custom_table.setItem(r, 1, QtWidgets.QTableWidgetItem(android_name))
        self.custom_table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(gid)))

    def _add_custom_dir(self) -> None:
        src = QtWidgets.QFileDialog.getExistingDirectory(self, "选择要共享的宿主机目录")
        if not src:
            return
        default_name = Path(src).name or "Shared"
        name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Android 共享目录名",
            "该目录将在 Android 的 /storage/emulated/0/<名称> 中可见:",
            text=default_name,
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        gid = self._gid
        if self.custom_table.rowCount() > 0:
            item = self.custom_table.item(0, 2)
            if item and item.text().isdigit():
                gid = int(item.text())
        err = validate_shared_directory_entry(src, name, gid)
        if err:
            show_message(self, "无效目录", err, is_error=True)
            return
        self._insert_custom_row(src, name, gid)

    def _add_recommended_dir(self) -> None:
        rec_dir = Path.home() / "Downloads" / "Droidloom"
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(rec_dir, 0o2755)
        except OSError as exc:
            show_message(self, "推荐目录", f"无法准备 {rec_dir}: {exc}", is_error=True)
            return
        src = str(rec_dir)
        for r in range(self.custom_table.rowCount()):
            src_it = self.custom_table.item(r, 0)
            name_it = self.custom_table.item(r, 1)
            if (src_it and src_it.text() == src) or (name_it and name_it.text() == "Droidloom"):
                show_message(self, "推荐目录", f"{src}（Droidloom）已在映射列表中，请记得点击「保存」。")
                return
        err = validate_shared_directory_entry(src, "Droidloom", self._gid)
        if err:
            show_message(self, "无效目录", err, is_error=True)
            return
        self._insert_custom_row(src, "Droidloom", self._gid)
        show_message(self, "推荐目录", f"已添加 {src} -> /storage/emulated/0/Droidloom，请点击下方「保存」使配置生效。")

    def _remove_custom_dir(self) -> None:
        r = self.custom_table.currentRow()
        if r >= 0:
            self.custom_table.removeRow(r)

    def _browse_gapps(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 Google Apps 附加目录")
        if d:
            self.gapps_edit.setText(d)

    def _rescan_media(self) -> None:
        self._spawn(
            trigger_media_rescan,
            lambda res: show_message(
                self,
                "媒体库刷新",
                "已通知 Android 扫描 /storage/emulated/0/" if res[0] else f"刷新失败: {res[1]}",
                is_error=not res[0],
            ),
        )

    def _save(self) -> None:
        if not self._cell:
            show_message(self, "错误", "未能加载 /etc/droidloom/cell.json", is_error=True)
            return
        updated = copy.deepcopy(self._cell)
        updated["share_user_folders"] = False

        dirs = []
        setgid_candidates = []
        for r in range(self.custom_table.rowCount()):
            src = self.custom_table.item(r, 0).text().strip()
            name = self.custom_table.item(r, 1).text().strip()
            gid_txt = self.custom_table.item(r, 2).text().strip()
            gid = int(gid_txt) if gid_txt.isdigit() else self._gid
            err = validate_shared_directory_entry(src, name, gid)
            if err:
                show_message(self, "校验失败", err, is_error=True)
                return
            dirs.append({"source": src, "android_name": name, "host_gid": gid})
            setgid_candidates.append(src)
        updated["shared_storage_directories"] = dirs

        data_dir = self.data_dir_edit.text().strip()
        if data_dir:
            updated["data_dir"] = data_dir
        gapps = self.gapps_edit.text().strip()
        if gapps:
            updated["gapps_dir"] = gapps
        else:
            updated.pop("gapps_dir", None)

        if self.adv_toggle.isChecked():
            try:
                adv = json.loads(self.adv_edit.toPlainText())
                if "android_file_overrides" in adv:
                    updated["android_file_overrides"] = adv["android_file_overrides"]
                if "android_runtime_directories" in adv:
                    updated["android_runtime_directories"] = adv["android_runtime_directories"]
            except json.JSONDecodeError as exc:
                show_message(self, "JSON 格式错误", str(exc), is_error=True)
                return

        self.save_btn.setEnabled(False)
        self._spawn(
            lambda: save_cell_spec_privileged(updated, setgid_dirs=setgid_candidates),
            lambda res: (
                self.save_btn.setEnabled(True),
                setattr(self, "_cell", updated) if res[0] else None,
                self._populate(),
                show_message(self, "存储配置", res[1], is_error=not res[0]),
            ),
            lambda e: (
                self.save_btn.setEnabled(True),
                show_message(self, "保存失败", e, is_error=True),
            ),
        )
