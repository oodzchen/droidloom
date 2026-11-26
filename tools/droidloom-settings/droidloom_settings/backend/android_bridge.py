"""Bounded ADB bridge for Android runtime properties, network proxy/DNS, orientation, and tweaks."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

ADB_BIN = "/usr/bin/adb"
DEFAULT_SELECTOR = "device:droidloom"


def _adb_cmd(selector: str = DEFAULT_SELECTOR) -> list[str]:
    exe = ADB_BIN if shutil.which("adb") is None else shutil.which("adb") or ADB_BIN
    return [exe, "-s", selector]


def is_adb_connected(cell_ip: str = "10.177.0.2") -> tuple[bool, str]:
    exe = shutil.which("adb") or ADB_BIN
    try:
        res = subprocess.run(
            [exe, "devices", "-l"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=4,
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        for line in res.stdout.splitlines()[1:]:
            if "device:droidloom" in line or f"{cell_ip}:5555" in line:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    return True, parts[0]
                return False, line.strip()
        return False, "未检测到已连接的 Droidloom ADB 设备"
    except Exception as exc:
        return False, str(exc)


def reconnect_adb(cell_ip: str = "10.177.0.2") -> tuple[bool, str]:
    exe = shutil.which("adb") or ADB_BIN
    try:
        res = subprocess.run(
            [exe, "connect", f"{cell_ip}:5555"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=6,
        )
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0 and "connected" in out, out
    except Exception as exc:
        return False, str(exc)


def adb_shell(
    command: str, selector: str = DEFAULT_SELECTOR, timeout: float = 6.0
) -> tuple[bool, str]:
    try:
        res = subprocess.run(
            [*_adb_cmd(selector), "shell", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def getprop(name: str, selector: str = DEFAULT_SELECTOR) -> str:
    ok, out = adb_shell(f"getprop {shlex_quote(name)}", selector=selector)
    return out.strip() if ok else ""


def setprop(name: str, value: str, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    return adb_shell(
        f"setprop {shlex_quote(name)} {shlex_quote(value)}", selector=selector
    )


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


@dataclass
class AndroidSnapshot:
    connected: bool = False
    selector: str = DEFAULT_SELECTOR
    # Orientation & display
    user_rotation_mode: str = "free"  # "free" or "lock"
    user_rotation_index: int = 0
    fixed_to_user_rotation: str = "default"
    accelerometer_rotation: int = 0
    physical_size: str = ""
    physical_density: str = ""
    override_density: str = ""
    sensor_status: str = ""
    font_scale: str = "1.0"
    window_anim_scale: str = "1.0"
    transition_anim_scale: str = "1.0"
    animator_scale: str = "1.0"
    show_touches: bool = False
    pointer_location: bool = False
    # Network inside Android
    http_proxy: str = ""
    proxy_exclusion: str = ""
    private_dns_mode: str = "opportunistic"
    private_dns_specifier: str = ""
    airplane_mode: bool = False
    # Hardware & spoofing properties
    soc_model_persist: str = ""
    soc_model_live: str = ""
    angle_renderer_persist: str = ""
    angle_version_persist: str = ""
    ro_product_model: str = ""
    ro_product_brand: str = ""
    ro_product_manufacturer: str = ""
    ro_hardware_vulkan: str = ""
    minigbm_debug: str = ""
    extra_live_props: dict[str, str] = field(default_factory=dict)


def query_android_snapshot(cell_ip: str = "10.177.0.2") -> AndroidSnapshot:
    snap = AndroidSnapshot()
    connected, target = is_adb_connected(cell_ip=cell_ip)
    if not connected:
        return snap
    selector = target if target else DEFAULT_SELECTOR
    snap.connected = True
    snap.selector = selector

    script = r"""
echo "===WM==="
wm size 2>/dev/null || true
wm density 2>/dev/null || true
wm user-rotation 2>/dev/null || true
wm fixed-to-user-rotation 2>/dev/null || true
echo "===SETTINGS==="
echo "acc=$(settings get system accelerometer_rotation 2>/dev/null)"
echo "urot=$(settings get system user_rotation 2>/dev/null)"
echo "font=$(settings get system font_scale 2>/dev/null)"
echo "wanim=$(settings get global window_animation_scale 2>/dev/null)"
echo "tanim=$(settings get global transition_animation_scale 2>/dev/null)"
echo "aanim=$(settings get global animator_duration_scale 2>/dev/null)"
echo "touches=$(settings get system show_touches 2>/dev/null)"
echo "ptr=$(settings get system pointer_location 2>/dev/null)"
echo "proxy=$(settings get global http_proxy 2>/dev/null)"
echo "pexcl=$(settings get global global_http_proxy_exclusion_list 2>/dev/null)"
echo "pdns_mode=$(settings get global private_dns_mode 2>/dev/null)"
echo "pdns_spec=$(settings get global private_dns_specifier 2>/dev/null)"
echo "air=$(settings get global airplane_mode_on 2>/dev/null)"
echo "===PROPS==="
for p in persist.vendor.droidloom.soc_model ro.soc.model persist.vendor.droidloom.angle.gl_renderer persist.vendor.droidloom.angle.gl_version ro.product.model ro.product.brand ro.product.manufacturer ro.hardware.vulkan vendor.minigbm.debug ro.droidloom.device_name; do
  echo "$p=$(getprop "$p" 2>/dev/null)"
done
echo "===SENSOR==="
dumpsys sensorservice 2>/dev/null | head -n 5 || true
"""
    ok, out = adb_shell(script, selector=selector, timeout=8.0)
    if not ok:
        return snap

    section = ""
    sensor_lines: list[str] = []
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if line in ("===WM===", "===SETTINGS===", "===PROPS===", "===SENSOR==="):
            section = line
            continue
        if section == "===WM===":
            if line.startswith("Physical size:"):
                snap.physical_size = line.split(":", 1)[1].strip()
            elif line.startswith("Physical density:"):
                snap.physical_density = line.split(":", 1)[1].strip()
            elif line.startswith("Override density:"):
                snap.override_density = line.split(":", 1)[1].strip()
            elif line.startswith("lock"):
                snap.user_rotation_mode = "lock"
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    snap.user_rotation_index = int(parts[1]) % 4
            elif line == "free":
                snap.user_rotation_mode = "free"
            elif line.startswith("fixed-to-user-rotation:"):
                snap.fixed_to_user_rotation = line.split(":", 1)[1].strip()
            elif line in ("enabled", "disabled", "default"):
                snap.fixed_to_user_rotation = line
        elif section == "===SETTINGS===" and "=" in line:
            k, v = line.split("=", 1)
            v = "" if v == "null" else v.strip()
            if k == "acc" and v.isdigit():
                snap.accelerometer_rotation = int(v)
            elif k == "urot" and v.isdigit():
                snap.user_rotation_index = int(v) % 4
            elif k == "font" and v:
                snap.font_scale = v
            elif k == "wanim" and v:
                snap.window_anim_scale = v
            elif k == "tanim" and v:
                snap.transition_anim_scale = v
            elif k == "aanim" and v:
                snap.animator_scale = v
            elif k == "touches":
                snap.show_touches = v == "1"
            elif k == "ptr":
                snap.pointer_location = v == "1"
            elif k == "proxy":
                snap.http_proxy = "" if v in ("", ":0") else v
            elif k == "pexcl":
                snap.proxy_exclusion = v
            elif k == "pdns_mode" and v:
                snap.private_dns_mode = v
            elif k == "pdns_spec":
                snap.private_dns_specifier = v
            elif k == "air":
                snap.airplane_mode = v == "1"
        elif section == "===PROPS===" and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            snap.extra_live_props[k] = v
            if k == "persist.vendor.droidloom.soc_model":
                snap.soc_model_persist = v
            elif k == "ro.soc.model":
                snap.soc_model_live = v
            elif k == "persist.vendor.droidloom.angle.gl_renderer":
                snap.angle_renderer_persist = v
            elif k == "persist.vendor.droidloom.angle.gl_version":
                snap.angle_version_persist = v
            elif k == "ro.product.model":
                snap.ro_product_model = v
            elif k == "ro.product.brand":
                snap.ro_product_brand = v
            elif k == "ro.product.manufacturer":
                snap.ro_product_manufacturer = v
            elif k == "ro.hardware.vulkan":
                snap.ro_hardware_vulkan = v
            elif k == "vendor.minigbm.debug":
                snap.minigbm_debug = v
        elif section == "===SENSOR===" and line:
            sensor_lines.append(line)

    snap.sensor_status = " / ".join(sensor_lines[:2]) if sensor_lines else "无传感器"
    return snap


def apply_android_rotation(
    mode: str,
    rotation_index: int = 0,
    fixed_mode: str | None = None,
    selector: str = DEFAULT_SELECTOR,
) -> tuple[bool, str]:
    idx = rotation_index % 4
    cmds: list[str] = []
    if fixed_mode in ("default", "enabled", "disabled"):
        cmds.append(f"wm fixed-to-user-rotation {fixed_mode}")
    if mode == "free":
        cmds.append("settings put system accelerometer_rotation 1")
        cmds.append("wm user-rotation free")
    else:
        cmds.append("settings put system accelerometer_rotation 0")
        cmds.append(f"settings put system user_rotation {idx}")
        cmds.append(f"wm user-rotation lock {idx}")
    return adb_shell(" && ".join(cmds), selector=selector)


def apply_android_proxy(
    host: str,
    port: int | None,
    exclusion_list: str = "",
    selector: str = DEFAULT_SELECTOR,
) -> tuple[bool, str]:
    host = host.strip()
    if not host or not port or port <= 0:
        cmd = (
            "settings delete global http_proxy ; "
            "settings delete global global_http_proxy_host ; "
            "settings delete global global_http_proxy_port ; "
            "settings delete global global_http_proxy_exclusion_list"
        )
        return adb_shell(cmd, selector=selector)
    proxy_str = f"{host}:{int(port)}"
    cmds = [
        f"settings put global http_proxy {shlex_quote(proxy_str)}",
        f"settings put global global_http_proxy_host {shlex_quote(host)}",
        f"settings put global global_http_proxy_port {int(port)}",
    ]
    if exclusion_list.strip():
        cmds.append(
            f"settings put global global_http_proxy_exclusion_list {shlex_quote(exclusion_list.strip())}"
        )
    else:
        cmds.append("settings delete global global_http_proxy_exclusion_list")
    return adb_shell(" && ".join(cmds), selector=selector)


def apply_android_private_dns(
    mode: str, specifier: str = "", selector: str = DEFAULT_SELECTOR
) -> tuple[bool, str]:
    if mode not in ("off", "opportunistic", "hostname"):
        return False, "无效的 Private DNS 模式"
    cmds = [f"settings put global private_dns_mode {shlex_quote(mode)}"]
    if mode == "hostname":
        if not specifier.strip():
            return False, "hostname 模式必须填写 DoT 服务器域名"
        cmds.append(
            f"settings put global private_dns_specifier {shlex_quote(specifier.strip())}"
        )
    else:
        cmds.append("settings delete global private_dns_specifier || true")
    return adb_shell(" && ".join(cmds), selector=selector)


def apply_android_visual_tweaks(
    font_scale: float,
    window_scale: float,
    transition_scale: float,
    animator_scale: float,
    show_touches: bool,
    pointer_location: bool,
    selector: str = DEFAULT_SELECTOR,
) -> tuple[bool, str]:
    cmds = [
        f"settings put system font_scale {font_scale:.2f}",
        f"settings put global window_animation_scale {window_scale:.2f}",
        f"settings put global transition_animation_scale {transition_scale:.2f}",
        f"settings put global animator_duration_scale {animator_scale:.2f}",
        f"settings put system show_touches {1 if show_touches else 0}",
        f"settings put system pointer_location {1 if pointer_location else 0}",
    ]
    return adb_shell(" && ".join(cmds), selector=selector)


def apply_hardware_spoof_props(
    soc_model: str,
    angle_renderer: str,
    angle_version: str,
    selector: str = DEFAULT_SELECTOR,
) -> tuple[bool, str]:
    cmds = [
        f"setprop persist.vendor.droidloom.soc_model {shlex_quote(soc_model.strip())}",
        f"setprop persist.vendor.droidloom.angle.gl_renderer {shlex_quote(angle_renderer.strip())}",
        f"setprop persist.vendor.droidloom.angle.gl_version {shlex_quote(angle_version.strip())}",
    ]
    return adb_shell(" && ".join(cmds), selector=selector)


def trigger_media_rescan(selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    cmd = (
        "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        "-d file:///storage/emulated/0/"
    )
    return adb_shell(cmd, selector=selector)


SYSTEM_APP_LABELS: dict[str, str] = {
    "android": "Android 系统核心",
    "com.android.droidloom.home": "Droidloom 占位桌面",
    "com.android.droidloom.ime": "Droidloom 宿主机输入法",
    "com.android.systemui": "Droidloom SystemUI",
    "com.android.settings": "Android 系统设置",
    "com.android.documentsui": "文件管理器",
    "com.android.permissioncontroller": "权限管理服务",
    "com.android.providers.media.module": "媒体存储服务",
    "com.android.providers.calendar": "日历存储服务",
    "com.android.externalstorage": "外部存储服务",
    "com.android.webview": "Android System WebView",
    "com.android.packageinstaller": "软件包安装程序",
    "com.android.networkstack": "网络栈",
    "com.android.networkstack.process": "网络栈服务",
    "com.android.networkstack.tethering": "网络共享服务",
    "com.android.phone": "电话服务",
    "com.android.keychain": "密钥链",
    "com.android.se": "安全元件服务",
    "com.android.printspooler": "打印后台处理",
    "com.android.location.fused": "融合定位服务",
    "com.android.musicfx": "音效服务",
    "com.android.server.telecom": "通话管理服务",
    "android.ext.services": "Android 扩展服务",
    "com.android.dynsystem": "动态系统服务",
    "com.android.statementservice": "意图链接验证服务",
    "com.android.rkpdapp": "远程密钥配置服务",
    "com.android.managedprovisioning": "设备配置服务",
    "com.android.devicediagnostics": "设备诊断服务",
    "com.android.cellbroadcastreceiver.module": "小区广播接收器",
    "com.android.imsserviceentitlement": "IMS 授权服务",
    "com.android.healthconnect.controller": "Health Connect",
    "com.android.federatedcompute.services": "联邦计算服务",
    "com.android.ondevicepersonalization.services": "端侧个性化服务",
    "com.android.adservices.api": "广告隐私服务",
    "com.android.ons": "Opportunistic Network",
    "com.android.stk": "SIM 卡应用",
}


_DROIDLOOM_INTERNAL_PACKAGES = frozenset(
    {
        "com.android.droidloom.home",
        "com.android.droidloom.ime",
    }
)

_CORE_ANDROID_PACKAGES = frozenset(SYSTEM_APP_LABELS.keys())


def query_android_dnd(selector: str = DEFAULT_SELECTOR) -> tuple[bool, int]:
    ok, out = adb_shell("settings get global zen_mode", selector=selector, timeout=4.0)
    if not ok:
        return False, 0
    val = out.strip()
    if val.isdigit():
        mode = int(val)
        return mode > 0, mode
    return False, 0


def set_android_dnd(enabled: bool, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    arg = "on" if enabled else "off"
    return adb_shell(f"cmd notification set_dnd {arg}", selector=selector, timeout=4.0)


def query_quick_settings_snapshot(cell_ip: str = "10.177.0.2") -> dict[str, Any]:
    """Lightweight ADB query for Quick Settings tile states (avoids heavy sensorservice dump)."""
    connected, target = is_adb_connected(cell_ip=cell_ip)
    if not connected:
        return {"connected": False}
    selector = target or DEFAULT_SELECTOR
    script = r"""
echo "zen=$(settings get global zen_mode 2>/dev/null)"
echo "night=$(cmd uimode night 2>/dev/null)"
echo "acc=$(settings get system accelerometer_rotation 2>/dev/null)"
echo "urot=$(settings get system user_rotation 2>/dev/null)"
echo "air=$(cmd connectivity airplane-mode 2>/dev/null || settings get global airplane_mode_on 2>/dev/null)"
echo "proxy=$(settings get global http_proxy 2>/dev/null)"
echo "stay_on=$(settings get global stay_on_while_plugged_in 2>/dev/null)"
echo "anim=$(settings get global animator_duration_scale 2>/dev/null)"
echo "touches=$(settings get system show_touches 2>/dev/null)"
echo "ptr=$(settings get system pointer_location 2>/dev/null)"
"""
    ok, out = adb_shell(script, selector=selector, timeout=4.0)
    if not ok:
        return {"connected": False}
    res: dict[str, Any] = {
        "connected": True,
        "selector": selector,
        "dnd": False,
        "zen_mode": 0,
        "night_mode": False,
        "rotation_mode": "free",
        "rotation_index": 0,
        "airplane_mode": False,
        "http_proxy": "",
        "stay_awake": False,
        "anim_scale": 1.0,
        "show_touches": False,
        "pointer_location": False,
    }
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if k == "zen" and v.isdigit():
            zm = int(v)
            res["zen_mode"] = zm
            res["dnd"] = zm > 0
        elif k == "night":
            res["night_mode"] = "yes" in v.lower()
        elif k == "acc" and v.isdigit():
            res["rotation_mode"] = "free" if int(v) == 1 else "lock"
        elif k == "urot" and v.isdigit():
            res["rotation_index"] = int(v) % 4
        elif k == "air":
            res["airplane_mode"] = v.lower() in ("1", "enabled", "true")
        elif k == "proxy":
            res["http_proxy"] = "" if v in ("", "null", ":0") else v
        elif k == "stay_on":
            res["stay_awake"] = v not in ("", "0", "null")
        elif k == "anim":
            try:
                if v and v != "null":
                    res["anim_scale"] = float(v)
            except ValueError:
                pass
        elif k == "touches":
            res["show_touches"] = v == "1"
        elif k == "ptr":
            res["pointer_location"] = v == "1"
    return res


def set_android_airplane_mode(enabled: bool, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    arg = "enable" if enabled else "disable"
    return adb_shell(f"cmd connectivity airplane-mode {arg}", selector=selector, timeout=4.0)


def set_android_stay_awake(enabled: bool, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    val = "7" if enabled else "0"
    return adb_shell(f"settings put global stay_on_while_plugged_in {val}", selector=selector, timeout=4.0)


def set_android_anim_scale(scale: float, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    s = f"{max(0.0, min(10.0, float(scale))):.2f}"
    cmds = (
        f"settings put global window_animation_scale {s} && "
        f"settings put global transition_animation_scale {s} && "
        f"settings put global animator_duration_scale {s}"
    )
    return adb_shell(cmds, selector=selector, timeout=4.0)


def set_android_show_touches(enabled: bool, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    return adb_shell(
        f"settings put system show_touches {1 if enabled else 0}", selector=selector, timeout=4.0
    )


def set_android_pointer_location(enabled: bool, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    return adb_shell(
        f"settings put system pointer_location {1 if enabled else 0}", selector=selector, timeout=4.0
    )


def dispatch_media_key(key: str, selector: str = DEFAULT_SELECTOR) -> tuple[bool, str]:
    allowed = {"play", "pause", "play-pause", "next", "previous", "stop"}
    if key not in allowed:
        return False, f"不支持的媒体键: {key}"
    return adb_shell(f"cmd media_session dispatch {key}", selector=selector, timeout=4.0)


def snooze_adb_notification(
    key: str, selector: str = DEFAULT_SELECTOR, duration_ms: int = 86400000
) -> tuple[bool, str]:
    return adb_shell(
        f"cmd notification snooze --for {int(duration_ms)} {shlex_quote(key)}",
        selector=selector,
        timeout=4.0,
    )


def _extract_string_field(line: str, prefix: str) -> str | None:
    if prefix not in line:
        return None
    idx = line.find(prefix)
    rest = line[idx + len(prefix) :]
    if rest.startswith("String (") and rest.endswith(")"):
        return rest[len("String (") : -1]
    if rest == "null":
        return ""
    return rest


def parse_dumpsys_notifications(raw_text: str) -> list[dict[str, Any]]:
    """Parse `dumpsys notification --noredact` active NotificationRecord blocks."""
    import re

    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_actions = False

    def flush() -> None:
        nonlocal current, in_actions
        if current is not None and current.get("key"):
            records.append(current)
        current = None
        in_actions = False

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if line.startswith("NotificationRecord("):
            flush()
            m_pkg = re.search(r"pkg=([^\s:]+)", line)
            m_key = re.search(r"key=([^\s:]+)", line)
            m_imp = re.search(r"importance=(-?\d+)", line)
            m_flags = re.search(r"flags=([^\s)]+)", line)
            m_cat = re.search(r"category=([^\s)]+)", line)
            m_grp = re.search(r"groupKey=([^\s)]+)", line)
            flags_str = m_flags.group(1) if m_flags else ""
            ongoing = any(
                f in flags_str for f in ("ONGOING_EVENT", "FOREGROUND_SERVICE", "NO_CLEAR")
            )
            importance = int(m_imp.group(1)) if m_imp else 3
            current = {
                "key": m_key.group(1) if m_key else "",
                "package": m_pkg.group(1) if m_pkg else "",
                "app": m_pkg.group(1) if m_pkg else "",
                "title": "",
                "body": "",
                "sub_text": "",
                "ongoing": ongoing,
                "clearable": not ongoing,
                "urgency": 0 if importance <= 2 else 1,
                "when": 0,
                "progress": -1,
                "_progress_val": 0,
                "_progress_max": 0,
                "_progress_indet": False,
                "category": (
                    ""
                    if not m_cat or m_cat.group(1) == "null"
                    else m_cat.group(1)
                ),
                "group": (
                    ""
                    if not m_grp or m_grp.group(1) == "null"
                    else m_grp.group(1)
                ),
                "actions": [{"key": "default", "label": "打开应用"}],
                "messages": [],
                "is_media": False,
                "source": "adb_fallback",
            }
            continue

        if current is None:
            continue

        # End of active notifications section if another dumpsys section begins
        if line.startswith(("Notification Preferences:", "Zen Mode:", "Snoozed notifications:")):
            flush()
            break

        if in_actions:
            if line == "}":
                in_actions = False
                continue
            m_act = re.match(r'^\[(\d+)\]\s+"([^"]*)"\s*->', line)
            if m_act:
                act_key = f"action-{m_act.group(1)}"
                act_lbl = m_act.group(2).strip() or act_key
                if not any(a["key"] == act_key for a in current["actions"]):
                    current["actions"].append({"key": act_key, "label": act_lbl})
            continue

        if line == "actions={":
            in_actions = True
        elif line.startswith("flags="):
            flags_str = line[6:].strip()
            ongoing = any(
                f in flags_str for f in ("ONGOING_EVENT", "FOREGROUND_SERVICE", "NO_CLEAR")
            )
            current["ongoing"] = ongoing
            current["clearable"] = not ongoing
        elif line.startswith("when="):
            parts = line[5:].split("/")
            for part in reversed(parts):
                if part.isdigit() and int(part) > 0:
                    current["when"] = int(part)
                    break
        elif "android.template=" in line and "MediaStyle" in line:
            current["is_media"] = True
        elif "android.title=" in line:
            val = _extract_string_field(line, "android.title=")
            if val is not None:
                current["title"] = val
        elif "android.bigText=" in line:
            val = _extract_string_field(line, "android.bigText=")
            if val:
                current["body"] = val
        elif "android.text=" in line:
            val = _extract_string_field(line, "android.text=")
            if val is not None and not current["body"]:
                current["body"] = val
        elif "android.subText=" in line:
            val = _extract_string_field(line, "android.subText=")
            if val:
                current["sub_text"] = val
        elif "android.progress=" in line:
            m = re.search(r"android\.progress=(?:Integer\s*\()?(\d+)", line)
            if m:
                current["_progress_val"] = int(m.group(1))
        elif "android.progressMax=" in line:
            m = re.search(r"android\.progressMax=(?:Integer\s*\()?(\d+)", line)
            if m:
                current["_progress_max"] = int(m.group(1))
        elif "android.progressIndeterminate=" in line:
            current["_progress_indet"] = "true" in line.lower()

    flush()
    for item in records:
        pmax = item.pop("_progress_max", 0)
        pval = item.pop("_progress_val", 0)
        pind = item.pop("_progress_indet", False)
        if pmax > 0 and not pind:
            item["progress"] = max(0, min(100, int(pval * 100 / pmax)))
    records.sort(key=lambda n: (not n["ongoing"], -int(n["when"]), n["key"]))
    return records


def query_adb_notifications(selector: str = DEFAULT_SELECTOR) -> list[dict[str, Any]]:
    ok, out = adb_shell("dumpsys notification --noredact", selector=selector, timeout=5.0)
    if not ok or not out:
        return []
    return parse_dumpsys_notifications(out)


def _proc_state_label(state: int | None) -> str:
    if state is None:
        return "运行中"
    if state <= 2:
        return "前台"
    if state <= 5:
        return "前台服务"
    if state <= 10:
        return "后台服务"
    if state <= 15:
        return "缓存"
    return "驻留"


def _normalize_component(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if "/" not in text:
        return "", ""
    pkg, cls = text.split("/", 1)
    if not pkg or not cls:
        return "", ""
    if cls.startswith("."):
        cls = f"{pkg}{cls}"
    return pkg, f"{pkg}/{cls}"


def parse_running_tasks_and_services(
    raw_text: str, include_system: bool = False
) -> dict[str, Any]:
    """Parse batched dumpsys recents/activities/services/processes + ps RSS output."""
    import re

    sections: dict[str, list[str]] = {}
    cur = ""
    for line in raw_text.splitlines():
        s = line.strip()
        if s in (
            "===RECENTS===",
            "===ACTIVITIES===",
            "===SERVICES===",
            "===PROCS===",
            "===PS===",
            "===USER_PKGS===",
        ):
            cur = s
            sections.setdefault(cur, [])
            continue
        if cur:
            sections[cur].append(line)

    has_user_pkgs_section = "===USER_PKGS===" in sections
    user_pkgs: set[str] = set()
    for line in sections.get("===USER_PKGS===", []):
        s = line.strip()
        if s.startswith("package:"):
            pkg = s[len("package:") :].strip()
            if pkg and pkg not in _DROIDLOOM_INTERNAL_PACKAGES:
                user_pkgs.add(pkg)

    # 1. RSS memory per package (aggregating main + child processes by package prefix)
    pkg_rss: dict[str, int] = {}
    pkg_pids: dict[str, int] = {}
    for line in sections.get("===PS===", []):
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rss_kb = int(parts[1])
        proc_name = parts[2]
        pkg = proc_name.split(":", 1)[0]
        if "." in pkg:
            pkg_rss[pkg] = pkg_rss.get(pkg, 0) + rss_kb
            pkg_pids[pkg] = pkg_pids.get(pkg, 0) + 1

    # 2. Visible/resumed activities from dumpsys activity activities
    visible_pkgs: set[str] = set()
    for line in sections.get("===ACTIVITIES===", []):
        m_res = re.search(r"(?:mResumedActivity|topResumedActivity)=.*?\s([^\s/]+)/", line)
        if m_res:
            visible_pkgs.add(m_res.group(1))
        if "visible=true" in line or "visibleRequested=true" in line:
            m_task = re.search(r"A=\d+:([^\s\}]+)", line)
            if m_task:
                visible_pkgs.add(m_task.group(1))

    # 3. Foreground & background services
    svc_map: dict[str, dict[str, Any]] = {}
    cur_svc_pkg = ""
    for line in sections.get("===SERVICES===", []):
        s = line.strip()
        if s.startswith("* ServiceRecord{"):
            m_cmp = re.search(r"u\d+\s+([^\s/]+)/([^\s\}]+)", s)
            cur_svc_pkg = m_cmp.group(1) if m_cmp else ""
            svc_name = m_cmp.group(2) if m_cmp else s
            if cur_svc_pkg:
                entry = svc_map.setdefault(
                    cur_svc_pkg,
                    {"is_foreground": False, "services": []},
                )
                if svc_name not in entry["services"]:
                    entry["services"].append(svc_name)
        elif cur_svc_pkg and s.startswith("packageName="):
            pkg = s.split("=", 1)[1].strip()
            if pkg:
                cur_svc_pkg = pkg
                svc_map.setdefault(cur_svc_pkg, {"is_foreground": False, "services": []})
        elif cur_svc_pkg and "isForeground=true" in s:
            svc_map[cur_svc_pkg]["is_foreground"] = True

    # 4. Process states from dumpsys activity processes
    proc_states: dict[str, int] = {}
    cur_proc_pkg = ""
    for line in sections.get("===PROCS===", []):
        s = line.strip()
        m_proc = re.search(r"ProcessRecord\{[^\s]+\s+\d+:([^/:]+)/", s)
        if m_proc:
            cur_proc_pkg = m_proc.group(1)
        elif cur_proc_pkg and "setProcState=" in s:
            m_st = re.search(r"setProcState=(\d+)", s)
            if m_st:
                st = int(m_st.group(1))
                prev = proc_states.get(cur_proc_pkg)
                if prev is None or st < prev:
                    proc_states[cur_proc_pkg] = st

    # 5. Recent tasks from dumpsys activity recents
    tasks: list[dict[str, Any]] = []
    seen_task_pkgs: set[str] = set()
    cur_task: dict[str, Any] | None = None
    in_recent_tasks = False

    def flush_task() -> None:
        nonlocal cur_task
        if cur_task is None:
            return
        affinity = str(cur_task.pop("_affinity", ""))
        orig_comp = str(cur_task.pop("_orig_comp", ""))
        intent_comp = str(cur_task.pop("_intent_comp", ""))
        activity_comp = str(cur_task.pop("_activity_comp", ""))
        pkg = ""
        norm_comp = ""
        for raw_comp in (orig_comp, intent_comp, activity_comp):
            p, c = _normalize_component(raw_comp)
            if p:
                pkg, norm_comp = p, c
                break
        if not pkg:
            pkg = affinity
        if pkg:
            cur_task["package"] = pkg
            cur_task["component"] = norm_comp
            cur_task["visible"] = (pkg in visible_pkgs) or (
                bool(affinity) and affinity in visible_pkgs
            )
            tasks.append(cur_task)
        cur_task = None

    for line in sections.get("===RECENTS===", []):
        s = line.strip()
        if s.startswith("Recent tasks:"):
            in_recent_tasks = True
            continue
        if s.startswith("Visible recent tasks"):
            break
        if not in_recent_tasks:
            continue
        if s.startswith("* Recent #"):
            flush_task()
            m_id = re.search(r"#(\d+)\s+type=(\S+)", s)
            m_aff = re.search(r"A=\d+:([^\s\}]+)", s)
            affinity = m_aff.group(1) if m_aff else ""
            cur_task = {
                "task_id": int(m_id.group(1)) if m_id else 0,
                "type": m_id.group(2) if m_id else "standard",
                "package": "",
                "component": "",
                "has_activities": True,
                "visible": False,
                "last_active_ms": 0,
                "_affinity": affinity,
                "_orig_comp": "",
                "_intent_comp": "",
                "_activity_comp": "",
            }
        elif cur_task is not None:
            if s.startswith("origActivity="):
                cur_task["_orig_comp"] = s.split("=", 1)[1].strip()
            elif s.startswith("mActivityComponent="):
                cur_task["_activity_comp"] = s.split("=", 1)[1].strip()
            elif s.startswith("intent="):
                m_cmp = re.search(r"cmp=([^\s\}]+)", s)
                if m_cmp:
                    cur_task["_intent_comp"] = m_cmp.group(1)
            elif s.startswith("Activities=[]"):
                cur_task["has_activities"] = False
            elif s.startswith("lastActiveTime="):
                m_t = re.search(r"lastActiveTime=(\d+)", s)
                if m_t:
                    cur_task["last_active_ms"] = int(m_t.group(1))
    flush_task()

    filtered_tasks: list[dict[str, Any]] = []
    for t in tasks:
        pkg = t["package"]
        if t.get("type") == "home" or pkg in ("com.android.droidloom.home", "com.android.systemui"):
            continue
        if pkg in seen_task_pkgs:
            continue
        seen_task_pkgs.add(pkg)
        svc_info = svc_map.get(pkg, {})
        t["rss_kb"] = pkg_rss.get(pkg, 0)
        t["process_count"] = pkg_pids.get(pkg, 0)
        t["foreground_service"] = bool(svc_info.get("is_foreground", False))
        t["running"] = bool(t["has_activities"] or t["process_count"] > 0)
        filtered_tasks.append(t)

    # 6. Background / service apps (only real ActivityManager services & processes, not native HALs)
    bg_pkgs = set(svc_map.keys()) | set(proc_states.keys())
    background_apps: list[dict[str, Any]] = []
    for pkg in sorted(bg_pkgs):
        if not pkg or "." not in pkg:
            continue
        if not include_system:
            if pkg.startswith(".") or pkg in _DROIDLOOM_INTERNAL_PACKAGES:
                continue
            if has_user_pkgs_section and user_pkgs:
                if pkg not in user_pkgs and pkg not in seen_task_pkgs:
                    continue
            else:
                if pkg in _CORE_ANDROID_PACKAGES or pkg.startswith(
                    ("com.android.", "android.", "media.")
                ):
                    continue
        svc_info = svc_map.get(pkg, {"is_foreground": False, "services": []})
        st = proc_states.get(pkg)
        is_fg = bool(svc_info.get("is_foreground", False))
        rss = pkg_rss.get(pkg, 0)
        if not is_fg and not svc_info["services"] and rss == 0:
            continue
        background_apps.append(
            {
                "package": pkg,
                "is_foreground_service": is_fg,
                "service_count": len(svc_info["services"]),
                "services": svc_info["services"][:8],
                "process_count": pkg_pids.get(pkg, 0),
                "rss_kb": rss,
                "proc_state": st,
                "proc_state_label": "前台服务" if is_fg else _proc_state_label(st),
                "visible": pkg in visible_pkgs,
                "in_recent_tasks": pkg in seen_task_pkgs,
            }
        )

    background_apps.sort(
        key=lambda a: (
            not a["is_foreground_service"],
            not a["visible"],
            -int(a["rss_kb"]),
            a["package"],
        )
    )
    return {
        "recent_tasks": filtered_tasks,
        "background_apps": background_apps,
    }


def query_running_tasks_and_services(
    cell_ip: str = "10.177.0.2", include_system: bool = False
) -> dict[str, Any]:
    connected, target = is_adb_connected(cell_ip=cell_ip)
    if not connected:
        return {"connected": False, "recent_tasks": [], "background_apps": []}
    selector = target or DEFAULT_SELECTOR
    script = r"""
echo "===USER_PKGS==="
pm list packages -3 2>/dev/null || true
echo "===RECENTS==="
dumpsys activity recents 2>/dev/null | head -n 250 || true
echo "===ACTIVITIES==="
dumpsys activity activities 2>/dev/null | grep -E 'Task\{|mResumedActivity|topResumedActivity|visible=' || true
echo "===SERVICES==="
dumpsys activity services 2>/dev/null | grep -E 'ServiceRecord\{|packageName=|isForeground=' || true
echo "===PROCS==="
dumpsys activity processes 2>/dev/null | grep -E 'ProcessRecord\{|setProcState=' || true
echo "===PS==="
ps -A -o PID,RSS,NAME 2>/dev/null || true
"""
    ok, out = adb_shell(script, selector=selector, timeout=6.0)
    if not ok:
        return {"connected": False, "recent_tasks": [], "background_apps": []}
    parsed = parse_running_tasks_and_services(out, include_system=include_system)
    parsed["connected"] = True
    return parsed
