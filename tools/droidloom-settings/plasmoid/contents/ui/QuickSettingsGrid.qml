import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: qsRoot

    spacing: Kirigami.Units.smallSpacing

    property bool editMode: false

    readonly property var allTileKeys: [
        "runtime",
        "restart_runtime",
        "night_mode",
        "dnd",
        "rotation",
        "session_mode",
        "games_driver",
        "mic_capture",
        "airplane_mode",
        "http_proxy",
        "stay_awake",
        "anim_speed",
        "show_touches",
        "pointer_location",
        "desktop_forwarding",
        "notif_badge",
        "media_rescan",
        "android_settings",
        "droidloom_settings",
        "apk_installer",
        "adb_reconnect"
    ]

    readonly property var activeKeys: root.activeQuickTiles()
    readonly property var availableKeys: {
        var cur = activeKeys
        return allTileKeys.filter(function(k) { return cur.indexOf(k) < 0 })
    }
    readonly property var displayedKeys: {
        if (Plasmoid.configuration.quickTilesExpanded || activeKeys.length <= 4) {
            return activeKeys
        }
        return activeKeys.slice(0, 4)
    }

    function rotationLabel() {
        if (root.runtimeState.rotation_mode === "free") return "自由旋转"
        var deg = (Number(root.runtimeState.rotation_index || 0) * 90) % 360
        return deg + "° 锁定"
    }

    function animLabel() {
        var s = Number(root.runtimeState.anim_scale)
        if (isNaN(s)) return "1×"
        if (s <= 0.05) return "已关闭"
        return s + "×"
    }

    function tileMeta(key) {
        switch (key) {
        case "runtime":
            return {
                key: "runtime",
                title: "Android 容器",
                subtitle: root.runtimeRunning ? (root.runtimeReady ? "运行中" : "启动中") : "已停止",
                icon: root.runtimeRunning ? "media-playback-start" : "system-shutdown",
                active: root.runtimeRunning,
                enabled: true,
                action: function() {
                    if (root.runtimeRunning) {
                        root.requestConfirm(
                            "停止 Android 容器？",
                            "停止 Droidloom 将关闭当前所有正在运行的 Android 应用窗口和后台服务。确认停止吗？",
                            "确认停止",
                            true,
                            function() { root.setQuickToggle("runtime", "stop") }
                        )
                    } else {
                        root.setQuickToggle("runtime", "start")
                    }
                }
            }
        case "restart_runtime":
            return {
                key: "restart_runtime",
                title: "重启容器",
                subtitle: "重载 Droidloom",
                icon: "system-reboot",
                active: false,
                enabled: true,
                action: function() {
                    root.requestConfirm(
                        "重启整个 Droidloom？",
                        "重启将关闭当前所有正在运行的 Android 应用并重新启动容器。确认立即重启吗？",
                        "确认重启",
                        true,
                        function() { root.setQuickToggle("restart_runtime", "restart") }
                    )
                }
            }
        case "night_mode":
            return {
                key: "night_mode",
                title: "深色模式",
                subtitle: root.runtimeState.night_mode ? "已开启" : "已关闭",
                icon: "weather-clear-night",
                active: Boolean(root.runtimeState.night_mode),
                enabled: root.runtimeRunning,
                action: function() { root.setQuickToggle("night_mode", !root.runtimeState.night_mode) }
            }
        case "dnd":
            return {
                key: "dnd",
                title: "勿扰模式",
                subtitle: root.runtimeState.dnd ? "已开启" : "已关闭",
                icon: "notifications-disabled",
                active: Boolean(root.runtimeState.dnd),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("dnd", !root.runtimeState.dnd) }
            }
        case "rotation":
            return {
                key: "rotation",
                title: "屏幕方向",
                subtitle: rotationLabel(),
                icon: "object-rotate-right",
                active: root.runtimeState.rotation_mode !== "free",
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("rotation", "cycle") }
            }
        case "session_mode":
            return {
                key: "session_mode",
                title: "窗口模式",
                subtitle: root.runtimeState.session_mode === "mobile" ? "移动全屏" : "桌面窗口",
                icon: root.runtimeState.session_mode === "mobile" ? "smartphone" : "window-duplicate",
                active: root.runtimeState.session_mode === "mobile",
                enabled: true,
                action: function() {
                    var nextMode = root.runtimeState.session_mode === "mobile" ? "desktop" : "mobile"
                    var targetLabel = nextMode === "mobile" ? "移动全屏" : "桌面窗口"
                    if (root.runtimeRunning) {
                        root.requestConfirm(
                            "切换窗口模式并重启 Droidloom？",
                            "切换到「" + targetLabel + "」模式需要重启整个 Droidloom 容器，当前打开的 Android 应用将被关闭。确认重启吗？",
                            "确认切换并重启",
                            true,
                            function() { root.setQuickToggle("session_mode", nextMode) }
                        )
                    } else {
                        root.setQuickToggle("session_mode", nextMode)
                    }
                }
            }
        case "games_driver":
            return {
                key: "games_driver",
                title: "游戏驱动",
                subtitle: String(root.runtimeState.games_driver || "angle").toUpperCase(),
                icon: "applications-games",
                active: root.runtimeState.games_driver === "mesa",
                enabled: root.runtimeRunning,
                action: function() {
                    root.setQuickToggle(
                        "games_driver",
                        root.runtimeState.games_driver === "mesa" ? "angle" : "mesa"
                    )
                }
            }
        case "mic_capture":
            return {
                key: "mic_capture",
                title: "麦克风",
                subtitle: root.runtimeState.mic_capture ? "允许采集" : "已静音",
                icon: root.runtimeState.mic_capture ? "audio-input-microphone" : "microphone-sensitivity-muted",
                active: Boolean(root.runtimeState.mic_capture),
                enabled: true,
                action: function() { root.setQuickToggle("mic_capture", !root.runtimeState.mic_capture) }
            }
        case "airplane_mode":
            return {
                key: "airplane_mode",
                title: "飞行模式",
                subtitle: root.runtimeState.airplane_mode ? "已开启" : "已关闭",
                icon: "airplane-mode",
                active: Boolean(root.runtimeState.airplane_mode),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("airplane_mode", !root.runtimeState.airplane_mode) }
            }
        case "http_proxy":
            return {
                key: "http_proxy",
                title: "宿主机代理",
                subtitle: root.runtimeState.http_proxy ? String(root.runtimeState.http_proxy) : "直连",
                icon: "network-workgroup",
                active: Boolean(root.runtimeState.http_proxy),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("http_proxy", !root.runtimeState.http_proxy) }
            }
        case "stay_awake":
            return {
                key: "stay_awake",
                title: "保持唤醒",
                subtitle: root.runtimeState.stay_awake ? "不休眠" : "默认休眠",
                icon: "preferences-desktop-screensaver",
                active: Boolean(root.runtimeState.stay_awake),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("stay_awake", !root.runtimeState.stay_awake) }
            }
        case "anim_speed":
            return {
                key: "anim_speed",
                title: "动画倍速",
                subtitle: animLabel(),
                icon: "speedometer",
                active: Number(root.runtimeState.anim_scale) !== 1.0,
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("anim_speed", "cycle") }
            }
        case "show_touches":
            return {
                key: "show_touches",
                title: "显示点按",
                subtitle: root.runtimeState.show_touches ? "已开启" : "已关闭",
                icon: "input-touchscreen",
                active: Boolean(root.runtimeState.show_touches),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("show_touches", !root.runtimeState.show_touches) }
            }
        case "pointer_location":
            return {
                key: "pointer_location",
                title: "指针位置",
                subtitle: root.runtimeState.pointer_location ? "已开启" : "已关闭",
                icon: "crosshairs",
                active: Boolean(root.runtimeState.pointer_location),
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("pointer_location", !root.runtimeState.pointer_location) }
            }
        case "desktop_forwarding":
            return {
                key: "desktop_forwarding",
                title: "弹窗同步",
                subtitle: root.runtimeState.desktop_forwarding ? "已同步" : "仅面板",
                icon: root.runtimeState.desktop_forwarding ? "notifications" : "notifications-disabled",
                active: Boolean(root.runtimeState.desktop_forwarding),
                enabled: true,
                action: function() { root.setQuickToggle("desktop_forwarding", !root.runtimeState.desktop_forwarding) }
            }
        case "notif_badge":
            return {
                key: "notif_badge",
                title: "通知角标",
                subtitle: Plasmoid.configuration.showNotificationBadge ? "已显示" : "已隐藏",
                icon: "tag-symbolic",
                active: Boolean(Plasmoid.configuration.showNotificationBadge),
                enabled: true,
                action: function() {
                    Plasmoid.configuration.showNotificationBadge = !Plasmoid.configuration.showNotificationBadge
                }
            }
        case "media_rescan":
            return {
                key: "media_rescan",
                title: "媒体扫描",
                subtitle: "刷新相册",
                icon: "view-refresh",
                active: false,
                enabled: root.runtimeRunning && Boolean(root.runtimeState.adb_connected),
                action: function() { root.setQuickToggle("media_rescan", "scan") }
            }
        case "android_settings":
            return {
                key: "android_settings",
                title: "系统设置",
                subtitle: "Android 设置",
                icon: "preferences-system",
                active: false,
                enabled: root.runtimeRunning,
                action: function() { root.setQuickToggle("android_settings", "open") }
            }
        case "droidloom_settings":
            return {
                key: "droidloom_settings",
                title: "控制中心",
                subtitle: "Droidloom 设置",
                icon: "org.droidloom.Settings",
                active: false,
                enabled: true,
                action: function() { root.setQuickToggle("droidloom_settings", "open") }
            }
        case "apk_installer":
            return {
                key: "apk_installer",
                title: "安装 APK",
                subtitle: "本地安装包",
                icon: "list-add",
                active: false,
                enabled: true,
                action: function() { root.setQuickToggle("apk_installer", "open") }
            }
        case "adb_reconnect":
            return {
                key: "adb_reconnect",
                title: "重连 ADB",
                subtitle: root.runtimeState.adb_connected ? "已连接" : "未连接",
                icon: "network-connect",
                active: Boolean(root.runtimeState.adb_connected),
                enabled: root.runtimeRunning,
                action: function() { root.setQuickToggle("adb_reconnect", "reconnect") }
            }
        default:
            return {
                key: key,
                title: key,
                subtitle: "",
                icon: "configure",
                active: false,
                enabled: false,
                action: function() {}
            }
        }
    }

    // Header row for Quick Settings
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            text: qsRoot.editMode ? "编辑快捷开关（点击添加/删除/排序）" : "快捷开关"
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }

        PlasmaComponents3.ToolButton {
            visible: !qsRoot.editMode && qsRoot.activeKeys.length > 4
            icon.name: Plasmoid.configuration.quickTilesExpanded ? "go-up" : "go-down"
            text: Plasmoid.configuration.quickTilesExpanded ? "收起" : ("展开 (" + qsRoot.activeKeys.length + ")")
            display: AbstractButton.TextBesideIcon
            onClicked: Plasmoid.configuration.quickTilesExpanded = !Plasmoid.configuration.quickTilesExpanded
        }

        PlasmaComponents3.ToolButton {
            visible: !qsRoot.editMode
            icon.name: "document-edit"
            text: "编辑"
            display: AbstractButton.TextBesideIcon
            onClicked: qsRoot.editMode = true
        }

        PlasmaComponents3.ToolButton {
            visible: qsRoot.editMode
            icon.name: "edit-undo"
            text: "重置"
            display: AbstractButton.TextBesideIcon
            onClicked: root.resetQuickTiles()
        }

        PlasmaComponents3.ToolButton {
            visible: qsRoot.editMode
            icon.name: "dialog-ok-apply"
            text: "完成"
            display: AbstractButton.TextBesideIcon
            onClicked: qsRoot.editMode = false
        }
    }

    // Normal Quick Settings Grid
    GridLayout {
        visible: !qsRoot.editMode
        Layout.fillWidth: true
        columns: 4
        rowSpacing: Kirigami.Units.smallSpacing
        columnSpacing: Kirigami.Units.smallSpacing

        Repeater {
            model: qsRoot.displayedKeys

            delegate: Rectangle {
                required property string modelData
                readonly property var tile: qsRoot.tileMeta(modelData)

                Layout.fillWidth: true
                Layout.preferredWidth: 0
                Layout.preferredHeight: Kirigami.Units.gridUnit * 3.1
                radius: Kirigami.Units.cornerRadius * 1.5
                opacity: tile.enabled ? 1.0 : 0.45

                color: {
                    if (tile.active) {
                        return tileMouse.containsMouse
                            ? Qt.lighter(Kirigami.Theme.highlightColor, 1.1)
                            : Kirigami.Theme.highlightColor
                    }
                    return tileMouse.containsMouse
                        ? Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12)
                        : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.06)
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Kirigami.Units.smallSpacing
                    spacing: 2

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 4

                        Kirigami.Icon {
                            source: tile.icon
                            Layout.preferredWidth: Kirigami.Units.iconSizes.small
                            Layout.preferredHeight: Kirigami.Units.iconSizes.small
                            color: tile.active ? Kirigami.Theme.highlightedTextColor : Kirigami.Theme.textColor
                        }

                        Item { Layout.fillWidth: true }

                        PlasmaComponents3.BusyIndicator {
                            visible: root.busyToggle === tile.key
                            running: visible
                            Layout.preferredWidth: Kirigami.Units.iconSizes.small
                            Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        }
                    }

                    PlasmaComponents3.Label {
                        Layout.fillWidth: true
                        text: tile.title
                        font.bold: true
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        elide: Text.ElideRight
                        color: tile.active ? Kirigami.Theme.highlightedTextColor : Kirigami.Theme.textColor
                    }

                    PlasmaComponents3.Label {
                        Layout.fillWidth: true
                        text: tile.subtitle
                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                        opacity: tile.active ? 0.9 : 0.7
                        elide: Text.ElideRight
                        color: tile.active ? Kirigami.Theme.highlightedTextColor : Kirigami.Theme.textColor
                    }
                }

                MouseArea {
                    id: tileMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: tile.enabled && root.busyToggle !== tile.key
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: tile.action()
                }
            }
        }
    }

    // Android-style Inline Edit Panel (Add / Remove / Reorder)
    PlasmaComponents3.ScrollView {
        id: editScroll
        visible: qsRoot.editMode
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 15
        contentWidth: availableWidth
        PlasmaComponents3.ScrollBar.horizontal.policy: PlasmaComponents3.ScrollBar.AlwaysOff
        clip: true

        ColumnLayout {
            width: editScroll.availableWidth
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "已启用（" + qsRoot.activeKeys.length + "）"
                font.bold: true
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                opacity: 0.8
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4

                Repeater {
                    model: qsRoot.activeKeys
                    delegate: Rectangle {
                        required property string modelData
                        required property int index
                        readonly property var tile: qsRoot.tileMeta(modelData)

                        Layout.fillWidth: true
                        implicitHeight: activeEditRow.implicitHeight + 10
                        radius: Kirigami.Units.cornerRadius
                        color: Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.14)

                        RowLayout {
                            id: activeEditRow
                            anchors.fill: parent
                            anchors.margins: 5
                            spacing: Kirigami.Units.smallSpacing

                            Kirigami.Icon {
                                source: tile.icon
                                Layout.preferredWidth: 20
                                Layout.preferredHeight: 20
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0
                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: tile.title
                                    font.bold: true
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    elide: Text.ElideRight
                                }
                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: tile.subtitle
                                    font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                    opacity: 0.7
                                    elide: Text.ElideRight
                                }
                            }

                            PlasmaComponents3.ToolButton {
                                icon.name: "go-up"
                                enabled: index > 0
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                onClicked: root.moveQuickTile(modelData, -1)
                                PlasmaComponents3.ToolTip.text: "上移"
                                PlasmaComponents3.ToolTip.visible: hovered
                            }

                            PlasmaComponents3.ToolButton {
                                icon.name: "go-down"
                                enabled: index < qsRoot.activeKeys.length - 1
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                onClicked: root.moveQuickTile(modelData, 1)
                                PlasmaComponents3.ToolTip.text: "下移"
                                PlasmaComponents3.ToolTip.visible: hovered
                            }

                            PlasmaComponents3.ToolButton {
                                icon.name: "list-remove"
                                enabled: qsRoot.activeKeys.length > 1
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                onClicked: root.removeQuickTile(modelData)
                                PlasmaComponents3.ToolTip.text: "移除"
                                PlasmaComponents3.ToolTip.visible: hovered
                            }
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                visible: qsRoot.availableKeys.length > 0
                Layout.fillWidth: true
                text: "可添加的快捷方式（" + qsRoot.availableKeys.length + "）"
                font.bold: true
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                opacity: 0.8
                Layout.topMargin: 6
            }

            ColumnLayout {
                visible: qsRoot.availableKeys.length > 0
                Layout.fillWidth: true
                spacing: 4

                Repeater {
                    model: qsRoot.availableKeys
                    delegate: Rectangle {
                        required property string modelData
                        readonly property var tile: qsRoot.tileMeta(modelData)

                        Layout.fillWidth: true
                        implicitHeight: availEditRow.implicitHeight + 10
                        radius: Kirigami.Units.cornerRadius
                        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.06)

                        RowLayout {
                            id: availEditRow
                            anchors.fill: parent
                            anchors.margins: 5
                            spacing: Kirigami.Units.smallSpacing

                            Kirigami.Icon {
                                source: tile.icon
                                Layout.preferredWidth: 20
                                Layout.preferredHeight: 20
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0
                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: tile.title
                                    font.bold: true
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    elide: Text.ElideRight
                                }
                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: tile.subtitle
                                    font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                    opacity: 0.65
                                    elide: Text.ElideRight
                                }
                            }

                            PlasmaComponents3.ToolButton {
                                icon.name: "list-add"
                                text: "添加"
                                display: AbstractButton.TextBesideIcon
                                onClicked: root.addQuickTile(modelData)
                            }
                        }
                    }
                }
            }
        }
    }
}
