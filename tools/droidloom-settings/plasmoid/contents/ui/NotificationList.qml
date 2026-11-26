import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: notifRoot

    spacing: Kirigami.Units.smallSpacing

    readonly property var items: root.notificationsData.notifications || []
    readonly property bool hasClearable: {
        for (var i = 0; i < items.length; ++i) {
            if (items[i].clearable) return true
        }
        return false
    }

    function formatTime(ms) {
        if (!ms || ms <= 0) return ""
        var diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000))
        if (diffSec < 60) return "刚刚"
        if (diffSec < 3600) return Math.floor(diffSec / 60) + " 分钟前"
        var d = new Date(ms)
        var hh = ("0" + d.getHours()).slice(-2)
        var mm = ("0" + d.getMinutes()).slice(-2)
        return hh + ":" + mm
    }

    function formatActionLabel(label) {
        if (!label) return ""
        var low = String(label).trim().toLowerCase()
        if (low === "pre" || low === "prev" || low === "previous") return "⏮ 上一首"
        if (low === "toggle") return "⏯ 播放/暂停"
        if (low === "next") return "⏭ 下一首"
        if (low === "like") return "♥ 喜欢"
        if (low === "lyric") return "词"
        if (low === "stop") return "⏹ 停止"
        return label
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            text: "实时通知 (" + notifRoot.items.length + ")"
            font.bold: true
            Layout.fillWidth: true
        }

        PlasmaComponents3.ToolButton {
            checkable: true
            checked: Boolean(Plasmoid.configuration.showNotificationBadge)
            icon.name: "tag-symbolic"
            text: "角标"
            display: AbstractButton.TextBesideIcon
            onClicked: Plasmoid.configuration.showNotificationBadge = !Plasmoid.configuration.showNotificationBadge
            PlasmaComponents3.ToolTip.text: "在面板图标上显示/隐藏未读通知数量角标"
            PlasmaComponents3.ToolTip.visible: hovered
        }

        PlasmaComponents3.ToolButton {
            checkable: true
            checked: Boolean(root.runtimeState.desktop_forwarding)
            icon.name: checked ? "notifications" : "notifications-disabled"
            text: "同步弹窗"
            display: AbstractButton.TextBesideIcon
            onClicked: root.setQuickToggle("desktop_forwarding", !root.runtimeState.desktop_forwarding)
            PlasmaComponents3.ToolTip.text: "开启时 Android 通知同时推送到 Plasma 全局通知"
            PlasmaComponents3.ToolTip.visible: hovered
        }

        PlasmaComponents3.ToolButton {
            icon.name: "edit-clear-all"
            text: "全部清除"
            enabled: notifRoot.hasClearable
            onClicked: root.dismissAllNotifications()
            PlasmaComponents3.ToolTip.text: "清除所有可关闭通知（保留常驻通知）"
            PlasmaComponents3.ToolTip.visible: hovered
        }
    }

    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true

        PlasmaExtras.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.largeSpacing * 2
            visible: notifRoot.items.length === 0
            iconName: root.runtimeRunning ? "notifications" : "system-shutdown"
            text: root.runtimeRunning ? "暂无 Android 通知" : "Android 容器未运行"
            explanation: root.runtimeRunning
                ? "有新通知或常驻前台服务通知时会实时显示在这里，左右滑动可清除通知。"
                : "点击上方“Android 容器”磁贴即可启动。"
        }

        PlasmaComponents3.ScrollView {
            anchors.fill: parent
            visible: notifRoot.items.length > 0
            PlasmaComponents3.ScrollBar.horizontal.policy: PlasmaComponents3.ScrollBar.AlwaysOff
            clip: true

            ListView {
                id: notifList
                model: notifRoot.items
                spacing: Kirigami.Units.smallSpacing

                delegate: Item {
                    id: swipeWrapper
                    required property var modelData

                    width: notifList.width - Kirigami.Units.smallSpacing
                    implicitHeight: notifCard.implicitHeight
                    clip: true

                    // Swipe reveal backdrop
                    Rectangle {
                        anchors.fill: parent
                        radius: Kirigami.Units.cornerRadius * 1.5
                        visible: Math.abs(notifCard.x) > 4
                        color: modelData.clearable
                            ? Qt.rgba(Kirigami.Theme.negativeTextColor.r, Kirigami.Theme.negativeTextColor.g, Kirigami.Theme.negativeTextColor.b, 0.28)
                            : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.08)

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: Kirigami.Units.mediumSpacing

                            RowLayout {
                                visible: notifCard.x > 4
                                spacing: 4
                                Kirigami.Icon {
                                    source: modelData.clearable ? "edit-delete" : "object-locked"
                                    Layout.preferredWidth: 18
                                    Layout.preferredHeight: 18
                                }
                                PlasmaComponents3.Label {
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    text: modelData.clearable
                                        ? (Math.abs(notifCard.x) >= swipeWrapper.width * 0.35 ? "松手清除" : "右滑清除")
                                        : "常驻不可滑除"
                                }
                            }

                            Item { Layout.fillWidth: true }

                            RowLayout {
                                visible: notifCard.x < -4
                                spacing: 4
                                PlasmaComponents3.Label {
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    text: modelData.clearable
                                        ? (Math.abs(notifCard.x) >= swipeWrapper.width * 0.35 ? "松手清除" : "左滑清除")
                                        : "常驻不可滑除"
                                }
                                Kirigami.Icon {
                                    source: modelData.clearable ? "edit-delete" : "object-locked"
                                    Layout.preferredWidth: 18
                                    Layout.preferredHeight: 18
                                }
                            }
                        }
                    }

                    // Foreground swipeable notification card
                    Rectangle {
                        id: notifCard
                        x: 0
                        width: swipeWrapper.width
                        implicitHeight: cardCol.implicitHeight + Kirigami.Units.smallSpacing * 2
                        radius: Kirigami.Units.cornerRadius * 1.5
                        opacity: modelData.clearable
                            ? Math.max(0.25, 1.0 - Math.abs(x) / Math.max(1, width * 0.9))
                            : 1.0
                        color: hoverHandler.hovered
                            ? Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.09)
                            : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.05)
                        border.width: modelData.ongoing ? 1 : 0
                        border.color: Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.45)

                        readonly property var interactiveActions: (modelData.actions || []).filter(function(a) {
                            return a.key !== "default"
                        })

                        HoverHandler {
                            id: hoverHandler
                        }

                        TapHandler {
                            onTapped: root.invokeNotificationAction(swipeWrapper.modelData.key, "default")
                        }

                        DragHandler {
                            id: swipeDrag
                            target: notifCard
                            yAxis.enabled: false
                            xAxis.enabled: true
                            dragThreshold: 12
                            xAxis.minimum: swipeWrapper.modelData.clearable ? -swipeWrapper.width : -32
                            xAxis.maximum: swipeWrapper.modelData.clearable ? swipeWrapper.width : 32
                            onActiveChanged: {
                                if (!active) {
                                    if (swipeWrapper.modelData.clearable && Math.abs(notifCard.x) >= swipeWrapper.width * 0.35) {
                                        dismissSlideAnim.to = notifCard.x > 0 ? swipeWrapper.width : -swipeWrapper.width
                                        dismissSlideAnim.restart()
                                    } else {
                                        snapBackAnim.restart()
                                    }
                                }
                            }
                        }

                        NumberAnimation {
                            id: snapBackAnim
                            target: notifCard
                            property: "x"
                            to: 0
                            duration: 160
                            easing.type: Easing.OutCubic
                        }

                        NumberAnimation {
                            id: dismissSlideAnim
                            target: notifCard
                            property: "x"
                            duration: 140
                            easing.type: Easing.InQuad
                            onFinished: {
                                root.dismissNotification(swipeWrapper.modelData.key)
                            }
                        }

                        ColumnLayout {
                            id: cardCol
                            anchors.fill: parent
                            anchors.margins: Kirigami.Units.smallSpacing
                            spacing: 4

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 6

                                Image {
                                    visible: Boolean(swipeWrapper.modelData.icon_path)
                                    source: swipeWrapper.modelData.icon_path || ""
                                    Layout.preferredWidth: 16
                                    Layout.preferredHeight: 16
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true
                                }

                                Kirigami.Icon {
                                    visible: !swipeWrapper.modelData.icon_path
                                    source: "application-x-executable"
                                    Layout.preferredWidth: 16
                                    Layout.preferredHeight: 16
                                }

                                PlasmaComponents3.Label {
                                    text: swipeWrapper.modelData.app || swipeWrapper.modelData.package
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    font.bold: true
                                    opacity: 0.85
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                Rectangle {
                                    visible: Boolean(swipeWrapper.modelData.ongoing)
                                    radius: 4
                                    color: Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.22)
                                    implicitWidth: ongoingLbl.implicitWidth + 8
                                    implicitHeight: ongoingLbl.implicitHeight + 2

                                    PlasmaComponents3.Label {
                                        id: ongoingLbl
                                        anchors.centerIn: parent
                                        text: "常驻"
                                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                        color: Kirigami.Theme.highlightColor
                                    }
                                }

                                PlasmaComponents3.Label {
                                    text: notifRoot.formatTime(swipeWrapper.modelData.when)
                                    font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                    opacity: 0.6
                                }

                                PlasmaComponents3.ToolButton {
                                    visible: Boolean(swipeWrapper.modelData.clearable)
                                    icon.name: "window-close"
                                    Layout.preferredWidth: 20
                                    Layout.preferredHeight: 20
                                    onClicked: root.dismissNotification(swipeWrapper.modelData.key)
                                    PlasmaComponents3.ToolTip.text: "清除此通知（也可左右滑动）"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    PlasmaComponents3.Label {
                                        visible: Boolean(swipeWrapper.modelData.title)
                                        text: swipeWrapper.modelData.title || ""
                                        font.bold: true
                                        wrapMode: Text.WordWrap
                                        Layout.fillWidth: true
                                    }

                                    PlasmaComponents3.Label {
                                        visible: Boolean(swipeWrapper.modelData.body)
                                        text: swipeWrapper.modelData.body || ""
                                        opacity: 0.85
                                        wrapMode: Text.WordWrap
                                        maximumLineCount: 4
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }

                                    Repeater {
                                        model: (swipeWrapper.modelData.messages || []).slice(-3)
                                        delegate: PlasmaComponents3.Label {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                                            opacity: 0.82
                                            wrapMode: Text.WordWrap
                                            text: (modelData.sender ? (modelData.sender + ": ") : "") + (modelData.text || "")
                                        }
                                    }
                                }

                                Image {
                                    visible: Boolean(swipeWrapper.modelData.large_icon_path)
                                    source: swipeWrapper.modelData.large_icon_path || ""
                                    Layout.preferredWidth: 40
                                    Layout.preferredHeight: 40
                                    fillMode: Image.PreserveAspectCrop
                                    smooth: true
                                }
                            }

                            PlasmaComponents3.ProgressBar {
                                visible: Number(swipeWrapper.modelData.progress) >= 0
                                from: 0
                                to: 100
                                value: Math.max(0, Number(swipeWrapper.modelData.progress || 0))
                                Layout.fillWidth: true
                            }

                            Flow {
                                Layout.fillWidth: true
                                spacing: 4
                                visible: notifCard.interactiveActions.length > 0

                                Repeater {
                                    model: notifCard.interactiveActions
                                    delegate: PlasmaComponents3.Button {
                                        required property var modelData
                                        text: notifRoot.formatActionLabel(modelData.label || modelData.key)
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        onClicked: root.invokeNotificationAction(swipeWrapper.modelData.key, modelData.key)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
