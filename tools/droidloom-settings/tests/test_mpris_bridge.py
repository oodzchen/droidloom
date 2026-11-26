"""Unit and D-Bus integration tests for Android -> Plasma MPRIS 2 media bridge."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from droidloom_settings.backend.mpris_bridge import (
    MPRIS_IFACE_PLAYER,
    MPRIS_IFACE_ROOT,
    MPRIS_OBJECT_PATH,
    MediaArtCache,
    MprisPlayerInstance,
    parse_dumpsys_media_session,
    sanitize_bus_segment,
)

SAMPLE_DUMPSYS_MEDIA = """
MEDIA SESSION SERVICE (dumpsys media_session)

User Records:
Record for full_user=0
  Sessions Stack - have 2 sessions:
    MediaSession com.netease.cloudmusic/MediaSession/2 (userId=0)
      ownerPid=1676, ownerUid=10107, userId=0
      package=com.netease.cloudmusic
      active=true
      state=PlaybackState {state=PAUSED(2), position=17660, buffered position=0, speed=1.0, updated=748873308, actions=822, custom actions=[]}
      metadata: size=11, description=真夜中のドア, 松原みき, Light Mellow
    MediaSession com.android.server.telecom/HeadsetMediaButton/1 (userId=0)
      package=com.android.server.telecom
      state=null
      metadata: null
Audio playback (lastly played comes first)
"""

# 1x1 red PNG
_SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


class MprisBridgeTests(unittest.TestCase):
    def test_sanitize_bus_segment_and_dumpsys_parser(self) -> None:
        self.assertEqual(sanitize_bus_segment("com.netease.cloudmusic"), "com_netease_cloudmusic")
        self.assertEqual(sanitize_bus_segment("123.app-test"), "app_123_app_test")

        sessions = parse_dumpsys_media_session(SAMPLE_DUMPSYS_MEDIA)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["package"], "com.netease.cloudmusic")
        self.assertEqual(s["status"], "Paused")
        self.assertEqual(s["position_ms"], 17660)
        self.assertEqual(s["title"], "真夜中のドア")
        self.assertEqual(s["artist"], "松原みき")
        self.assertEqual(s["album"], "Light Mellow")
        self.assertTrue(s["can_play"])
        self.assertTrue(s["can_next"])
        self.assertTrue(s["can_seek"])

    def test_media_art_cache_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = MediaArtCache(cache_dir=Path(tmp))
            b64 = base64.b64encode(_SAMPLE_PNG).decode("ascii")
            uri1 = cache.store_base64("hash_abc", b64)
            self.assertTrue(uri1.startswith("file://"))
            p1 = Path(uri1.removeprefix("file://"))
            self.assertTrue(p1.is_file())
            mtime1 = p1.stat().st_mtime_ns
            uri2 = cache.store_base64("hash_abc", b64)
            self.assertEqual(uri1, uri2)
            self.assertEqual(p1.stat().st_mtime_ns, mtime1)

    def test_mpris_player_dbus_properties_methods_and_signals(self) -> None:
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

            with tempfile.TemporaryDirectory() as tmp:
                cache = MediaArtCache(cache_dir=Path(tmp))
                cmds: list[dict[str, object]] = []
                raised: list[str] = []
                quit_pkgs: list[str] = []
                bus_name = f"org.mpris.MediaPlayer2.droidloom_test_{os.getpid()}"

                player = MprisPlayerInstance(
                    package="com.netease.cloudmusic",
                    identity="NetEase Music",
                    desktop_entry="droidloom-test",
                    on_command=lambda c: cmds.append(c),
                    on_raise=lambda p: raised.append(p),
                    on_quit=lambda p: quit_pkgs.append(p),
                    art_cache=cache,
                    connection=server_conn,
                    bus_name_override=bus_name,
                )

                seeked_positions: list[int] = []
                prop_changes: list[tuple[str, dict[str, object]]] = []
                sub_seek = client_conn.signal_subscribe(
                    bus_name,
                    MPRIS_IFACE_PLAYER,
                    "Seeked",
                    MPRIS_OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    lambda _c, _s, _p, _i, _sig, params: seeked_positions.append(
                        int(params.unpack()[0])
                    ),
                )
                sub_props = client_conn.signal_subscribe(
                    bus_name,
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    MPRIS_OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    lambda _c, _s, _p, _i, _sig, params: prop_changes.append(
                        (str(params.unpack()[0]), dict(params.unpack()[1]))
                    ),
                )

                try:
                    player.update_from_session(
                        {
                            "package": "com.netease.cloudmusic",
                            "status": "Playing",
                            "position_ms": 10000,
                            "speed": 1.0,
                            "can_play": True,
                            "can_pause": True,
                            "can_stop": False,
                            "can_next": True,
                            "can_previous": True,
                            "can_seek": True,
                            "can_set_speed": True,
                            "title": "Song A",
                            "artist": "Artist A",
                            "album": "Album A",
                            "duration_ms": 120000,
                            "art_hash": "art1",
                            "art_png_b64": base64.b64encode(_SAMPLE_PNG).decode("ascii"),
                            "volume": 0.6,
                            "compat_mode_supported": True,
                            "repeat_mode": 2,
                            "shuffle_mode": 1,
                        }
                    )

                    loop = GLib.MainLoop()
                    results: dict[str, object] = {}

                    def run_client() -> None:
                        try:
                            props_proxy = Gio.DBusProxy.new_sync(
                                client_conn,
                                Gio.DBusProxyFlags.NONE,
                                None,
                                bus_name,
                                MPRIS_OBJECT_PATH,
                                "org.freedesktop.DBus.Properties",
                                None,
                            )
                            root_all = props_proxy.call_sync(
                                "GetAll",
                                GLib.Variant("(s)", (MPRIS_IFACE_ROOT,)),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            ).unpack()[0]
                            results["root"] = root_all

                            player_all = props_proxy.call_sync(
                                "GetAll",
                                GLib.Variant("(s)", (MPRIS_IFACE_PLAYER,)),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            ).unpack()[0]
                            results["player"] = player_all

                            time.sleep(0.15)
                            pos2 = props_proxy.call_sync(
                                "Get",
                                GLib.Variant("(ss)", (MPRIS_IFACE_PLAYER, "Position")),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            ).unpack()[0]
                            results["pos2"] = pos2

                            # Set LoopStatus, Shuffle, Rate, Volume
                            for pname, pval in (
                                ("LoopStatus", GLib.Variant("s", "Track")),
                                ("Shuffle", GLib.Variant("b", False)),
                                ("Rate", GLib.Variant("d", 1.5)),
                                ("Volume", GLib.Variant("d", 0.8)),
                            ):
                                props_proxy.call_sync(
                                    "Set",
                                    GLib.Variant("(ssv)", (MPRIS_IFACE_PLAYER, pname, pval)),
                                    Gio.DBusCallFlags.NONE,
                                    3000,
                                    None,
                                )

                            root_proxy = Gio.DBusProxy.new_sync(
                                client_conn,
                                Gio.DBusProxyFlags.NONE,
                                None,
                                bus_name,
                                MPRIS_OBJECT_PATH,
                                MPRIS_IFACE_ROOT,
                                None,
                            )
                            root_proxy.call_sync("Raise", None, Gio.DBusCallFlags.NONE, 3000, None)
                            root_proxy.call_sync("Quit", None, Gio.DBusCallFlags.NONE, 3000, None)

                            player_proxy = Gio.DBusProxy.new_sync(
                                client_conn,
                                Gio.DBusProxyFlags.NONE,
                                None,
                                bus_name,
                                MPRIS_OBJECT_PATH,
                                MPRIS_IFACE_PLAYER,
                                None,
                            )
                            for m in ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous"):
                                player_proxy.call_sync(
                                    m, None, Gio.DBusCallFlags.NONE, 3000, None
                                )
                            player_proxy.call_sync(
                                "Seek",
                                GLib.Variant("(x)", (5_000_000,)),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            )
                            track_id = player_all["Metadata"]["mpris:trackid"]
                            player_proxy.call_sync(
                                "SetPosition",
                                GLib.Variant("(ox)", (track_id, 42_000_000)),
                                Gio.DBusCallFlags.NONE,
                                3000,
                                None,
                            )
                            time.sleep(0.05)
                        finally:
                            GLib.idle_add(loop.quit)

                    threading.Thread(target=run_client, daemon=True).start()
                    GLib.timeout_add(6000, loop.quit)
                    loop.run()

                    root_all = results["root"]
                    self.assertEqual(root_all["Identity"], "NetEase Music")
                    self.assertEqual(root_all["DesktopEntry"], "droidloom-test")
                    self.assertTrue(root_all["CanRaise"])
                    self.assertTrue(root_all["CanQuit"])

                    player_all = results["player"]
                    self.assertEqual(player_all["PlaybackStatus"], "Playing")
                    self.assertEqual(player_all["LoopStatus"], "Playlist")
                    self.assertTrue(player_all["Shuffle"])
                    self.assertAlmostEqual(player_all["Volume"], 0.6, places=2)
                    self.assertTrue(player_all["CanSeek"])
                    self.assertTrue(player_all["CanGoNext"])
                    self.assertTrue(player_all["CanGoPrevious"])

                    md = player_all["Metadata"]
                    self.assertEqual(md["xesam:title"], "Song A")
                    self.assertEqual(md["xesam:artist"], ["Artist A"])
                    self.assertEqual(md["xesam:album"], "Album A")
                    self.assertEqual(md["mpris:length"], 120_000_000)
                    self.assertTrue(str(md["mpris:artUrl"]).startswith("file://"))

                    self.assertGreater(int(results["pos2"]), int(player_all["Position"]))

                    cmd_ops = [c["cmd"] for c in cmds]
                    self.assertIn("set_loop_status", cmd_ops)
                    self.assertIn("set_shuffle", cmd_ops)
                    self.assertIn("set_rate", cmd_ops)
                    self.assertIn("set_volume", cmd_ops)
                    for expected_op in ("play", "pause", "play_pause", "stop", "next", "previous"):
                        self.assertIn(expected_op, cmd_ops)
                    seek_cmds = [c for c in cmds if c["cmd"] == "seek_to"]
                    self.assertEqual(seek_cmds[-1]["position_ms"], 42000)

                    self.assertEqual(raised, ["com.netease.cloudmusic"])
                    self.assertEqual(quit_pkgs, ["com.netease.cloudmusic"])
                    self.assertIn(42_000_000, seeked_positions)
                    self.assertGreaterEqual(len(prop_changes), 1)
                finally:
                    client_conn.signal_unsubscribe(sub_seek)
                    client_conn.signal_unsubscribe(sub_props)
                    player.close()
                    server_conn.close_sync(None)
                    client_conn.close_sync(None)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
