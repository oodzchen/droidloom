"""Offscreen PySide6 layout, status bar, resize grip, and wrapping tests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets

from droidloom_settings.ui.apk_install_dialog import ApkInstallDialog
from droidloom_settings.ui.main_window import MainWindow
from droidloom_settings.ui.widgets import (
    DragResizeHandle,
    StatusIndicator,
    wait_all_workers,
    wrap_resizable,
)


def _get_app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test"])
    return app


class UiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _get_app()

    def test_status_indicator_has_no_pill_background(self) -> None:
        ind = StatusIndicator("运行中", "ok")
        self.assertEqual(ind.text(), "运行中")
        self.assertEqual(ind.tone(), "ok")
        ss = ind.label().styleSheet()
        self.assertIn("background: transparent", ss)
        self.assertNotIn("border-radius", ss)
        ind.set_status("异常", "err")
        self.assertEqual(ind.text(), "异常")
        self.assertEqual(ind.tone(), "err")
        pm = ind.grab()
        self.assertFalse(pm.isNull())

    def test_main_window_bottom_status_bar_and_resize(self) -> None:
        win = MainWindow()
        try:
            win.show()
            self.app.processEvents()
            self.assertFalse(hasattr(win, "top_state_pill"))
            self.assertEqual(win.page_overview.findChildren(QtWidgets.QFrame, "statTile"), [])
            sb = win.statusBar()
            self.assertIsNotNone(sb)
            self.assertTrue(sb.isSizeGripEnabled())

            win._on_status_updated(
                "running",
                {
                    "ready": True,
                    "android_version": "17",
                    "memory_bytes": 1024 * 1024 * 512,
                    "processes": 42,
                    "app_processes": 5,
                    "adb_ok": True,
                },
            )
            self.app.processEvents()
            self.assertEqual(win.status_indicator.text(), "运行中")
            self.assertIn("Android 17", win.status_detail_lbl.full_text())
            self.assertIn("ADB 已连接", win.status_detail_lbl.full_text())

            win._on_status_updated("stopped", {"adb_ok": False})
            self.assertEqual(win.status_indicator.text(), "已停止")
            self.assertIn("容器未运行", win.status_detail_lbl.full_text())

            for w, h in ((560, 420), (1380, 900)):
                win.resize(w, h)
                self.app.processEvents()
                self.assertEqual(win.width(), w)
                self.assertEqual(win.height(), h)

            for i in range(win.nav.count()):
                win.nav.setCurrentRow(i)
                self.app.processEvents()
        finally:
            win.close()
            wait_all_workers(3000)
            self.app.processEvents()

    def test_network_mode_hint_wraps_without_clipping(self) -> None:
        win = MainWindow(initial_page="network")
        try:
            win.show()
            for width, height in ((1100, 760), (560, 640)):
                win.resize(width, height)
                self.app.processEvents()
                for mode in ("nat", "bridge", "host", "offline"):
                    idx = win.page_network.net_mode_combo.findData(mode)
                    self.assertGreaterEqual(idx, 0)
                    win.page_network.net_mode_combo.setCurrentIndex(idx)
                    self.app.processEvents()
                    lbl = win.page_network.mode_hint_lbl
                    self.assertTrue(lbl.isVisible())
                    self.assertTrue(len(lbl.text()) > 0)
                    need_h = lbl.heightForWidth(max(80, lbl.width()))
                    self.assertGreaterEqual(
                        lbl.height(),
                        need_h - 1,
                        f"mode_hint_lbl clipped for mode={mode} at width={width}: height={lbl.height()} < need={need_h}",
                    )
        finally:
            win.close()
            wait_all_workers(3000)
            self.app.processEvents()

    def test_network_mode_controls_and_bridge_dhcp_ui(self) -> None:
        win = MainWindow(initial_page="network")
        try:
            win.show()
            win.resize(1100, 760)
            self.app.processEvents()
            net = win.page_network

            for mode in ("nat", "offline"):
                net.net_mode_combo.setCurrentIndex(net.net_mode_combo.findData(mode))
                self.app.processEvents()
                self.assertTrue(net.apply_live_rules_btn.isVisible())
                self.assertTrue(net.apply_live_rules_btn.isEnabled())
                self.assertTrue(net.cell_ip_edit.isVisible())

            net.net_mode_combo.setCurrentIndex(net.net_mode_combo.findData("host"))
            self.app.processEvents()
            self.assertFalse(net.apply_live_rules_btn.isVisible())
            self.assertFalse(net.cell_ip_edit.isVisible())

            net.net_mode_combo.setCurrentIndex(net.net_mode_combo.findData("bridge"))
            self.app.processEvents()
            self.assertFalse(net.bridge_combo.isEditable())
            self.assertFalse(net.apply_live_rules_btn.isVisible())
            self.assertFalse(net.cell_ip_edit.isVisible())
            self.assertTrue(net.bridge_dhcp_lbl.isVisible())
            self.assertIn("DHCP", net.bridge_dhcp_lbl.text())
            self.assertFalse(net.bridge_static_ip_chk.isChecked())
            self.assertFalse(net.bridge_adb_ip_edit.isVisible())

            net.bridge_static_ip_chk.setChecked(True)
            self.app.processEvents()
            self.assertTrue(net.bridge_adb_ip_edit.isVisible())
        finally:
            win.close()
            wait_all_workers(3000)
            self.app.processEvents()

    def test_drag_resize_handle_and_wrap_resizable(self) -> None:
        table = QtWidgets.QTableWidget(2, 2)
        box = wrap_resizable(table, default_height=150, min_height=88, max_height=500)
        box.resize(400, 220)
        box.show()
        self.app.processEvents()

        handle: DragResizeHandle = box.resize_handle  # type: ignore[attr-defined]
        self.assertEqual(table.minimumHeight(), 150)
        self.assertEqual(table.maximumHeight(), 150)
        self.assertFalse(handle.grab().isNull())

        # Simulate vertical drag +70px
        press = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(390, 8),
            QtCore.QPointF(390, 8),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        move = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(390, 78),
            QtCore.QPointF(390, 78),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        release = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonRelease,
            QtCore.QPointF(390, 78),
            QtCore.QPointF(390, 78),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        handle.mousePressEvent(press)
        handle.mouseMoveEvent(move)
        handle.mouseReleaseEvent(release)
        self.assertEqual(table.minimumHeight(), 150 + 70)
        self.assertEqual(table.maximumHeight(), 150 + 70)

        # Double-click resets back to default_height
        dbl = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonDblClick,
            QtCore.QPointF(390, 8),
            QtCore.QPointF(390, 8),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        handle.mouseDoubleClickEvent(dbl)
        self.assertEqual(table.minimumHeight(), 150)
        self.assertEqual(table.maximumHeight(), 150)
        box.close()

    def test_apk_install_dialog_instantiates_and_resizes(self) -> None:
        dlg = ApkInstallDialog([])
        try:
            dlg.show()
            dlg.resize(420, 360)
            self.app.processEvents()
            self.assertTrue(dlg.isSizeGripEnabled())
        finally:
            dlg.close()
            wait_all_workers(3000)

    def test_integration_hardware_and_storage_controls_layout(self) -> None:
        win = MainWindow(initial_page="integration")
        try:
            win.show()
            for width, height in ((1100, 760), (560, 640)):
                win.resize(width, height)
                self.app.processEvents()
                integ = win.page_integration
                self.assertGreaterEqual(integ.clipboard_sync_combo.count(), 3)
                self.assertGreaterEqual(integ.host_brightness_combo.count(), 3)
                for lbl in (integ.clipboard_hint_lbl, integ.bridge_hint_lbl):
                    self.assertTrue(lbl.isVisible())
                    need_h = lbl.heightForWidth(max(80, lbl.width()))
                    self.assertGreaterEqual(lbl.height(), need_h - 1)

            hw = win.page_hardware
            self.assertGreaterEqual(hw.audio_volume_mode_combo.count(), 4)
            self.assertEqual(hw.cpu_quota_spin.specialValueText(), "不限")
            self.assertEqual(hw.swap_max_spin.specialValueText(), "不限")
            self.assertEqual(hw.cpu_weight_spin.value(), 100)
            self.assertEqual(hw.io_weight_spin.value(), 100)
            hw._apply_effective_limits_to_ui(
                {
                    "MemoryHigh": "13276307456",
                    "MemoryMax": "16595382272",
                    "MemorySwapMax": "infinity",
                    "TasksMax": "8192",
                    "CPUQuotaPerSecUSec": "infinity",
                    "AllowedCPUs": "",
                    "CPUWeight": "[not set]",
                    "IOWeight": "[not set]",
                }
            )
            self.assertEqual(hw.mem_high_spin.value(), 12661)
            self.assertEqual(hw.mem_max_spin.value(), 15827)
            self.assertEqual(hw.tasks_max_spin.value(), 8192)

            disp = win.page_display
            disp._highlight_rotation("lock", 1)
            self.assertEqual(disp.rot_buttons[1].property("role"), "primary")
            self.assertEqual(disp.rot_buttons[0].property("role"), "")
            self.assertEqual(disp.free_rot_btn.property("role"), "")
            disp._highlight_rotation("free", 0)
            self.assertEqual(disp.free_rot_btn.property("role"), "primary")
            self.assertTrue(all(b.property("role") == "" for b in disp.rot_buttons.values()))

            st = win.page_storage
            self.assertFalse(hasattr(st, "share_user_chk"))
            self.assertFalse(hasattr(st, "xdg_table"))
            self.assertEqual(st.add_recommended_btn.text(), "添加推荐目录")
        finally:
            win.close()
            wait_all_workers(3000)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
