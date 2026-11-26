"""Unit tests for notification/task parsers, RGBA->PNG encoder, PanelController, and CLI --package."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtGui

from droidloom_settings.backend.android_bridge import (
    parse_dumpsys_notifications,
    parse_running_tasks_and_services,
)
from droidloom_settings.backend.control_client import ControlResponse
from droidloom_settings.backend.panel_controller import (
    PanelController,
    encode_rgba_hex_to_png,
)
from droidloom_settings.main import parse_args


SAMPLE_DUMPSYS_NOTIF = """
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x09b7fcb5: pkg=com.netease.cloudmusic user=UserHandle{0} id=1 tag=null importance=2 key=0|com.netease.cloudmusic|1|null|10107: Notification(channel=netease_music shortcut=null contentView=null vibrate=null sound=null tick defaults=0 flags=NO_CLEAR|FOREGROUND_SERVICE color=0x00000000 category=transport groupKey=com.netease.cloudmusic.musicplay actions=5 vis=PUBLIC))
      flags=NO_CLEAR|FOREGROUND_SERVICE
            when=0/1795658284992
            actions={
                [0] "like" -> PendingIntent{5184ca6: PendingIntentRecord{2448fd com.netease.cloudmusic startForegroundService}}
                [1] "pre" -> PendingIntent{11766e7: PendingIntentRecord{aa612a8 com.netease.cloudmusic startForegroundService}}
                [2] "toggle" -> PendingIntent{5fb6294: PendingIntentRecord{e853acb com.netease.cloudmusic startForegroundService}}
                [3] "next" -> PendingIntent{68a413d: PendingIntentRecord{2ab8a7 com.netease.cloudmusic startForegroundService}}
                [4] "lyric" -> PendingIntent{aec8d32: PendingIntentRecord{85551c1 com.netease.cloudmusic startForegroundService}}
              }
                android.title=String (坚强女人心 (电视剧《武十郎》主题曲))
                android.template=String (android.app.Notification$MediaStyle)
                android.subText=null
                android.text=String (杨千嬅 - 坚强女人心)
    NotificationRecord(0x01e72a33: pkg=com.tencent.mm user=UserHandle{0} id=19 tag=null importance=4 key=0|com.tencent.mm|19|null|10106: Notification(channel=msg flags=AUTO_CANCEL|CAN_COLORIZE category=msg groupKey=null vis=PRIVATE))
      flags=AUTO_CANCEL|CAN_COLORIZE
            when=1795656993687/1795656993687
                android.title=String (张三)
                android.text=String (你好)
                android.bigText=String (你好，晚上一起吃饭吗？)
                android.progress=Integer (50)
                android.progressMax=Integer (100)
                android.progressIndeterminate=Boolean (false)
  Notification Preferences:
    AppSettings: com.tencent.mm
"""

SAMPLE_TASKS_AND_SERVICES = """
===USER_PKGS===
package:com.netease.cloudmusic
package:com.tencent.mm
package:com.twitter.android
package:com.android.droidloom.home
package:com.android.droidloom.ime
===RECENTS===
ACTIVITY MANAGER RECENT TASKS (dumpsys activity recents)
  Recent tasks:
  * Recent #0: Task{35ba3a #17 type=standard A=10107:com.netease.cloudmusic}
    mActivityComponent=com.netease.cloudmusic/.activity.MainActivity
    Activities=[ActivityRecord{59202917 u0 com.netease.cloudmusic/.activity.IconChangeDefaultAlias t17}]
    lastActiveTime=743566017 (inactive for 313s)
  * Recent #1: Task{ab0bd6e #2 type=home A=10102:com.android.droidloom.home nonResizable}
    mActivityComponent=com.android.droidloom.home/.HomeActivity
    lastActiveTime=742726161
  * Recent #2: Task{170ea64 #7 type=undefined A=10110:com.twitter.android}
    mActivityComponent=com.twitter.android/com.x.android.main.MainActivity
    Activities=[]
    lastActiveTime=742726457
  Visible recent tasks (most recent first):
  * RecentTaskInfo #0:
===ACTIVITIES===
  topResumedActivity=ActivityRecord{123 u0 com.netease.cloudmusic/.activity.MainActivity t17}
  Task{35ba3a #17 type=standard A=10107:com.netease.cloudmusic U=0 visible=true}
===SERVICES===
  * ServiceRecord{4c6b793 u0 com.netease.cloudmusic/.service.MainProcessPlayService c:com.netease.cloudmusic}
    packageName=com.netease.cloudmusic
    isForeground=true foregroundId=1
  * ServiceRecord{7c83f04 u0 com.tencent.mm/.ipcinvoker.wx_extension.service.MainProcessIPCService c:com.tencent.mm}
    packageName=com.tencent.mm
  * ServiceRecord{fabb768 u0 com.android.networkstack/com.android.server.NetworkStackService c:android}
    packageName=com.android.networkstack
===PROCS===
  *APP* UID 10107 ProcessRecord{82f4f4 2386:com.netease.cloudmusic/u0a107}
    curProcState=2 mRepProcState=2 setProcState=2
  *APP* UID 10106 ProcessRecord{9267723 1285:com.tencent.mm/u0a106}
    curProcState=10 mRepProcState=10 setProcState=10
  *APP* UID 10078 ProcessRecord{78eb0d8 995:.adservices/u0a78}
    curProcState=19 mRepProcState=19 setProcState=19
===PS===
  2386 150000 com.netease.cloudmusic
  3006  40000 com.netease.cloudmusic:pushservice
  1285 210000 com.tencent.mm
  655  120000 com.android.networkstack.process
  888   40000 android.process.acore
  889   12000 android.hardware.power-service.droidloom
  890   18000 media.swcodec
  891    5000 rpmb_dev.test.system
"""


class _FakeClient:
    def status(self):
        return ControlResponse(ok=True, state="running", message="running")

    def stats(self):
        return ControlResponse(
            ok=True,
            state="running",
            message="ok",
            stats={"ready": True, "android_version": "17", "memory_bytes": 1024**3, "processes": 50, "app_processes": 4},
        )

    def games_driver(self):
        return "angle"

    def list_applications(self):
        return [
            {"name": "网易云音乐", "package": "com.netease.cloudmusic", "component": "com.netease.cloudmusic/.Main"},
            {"name": "微信", "package": "com.tencent.mm", "component": "com.tencent.mm/.ui.LauncherUI"},
        ]

    def application_icons(self, components, size=96):
        png = encode_rgba_hex_to_png("ff0000ff" * 4, 2, 2)
        return {c: png for c in components}

    def launch(self, package, component=None, resolution=None):
        return ControlResponse(ok=True, state="running", message=f"launched {package}")

    def application_action(self, package, action):
        return ControlResponse(ok=True, state="running", message=f"{action} {package}")


class PanelControllerTests(unittest.TestCase):
    def test_cli_parse_package_and_panel_daemon(self) -> None:
        args = parse_args(["--page", "apps", "--package", "com.tencent.mm"])
        self.assertEqual(args.page, "apps")
        self.assertEqual(args.package, "com.tencent.mm")
        self.assertFalse(args.panel_daemon)
        args2 = parse_args(["--panel-daemon"])
        self.assertTrue(args2.panel_daemon)

    def test_encode_rgba_hex_to_png_roundtrip(self) -> None:
        hex_str = ("112233ff" * 4)  # 2x2
        png = encode_rgba_hex_to_png(hex_str, 2, 2)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        img = QtGui.QImage()
        self.assertTrue(img.loadFromData(png, "PNG"))
        self.assertEqual(img.width(), 2)
        self.assertEqual(img.height(), 2)
        self.assertEqual(encode_rgba_hex_to_png("bad", 2, 2), b"")

    def test_parse_dumpsys_notifications(self) -> None:
        notes = parse_dumpsys_notifications(SAMPLE_DUMPSYS_NOTIF)
        self.assertEqual(len(notes), 2)
        # Ongoing comes first
        self.assertEqual(notes[0]["package"], "com.netease.cloudmusic")
        self.assertTrue(notes[0]["ongoing"])
        self.assertFalse(notes[0]["clearable"])
        self.assertIn("坚强女人心", notes[0]["title"])
        self.assertEqual(notes[0]["category"], "transport")
        self.assertTrue(notes[0]["is_media"])
        self.assertEqual(
            [a["label"] for a in notes[0]["actions"]],
            ["打开应用", "like", "pre", "toggle", "next", "lyric"],
        )

        self.assertEqual(notes[1]["package"], "com.tencent.mm")
        self.assertFalse(notes[1]["ongoing"])
        self.assertTrue(notes[1]["clearable"])
        self.assertEqual(notes[1]["body"], "你好，晚上一起吃饭吗？")
        self.assertEqual(notes[1]["progress"], 50)

    def test_parse_running_tasks_and_services(self) -> None:
        res = parse_running_tasks_and_services(SAMPLE_TASKS_AND_SERVICES, include_system=False)
        tasks = res["recent_tasks"]
        # droidloom.home must be excluded
        self.assertEqual([t["package"] for t in tasks], ["com.netease.cloudmusic", "com.twitter.android"])
        self.assertTrue(tasks[0]["visible"])
        self.assertTrue(tasks[0]["foreground_service"])
        self.assertEqual(tasks[0]["rss_kb"], 190000)

        bg = res["background_apps"]
        pkgs = [a["package"] for a in bg]
        self.assertIn("com.netease.cloudmusic", pkgs)
        self.assertIn("com.tencent.mm", pkgs)
        self.assertNotIn("com.android.networkstack", pkgs)
        self.assertNotIn("android.process.acore", pkgs)
        self.assertNotIn("android.hardware.power-service.droidloom", pkgs)
        self.assertNotIn("media.swcodec", pkgs)
        self.assertNotIn(".adservices", pkgs)
        self.assertNotIn("rpmb_dev.test.system", pkgs)
        self.assertEqual(bg[0]["package"], "com.netease.cloudmusic")
        self.assertEqual(bg[0]["proc_state_label"], "前台服务")

    def test_controller_enriches_notifications_and_apps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            ctrl = PanelController(
                client=_FakeClient(),
                cache_dir=t / "cache",
                config_dir=t / "cfg",
                apps_dir=t / "apps",
            )
            apps_res = ctrl.get_apps()
            self.assertTrue(apps_res["connected"])
            self.assertEqual(len(apps_res["apps"]), 2)
            self.assertTrue(apps_res["apps"][0]["icon_path"].startswith("file://"))

            native_snap = {
                "connected": True,
                "desktop_forwarding": True,
                "notifications": [
                    {
                        "key": "0|com.tencent.mm|1",
                        "package": "com.tencent.mm",
                        "app": "微信",
                        "title": "新消息",
                        "body": "测试内容",
                        "ongoing": False,
                        "clearable": True,
                        "when": 1700000000000,
                        "progress": -1,
                        "actions": [{"key": "default", "label": "Open"}],
                        "large_icon": "00ff00ff" * (96 * 96),
                    }
                ],
            }
            notif_res = ctrl.get_notifications(native_snapshot=native_snap)
            self.assertEqual(notif_res["backend"], "dbus")
            self.assertEqual(notif_res["unread_count"], 1)
            self.assertTrue(notif_res["notifications"][0]["large_icon_path"].startswith("file://"))

            with mock.patch(
                "droidloom_settings.backend.panel_controller.query_quick_settings_snapshot",
                return_value={"connected": True, "night_mode": True, "dnd": False, "rotation_mode": "free", "rotation_index": 0},
            ):
                st = ctrl.get_state(native_snapshot=native_snap)
            self.assertTrue(st["running"])
            self.assertTrue(st["night_mode"])
            self.assertEqual(st["notification_backend"], "dbus")

    def test_adb_fallback_media_actions_dismiss_and_quick_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            ctrl = PanelController(
                client=_FakeClient(),
                cache_dir=t / "cache",
                config_dir=t / "cfg",
                apps_dir=t / "apps",
            )
            parsed = parse_dumpsys_notifications(SAMPLE_DUMPSYS_NOTIF)
            with (
                mock.patch(
                    "droidloom_settings.backend.panel_controller.is_adb_connected",
                    return_value=(True, "device:droidloom"),
                ),
                mock.patch(
                    "droidloom_settings.backend.panel_controller.query_adb_notifications",
                    return_value=parsed,
                ),
                mock.patch(
                    "droidloom_settings.backend.panel_controller.dispatch_media_key",
                    return_value=(True, "ok"),
                ) as mock_media,
                mock.patch(
                    "droidloom_settings.backend.panel_controller.snooze_adb_notification",
                    return_value=(True, "ok"),
                ),
            ):
                res = ctrl.get_notifications()
                self.assertEqual(res["unread_count"], 2)

                # Trigger MediaStyle toggle -> play-pause
                ok_act, _ = ctrl.invoke_notification_action(
                    "0|com.netease.cloudmusic|1|null|10107", "action-2"
                )
                self.assertTrue(ok_act)
                mock_media.assert_called_once_with("play-pause", selector="device:droidloom")

                # Ongoing cannot be dismissed
                ok_ong, _ = ctrl.dismiss_notification("0|com.netease.cloudmusic|1|null|10107")
                self.assertFalse(ok_ong)

                # Clearable notification can be dismissed and disappears immediately
                ok_clr, _ = ctrl.dismiss_notification("0|com.tencent.mm|19|null|10106")
                self.assertTrue(ok_clr)
                res2 = ctrl.get_notifications()
                self.assertEqual(res2["unread_count"], 1)

            with (
                mock.patch(
                    "droidloom_settings.backend.panel_controller.is_adb_connected",
                    return_value=(True, "device:droidloom"),
                ),
                mock.patch(
                    "droidloom_settings.backend.panel_controller.set_android_airplane_mode",
                    return_value=(True, "ok"),
                ) as mock_air,
                mock.patch(
                    "droidloom_settings.backend.panel_controller.set_android_stay_awake",
                    return_value=(True, "ok"),
                ) as mock_stay,
            ):
                ok_a, _ = ctrl.set_quick_toggle("airplane_mode", "true")
                self.assertTrue(ok_a)
                mock_air.assert_called_once_with(True, selector="device:droidloom")
                ok_s, _ = ctrl.set_quick_toggle("stay_awake", "true")
                self.assertTrue(ok_s)
                mock_stay.assert_called_once_with(True, selector="device:droidloom")


if __name__ == "__main__":
    unittest.main()
