import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: drawerRoot

    spacing: Kirigami.Units.smallSpacing

    readonly property var allApps: root.appsData.apps || []
    readonly property var filteredApps: {
        var q = searchField.text.trim().toLowerCase()
        if (!q) return allApps
        var out = []
        for (var i = 0; i < allApps.length; ++i) {
            var a = allApps[i]
            var n = String(a.name || "").toLowerCase()
            var p = String(a.package || "").toLowerCase()
            if (n.indexOf(q) >= 0 || p.indexOf(q) >= 0) {
                out.push(a)
            }
        }
        return out
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        PlasmaExtras.SearchField {
            id: searchField
            Layout.fillWidth: true
            placeholderText: "搜索应用或包名..."
        }

        PlasmaComponents3.ToolButton {
            icon.name: "view-refresh"
            onClicked: root.fetchApps(true)
            PlasmaComponents3.ToolTip.text: "刷新应用列表"
            PlasmaComponents3.ToolTip.visible: hovered
        }

        PlasmaComponents3.ToolButton {
            icon.name: "list-add"
            onClicked: root.openApkInstaller()
            PlasmaComponents3.ToolTip.text: "安装 APK"
            PlasmaComponents3.ToolTip.visible: hovered
        }
    }

    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true

        PlasmaExtras.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.largeSpacing * 2
            visible: drawerRoot.filteredApps.length === 0
            iconName: root.runtimeRunning ? "applications-all" : "system-shutdown"
            text: root.runtimeRunning ? "未找到匹配的 Android 应用" : "Android 容器未运行"
            explanation: root.runtimeRunning ? "" : "启动 Android 容器后即可加载已安装应用列表。"
        }

        PlasmaComponents3.ScrollView {
            anchors.fill: parent
            anchors.leftMargin: Kirigami.Units.smallSpacing
            anchors.rightMargin: Kirigami.Units.smallSpacing
            anchors.bottomMargin: Kirigami.Units.smallSpacing
            visible: drawerRoot.filteredApps.length > 0
            PlasmaComponents3.ScrollBar.horizontal.policy: PlasmaComponents3.ScrollBar.AlwaysOff
            clip: true

            GridView {
                id: appGrid
                model: drawerRoot.filteredApps
                cellWidth: Math.max(84, Math.floor(width / 4))
                cellHeight: 88

                delegate: Item {
                    required property var modelData
                    width: appGrid.cellWidth
                    height: appGrid.cellHeight

                    Rectangle {
                        anchors.fill: parent
                        anchors.margins: 3
                        radius: Kirigami.Units.cornerRadius * 1.5
                        color: tileMouse.containsMouse
                            ? Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.1)
                            : "transparent"

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 4
                            spacing: 4

                            Image {
                                visible: Boolean(modelData.icon_path)
                                source: modelData.icon_path || ""
                                Layout.preferredWidth: 40
                                Layout.preferredHeight: 40
                                Layout.alignment: Qt.AlignHCenter
                                fillMode: Image.PreserveAspectFit
                                smooth: true
                            }

                            Kirigami.Icon {
                                visible: !modelData.icon_path
                                source: "application-x-executable"
                                Layout.preferredWidth: 40
                                Layout.preferredHeight: 40
                                Layout.alignment: Qt.AlignHCenter
                            }

                            PlasmaComponents3.Label {
                                Layout.fillWidth: true
                                text: modelData.name || modelData.package
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                horizontalAlignment: Text.AlignHCenter
                                maximumLineCount: 2
                                wrapMode: Text.Wrap
                                elide: Text.ElideRight
                            }
                        }

                        MouseArea {
                            id: tileMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            cursorShape: Qt.PointingHandCursor
                            onClicked: function(mouse) {
                                if (mouse.button === Qt.RightButton) {
                                    ctxMenu.targetApp = modelData
                                    ctxMenu.popup()
                                } else {
                                    root.launchApp(modelData.package, modelData.component)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    PlasmaComponents3.Menu {
        id: ctxMenu
        property var targetApp: ({})

        PlasmaComponents3.MenuItem {
            text: "启动应用"
            icon.name: "media-playback-start"
            onTriggered: root.launchApp(ctxMenu.targetApp.package, ctxMenu.targetApp.component)
        }
        PlasmaComponents3.MenuItem {
            text: "强制停止"
            icon.name: "process-stop"
            onTriggered: root.stopApp(ctxMenu.targetApp.package)
        }
        PlasmaComponents3.MenuItem {
            text: "在 Droidloom 设置中配置..."
            icon.name: "configure"
            onTriggered: root.openSettings("apps", ctxMenu.targetApp.package)
        }
        PlasmaComponents3.MenuItem {
            text: "Android 应用信息"
            icon.name: "dialog-information"
            onTriggered: root.openAndroidAppInfo(ctxMenu.targetApp.package)
        }
    }
}
