"""Session D-Bus daemon (org.droidloom.ControlCenter) bridging Plasma 6 Plasmoid & droidloom-settings."""

from __future__ import annotations

import json
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .mpris_bridge import AndroidMprisBridgeManager
from .panel_controller import PanelController
from .user_configs import load_panel_config

BUS_NAME = "org.droidloom.ControlCenter"
OBJECT_PATH = "/org/droidloom/ControlCenter"
INTERFACE_NAME = "org.droidloom.ControlCenter"

NOTIF_BUS_NAME = "org.droidloom.Notifications"
NOTIF_OBJECT_PATH = "/org/droidloom/Notifications"
NOTIF_INTERFACE_NAME = "org.droidloom.Notifications"

INTROSPECTION_XML = f"""
<node>
  <interface name="{INTERFACE_NAME}">
    <method name="GetState">
      <arg type="s" name="state_json" direction="out"/>
    </method>
    <method name="GetNotifications">
      <arg type="s" name="notifications_json" direction="out"/>
    </method>
    <method name="GetTasks">
      <arg type="b" name="include_system" direction="in"/>
      <arg type="s" name="tasks_json" direction="out"/>
    </method>
    <method name="GetApps">
      <arg type="b" name="force_refresh" direction="in"/>
      <arg type="s" name="apps_json" direction="out"/>
    </method>
    <method name="RefreshAll">
      <arg type="b" name="started" direction="out"/>
    </method>
    <method name="LaunchApp">
      <arg type="s" name="package" direction="in"/>
      <arg type="s" name="component" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="StopApp">
      <arg type="s" name="package" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="OpenAndroidAppInfo">
      <arg type="s" name="package" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="ClearBackgroundTasks">
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="InvokeNotificationAction">
      <arg type="s" name="key" direction="in"/>
      <arg type="s" name="action" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="DismissNotification">
      <arg type="s" name="key" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="DismissAllNotifications">
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="SetQuickToggle">
      <arg type="s" name="toggle" direction="in"/>
      <arg type="s" name="value" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="OpenSettings">
      <arg type="s" name="page" direction="in"/>
      <arg type="s" name="package" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="OpenApkInstaller">
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <signal name="StateChanged">
      <arg type="s" name="state_json"/>
    </signal>
    <signal name="NotificationsChanged">
      <arg type="s" name="notifications_json"/>
      <arg type="u" name="unread_count"/>
      <arg type="u" name="ongoing_count"/>
    </signal>
    <signal name="TasksChanged">
      <arg type="s" name="tasks_json"/>
    </signal>
    <signal name="AppsChanged">
      <arg type="s" name="apps_json"/>
    </signal>
    <signal name="ActionFinished">
      <arg type="s" name="action_id"/>
      <arg type="b" name="ok"/>
      <arg type="s" name="message"/>
    </signal>
  </interface>
</node>
"""


class NotificationDBusBridge:
    """Watches org.droidloom.Notifications on the session bus and forwards events."""

    def __init__(
        self,
        connection: Any,
        on_snapshot_changed: Callable[[], None],
    ) -> None:
        from gi.repository import Gio

        self._conn = connection
        self._on_snapshot_changed = on_snapshot_changed
        self._proxy: Any | None = None
        self._owner: str | None = None
        self._sub_id: int | None = None
        self._watch_id = Gio.bus_watch_name_on_connection(
            connection,
            NOTIF_BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_appeared,
            self._on_vanished,
        )

    def close(self) -> None:
        from gi.repository import Gio

        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._sub_id and self._conn is not None:
            self._conn.signal_unsubscribe(self._sub_id)
            self._sub_id = None
        self._proxy = None
        self._owner = None

    def _on_appeared(self, connection: Any, _name: str, name_owner: str) -> None:
        from gi.repository import Gio, GLib

        self._owner = name_owner
        try:
            self._proxy = Gio.DBusProxy.new_sync(
                connection,
                Gio.DBusProxyFlags.NONE,
                None,
                NOTIF_BUS_NAME,
                NOTIF_OBJECT_PATH,
                NOTIF_INTERFACE_NAME,
                None,
            )
            if self._sub_id is None:
                self._sub_id = connection.signal_subscribe(
                    NOTIF_BUS_NAME,
                    NOTIF_INTERFACE_NAME,
                    "SnapshotChanged",
                    NOTIF_OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    lambda *_args: self._on_snapshot_changed(),
                )
            cfg = load_panel_config()
            if not cfg.desktop_forwarding and self._proxy is not None:
                try:
                    self._proxy.call_sync(
                        "SetDesktopForwarding",
                        GLib.Variant("(b)", (False,)),
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                except Exception:
                    pass
        except Exception:
            self._proxy = None
        self._on_snapshot_changed()

    def _on_vanished(self, _connection: Any, _name: str) -> None:
        self._owner = None
        self._proxy = None
        self._on_snapshot_changed()

    def is_connected(self) -> bool:
        return self._proxy is not None and bool(self._owner)

    def get_snapshot(self) -> dict[str, Any] | None:
        from gi.repository import Gio

        if self._proxy is None:
            return None
        try:
            res = self._proxy.call_sync(
                "GetSnapshot", None, Gio.DBusCallFlags.NONE, 2000, None
            )
            raw = res.unpack()[0]
            return json.loads(raw)
        except Exception:
            return None

    def invoke_action(self, key: str, action: str) -> bool:
        from gi.repository import Gio, GLib

        if self._proxy is None:
            return False
        try:
            res = self._proxy.call_sync(
                "InvokeAction",
                GLib.Variant("(ss)", (key, action)),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return bool(res.unpack()[0])
        except Exception:
            return False

    def dismiss(self, key: str) -> bool:
        from gi.repository import Gio, GLib

        if self._proxy is None:
            return False
        try:
            res = self._proxy.call_sync(
                "Dismiss",
                GLib.Variant("(s)", (key,)),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return bool(res.unpack()[0])
        except Exception:
            return False

    def dismiss_all(self) -> int:
        from gi.repository import Gio

        if self._proxy is None:
            return 0
        try:
            res = self._proxy.call_sync(
                "DismissAll", None, Gio.DBusCallFlags.NONE, 3000, None
            )
            return int(res.unpack()[0])
        except Exception:
            return 0

    def set_desktop_forwarding(self, enabled: bool) -> bool:
        from gi.repository import Gio, GLib

        if self._proxy is None:
            return enabled
        try:
            res = self._proxy.call_sync(
                "SetDesktopForwarding",
                GLib.Variant("(b)", (bool(enabled),)),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return bool(res.unpack()[0])
        except Exception:
            return enabled


class ControlCenterService:
    """Registers org.droidloom.ControlCenter on a Gio.DBusConnection."""

    def __init__(
        self,
        connection: Any,
        controller: PanelController | None = None,
        pool: ThreadPoolExecutor | None = None,
        mpris_manager: Any | None = None,
        enable_mpris: bool = True,
    ) -> None:
        from gi.repository import Gio, GLib

        self.connection = connection
        self._pool = pool or ThreadPoolExecutor(max_workers=4, thread_name_prefix="dl-panel")
        self._owns_pool = pool is None
        self.controller = controller or PanelController()
        if self.controller.notif_bridge is None:
            self.controller.notif_bridge = NotificationDBusBridge(
                connection, self._on_notifications_pushed
            )
        if mpris_manager is not None:
            self._mpris_manager: Any | None = mpris_manager
        elif enable_mpris:
            self._mpris_manager = AndroidMprisBridgeManager(controller=self.controller)
            self._mpris_manager.start()
        else:
            self._mpris_manager = None
        self._node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._reg_id = connection.register_object(
            OBJECT_PATH,
            self._node_info.interfaces[0],
            self._handle_method_call,
            None,
            None,
        )
        self._GLib = GLib

    def close(self) -> None:
        if self._mpris_manager is not None:
            self._mpris_manager.close()
        if self._reg_id:
            self.connection.unregister_object(self._reg_id)
            self._reg_id = 0
        if isinstance(self.controller.notif_bridge, NotificationDBusBridge):
            self.controller.notif_bridge.close()
        if self._owns_pool:
            self._pool.shutdown(wait=False)

    def _emit(self, signal_name: str, variant: Any) -> None:
        if not self._reg_id:
            return
        try:
            self.connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME, signal_name, variant
            )
        except Exception:
            pass

    def _on_notifications_pushed(self) -> None:
        def work() -> tuple[dict[str, Any], dict[str, Any]]:
            notif = self.controller.get_notifications()
            state = self.controller.get_state()
            return notif, state

        def done(res: tuple[dict[str, Any], dict[str, Any]]) -> None:
            notif, state = res
            self._emit(
                "NotificationsChanged",
                self._GLib.Variant(
                    "(suu)",
                    (
                        json.dumps(notif, ensure_ascii=False),
                        int(notif.get("unread_count", 0)),
                        int(notif.get("ongoing_count", 0)),
                    ),
                ),
            )
            self._emit(
                "StateChanged",
                self._GLib.Variant("(s)", (json.dumps(state, ensure_ascii=False),)),
            )

        self._submit(work, done)

    def _submit(self, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        fut = self._pool.submit(fn)

        def _cb(f: Any) -> None:
            try:
                result = f.result()
            except Exception as exc:
                result = exc

            def _idle() -> bool:
                if isinstance(result, Exception):
                    return False
                on_done(result)
                return False

            self._GLib.idle_add(_idle)

        fut.add_done_callback(_cb)

    def _reply_json(self, invocation: Any, fn: Callable[[], dict[str, Any]]) -> None:
        fut = self._pool.submit(fn)

        def _cb(f: Any) -> None:
            try:
                payload = json.dumps(f.result(), ensure_ascii=False)
                err = None
            except Exception as exc:
                payload = ""
                err = str(exc)

            def _idle() -> bool:
                if err is not None:
                    invocation.return_dbus_error(f"{INTERFACE_NAME}.Error", err)
                else:
                    invocation.return_value(self._GLib.Variant("(s)", (payload,)))
                return False

            self._GLib.idle_add(_idle)

        fut.add_done_callback(_cb)

    def _reply_action(
        self,
        invocation: Any,
        action_id: str,
        fn: Callable[[], tuple[bool, str]],
        refresh_notifications: bool = False,
        refresh_tasks: bool = False,
        refresh_state: bool = False,
        emit_apps_if_changed: bool = False,
    ) -> None:
        def wrapped() -> dict[str, Any]:
            rev_before = self.controller.apps_revision
            ok, msg = fn()
            extra: dict[str, Any] = {"ok": bool(ok), "message": str(msg)}
            if refresh_notifications:
                extra["notif"] = self.controller.get_notifications()
            if refresh_tasks:
                extra["tasks"] = self.controller.get_tasks()
            if refresh_state:
                extra["state"] = self.controller.get_state()
            if emit_apps_if_changed and self.controller.apps_revision != rev_before:
                extra["apps"] = {"connected": True, "apps": self.controller.cached_apps()}
            return extra

        fut = self._pool.submit(wrapped)

        def _cb(f: Any) -> None:
            try:
                res = f.result()
                err = None
            except Exception as exc:
                res = {"ok": False, "message": str(exc)}
                err = str(exc)

            def _idle() -> bool:
                if err is not None:
                    invocation.return_value(self._GLib.Variant("(bs)", (False, err)))
                else:
                    invocation.return_value(
                        self._GLib.Variant("(bs)", (res["ok"], res["message"]))
                    )
                self._emit(
                    "ActionFinished",
                    self._GLib.Variant("(sbs)", (action_id, res["ok"], res["message"])),
                )
                if "notif" in res:
                    n = res["notif"]
                    self._emit(
                        "NotificationsChanged",
                        self._GLib.Variant(
                            "(suu)",
                            (
                                json.dumps(n, ensure_ascii=False),
                                int(n.get("unread_count", 0)),
                                int(n.get("ongoing_count", 0)),
                            ),
                        ),
                    )
                if "tasks" in res:
                    self._emit(
                        "TasksChanged",
                        self._GLib.Variant(
                            "(s)", (json.dumps(res["tasks"], ensure_ascii=False),)
                        ),
                    )
                if "state" in res:
                    self._emit(
                        "StateChanged",
                        self._GLib.Variant(
                            "(s)", (json.dumps(res["state"], ensure_ascii=False),)
                        ),
                    )
                if "apps" in res:
                    self._emit(
                        "AppsChanged",
                        self._GLib.Variant(
                            "(s)", (json.dumps(res["apps"], ensure_ascii=False),)
                        ),
                    )
                return False

            self._GLib.idle_add(_idle)

        fut.add_done_callback(_cb)

    def _handle_method_call(
        self,
        _connection: Any,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        method_name: str,
        parameters: Any,
        invocation: Any,
    ) -> None:
        args = parameters.unpack() if parameters is not None else ()
        if method_name == "GetState":
            self._reply_json(invocation, self.controller.get_state)
        elif method_name == "GetNotifications":
            self._reply_json(invocation, self.controller.get_notifications)
        elif method_name == "GetTasks":
            include_sys = bool(args[0]) if args else False
            self._reply_json(invocation, lambda: self.controller.get_tasks(include_sys))
        elif method_name == "GetApps":
            force = bool(args[0]) if args else False
            self._reply_json(invocation, lambda: self.controller.get_apps(force))
        elif method_name == "RefreshAll":
            def work() -> dict[str, Any]:
                apps = self.controller.get_apps(True)
                notif = self.controller.get_notifications()
                tasks = self.controller.get_tasks(False)
                state = self.controller.get_state()
                return {"apps": apps, "notif": notif, "tasks": tasks, "state": state}

            def done(res: dict[str, Any]) -> None:
                n = res["notif"]
                self._emit(
                    "StateChanged",
                    self._GLib.Variant("(s)", (json.dumps(res["state"], ensure_ascii=False),)),
                )
                self._emit(
                    "NotificationsChanged",
                    self._GLib.Variant(
                        "(suu)",
                        (
                            json.dumps(n, ensure_ascii=False),
                            int(n.get("unread_count", 0)),
                            int(n.get("ongoing_count", 0)),
                        ),
                    ),
                )
                self._emit(
                    "TasksChanged",
                    self._GLib.Variant("(s)", (json.dumps(res["tasks"], ensure_ascii=False),)),
                )
                self._emit(
                    "AppsChanged",
                    self._GLib.Variant("(s)", (json.dumps(res["apps"], ensure_ascii=False),)),
                )

            self._submit(work, done)
            invocation.return_value(self._GLib.Variant("(b)", (True,)))
        elif method_name == "LaunchApp":
            pkg, comp = str(args[0]), str(args[1])
            self._reply_action(
                invocation,
                f"launch:{pkg}",
                lambda: self.controller.launch_app(pkg, comp),
                emit_apps_if_changed=True,
            )
        elif method_name == "StopApp":
            pkg = str(args[0])
            self._reply_action(
                invocation,
                f"stop:{pkg}",
                lambda: self.controller.stop_app(pkg),
                refresh_tasks=True,
            )
        elif method_name == "OpenAndroidAppInfo":
            pkg = str(args[0])
            self._reply_action(
                invocation, f"info:{pkg}", lambda: self.controller.open_android_app_info(pkg)
            )
        elif method_name == "ClearBackgroundTasks":
            self._reply_action(
                invocation,
                "clear_tasks",
                self.controller.clear_background_tasks,
                refresh_tasks=True,
                refresh_state=True,
            )
        elif method_name == "InvokeNotificationAction":
            key, action = str(args[0]), str(args[1])
            self._reply_action(
                invocation,
                f"notif_action:{key}",
                lambda: self.controller.invoke_notification_action(key, action),
                refresh_notifications=True,
            )
        elif method_name == "DismissNotification":
            key = str(args[0])
            self._reply_action(
                invocation,
                f"notif_dismiss:{key}",
                lambda: self.controller.dismiss_notification(key),
                refresh_notifications=True,
                refresh_state=True,
            )
        elif method_name == "DismissAllNotifications":
            self._reply_action(
                invocation,
                "notif_dismiss_all",
                self.controller.dismiss_all_notifications,
                refresh_notifications=True,
                refresh_state=True,
            )
        elif method_name == "SetQuickToggle":
            toggle, value = str(args[0]), str(args[1])
            self._reply_action(
                invocation,
                f"toggle:{toggle}",
                lambda: self.controller.set_quick_toggle(toggle, value),
                refresh_notifications=(toggle == "desktop_forwarding"),
                refresh_state=True,
            )
        elif method_name == "OpenSettings":
            page, pkg = str(args[0]), str(args[1])
            self._reply_action(
                invocation, f"open_settings:{page}", lambda: self.controller.open_settings(page, pkg)
            )
        elif method_name == "OpenApkInstaller":
            self._reply_action(
                invocation, "open_apk_installer", self.controller.open_apk_installer
            )
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method {method_name}",
            )


def run_panel_daemon() -> int:
    """Run blocking session D-Bus service org.droidloom.ControlCenter."""
    try:
        from gi.repository import Gio, GLib
    except Exception as exc:
        print(f"droidloom-panel-daemon: PyGObject unavailable: {exc}", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    service_holder: list[ControlCenterService] = []

    def on_bus_acquired(connection: Any, _name: str) -> None:
        service_holder.append(ControlCenterService(connection))

    def on_name_lost(_connection: Any, _name: str) -> None:
        loop.quit()

    owner_id = Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
        on_bus_acquired,
        None,
        on_name_lost,
    )

    def handle_sig(_signum: int, _frame: Any) -> None:
        loop.quit()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        loop.run()
    finally:
        for svc in service_holder:
            svc.close()
        Gio.bus_unown_name(owner_id)
    return 0
