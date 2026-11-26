import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.kirigami as Kirigami
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string daemonService: "org.droidloom.ControlCenter"
    readonly property string daemonPath: "/org/droidloom/ControlCenter"
    readonly property string daemonIface: "org.droidloom.ControlCenter"

    property var runtimeState: ({
        state: "stopped",
        running: false,
        ready: false,
        android_version: "",
        memory_bytes: 0,
        processes: 0,
        app_processes: 0,
        session_mode: "desktop",
        games_driver: "angle",
        mic_capture: false,
        desktop_forwarding: true,
        adb_connected: false,
        night_mode: false,
        dnd: false,
        rotation_mode: "free",
        rotation_index: 0,
        airplane_mode: false,
        http_proxy: "",
        stay_awake: false,
        anim_scale: 1.0,
        show_touches: false,
        pointer_location: false,
        notification_backend: "unavailable",
        unread_count: 0,
        ongoing_count: 0
    })

    property var notificationsData: ({
        backend: "unavailable",
        connected: false,
        desktop_forwarding: true,
        notifications: [],
        unread_count: 0,
        ongoing_count: 0
    })

    property var tasksData: ({
        connected: false,
        recent_tasks: [],
        background_apps: []
    })

    property var appsData: ({
        connected: false,
        apps: []
    })

    property string busyToggle: ""
    property string statusToast: ""
    property bool statusToastError: false
    property bool daemonOnline: serviceWatcher.registered
    property var pendingConfirm: null

    readonly property var defaultQuickTiles: [
        "runtime", "night_mode", "dnd", "rotation",
        "session_mode", "games_driver", "mic_capture", "android_settings"
    ]

    function activeQuickTiles() {
        var list = Plasmoid.configuration.quickTiles
        if (!list || list.length === 0) return defaultQuickTiles.slice()
        return Array.from(list)
    }

    function addQuickTile(key) {
        var arr = activeQuickTiles()
        if (arr.indexOf(key) >= 0) return
        arr.push(key)
        Plasmoid.configuration.quickTiles = arr
    }

    function removeQuickTile(key) {
        var arr = activeQuickTiles().filter(function(k) { return k !== key })
        Plasmoid.configuration.quickTiles = arr
    }

    function moveQuickTile(key, delta) {
        var arr = activeQuickTiles()
        var idx = arr.indexOf(key)
        if (idx < 0) return
        var next = idx + delta
        if (next < 0 || next >= arr.length) return
        var tmp = arr[idx]
        arr[idx] = arr[next]
        arr[next] = tmp
        Plasmoid.configuration.quickTiles = arr
    }

    function resetQuickTiles() {
        Plasmoid.configuration.quickTiles = defaultQuickTiles.slice()
    }

    function requestConfirm(title, body, confirmLabel, destructive, callback) {
        pendingConfirm = {
            title: title,
            body: body,
            confirmLabel: confirmLabel || "确认",
            destructive: Boolean(destructive),
            callback: callback
        }
    }

    function confirmPendingAction() {
        var item = pendingConfirm
        pendingConfirm = null
        if (item && item.callback) item.callback()
    }

    function cancelPendingAction() {
        pendingConfirm = null
    }

    readonly property bool runtimeRunning: Boolean(runtimeState.running)
    readonly property bool runtimeReady: Boolean(runtimeState.ready)
    readonly property int unreadCount: Number(notificationsData.unread_count || runtimeState.unread_count || 0)
    readonly property int ongoingCount: Number(notificationsData.ongoing_count || runtimeState.ongoing_count || 0)
    readonly property int activeTasksCount: (tasksData.recent_tasks ? tasksData.recent_tasks.length : 0)
        + (tasksData.background_apps ? tasksData.background_apps.length : 0)
    readonly property int appsCount: appsData.apps ? appsData.apps.length : 0

    switchWidth: Kirigami.Units.gridUnit * 18
    switchHeight: Kirigami.Units.gridUnit * 22

    Plasmoid.icon: "org.droidloom.Settings"
    Plasmoid.title: "Droidloom 控制中心"
    Plasmoid.status: (Plasmoid.configuration.alwaysShowInTray || runtimeRunning || unreadCount > 0)
        ? PlasmaCore.Types.ActiveStatus
        : PlasmaCore.Types.PassiveStatus

    toolTipMainText: runtimeRunning
        ? ("Droidloom 运行中" + (runtimeState.android_version ? " (Android " + runtimeState.android_version + ")" : ""))
        : "Droidloom 已停止"
    toolTipSubText: runtimeRunning
        ? ("未读通知: " + unreadCount + (ongoingCount > 0 ? " (含常驻 " + ongoingCount + ")" : "")
            + " · 后台项目: " + activeTasksCount)
        : "点击展开控制中心以启动容器或打开设置"

    compactRepresentation: CompactRepresentation {}
    fullRepresentation: FullRepresentation {}

    function showToast(msg, isError) {
        if (!msg) return
        statusToast = msg
        statusToastError = Boolean(isError)
        toastTimer.restart()
    }

    function safeParseJson(raw, fallback) {
        if (!raw) return fallback
        try {
            return JSON.parse(raw)
        } catch (e) {
            return fallback
        }
    }

    function applyStateJson(raw) {
        var parsed = safeParseJson(raw, null)
        if (parsed) runtimeState = parsed
    }

    function applyNotificationsJson(raw) {
        var parsed = safeParseJson(raw, null)
        if (parsed) notificationsData = parsed
    }

    function applyTasksJson(raw) {
        var parsed = safeParseJson(raw, null)
        if (parsed) tasksData = parsed
    }

    function applyAppsJson(raw) {
        var parsed = safeParseJson(raw, null)
        if (parsed) appsData = parsed
    }

    function callDaemon(member, args, signature, onOk, onErr) {
        var msg = {
            service: daemonService,
            path: daemonPath,
            iface: daemonIface,
            member: member
        }
        if (args && args.length > 0) {
            msg.arguments = args
        }
        if (signature) {
            msg.signature = signature
        }
        var reply = DBus.SessionBus.asyncCall(msg)
        reply.finished.connect(function() {
            if (reply.isError || !reply.isValid) {
                var errMsg = reply.error ? reply.error.message : "D-Bus 调用失败"
                if (onErr) onErr(errMsg)
                return
            }
            if (onOk) onOk(reply)
        })
    }

    function fetchState() {
        callDaemon("GetState", [], "", function(r) { applyStateJson(r.value) }, null)
    }

    function fetchNotifications() {
        callDaemon("GetNotifications", [], "", function(r) { applyNotificationsJson(r.value) }, null)
    }

    function fetchTasks(includeSys) {
        callDaemon(
            "GetTasks",
            [new DBus.bool(Boolean(includeSys))],
            "(b)",
            function(r) { applyTasksJson(r.value) },
            null
        )
    }

    function fetchApps(forceRefresh) {
        callDaemon(
            "GetApps",
            [new DBus.bool(Boolean(forceRefresh))],
            "(b)",
            function(r) { applyAppsJson(r.value) },
            null
        )
    }

    function refreshAll() {
        callDaemon("RefreshAll", [], "", null, function(err) { showToast(err, true) })
        fetchTasks(Plasmoid.configuration.showSystemTasks)
    }

    function handleActionReply(reply, busyKey) {
        if (busyKey && busyToggle === busyKey) busyToggle = ""
        var vals = reply.values || []
        var ok = vals.length > 0 ? Boolean(vals[0]) : true
        var msg = vals.length > 1 ? String(vals[1]) : ""
        if (msg) showToast(msg, !ok)
    }

    function launchApp(pkg, comp) {
        callDaemon(
            "LaunchApp",
            [new DBus.string(pkg), new DBus.string(comp || "")],
            "(ss)",
            function(r) {
                handleActionReply(r, "")
                if (Plasmoid.configuration.closeOnAppLaunch) {
                    root.expanded = false
                }
            },
            function(err) { showToast(err, true) }
        )
    }

    function stopApp(pkg) {
        callDaemon(
            "StopApp",
            [new DBus.string(pkg)],
            "(s)",
            function(r) {
                handleActionReply(r, "")
                fetchTasks(Plasmoid.configuration.showSystemTasks)
            },
            function(err) { showToast(err, true) }
        )
    }

    function openAndroidAppInfo(pkg) {
        callDaemon(
            "OpenAndroidAppInfo",
            [new DBus.string(pkg)],
            "(s)",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    function clearBackgroundTasks() {
        busyToggle = "clear_tasks"
        callDaemon(
            "ClearBackgroundTasks",
            [],
            "",
            function(r) {
                handleActionReply(r, "clear_tasks")
                fetchTasks(Plasmoid.configuration.showSystemTasks)
            },
            function(err) {
                busyToggle = ""
                showToast(err, true)
            }
        )
    }

    function invokeNotificationAction(key, action) {
        callDaemon(
            "InvokeNotificationAction",
            [new DBus.string(key), new DBus.string(action)],
            "(ss)",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    function dismissNotification(key) {
        callDaemon(
            "DismissNotification",
            [new DBus.string(key)],
            "(s)",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    function dismissAllNotifications() {
        callDaemon(
            "DismissAllNotifications",
            [],
            "",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    function setQuickToggle(toggle, value) {
        busyToggle = toggle
        callDaemon(
            "SetQuickToggle",
            [new DBus.string(toggle), new DBus.string(String(value))],
            "(ss)",
            function(r) { handleActionReply(r, toggle) },
            function(err) {
                if (busyToggle === toggle) busyToggle = ""
                showToast(err, true)
            }
        )
    }

    function openSettings(page, pkg) {
        callDaemon(
            "OpenSettings",
            [new DBus.string(page || "overview"), new DBus.string(pkg || "")],
            "(ss)",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    function openApkInstaller() {
        callDaemon(
            "OpenApkInstaller",
            [],
            "",
            function(r) { handleActionReply(r, "") },
            function(err) { showToast(err, true) }
        )
    }

    DBus.DBusServiceWatcher {
        id: serviceWatcher
        busType: DBus.BusType.Session
        watchedService: root.daemonService
        onRegisteredChanged: {
            if (registered) {
                root.fetchState()
                root.fetchNotifications()
            }
        }
    }

    DBus.SignalWatcher {
        id: signalWatcher
        busType: DBus.BusType.Session
        service: root.daemonService
        path: root.daemonPath
        iface: root.daemonIface

        signal dbusStateChanged(string stateJson)
        signal dbusNotificationsChanged(string notificationsJson, int unreadCount, int ongoingCount)
        signal dbusTasksChanged(string tasksJson)
        signal dbusAppsChanged(string appsJson)
        signal dbusActionFinished(string actionId, bool ok, string message)

        onDbusStateChanged: function(stateJson) {
            root.applyStateJson(stateJson)
        }
        onDbusNotificationsChanged: function(notificationsJson, _u, _o) {
            root.applyNotificationsJson(notificationsJson)
        }
        onDbusTasksChanged: function(tasksJson) {
            root.applyTasksJson(tasksJson)
        }
        onDbusAppsChanged: function(appsJson) {
            root.applyAppsJson(appsJson)
        }
        onDbusActionFinished: function(_id, ok, message) {
            if (!ok && message) {
                root.showToast(message, true)
            }
        }
    }

    Timer {
        id: expandedPollTimer
        interval: 5000
        repeat: true
        running: root.expanded && root.runtimeRunning
        onTriggered: {
            root.fetchTasks(Plasmoid.configuration.showSystemTasks)
            if (root.runtimeState.notification_backend === "adb_fallback") {
                root.fetchNotifications()
            }
        }
    }

    Timer {
        id: toastTimer
        interval: 4000
        repeat: false
        onTriggered: root.statusToast = ""
    }

    onExpandedChanged: {
        if (expanded) {
            refreshAll()
        }
    }

    Component.onCompleted: {
        fetchState()
        fetchNotifications()
    }
}
