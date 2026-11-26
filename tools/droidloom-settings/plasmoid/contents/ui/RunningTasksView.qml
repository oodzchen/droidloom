import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: tasksRoot

    spacing: Kirigami.Units.smallSpacing

    readonly property var recentTasks: root.tasksData.recent_tasks || []
    readonly property var bgApps: root.tasksData.background_apps || []

    function formatRss(kb) {
        if (!kb || kb <= 0) return ""
        if (kb >= 1024) return Math.round(kb / 1024) + " MB"
        return kb + " KB"
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.CheckBox {
            text: "显示系统应用与服务"
            checked: Plasmoid.configuration.showSystemTasks
            onToggled: {
                Plasmoid.configuration.showSystemTasks = checked
                root.fetchTasks(checked)
            }
        }

        Item { Layout.fillWidth: true }

        PlasmaComponents3.Button {
            icon.name: "edit-clear"
            text: "清理后台"
            enabled: root.runtimeRunning && root.busyToggle !== "clear_tasks"
            onClicked: root.clearBackgroundTasks()
        }
    }

    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true

        PlasmaExtras.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.largeSpacing * 2
            visible: !root.runtimeRunning || (tasksRoot.recentTasks.length === 0 && tasksRoot.bgApps.length === 0)
            iconName: root.runtimeRunning ? "view-process-all" : "system-shutdown"
            text: root.runtimeRunning ? "当前没有活动任务或后台服务" : "Android 容器未运行"
        }

        PlasmaComponents3.ScrollView {
            id: tasksScroll
            anchors.fill: parent
            anchors.leftMargin: Kirigami.Units.smallSpacing
            anchors.rightMargin: Kirigami.Units.smallSpacing
            anchors.bottomMargin: Kirigami.Units.smallSpacing
            visible: root.runtimeRunning && (tasksRoot.recentTasks.length > 0 || tasksRoot.bgApps.length > 0)
            contentWidth: availableWidth
            PlasmaComponents3.ScrollBar.horizontal.policy: PlasmaComponents3.ScrollBar.AlwaysOff
            clip: true

            ColumnLayout {
                width: tasksScroll.availableWidth
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Label {
                    visible: tasksRoot.recentTasks.length > 0
                    text: "活跃与最近任务 (" + tasksRoot.recentTasks.length + ")"
                    font.bold: true
                    opacity: 0.85
                }

                Repeater {
                    model: tasksRoot.recentTasks
                    delegate: Rectangle {
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: taskCol.implicitHeight + Kirigami.Units.smallSpacing * 2
                        radius: Kirigami.Units.cornerRadius * 1.5
                        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.05)

                        ColumnLayout {
                            id: taskCol
                            anchors.fill: parent
                            anchors.margins: Kirigami.Units.smallSpacing
                            spacing: 4

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                Image {
                                    visible: Boolean(modelData.icon_path)
                                    source: modelData.icon_path || ""
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true
                                }
                                Kirigami.Icon {
                                    visible: !modelData.icon_path
                                    source: "application-x-executable"
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30
                                }

                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: modelData.name || modelData.package
                                    font.bold: true
                                    wrapMode: Text.Wrap
                                }

                                Rectangle {
                                    radius: 4
                                    color: modelData.visible
                                        ? Qt.rgba(Kirigami.Theme.positiveTextColor.r, Kirigami.Theme.positiveTextColor.g, Kirigami.Theme.positiveTextColor.b, 0.18)
                                        : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.1)
                                    implicitWidth: taskBadgeLbl.implicitWidth + 10
                                    implicitHeight: taskBadgeLbl.implicitHeight + 4

                                    PlasmaComponents3.Label {
                                        id: taskBadgeLbl
                                        anchors.centerIn: parent
                                        text: modelData.visible ? "前台" : (modelData.running ? "后台活跃" : "最近")
                                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                    }
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1
                                    PlasmaComponents3.Label {
                                        Layout.fillWidth: true
                                        text: modelData.package
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        opacity: 0.72
                                        wrapMode: Text.WrapAnywhere
                                    }
                                    PlasmaComponents3.Label {
                                        visible: tasksRoot.formatRss(modelData.rss_kb).length > 0
                                        Layout.fillWidth: true
                                        text: "内存占用: " + tasksRoot.formatRss(modelData.rss_kb)
                                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                        opacity: 0.65
                                    }
                                }

                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "media-playback-start"
                                    onClicked: root.launchApp(modelData.package, modelData.component)
                                    PlasmaComponents3.ToolTip.text: "切换到该应用"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "process-stop"
                                    onClicked: root.stopApp(modelData.package)
                                    PlasmaComponents3.ToolTip.text: "强制停止"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "configure"
                                    onClicked: root.openSettings("apps", modelData.package)
                                    PlasmaComponents3.ToolTip.text: "在设置中管理此应用"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                            }
                        }
                    }
                }

                PlasmaComponents3.Label {
                    visible: tasksRoot.bgApps.length > 0
                    text: "后台与前台服务进程 (" + tasksRoot.bgApps.length + ")"
                    font.bold: true
                    opacity: 0.85
                    Layout.topMargin: Kirigami.Units.smallSpacing
                }

                Repeater {
                    model: tasksRoot.bgApps
                    delegate: Rectangle {
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: bgCol.implicitHeight + Kirigami.Units.smallSpacing * 2
                        radius: Kirigami.Units.cornerRadius * 1.5
                        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.05)

                        ColumnLayout {
                            id: bgCol
                            anchors.fill: parent
                            anchors.margins: Kirigami.Units.smallSpacing
                            spacing: 4

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                Image {
                                    visible: Boolean(modelData.icon_path)
                                    source: modelData.icon_path || ""
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true
                                }
                                Kirigami.Icon {
                                    visible: !modelData.icon_path
                                    source: "application-x-executable"
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30
                                }

                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    text: modelData.name || modelData.package
                                    font.bold: true
                                    wrapMode: Text.Wrap
                                }

                                Rectangle {
                                    radius: 4
                                    color: modelData.is_foreground_service
                                        ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.22)
                                        : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.1)
                                    implicitWidth: bgBadgeLbl.implicitWidth + 10
                                    implicitHeight: bgBadgeLbl.implicitHeight + 4

                                    PlasmaComponents3.Label {
                                        id: bgBadgeLbl
                                        anchors.centerIn: parent
                                        text: modelData.proc_state_label || "运行中"
                                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                        color: modelData.is_foreground_service ? Kirigami.Theme.highlightColor : Kirigami.Theme.textColor
                                    }
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1
                                    PlasmaComponents3.Label {
                                        Layout.fillWidth: true
                                        text: modelData.package
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        opacity: 0.72
                                        wrapMode: Text.WrapAnywhere
                                    }
                                    PlasmaComponents3.Label {
                                        Layout.fillWidth: true
                                        font.pointSize: Math.max(7, Kirigami.Theme.smallFont.pointSize - 1)
                                        opacity: 0.65
                                        wrapMode: Text.WordWrap
                                        text: {
                                            var parts = []
                                            if (modelData.service_count > 0) parts.push(modelData.service_count + " 个服务")
                                            if (modelData.process_count > 0) parts.push(modelData.process_count + " 进程")
                                            var mem = tasksRoot.formatRss(modelData.rss_kb)
                                            if (mem) parts.push(mem)
                                            return parts.join(" · ")
                                        }
                                    }
                                }

                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "media-playback-start"
                                    onClicked: root.launchApp(modelData.package, modelData.component)
                                    PlasmaComponents3.ToolTip.text: "打开应用"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "process-stop"
                                    onClicked: root.stopApp(modelData.package)
                                    PlasmaComponents3.ToolTip.text: "强制停止"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                                PlasmaComponents3.ToolButton {
                                    Layout.alignment: Qt.AlignBottom
                                    icon.name: "configure"
                                    onClicked: root.openSettings("apps", modelData.package)
                                    PlasmaComponents3.ToolTip.text: "在设置中管理此应用"
                                    PlasmaComponents3.ToolTip.visible: hovered
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
