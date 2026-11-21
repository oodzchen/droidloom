# Droidloom Settings & APK Installer (`droidloom-settings`)

PySide6 (Qt 6) desktop control center, graphical APK installer, `.apk` MIME handler, and host accelerometer orientation sync daemon for Droidloom.

## Features

- **Overview & Lifecycle**: Real-time runtime telemetry (Android version, uptime, ready time, memory/swap, CPU usage, process counts), desktop/mobile session mode switching, systemd autostart toggle, ADB status/reconnect, and full storage backup/restore.
- **Network Translation & Proxy Bridge**:
  - One-click host proxy bridge (auto-detects local proxy listeners, inserts live `nftables` allow rules in `inet droidloom_u<uid> input`, and sets Android `http_proxy`).
  - Custom `CellSpec.network` configuration: NAT vs Offline sandbox mode, gateway/cell IPv4 & prefix, custom `wlan0` MAC, custom DHCP DNS (up to 4 servers), allowed host ports, and DNAT port forwarding (`host_port -> cell_port`).
  - Android Private DNS (DoT) and host IPv4 forwarding / `firewalld` checks.
- **Storage & Shared Folder Mapping**:
  - Toggle and preview XDG user folders (`Downloads -> Download`, `Pictures`, `Documents`, `Music`, `Videos -> Movies`).
  - Manage custom `shared_storage_directories`, check `data_dir` disk usage, configure `gapps_dir`, inspect overrides, and trigger Android `MediaScanner`.
- **Hardware, Spoofing, Audio & Resource Limits**:
  - Host DRM GPU detection, `gpu`/`graphics_backend`/`render_node` selection, and live game GL driver toggle (`ANGLE` vs `Mesa`).
  - SoC / ANGLE GL persistent property spoofing and `CellSpec.device_identity` overrides.
  - PipeWire audio capture permission & sink routing (`droidloom-audio`), `droidloom-braun` camera status, `droidloom-devices` status.
  - Systemd cgroup v2 limits (`CPUQuota`, `AllowedCPUs`, `CPUWeight`, `MemoryHigh`, `MemoryMax`, `MemorySwapMax`, `TasksMax`, `IOWeight`).
- **Display, Multi-Monitor DPI & Gravity/Orientation Simulation**:
  - Interactive 0°/90°/180°/270° rotation dial, lock/free rotation, `wm fixed-to-user-rotation` enforcement, and window axis swap.
  - Optional background `droidloom-orientation-sync.service` tracking host `iio-sensor-proxy` accelerometer orientation.
  - Default window policy (`window-policy-v1.json`), hot-reloaded per-output `density-v1.json`, built-in DPI, dark mode, font scale, and animation speed tweaks.
- **Graphical APK Installer & Application Manager**:
  - Pure-Python APK inspector (name, package, version, icon, SDK, permissions, ABI compatibility, split-bundle detection) with drag-and-drop and live installation progress.
  - Registers `org.droidloom.ApkInstaller.desktop` as the default handler for `application/vnd.android.package-archive` (`.apk`).
  - Installed app browser with icons, size details, force-stop/clear cache/clear data/uninstall, and per-app window size + `X-Droidloom-Resolution` configuration.
- **Diagnostics**: Live Android logcat & crash buffer viewer, `droidloom-doctor` check table, and systemd journal viewer.

## User Installation

```bash
bash tools/droidloom-settings/install-user.sh
```

This installs `droidloom-settings` and `droidloom-apk-installer` into `~/.local/bin/`, desktop entries into `~/.local/share/applications/`, the icon into `~/.local/share/icons/`, the orientation sync service into `~/.config/systemd/user/`, and sets `org.droidloom.ApkInstaller.desktop` as the default `.apk` opener.
