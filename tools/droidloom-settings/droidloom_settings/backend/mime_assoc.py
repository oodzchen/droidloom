"""Desktop entry and APK MIME type association helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

APK_MIME = "application/vnd.android.package-archive"
APK_ICON_NAME = "org.droidloom.Settings"
APK_MIME_XML_NAME = "org.droidloom.ApkInstaller.xml"
APK_DESKTOP_ID = "org.droidloom.ApkInstaller.desktop"
SETTINGS_DESKTOP_ID = "org.droidloom.Settings.desktop"


def xdg_data_home() -> Path:
    env = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(env) if env else (Path.home() / ".local/share")


def apk_mime_package_path(data_home: Path | None = None) -> Path:
    return (data_home or xdg_data_home()) / "mime/packages" / APK_MIME_XML_NAME


def render_apk_mime_xml(icon_name: str = APK_ICON_NAME) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
        f'  <mime-type type="{APK_MIME}">\n'
        f'    <icon name="{icon_name}"/>\n'
        "  </mime-type>\n"
        "</mime-info>\n"
    )


def is_apk_mime_icon_installed(data_home: Path | None = None) -> bool:
    base = data_home or xdg_data_home()
    pkg_file = apk_mime_package_path(base)
    icons_index = base / "mime/icons"
    if not pkg_file.is_file() or not icons_index.is_file():
        return False
    try:
        pkg_text = pkg_file.read_text(encoding="utf-8")
        if f'name="{APK_ICON_NAME}"' not in pkg_text:
            return False
        expected_line = f"{APK_MIME}:{APK_ICON_NAME}"
        return any(
            line.strip() == expected_line
            for line in icons_index.read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return False


def _refresh_desktop_caches(base: Path, *, clear_kde_icon_cache: bool) -> None:
    hicolor_dir = base / "icons/hicolor"
    gtk_cache = shutil.which("gtk-update-icon-cache")
    if gtk_cache and hicolor_dir.is_dir():
        try:
            subprocess.run(
                [gtk_cache, "-f", "-t", str(hicolor_dir)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

    if clear_kde_icon_cache:
        # Refresh KDE desktop caches incrementally without deleting the host's
        # shared ~/.cache/icon-cache.kcache used by Plasma and desktop apps.
        for tool, args in (
            ("kbuildsycoca6", []),
            ("kbuildsycoca5", []),
        ):
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                res = subprocess.run(
                    [exe, *args],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=10,
                )
                if res.returncode == 0:
                    break
            except Exception:
                pass


def install_apk_mime_icon(data_home: Path | None = None) -> tuple[bool, str]:
    is_default_home = data_home is None
    base = data_home or xdg_data_home()
    pkg_file = apk_mime_package_path(base)
    pkg_file.parent.mkdir(parents=True, exist_ok=True)
    pkg_file.write_text(render_apk_mime_xml(), encoding="utf-8")

    umd = shutil.which("update-mime-database")
    if not umd:
        return False, "未找到 update-mime-database 命令"
    try:
        res = subprocess.run(
            [umd, str(base / "mime")],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip() or "update-mime-database 执行失败"
    except Exception as exc:
        return False, str(exc)

    _refresh_desktop_caches(base, clear_kde_icon_cache=is_default_home)
    if is_apk_mime_icon_installed(base):
        return True, f"已安装 .apk ({APK_MIME}) MIME 图标关联 ({APK_ICON_NAME})"
    return False, f"已更新 MIME 数据库，但未确认 {APK_MIME}:{APK_ICON_NAME} 写入 {base / 'mime/icons'}"


def remove_apk_mime_icon(data_home: Path | None = None) -> tuple[bool, str]:
    is_default_home = data_home is None
    base = data_home or xdg_data_home()
    pkg_file = apk_mime_package_path(base)
    removed = False
    if pkg_file.is_file():
        pkg_file.unlink()
        removed = True

    mime_dir = base / "mime"
    umd = shutil.which("update-mime-database")
    if umd and mime_dir.is_dir():
        try:
            res = subprocess.run(
                [umd, str(mime_dir)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode != 0:
                return False, (res.stderr or res.stdout).strip() or "update-mime-database 执行失败"
        except Exception as exc:
            return False, str(exc)

    _refresh_desktop_caches(base, clear_kde_icon_cache=is_default_home)
    return True, ("已移除 .apk MIME 图标覆盖" if removed else "未发现已安装的 .apk MIME 图标覆盖")


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
    base = xdg_data_home()
    apps_dir = base / "applications"
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

        icon_ok, icon_msg = install_apk_mime_icon(None)

        udd = shutil.which("update-desktop-database")
        if udd:
            subprocess.run(
                [udd, str(apps_dir)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )

        current = query_default_apk_handler()
        if current == APK_DESKTOP_ID:
            if icon_ok:
                return True, f"已将 .apk ({APK_MIME}) 默认打开程序设为 {APK_DESKTOP_ID}，并同步关联 Droidloom 文件图标"
            return False, f"默认打开程序已设为 {APK_DESKTOP_ID}，但 APK 图标同步失败: {icon_msg}"
        return False, f"设置已提交但查询结果为: {current or '(空)'}"
    except Exception as exc:
        return False, str(exc)
