import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

Item {
    id: fullRoot

    Layout.preferredWidth: Kirigami.Units.gridUnit * 26
    Layout.preferredHeight: Kirigami.Units.gridUnit * 35
    Layout.minimumWidth: Kirigami.Units.gridUnit * 21
    Layout.minimumHeight: Kirigami.Units.gridUnit * 25

    property int currentTab: Math.max(0, Math.min(2, Plasmoid.configuration.defaultTab))

    function formatMem(bytes) {
        if (!bytes || bytes <= 0) return ""
        var gb = bytes / (1024 * 1024 * 1024)
        if (gb >= 1.0) return gb.toFixed(1) + " GiB"
        return Math.round(bytes / (1024 * 1024)) + " MiB"
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Header
        PlasmaExtras.PlasmoidHeading {
            Layout.fillWidth: true
            contentItem: RowLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Icon {
                    source: "org.droidloom.Settings"
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    PlasmaComponents3.Label {
                        text: "Droidloom 控制中心"
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }

                    PlasmaComponents3.Label {
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        opacity: 0.72
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        text: {
                            if (!root.daemonOnline) return "正在连接控制服务..."
                            if (!root.runtimeRunning) return "Android 容器已停止"
                            var parts = []
                            if (root.runtimeState.android_version) parts.push("Android " + root.runtimeState.android_version)
                            var mem = fullRoot.formatMem(root.runtimeState.memory_bytes)
                            if (mem) parts.push(mem)
                            parts.push(root.runtimeReady ? "已就绪" : "启动中")
                            return parts.join(" · ")
                        }
                    }
                }

                PlasmaComponents3.ToolButton {
                    icon.name: "view-refresh"
                    onClicked: root.refreshAll()
                    PlasmaComponents3.ToolTip.text: "刷新全部状态"
                    PlasmaComponents3.ToolTip.visible: hovered
                }

                PlasmaComponents3.ToolButton {
                    icon.name: "list-add"
                    onClicked: root.openApkInstaller()
                    PlasmaComponents3.ToolTip.text: "安装 APK"
                    PlasmaComponents3.ToolTip.visible: hovered
                }

                PlasmaComponents3.ToolButton {
                    icon.name: "configure"
                    onClicked: root.openSettings("overview", "")
                    PlasmaComponents3.ToolTip.text: "打开 Droidloom 设置中心"
                    PlasmaComponents3.ToolTip.visible: hovered
                }
            }
        }

        // Navigation Tabs
        PlasmaComponents3.TabBar {
            id: tabBar
            Layout.fillWidth: true
            currentIndex: fullRoot.currentTab
            onCurrentIndexChanged: fullRoot.currentTab = currentIndex

            PlasmaComponents3.TabButton {
                text: root.unreadCount > 0 ? ("控制中心 (" + root.unreadCount + ")") : "控制中心"
            }
            PlasmaComponents3.TabButton {
                text: root.activeTasksCount > 0 ? ("后台任务 (" + root.activeTasksCount + ")") : "后台任务"
            }
            PlasmaComponents3.TabButton {
                text: root.appsCount > 0 ? ("全部应用 (" + root.appsCount + ")") : "全部应用"
            }
        }

        // Main Content Area
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: fullRoot.currentTab

            // Tab 0: Quick Settings + Live Notifications
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Kirigami.Units.smallSpacing

                QuickSettingsGrid {
                    Layout.fillWidth: true
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    Layout.rightMargin: Kirigami.Units.smallSpacing
                }

                Kirigami.Separator {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    Layout.rightMargin: Kirigami.Units.smallSpacing
                }

                NotificationList {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    Layout.rightMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                }
            }

            // Tab 1: Running Tasks & Background Services
            RunningTasksView {
                Layout.fillWidth: true
                Layout.fillHeight: true
            }

            // Tab 2: All Applications Drawer
            AppDrawerView {
                Layout.fillWidth: true
                Layout.fillHeight: true
            }
        }

        // Toast Banner
        Rectangle {
            visible: root.statusToast.length > 0
            Layout.fillWidth: true
            implicitHeight: footerRow.implicitHeight + Kirigami.Units.smallSpacing * 2
            color: root.statusToastError
                ? Qt.rgba(Kirigami.Theme.negativeTextColor.r, Kirigami.Theme.negativeTextColor.g, Kirigami.Theme.negativeTextColor.b, 0.16)
                : Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.12)

            RowLayout {
                id: footerRow
                anchors.fill: parent
                anchors.margins: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    text: root.statusToast
                }

                PlasmaComponents3.ToolButton {
                    icon.name: "dialog-close"
                    onClicked: root.statusToast = ""
                }
            }
        }
    }

    // In-popup modal confirmation overlay (Wayland-safe: does not dismiss popup)
    Rectangle {
        id: confirmOverlay
        anchors.fill: parent
        visible: root.pendingConfirm !== null
        z: 100
        color: Qt.rgba(0, 0, 0, 0.48)
        focus: visible
        Keys.onEscapePressed: root.cancelPendingAction()

        MouseArea {
            anchors.fill: parent
            onClicked: root.cancelPendingAction()
        }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - Kirigami.Units.largeSpacing * 2, Kirigami.Units.gridUnit * 21)
            implicitHeight: confirmCol.implicitHeight + Kirigami.Units.largeSpacing * 2
            radius: Kirigami.Units.cornerRadius * 2
            color: Kirigami.Theme.backgroundColor
            border.width: 1
            border.color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.2)

            MouseArea {
                anchors.fill: parent
                // Consume clicks inside dialog card
            }

            ColumnLayout {
                id: confirmCol
                anchors.fill: parent
                anchors.margins: Kirigami.Units.largeSpacing
                spacing: Kirigami.Units.mediumSpacing

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.Icon {
                        source: "dialog-warning"
                        Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                        Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                    }

                    PlasmaComponents3.Label {
                        Layout.fillWidth: true
                        text: root.pendingConfirm ? root.pendingConfirm.title : ""
                        font.bold: true
                        font.pointSize: Kirigami.Theme.defaultFont.pointSize + 1
                        wrapMode: Text.WordWrap
                    }
                }

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    text: root.pendingConfirm ? root.pendingConfirm.body : ""
                    wrapMode: Text.WordWrap
                    opacity: 0.88
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Item { Layout.fillWidth: true }

                    PlasmaComponents3.Button {
                        text: "取消"
                        onClicked: root.cancelPendingAction()
                    }

                    PlasmaComponents3.Button {
                        text: root.pendingConfirm ? root.pendingConfirm.confirmLabel : "确认"
                        highlighted: true
                        icon.name: (root.pendingConfirm && root.pendingConfirm.destructive) ? "system-reboot" : "dialog-ok-apply"
                        onClicked: root.confirmPendingAction()
                    }
                }
            }
        }
    }
}
