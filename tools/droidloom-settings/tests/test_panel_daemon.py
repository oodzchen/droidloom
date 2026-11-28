"""Integration test for org.droidloom.ControlCenter Gio.DBus service on a private session bus."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from droidloom_settings.backend.control_client import ControlError, ControlResponse
from droidloom_settings.backend.panel_controller import PanelController
from droidloom_settings.backend.panel_daemon import (
    BUS_NAME,
    INTERFACE_NAME,
    OBJECT_PATH,
    ControlCenterService,
)


class _DummyClient:
    def status(self):
        return ControlResponse(ok=True, state="stopped", message="stopped")

    def launch(self, package, component=None, resolution=None):
        return ControlResponse(ok=True, state="stopped", message=f"launch:{package}")

    def application_action(self, package, action):
        return ControlResponse(ok=True, state="stopped", message=f"{action}:{package}")


class PanelDaemonTests(unittest.TestCase):
    def test_dbus_service_methods_and_signals(self) -> None:
        from gi.repository import Gio, GLib

        proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            addr = proc.stdout.readline().strip()
            self.assertTrue(addr)

            with tempfile.TemporaryDirectory() as tmp:
                t = Path(tmp)
                ctrl = PanelController(
                    client=_DummyClient(),
                    cache_dir=t / "cache",
                    config_dir=t / "cfg",
                    apps_dir=t / "apps",
                )
                server_conn = Gio.DBusConnection.new_for_address_sync(
                    addr,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                client_conn = Gio.DBusConnection.new_for_address_sync(
                    addr,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                # Request bus name on server_conn
                server_conn.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "RequestName",
                    GLib.Variant("(su)", (BUS_NAME, 0)),
                    GLib.VariantType("(u)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )

                svc = ControlCenterService(server_conn, controller=ctrl, enable_mpris=False)
                try:
                    loop = GLib.MainLoop()
                    results: dict[str, object] = {}

                    def run_calls() -> None:
                        try:
                            proxy = Gio.DBusProxy.new_sync(
                                client_conn,
                                Gio.DBusProxyFlags.NONE,
                                None,
                                BUS_NAME,
                                OBJECT_PATH,
                                INTERFACE_NAME,
                                None,
                            )
                            raw_state = proxy.call_sync(
                                "GetState", None, Gio.DBusCallFlags.NONE, 3000, None
                            ).unpack()[0]
                            results["state"] = json.loads(raw_state)

                            raw_notifs = proxy.call_sync(
                                "GetNotifications", None, Gio.DBusCallFlags.NONE, 3000, None
                            ).unpack()[0]
                            results["notifs"] = json.loads(raw_notifs)

                            ok, msg = proxy.call_sync(
                                "SetQuickToggle",
                                GLib.Variant("(ss)", ("desktop_forwarding", "false")),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            ).unpack()
                            results["toggle"] = (ok, msg)
                        finally:
                            GLib.idle_add(loop.quit)

                    import threading

                    threading.Thread(target=run_calls, daemon=True).start()
                    GLib.timeout_add(5000, loop.quit)
                    loop.run()

                    self.assertIn("state", results)
                    self.assertFalse(results["state"]["running"])  # type: ignore[index]
                    self.assertEqual(results["notifs"]["backend"], "unavailable")  # type: ignore[index]
                    self.assertTrue(results["toggle"][0])  # type: ignore[index]
                finally:
                    svc.close()
                    server_conn.close_sync(None)
                    client_conn.close_sync(None)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_refresh_all_and_launch_recovery_emit_apps_changed(self) -> None:
        from gi.repository import Gio, GLib

        class _RefreshClient:
            def __init__(self) -> None:
                self.comp = "com.netease.cloudmusic/.activity.IconChangeDefaultAlias"
                self.list_calls = 0

            def status(self):
                return ControlResponse(ok=True, state="running", message="running")

            def stats(self):
                return ControlResponse(
                    ok=True,
                    state="running",
                    message="ok",
                    stats={
                        "ready": True,
                        "android_version": "17",
                        "memory_bytes": 1024**3,
                        "processes": 10,
                        "app_processes": 1,
                    },
                )

            def games_driver(self):
                return "angle"

            def list_applications(self):
                self.list_calls += 1
                return [
                    {
                        "name": "NetEase Music",
                        "package": "com.netease.cloudmusic",
                        "component": self.comp,
                    }
                ]

            def application_icons(self, components, size=96):
                return {}

            def launch(self, package, component=None, resolution=None):
                if component == "com.netease.cloudmusic/.activity.IconChangeDefaultAlias":
                    raise ControlError("Activity start failed: -92")
                return ControlResponse(ok=True, state="running", message=f"launch:{package}")

        proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            addr = proc.stdout.readline().strip()
            self.assertTrue(addr)

            with tempfile.TemporaryDirectory() as tmp:
                t = Path(tmp)
                client = _RefreshClient()
                ctrl = PanelController(
                    client=client,
                    cache_dir=t / "cache",
                    config_dir=t / "cfg",
                    apps_dir=t / "apps",
                    state_dir=t / "state",
                )
                server_conn = Gio.DBusConnection.new_for_address_sync(
                    addr,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                client_conn = Gio.DBusConnection.new_for_address_sync(
                    addr,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                server_conn.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "RequestName",
                    GLib.Variant("(su)", (BUS_NAME, 0)),
                    GLib.VariantType("(u)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )

                svc = ControlCenterService(server_conn, controller=ctrl, enable_mpris=False)
                apps_signals: list[dict[str, object]] = []
                import threading

                sig_event = threading.Event()
                loop = GLib.MainLoop()

                def on_apps_changed(_c, _s, _p, _i, _sig, params) -> None:
                    apps_signals.append(json.loads(params.unpack()[0]))
                    sig_event.set()

                sub_id = client_conn.signal_subscribe(
                    BUS_NAME,
                    INTERFACE_NAME,
                    "AppsChanged",
                    OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    on_apps_changed,
                )
                try:
                    results: dict[str, object] = {}

                    def run_calls() -> None:
                        try:
                            proxy = Gio.DBusProxy.new_sync(
                                client_conn,
                                Gio.DBusProxyFlags.NONE,
                                None,
                                BUS_NAME,
                                OBJECT_PATH,
                                INTERFACE_NAME,
                                None,
                            )
                            # Populate cache via GetApps(False)
                            proxy.call_sync(
                                "GetApps",
                                GLib.Variant("(b)", (False,)),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            )
                            # Update component without touching manifest, then call RefreshAll
                            client.comp = "com.netease.cloudmusic/.activity.LoadingActivity"
                            sig_event.clear()
                            proxy.call_sync(
                                "RefreshAll",
                                None,
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            )
                            sig_event.wait(timeout=3.0)
                            # Change component again and launch with stale component
                            client.comp = "com.netease.cloudmusic/.activity.MainActivity"
                            sig_event.clear()
                            ok, msg = proxy.call_sync(
                                "LaunchApp",
                                GLib.Variant(
                                    "(ss)",
                                    (
                                        "com.netease.cloudmusic",
                                        "com.netease.cloudmusic/.activity.IconChangeDefaultAlias",
                                    ),
                                ),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            ).unpack()
                            results["launch"] = (ok, msg)
                            sig_event.wait(timeout=3.0)
                        finally:
                            GLib.idle_add(loop.quit)

                    threading.Thread(target=run_calls, daemon=True).start()
                    GLib.timeout_add(5000, loop.quit)
                    loop.run()

                    self.assertEqual(client.list_calls, 3)
                    self.assertEqual(results["launch"][0], True)  # type: ignore[index]
                    self.assertGreaterEqual(len(apps_signals), 2)
                    self.assertEqual(
                        apps_signals[0]["apps"][0]["component"],  # type: ignore[index]
                        "com.netease.cloudmusic/.activity.LoadingActivity",
                    )
                    self.assertEqual(
                        apps_signals[-1]["apps"][0]["component"],  # type: ignore[index]
                        "com.netease.cloudmusic/.activity.MainActivity",
                    )
                finally:
                    client_conn.signal_unsubscribe(sub_id)
                    svc.close()
                    server_conn.close_sync(None)
                    client_conn.close_sync(None)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
