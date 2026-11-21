"""CLI and GUI entry point for Droidloom Settings, APK Installer, and Orientation Sync."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backend.orientation_daemon import run_orientation_daemon


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="droidloom-settings",
        description="Droidloom 设置中心、APK 安装程序与物理方向同步服务",
    )
    parser.add_argument(
        "--page",
        choices=["overview", "network", "storage", "hardware", "display", "apps", "diagnostics"],
        default="overview",
        help="打开设置中心时默认定位的页面",
    )
    parser.add_argument(
        "--install-apk",
        nargs="+",
        metavar="APK",
        help="以图形化安装弹窗打开一个或多个 APK 文件",
    )
    parser.add_argument(
        "--orientation-daemon",
        action="store_true",
        help="运行宿主机物理重力感应与 Android 方向同步守护进程",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.orientation_daemon:
        return run_orientation_daemon()

    from PySide6 import QtWidgets
    from .ui.apk_install_dialog import ApkInstallDialog
    from .ui.icons import load_app_icon
    from .ui.main_window import MainWindow
    from .ui.widgets import wait_all_workers

    app = QtWidgets.QApplication([sys.argv[0]])
    app.aboutToQuit.connect(lambda: wait_all_workers(4000))
    app.setApplicationName("Droidloom Settings")
    desktop_id = "org.droidloom.ApkInstaller" if args.install_apk else "org.droidloom.Settings"
    app.setDesktopFileName(desktop_id)

    app_icon = load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    if args.install_apk:
        apk_paths = [Path(p) for p in args.install_apk]
        dlg = ApkInstallDialog(apk_paths)
        win_holder: list[MainWindow] = []

        def open_settings():
            w = MainWindow(initial_page="apps")
            win_holder.append(w)
            w.show()
            dlg.accept()

        dlg.open_settings_requested.connect(open_settings)
        dlg.show()
        return app.exec()

    win = MainWindow(initial_page=args.page)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
