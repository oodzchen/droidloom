"""Desktop entry and APK MIME type association helpers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

APK_MIME = "application/vnd.android.package-archive"
APK_DESKTOP_ID = "org.droidloom.ApkInstaller.desktop"
SETTINGS_DESKTOP_ID = "org.droidloom.Settings.desktop"


def query_default_apk_handler() -> str:
    xdg_mime = shutil.which("xdg-mime")
    if not xdg_mime:
        return ""
    try:
        res = subprocess.run(
            [xdg_mime, "query", "default", APK_MIME],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def is_droidloom_default_apk_handler() -> bool:
    return query_default_apk_handler() == APK_DESKTOP_ID


def set_droidloom_default_apk_handler() -> tuple[bool, str]:
    apps_dir = Path.home() / ".local/share/applications"
    desktop_file = apps_dir / APK_DESKTOP_ID
    if not desktop_file.is_file():
        return (
            False,
            f"未找到 {desktop_file}，请先运行用户安装脚本 tools/droidloom-settings/install-user.sh",
        )
    xdg_mime = shutil.which("xdg-mime")
    if not xdg_mime:
        return False, "未找到 xdg-mime 命令"
    try:
        res = subprocess.run(
            [xdg_mime, "default", APK_DESKTOP_ID, APK_MIME],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip() or "xdg-mime 设置失败"
        udd = shutil.which("update-desktop-database")
        if udd:
            subprocess.run(
                [udd, str(apps_dir)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        kbuild = shutil.which("kbuildsycoca6")
        if kbuild:
            subprocess.run(
                [kbuild],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        current = query_default_apk_handler()
        if current == APK_DESKTOP_ID:
            return True, f"已将 .apk ({APK_MIME}) 默认打开程序设为 {APK_DESKTOP_ID}"
        return False, f"设置已提交但查询结果为: {current or '(空)'}"
    except Exception as exc:
        return False, str(exc)
