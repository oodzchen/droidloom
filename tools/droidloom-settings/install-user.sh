#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN_DIR="$HOME/.local/bin"
APP_SHARE="$DATA_HOME/droidloom-settings"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
USER_UNIT_DIR="$CONFIG_HOME/systemd/user"

mkdir -p "$BIN_DIR" "$APP_SHARE" "$APPS_DIR" "$ICON_DIR" "$USER_UNIT_DIR"

# Install self-contained Python package copy into ~/.local/share/droidloom-settings
rm -rf "$APP_SHARE/droidloom_settings"
cp -a "$SCRIPT_DIR/droidloom_settings" "$APP_SHARE/droidloom_settings"
find "$APP_SHARE/droidloom_settings" -type d -name '__pycache__' -exec rm -rf {} +

SETTINGS_BIN="$BIN_DIR/droidloom-settings"
INSTALLER_BIN="$BIN_DIR/droidloom-apk-installer"

cat > "$SETTINGS_BIN" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$APP_SHARE\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -B -m droidloom_settings.main "\$@"
EOF
chmod 0755 "$SETTINGS_BIN"

cat > "$INSTALLER_BIN" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$APP_SHARE\${PYTHONPATH:+:\$PYTHONPATH}"
if [ "\$#" -eq 0 ]; then
  exec /usr/bin/python3 -B -m droidloom_settings.main --page apps
else
  exec /usr/bin/python3 -B -m droidloom_settings.main --install-apk "\$@"
fi
EOF
chmod 0755 "$INSTALLER_BIN"

install -m 0644 "$SCRIPT_DIR/assets/org.droidloom.Settings.svg" \
  "$ICON_DIR/org.droidloom.Settings.svg"

sed "s|@SETTINGS_EXEC@|$SETTINGS_BIN|g" \
  "$SCRIPT_DIR/assets/org.droidloom.Settings.desktop" > "$APPS_DIR/org.droidloom.Settings.desktop"
chmod 0644 "$APPS_DIR/org.droidloom.Settings.desktop"

sed "s|@INSTALLER_EXEC@|$INSTALLER_BIN|g" \
  "$SCRIPT_DIR/assets/org.droidloom.ApkInstaller.desktop" > "$APPS_DIR/org.droidloom.ApkInstaller.desktop"
chmod 0644 "$APPS_DIR/org.droidloom.ApkInstaller.desktop"

sed "s|@SETTINGS_EXEC@|$SETTINGS_BIN|g" \
  "$SCRIPT_DIR/assets/droidloom-orientation-sync.service" > "$USER_UNIT_DIR/droidloom-orientation-sync.service"
chmod 0644 "$USER_UNIT_DIR/droidloom-orientation-sync.service"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime default org.droidloom.ApkInstaller.desktop application/vnd.android.package-archive
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 >/dev/null 2>&1 || true
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

echo "Installed Droidloom Settings to $SETTINGS_BIN"
echo "Installed Droidloom APK Installer to $INSTALLER_BIN"
echo "Default APK handler: $(xdg-mime query default application/vnd.android.package-archive 2>/dev/null || echo unknown)"
