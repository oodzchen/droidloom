"""Pure-Python backend controller for the Droidloom Plasma Control Center Plasmoid."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any, Callable

from .android_bridge import (
    DEFAULT_SELECTOR,
    SYSTEM_APP_LABELS,
    apply_android_proxy,
    apply_android_rotation,
    dispatch_media_key,
    is_adb_connected,
    query_adb_notifications,
    query_quick_settings_snapshot,
    query_running_tasks_and_services,
    reconnect_adb,
    set_android_airplane_mode,
    set_android_anim_scale,
    set_android_dnd,
    set_android_pointer_location,
    set_android_show_touches,
    set_android_stay_awake,
    snooze_adb_notification,
    trigger_media_rescan,
)
from .cell_config import DEFAULT_CELL_SPEC
from .control_client import ControlClient, ControlError
from .host_runtime import (
    detect_host_proxy_candidates,
    restart_droidloom,
    start_droidloom,
    stop_droidloom,
    systemctl_user,
)
from .user_configs import (
    PanelConfig,
    load_audio_settings,
    load_panel_config,
    load_session_mode,
    save_audio_settings,
    save_panel_config,
    save_session_mode,
    xdg_applications_dir,
)


def _xdg_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base and Path(base).is_absolute():
        return Path(base) / "droidloom-settings" / "panel-icons"
    return Path.home() / ".cache" / "droidloom-settings" / "panel-icons"


def _xdg_hicolor_apps_dirs() -> list[Path]:
    base = os.environ.get("XDG_DATA_HOME")
    data_home = Path(base) if (base and Path(base).is_absolute()) else Path.home() / ".local/share"
    return [
        data_home / "icons/hicolor/128x128/apps",
        data_home / "icons/hicolor/96x96/apps",
        data_home / "icons/hicolor/64x64/apps",
        data_home / "icons/hicolor/48x48/apps",
    ]


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def encode_rgba_hex_to_png(hex_str: str, width: int, height: int) -> bytes:
    """Encode RGBA8888 hex string (4 bytes per pixel) into a valid PNG image."""
    if not hex_str or width <= 0 or height <= 0:
        return b""
    expected_len = width * height * 8
    if len(hex_str) != expected_len:
        return b""
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return b""
    stride = width * 4
    scanlines = bytearray((stride + 1) * height)
    for y in range(height):
        src_off = y * stride
        dst_off = y * (stride + 1)
        scanlines[dst_off] = 0  # filter type 0
        scanlines[dst_off + 1 : dst_off + 1 + stride] = raw[src_off : src_off + stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(scanlines), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _scan_desktop_icons(apps_dir: Path | None = None) -> dict[str, str]:
    """Map package name -> file:/// icon URI from Droidloom-generated .desktop files."""
    d = apps_dir or xdg_applications_dir()
    if not d.is_dir():
        return {}
    icon_dirs = _xdg_hicolor_apps_dirs()
    out: dict[str, str] = {}
    for entry in sorted(d.glob("droidloom-*.desktop")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        pkg = ""
        icon_name = ""
        for line in text.splitlines():
            if line.startswith("X-Droidloom-Package="):
                pkg = line.split("=", 1)[1].strip()
            elif line.startswith("Icon="):
                icon_name = line.split("=", 1)[1].strip()
        if not pkg or not icon_name or pkg in out:
            continue
        if Path(icon_name).is_file():
            out[pkg] = Path(icon_name).as_uri()
            continue
        for idir in icon_dirs:
            cand = idir / f"{icon_name}.png"
            if cand.is_file():
                out[pkg] = cand.as_uri()
                break
    return out


class PanelController:
    """Aggregates Droidloom runtime state, notifications, tasks, apps, and quick toggles."""

    def __init__(
        self,
        client: ControlClient | None = None,
        cache_dir: Path | None = None,
        config_dir: Path | None = None,
        apps_dir: Path | None = None,
        notif_bridge: Any | None = None,
    ) -> None:
        self.client = client or ControlClient()
        self.cache_dir = cache_dir or _xdg_cache_dir()
        self.config_dir = config_dir
        self.apps_dir = apps_dir
        self.notif_bridge = notif_bridge
        self._apps_cache: list[dict[str, Any]] = []
        self._pkg_meta: dict[str, dict[str, Any]] = {}
        self._last_notifications: list[dict[str, Any]] = []
        self._locally_dismissed: dict[str, int] = {}

    def _cell_spec(self) -> dict[str, Any]:
        try:
            return json.loads(DEFAULT_CELL_SPEC.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _cell_ip(self) -> str:
        net = self._cell_spec().get("network") or {}
        ip = str(net.get("cell_ip") or "").strip()
        return ip or "10.177.0.2"

    def _host_ip(self) -> str:
        net = self._cell_spec().get("network") or {}
        ip = str(net.get("host_ip") or "").strip()
        if ip:
            return ip
        cell_ip = self._cell_ip()
        parts = cell_ip.split(".")
        if len(parts) == 4 and parts[3].isdigit():
            return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
        return "10.177.0.1"

    def _cache_png(self, key: str, png_bytes: bytes) -> str:
        if not png_bytes:
            return ""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        target = self.cache_dir / f"{digest}.png"
        if not target.is_file() or target.stat().st_size == 0:
            tmp = self.cache_dir / f".{digest}.{os.getpid()}.tmp"
            tmp.write_bytes(png_bytes)
            tmp.replace(target)
        return target.as_uri()

    def _cache_rgba_hex_icon(self, hex_str: str, size: int) -> str:
        if not hex_str:
            return ""
        digest = hashlib.sha1(hex_str.encode("ascii", errors="ignore")).hexdigest()[:16]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"rgba-{size}-{digest}.png"
        if target.is_file() and target.stat().st_size > 0:
            return target.as_uri()
        png = encode_rgba_hex_to_png(hex_str, size, size)
        if not png:
            return ""
        tmp = self.cache_dir / f".rgba-{size}-{digest}.{os.getpid()}.tmp"
        tmp.write_bytes(png)
        tmp.replace(target)
        return target.as_uri()

    def _ensure_app_catalog(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self._apps_cache and not force_refresh:
            return self._apps_cache
        desktop_icons = _scan_desktop_icons(self.apps_dir)
        apps: list[dict[str, Any]] = []
        try:
            raw_apps = self.client.list_applications()
        except ControlError:
            return self._apps_cache

        missing_comps: list[str] = []
        for entry in raw_apps:
            pkg = str(entry.get("package") or "")
            comp = str(entry.get("component") or "")
            if not pkg:
                continue
            icon_uri = desktop_icons.get(pkg, "")
            if not icon_uri and comp:
                digest = hashlib.sha1(f"app:{comp}".encode("utf-8")).hexdigest()[:16]
                cached = self.cache_dir / f"{digest}.png"
                if cached.is_file() and cached.stat().st_size > 0:
                    icon_uri = cached.as_uri()
                else:
                    missing_comps.append(comp)
            item = {
                "name": str(entry.get("name") or pkg),
                "package": pkg,
                "component": comp,
                "icon_path": icon_uri,
            }
            apps.append(item)

        if missing_comps:
            try:
                fetched = self.client.application_icons(missing_comps, size=96)
            except ControlError:
                fetched = {}
            for item in apps:
                comp = item["component"]
                if not item["icon_path"] and comp in fetched:
                    item["icon_path"] = self._cache_png(f"app:{comp}", fetched[comp])

        apps.sort(key=lambda a: (a["name"].lower(), a["package"]))
        self._apps_cache = apps
        self._pkg_meta = {a["package"]: a for a in apps}
        return self._apps_cache

    def _app_label_and_icon(self, package: str) -> tuple[str, str, str]:
        meta = self._pkg_meta.get(package)
        if meta is None and package:
            self._ensure_app_catalog(force_refresh=False)
            meta = self._pkg_meta.get(package)
        if meta is not None:
            return meta["name"], meta["component"], meta["icon_path"]
        desktop_icons = _scan_desktop_icons(self.apps_dir)
        return SYSTEM_APP_LABELS.get(package, package), "", desktop_icons.get(package, "")

    def get_state(self, native_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        status_resp = self.client.status()
        running = status_resp.state == "running"
        ready = False
        android_version = ""
        memory_bytes = 0
        processes = 0
        app_processes = 0
        if running:
            try:
                stats_resp = self.client.stats()
                st = stats_resp.stats or {}
                ready = bool(st.get("ready", False))
                android_version = str(st.get("android_version") or "")
                memory_bytes = int(st.get("memory_bytes") or 0)
                processes = int(st.get("processes") or 0)
                app_processes = int(st.get("app_processes") or 0)
            except ControlError:
                pass

        session_mode = load_session_mode()
        audio_cfg = load_audio_settings(self.config_dir)
        panel_cfg = load_panel_config(self.config_dir)

        games_driver = "angle"
        if running:
            try:
                games_driver = self.client.games_driver()
            except ControlError:
                pass

        qs = query_quick_settings_snapshot(self._cell_ip()) if running else {"connected": False}
        notif_info = self.get_notifications(native_snapshot=native_snapshot)

        return {
            "state": status_resp.state,
            "running": running,
            "ready": ready,
            "message": status_resp.message,
            "android_version": android_version,
            "memory_bytes": memory_bytes,
            "processes": processes,
            "app_processes": app_processes,
            "session_mode": session_mode,
            "games_driver": games_driver,
            "mic_capture": audio_cfg.allow_capture,
            "desktop_forwarding": notif_info.get("desktop_forwarding", panel_cfg.desktop_forwarding),
            "default_tab": panel_cfg.default_tab,
            "show_system_tasks": panel_cfg.show_system_tasks,
            "adb_connected": bool(qs.get("connected", False)),
            "night_mode": bool(qs.get("night_mode", False)),
            "dnd": bool(qs.get("dnd", False)),
            "rotation_mode": str(qs.get("rotation_mode", "free")),
            "rotation_index": int(qs.get("rotation_index", 0)),
            "airplane_mode": bool(qs.get("airplane_mode", False)),
            "http_proxy": str(qs.get("http_proxy", "")),
            "stay_awake": bool(qs.get("stay_awake", False)),
            "anim_scale": float(qs.get("anim_scale", 1.0)),
            "show_touches": bool(qs.get("show_touches", False)),
            "pointer_location": bool(qs.get("pointer_location", False)),
            "notification_backend": notif_info["backend"],
            "unread_count": notif_info["unread_count"],
            "ongoing_count": notif_info["ongoing_count"],
        }

    def get_notifications(self, native_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        panel_cfg = load_panel_config(self.config_dir)
        if native_snapshot is None and self.notif_bridge is not None:
            native_snapshot = self.notif_bridge.get_snapshot()

        if native_snapshot is not None and native_snapshot.get("connected"):
            out: list[dict[str, Any]] = []
            for raw in native_snapshot.get("notifications") or []:
                if raw.get("summary"):
                    continue
                pkg = str(raw.get("package") or "")
                app_name, _, fallback_icon = self._app_label_and_icon(pkg)
                icon_path = fallback_icon
                if not icon_path and raw.get("icon"):
                    icon_path = self._cache_rgba_hex_icon(str(raw["icon"]), 48)
                large_icon_path = ""
                if raw.get("large_icon"):
                    large_icon_path = self._cache_rgba_hex_icon(str(raw["large_icon"]), 96)
                out.append(
                    {
                        "key": str(raw.get("key") or ""),
                        "package": pkg,
                        "app": str(raw.get("app") or app_name or pkg),
                        "title": str(raw.get("title") or ""),
                        "body": str(raw.get("body") or ""),
                        "actions": list(raw.get("actions") or []),
                        "urgency": int(raw.get("urgency", 1)),
                        "ongoing": bool(raw.get("ongoing", False)),
                        "clearable": bool(raw.get("clearable", True)),
                        "when": int(raw.get("when") or 0),
                        "progress": int(raw.get("progress", -1)),
                        "category": str(raw.get("category") or ""),
                        "group": str(raw.get("group") or ""),
                        "messages": list(raw.get("messages") or []),
                        "icon_path": icon_path,
                        "large_icon_path": large_icon_path,
                        "source": "dbus",
                    }
                )
            ongoing_count = sum(1 for n in out if n["ongoing"])
            self._last_notifications = out
            return {
                "backend": "dbus",
                "connected": True,
                "desktop_forwarding": bool(
                    native_snapshot.get("desktop_forwarding", panel_cfg.desktop_forwarding)
                ),
                "notifications": out,
                "unread_count": len(out),
                "ongoing_count": ongoing_count,
            }

        status_resp = self.client.status()
        if status_resp.state == "running":
            connected, target = is_adb_connected(self._cell_ip())
            if connected:
                adb_notes = query_adb_notifications(selector=target or DEFAULT_SELECTOR)
                out = []
                active_keys: set[str] = set()
                for n in adb_notes:
                    key = str(n.get("key") or "")
                    when_ms = int(n.get("when") or 0)
                    active_keys.add(key)
                    if key in self._locally_dismissed:
                        if when_ms <= self._locally_dismissed[key]:
                            continue
                        del self._locally_dismissed[key]
                    pkg = str(n.get("package") or "")
                    app_name, _, icon_path = self._app_label_and_icon(pkg)
                    item = dict(n)
                    if app_name and app_name != pkg:
                        item["app"] = app_name
                    item["icon_path"] = icon_path
                    item["large_icon_path"] = ""
                    out.append(item)
                for stale_key in list(self._locally_dismissed.keys()):
                    if stale_key not in active_keys:
                        del self._locally_dismissed[stale_key]
                ongoing_count = sum(1 for n in out if n["ongoing"])
                self._last_notifications = out
                return {
                    "backend": "adb_fallback",
                    "connected": True,
                    "desktop_forwarding": panel_cfg.desktop_forwarding,
                    "notifications": out,
                    "unread_count": len(out),
                    "ongoing_count": ongoing_count,
                }

        self._last_notifications = []
        return {
            "backend": "unavailable",
            "connected": False,
            "desktop_forwarding": panel_cfg.desktop_forwarding,
            "notifications": [],
            "unread_count": 0,
            "ongoing_count": 0,
        }

    def get_tasks(self, include_system: bool = False) -> dict[str, Any]:
        status_resp = self.client.status()
        if status_resp.state != "running":
            return {"connected": False, "recent_tasks": [], "background_apps": []}
        self._ensure_app_catalog(force_refresh=False)
        data = query_running_tasks_and_services(
            cell_ip=self._cell_ip(), include_system=include_system
        )
        for task in data.get("recent_tasks") or []:
            name, comp, icon_path = self._app_label_and_icon(task["package"])
            task["name"] = name
            if not task.get("component"):
                task["component"] = comp
            task["icon_path"] = icon_path
        for app in data.get("background_apps") or []:
            name, comp, icon_path = self._app_label_and_icon(app["package"])
            app["name"] = name
            app["component"] = comp
            app["icon_path"] = icon_path
        return data

    def get_apps(self, force_refresh: bool = False) -> dict[str, Any]:
        status_resp = self.client.status()
        if status_resp.state != "running":
            return {"connected": False, "apps": []}
        apps = self._ensure_app_catalog(force_refresh=force_refresh)
        return {"connected": True, "apps": apps}

    def launch_app(self, package: str, component: str = "") -> tuple[bool, str]:
        try:
            comp = component.strip() or None
            resp = self.client.launch(package, component=comp)
            return True, resp.message or f"已启动 {package}"
        except ControlError as exc:
            return False, str(exc)

    def stop_app(self, package: str) -> tuple[bool, str]:
        try:
            resp = self.client.application_action(package, "stop")
            return True, resp.message or f"已停止 {package}"
        except ControlError as exc:
            return False, str(exc)

    def open_android_app_info(self, package: str) -> tuple[bool, str]:
        try:
            resp = self.client.application_action(package, "settings")
            return True, resp.message or f"已打开 {package} 应用详情"
        except ControlError as exc:
            return False, str(exc)

    def clear_background_tasks(self) -> tuple[bool, str]:
        tasks = self.get_tasks(include_system=False)
        if not tasks.get("connected"):
            return False, "ADB 未连接，无法清理后台任务"
        stopped = 0
        for app in tasks.get("background_apps") or []:
            if app.get("visible") or app.get("is_foreground_service"):
                continue
            pkg = app.get("package") or ""
            if not pkg:
                continue
            ok, _ = self.stop_app(pkg)
            if ok:
                stopped += 1
        return True, f"已清理 {stopped} 个后台应用"

    def invoke_notification_action(self, key: str, action: str) -> tuple[bool, str]:
        if self.notif_bridge is not None and self.notif_bridge.is_connected():
            ok = self.notif_bridge.invoke_action(key, action)
            return (True, "已触发通知操作") if ok else (False, "通知已失效或操作不可用")
        note = next((n for n in self._last_notifications if n.get("key") == key), None)
        if note is None:
            return False, "通知已失效或不存在"
        if action != "default":
            act_obj = next(
                (a for a in (note.get("actions") or []) if a.get("key") == action), None
            )
            label = str((act_obj or {}).get("label") or "").strip().lower()
            media_map = {
                "pre": "previous",
                "prev": "previous",
                "previous": "previous",
                "上一首": "previous",
                "上一曲": "previous",
                "toggle": "play-pause",
                "play": "play-pause",
                "pause": "play-pause",
                "play_pause": "play-pause",
                "play-pause": "play-pause",
                "播放": "play-pause",
                "暂停": "play-pause",
                "next": "next",
                "下一首": "next",
                "下一曲": "next",
                "stop": "stop",
                "停止": "stop",
            }
            if label in media_map:
                ok_adb, target = is_adb_connected(self._cell_ip())
                if ok_adb:
                    return dispatch_media_key(media_map[label], selector=target)
        if note.get("package"):
            ok_launch, msg = self.launch_app(note["package"])
            if action != "default" and ok_launch:
                return True, "已打开应用（部署新版 droidloom-wayland 后可直接后台触发此按钮）"
            return ok_launch, msg
        return False, "当前通知桥接不可用，请升级 droidloom-wayland"

    def dismiss_notification(self, key: str) -> tuple[bool, str]:
        if self.notif_bridge is not None and self.notif_bridge.is_connected():
            ok = self.notif_bridge.dismiss(key)
            return (True, "已清除通知") if ok else (False, "该通知不可清除或已关闭")
        note = next((n for n in self._last_notifications if n.get("key") == key), None)
        if note is None:
            return False, "通知已关闭"
        if not note.get("clearable", True):
            return False, "常驻通知不可清除"
        ok_adb, target = is_adb_connected(self._cell_ip())
        if ok_adb:
            snooze_adb_notification(key, selector=target)
        self._locally_dismissed[key] = int(note.get("when") or 0)
        return True, "已清除通知"

    def dismiss_all_notifications(self) -> tuple[bool, str]:
        if self.notif_bridge is not None and self.notif_bridge.is_connected():
            count = self.notif_bridge.dismiss_all()
            return True, f"已清除 {count} 条可关闭通知"
        ok_adb, target = is_adb_connected(self._cell_ip())
        count = 0
        for note in self._last_notifications:
            if not note.get("clearable", True):
                continue
            key = str(note.get("key") or "")
            if not key:
                continue
            if ok_adb:
                snooze_adb_notification(key, selector=target)
            self._locally_dismissed[key] = int(note.get("when") or 0)
            count += 1
        return True, f"已清除 {count} 条可关闭通知"

    def set_quick_toggle(self, toggle: str, value: str) -> tuple[bool, str]:
        t = toggle.strip()
        v = value.strip()
        if t == "runtime":
            if v == "start":
                return start_droidloom()
            if v == "stop":
                return stop_droidloom()
            if v == "restart":
                return restart_droidloom()
            return False, f"未知运行操作: {v}"

        if t == "restart_runtime":
            return restart_droidloom()

        if t == "night_mode":
            try:
                resp = self.client.set_night_mode(v.lower() in ("1", "true", "on", "yes"))
                return True, resp.message or "已切换深色模式"
            except ControlError as exc:
                return False, str(exc)

        if t == "dnd":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            return set_android_dnd(v.lower() in ("1", "true", "on", "yes"), selector=target)

        if t == "airplane_mode":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            enabled = v.lower() in ("1", "true", "on", "yes")
            ok_cmd, msg = set_android_airplane_mode(enabled, selector=target)
            return (True, f"飞行模式已{'开启' if enabled else '关闭'}") if ok_cmd else (False, msg)

        if t == "http_proxy":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            enabled = v.lower() in ("1", "true", "on", "yes")
            if not enabled:
                ok_cmd, msg = apply_android_proxy("", None, selector=target)
                return (True, "已清除 Android HTTP 代理") if ok_cmd else (False, msg)
            candidates = detect_host_proxy_candidates()
            if not candidates:
                return False, "未检测到宿主机本地代理端口，请在 Droidloom 设置 -> 网络中配置"
            port = int(candidates[0]["port"])
            host_ip = self._host_ip()
            ok_cmd, msg = apply_android_proxy(host_ip, port, selector=target)
            return (
                (True, f"已启用宿主机代理 {host_ip}:{port}（{candidates[0]['label']}）")
                if ok_cmd
                else (False, msg)
            )

        if t == "stay_awake":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            enabled = v.lower() in ("1", "true", "on", "yes")
            ok_cmd, msg = set_android_stay_awake(enabled, selector=target)
            return (True, f"保持唤醒已{'开启' if enabled else '关闭'}") if ok_cmd else (False, msg)

        if t == "anim_speed":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            if v == "cycle":
                qs = query_quick_settings_snapshot(self._cell_ip())
                cur = float(qs.get("anim_scale", 1.0))
                if cur > 0.75:
                    target_scale = 0.5
                elif cur > 0.1:
                    target_scale = 0.0
                else:
                    target_scale = 1.0
            else:
                try:
                    target_scale = float(v)
                except ValueError:
                    return False, f"无效动画倍速: {v}"
            ok_cmd, msg = set_android_anim_scale(target_scale, selector=target)
            return (True, f"系统动画倍速已设为 {target_scale:g}×") if ok_cmd else (False, msg)

        if t == "show_touches":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            enabled = v.lower() in ("1", "true", "on", "yes")
            ok_cmd, msg = set_android_show_touches(enabled, selector=target)
            return (True, f"显示点按操作已{'开启' if enabled else '关闭'}") if ok_cmd else (False, msg)

        if t == "pointer_location":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            enabled = v.lower() in ("1", "true", "on", "yes")
            ok_cmd, msg = set_android_pointer_location(enabled, selector=target)
            return (True, f"指针位置已{'开启' if enabled else '关闭'}") if ok_cmd else (False, msg)

        if t == "adb_reconnect":
            return reconnect_adb(self._cell_ip())

        if t == "droidloom_settings":
            return self.open_settings("overview", "")

        if t == "apk_installer":
            return self.open_apk_installer()

        if t == "rotation":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            if v == "free":
                return apply_android_rotation("free", 0, selector=target)
            if v == "cycle":
                qs = query_quick_settings_snapshot(self._cell_ip())
                if qs.get("rotation_mode") == "free":
                    return apply_android_rotation("lock", 0, selector=target)
                cur = int(qs.get("rotation_index", 0))
                if cur >= 3:
                    return apply_android_rotation("free", 0, selector=target)
                return apply_android_rotation("lock", cur + 1, selector=target)
            if v.isdigit():
                return apply_android_rotation("lock", int(v) % 4, selector=target)
            return False, f"无效方向值: {v}"

        if t == "session_mode":
            if v not in ("desktop", "mobile"):
                return False, "模式必须为 desktop 或 mobile"
            save_session_mode(v)
            if self.client.status().state == "running":
                return restart_droidloom(mode=v)
            return True, f"已设置为 {v} 模式（下次启动生效）"

        if t == "games_driver":
            if v not in ("angle", "mesa"):
                return False, "驱动必须为 angle 或 mesa"
            try:
                resp = self.client.set_games_driver(v)
                return True, resp.message or f"已切换为 {v}"
            except ControlError as exc:
                return False, str(exc)

        if t == "mic_capture":
            enabled = v.lower() in ("1", "true", "on", "yes")
            cfg = load_audio_settings(self.config_dir)
            cfg.allow_capture = enabled
            save_audio_settings(cfg, self.config_dir)
            ok, msg = systemctl_user(["restart", "droidloom-audio.service"])
            return (True, f"麦克风采集已{'开启' if enabled else '关闭'}") if ok else (False, msg)

        if t == "desktop_forwarding":
            enabled = v.lower() in ("1", "true", "on", "yes")
            pcfg = load_panel_config(self.config_dir)
            pcfg.desktop_forwarding = enabled
            save_panel_config(pcfg, self.config_dir)
            if self.notif_bridge is not None and self.notif_bridge.is_connected():
                self.notif_bridge.set_desktop_forwarding(enabled)
            return True, f"桌面通知弹窗同步已{'开启' if enabled else '关闭'}"

        if t == "android_settings":
            return self.launch_app("com.android.settings")

        if t == "media_rescan":
            ok, target = is_adb_connected(self._cell_ip())
            if not ok:
                return False, "ADB 未连接"
            return trigger_media_rescan(selector=target)

        return False, f"未知快捷开关: {t}"

    def _spawn_detached(self, cmd: list[str], label: str) -> tuple[bool, str]:
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, f"已打开 {label}"
        except Exception as exc:
            return False, f"启动 {label} 失败: {exc}"

    def open_settings(self, page: str = "overview", package: str = "") -> tuple[bool, str]:
        exe = shutil.which("droidloom-settings")
        local_bin = Path.home() / ".local/bin/droidloom-settings"
        if exe:
            cmd = [exe]
        elif local_bin.is_file():
            cmd = [str(local_bin)]
        else:
            cmd = [sys.executable, "-m", "droidloom_settings.main"]
        valid_pages = {"overview", "network", "storage", "hardware", "display", "apps", "diagnostics"}
        chosen_page = page if page in valid_pages else "overview"
        cmd.extend(["--page", chosen_page])
        if package.strip():
            cmd.extend(["--package", package.strip()])
        return self._spawn_detached(cmd, "Droidloom 设置")

    def open_apk_installer(self) -> tuple[bool, str]:
        exe = shutil.which("droidloom-apk-installer")
        local_bin = Path.home() / ".local/bin/droidloom-apk-installer"
        if exe:
            cmd = [exe]
        elif local_bin.is_file():
            cmd = [str(local_bin)]
        else:
            return self.open_settings(page="apps")
        return self._spawn_detached(cmd, "APK 安装器")
