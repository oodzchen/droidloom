import QtQuick
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Item {
    id: compactRoot

    implicitWidth: Kirigami.Units.iconSizes.medium
    implicitHeight: Kirigami.Units.iconSizes.medium

    Kirigami.Icon {
        id: appIcon
        anchors.centerIn: parent
        width: Math.min(parent.width, parent.height) * 0.9
        height: width
        source: Qt.resolvedUrl("../icons/droidloom-symbolic.svg")
        isMask: true
        color: Kirigami.Theme.textColor
        opacity: root.runtimeRunning ? 1.0 : 0.52
    }

    // Optional unread notifications badge (off by default)
    Rectangle {
        visible: Plasmoid.configuration.showNotificationBadge && root.unreadCount > 0
        height: Math.max(13, Math.round(appIcon.height * 0.42))
        width: Math.max(height, badgeText.implicitWidth + 6)
        radius: height / 2
        anchors.top: appIcon.top
        anchors.right: appIcon.right
        anchors.topMargin: -1
        anchors.rightMargin: -1
        color: Kirigami.Theme.highlightColor

        PlasmaComponents3.Label {
            id: badgeText
            anchors.centerIn: parent
            text: root.unreadCount > 99 ? "99+" : String(root.unreadCount)
            font.pixelSize: Math.max(8, Math.round(parent.height * 0.65))
            font.bold: true
            color: Kirigami.Theme.highlightedTextColor
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        onClicked: root.expanded = !root.expanded
    }
}
