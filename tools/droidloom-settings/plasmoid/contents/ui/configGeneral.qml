import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: cfgRoot

    property alias cfg_showNotificationBadge: badgeCheck.checked
    property alias cfg_closeOnAppLaunch: closeOnLaunchCheck.checked
    property alias cfg_defaultTab: defaultTabCombo.currentIndex
    property alias cfg_showSystemTasks: showSysCheck.checked

    Kirigami.FormLayout {
        PlasmaComponents3.CheckBox {
            id: badgeCheck
            Kirigami.FormData.label: "通知角标:"
            text: "在面板图标上显示未读通知数量角标"
        }

        PlasmaComponents3.CheckBox {
            id: closeOnLaunchCheck
            Kirigami.FormData.label: "启动行为:"
            text: "点击启动应用后自动收起控制中心"
        }

        PlasmaComponents3.ComboBox {
            id: defaultTabCombo
            Kirigami.FormData.label: "默认标签页:"
            model: ["控制中心（快捷开关与通知）", "后台任务", "全部应用"]
        }

        PlasmaComponents3.CheckBox {
            id: showSysCheck
            Kirigami.FormData.label: "后台列表:"
            text: "默认显示 Android 核心系统服务"
        }

        PlasmaComponents3.Button {
            Kirigami.FormData.label: "快捷开关:"
            text: "恢复默认快捷开关布局"
            icon.name: "edit-undo"
            onClicked: {
                Plasmoid.configuration.quickTiles = [
                    "runtime", "night_mode", "dnd", "rotation",
                    "session_mode", "games_driver", "mic_capture", "android_settings"
                ]
            }
        }

        PlasmaComponents3.Label {
            text: "提示：可直接在控制中心点击「编辑」自由添加、删除和排序最多 21 项快捷开关。"
            opacity: 0.72
            wrapMode: Text.WordWrap
        }
    }
}
