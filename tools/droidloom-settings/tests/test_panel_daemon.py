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

from droidloom_settings.backend.control_client import ControlResponse
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

                svc = ControlCenterService(server_conn, controller=ctrl)
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


if __name__ == "__main__":
    unittest.main()
