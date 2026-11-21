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
