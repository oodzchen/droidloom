"""Host physical accelerometer -> Android window rotation sync daemon & status query."""

from __future__ import annotations

import signal
import sys
from typing import Any

from .android_bridge import apply_android_rotation, is_adb_connected
from .user_configs import load_orientation_config

ORIENTATION_TO_ROTATION = {
    "normal": 0,
    "right-up": 1,
    "bottom-up": 2,
    "left-up": 3,
}


def query_host_sensor_status() -> dict[str, Any]:
    """Query iio-sensor-proxy over system D-Bus without claiming persistently."""
    try:
        from gi.repository import Gio
    except Exception as exc:
        return {"available": False, "error": f"PyGObject 不可用: {exc}"}

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "net.hadess.SensorProxy",
            "/net/hadess/SensorProxy",
            "net.hadess.SensorProxy",
            None,
        )
        has_accel = proxy.get_cached_property("HasAccelerometer")
        has_accel_bool = bool(has_accel.unpack()) if has_accel is not None else False
        orient_v = proxy.get_cached_property("AccelerometerOrientation")
        orient_str = str(orient_v.unpack()) if orient_v is not None else "undefined"

        # If orientation is undefined because nobody claimed it yet, claim briefly to read initial value
        if has_accel_bool and orient_str == "undefined":
            try:
                proxy.call_sync("ClaimAccelerometer", None, Gio.DBusCallFlags.NONE, 1500, None)
                orient_v = proxy.call_sync(
                    "org.freedesktop.DBus.Properties.Get",
                    None,
                    Gio.DBusCallFlags.NONE,
                    1500,
                    None,
                )
            except Exception:
                pass
            finally:
                try:
                    proxy.call_sync("ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 1500, None)
                except Exception:
                    pass
            # Re-read cached or fresh property via Get
            try:
                from gi.repository import GLib

                val = bus.call_sync(
                    "net.hadess.SensorProxy",
                    "/net/hadess/SensorProxy",
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant("(ss)", ("net.hadess.SensorProxy", "AccelerometerOrientation")),
                    GLib.VariantType("(v)"),
                    Gio.DBusCallFlags.NONE,
                    1500,
                    None,
                )
                orient_str = str(val.unpack()[0])
            except Exception:
                pass

        return {
            "available": has_accel_bool,
            "orientation": orient_str,
            "error": "",
        }
    except Exception as exc:
        return {"available": False, "orientation": "undefined", "error": str(exc)}


def map_orientation_to_rotation(orientation: str, offset: int = 0) -> int | None:
    if orientation not in ORIENTATION_TO_ROTATION:
        return None
    return (ORIENTATION_TO_ROTATION[orientation] + offset) % 4


def run_orientation_daemon() -> int:
    """Run blocking D-Bus listener syncing host accelerometer orientation to Droidloom."""
    try:
        from gi.repository import Gio, GLib
    except Exception as exc:
        print(f"droidloom-orientation-sync: PyGObject unavailable: {exc}", file=sys.stderr)
        return 1

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    proxy = Gio.DBusProxy.new_sync(
        bus,
        Gio.DBusProxyFlags.NONE,
        None,
        "net.hadess.SensorProxy",
        "/net/hadess/SensorProxy",
        "net.hadess.SensorProxy",
        None,
    )

    last_applied: int | None = None

    def apply_if_valid(orient_str: str) -> None:
        nonlocal last_applied
        cfg = load_orientation_config()
        if not cfg.follow_host_gravity:
            return
        rot = map_orientation_to_rotation(orient_str, cfg.rotation_offset)
        if rot is None or rot == last_applied:
            return
        ok, target = is_adb_connected()
        if not ok:
            return
        fixed = "enabled" if cfg.force_fixed_to_user_rotation else "default"
        ok_rot, msg = apply_android_rotation("lock", rot, fixed_mode=fixed, selector=target)
        if ok_rot:
            last_applied = rot
        else:
            print(f"droidloom-orientation-sync: rotation failed: {msg}", file=sys.stderr)

    def on_properties_changed(_proxy, changed_properties, _invalidated):
        props = changed_properties.unpack()
        if "AccelerometerOrientation" in props:
            apply_if_valid(str(props["AccelerometerOrientation"]))

    proxy.connect("g-properties-changed", on_properties_changed)
    try:
        proxy.call_sync("ClaimAccelerometer", None, Gio.DBusCallFlags.NONE, 3000, None)
    except Exception as exc:
        print(f"droidloom-orientation-sync: ClaimAccelerometer failed: {exc}", file=sys.stderr)
        return 1

    orient_prop = proxy.get_cached_property("AccelerometerOrientation")
    if orient_prop is not None:
        apply_if_valid(str(orient_prop.unpack()))

    loop = GLib.MainLoop()

    def handle_sig(_signum, _frame):
        loop.quit()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        loop.run()
    finally:
        try:
            proxy.call_sync("ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 2000, None)
        except Exception:
            pass
    return 0
