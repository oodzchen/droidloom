#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
BIN_DIR="$HOME/.local/bin"
APP_SHARE="$DATA_HOME/droidloom-settings"
APPS_DIR="$DATA_HOME/applications"
HICOLOR_DIR="$DATA_HOME/icons/hicolor"
MIME_DIR="$DATA_HOME/mime"
MIME_PKG_DIR="$MIME_DIR/packages"
USER_UNIT_DIR="$CONFIG_HOME/systemd/user"

ICON_NAME="org.droidloom.Settings"
APK_MIME="application/vnd.android.package-archive"
APK_MIME_ICON="application-vnd.android.package-archive"
ICON_SIZES=(16 22 24 32 48 64 96 128 192 256 512)
HIDPI_PAIRS=("16:32" "24:48" "32:64" "48:96" "64:128" "128:256" "256:512")

mkdir -p "$BIN_DIR" "$APP_SHARE" "$APPS_DIR" "$HICOLOR_DIR" "$MIME_PKG_DIR" "$USER_UNIT_DIR"

# Install self-contained Python package copy and bundled assets into ~/.local/share/droidloom-settings
rm -rf "$APP_SHARE/droidloom_settings" "$APP_SHARE/assets"
cp -a "$SCRIPT_DIR/droidloom_settings" "$APP_SHARE/droidloom_settings"
cp -a "$SCRIPT_DIR/assets" "$APP_SHARE/assets"
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

if [ ! -f "$HICOLOR_DIR/index.theme" ] && [ -f /usr/share/icons/hicolor/index.theme ]; then
  install -m 0644 /usr/share/icons/hicolor/index.theme "$HICOLOR_DIR/index.theme"
fi

# Ensure pre-rendered PNG icons exist if missing
missing_png=0
for s in "${ICON_SIZES[@]}"; do
  if [ ! -f "$SCRIPT_DIR/assets/icons/hicolor/${s}x${s}/apps/${ICON_NAME}.png" ]; then
    missing_png=1
    break
  fi
done
if [ "$missing_png" -eq 1 ]; then
  /usr/bin/python3 "$SCRIPT_DIR/scripts/generate_icons.py"
fi

# Install scalable SVG icons (apps + mimetypes)
mkdir -p "$HICOLOR_DIR/scalable/apps" "$HICOLOR_DIR/scalable/mimetypes"
install -m 0644 "$SCRIPT_DIR/assets/${ICON_NAME}.svg" \
  "$HICOLOR_DIR/scalable/apps/${ICON_NAME}.svg"
install -m 0644 "$SCRIPT_DIR/assets/${ICON_NAME}.svg" \
  "$HICOLOR_DIR/scalable/mimetypes/${ICON_NAME}.svg"
install -m 0644 "$SCRIPT_DIR/assets/${ICON_NAME}.svg" \
  "$HICOLOR_DIR/scalable/mimetypes/${APK_MIME_ICON}.svg"

# Install multi-size crisp PNG icons (apps + mimetypes)
for s in "${ICON_SIZES[@]}"; do
  src_png="$SCRIPT_DIR/assets/icons/hicolor/${s}x${s}/apps/${ICON_NAME}.png"
  mkdir -p "$HICOLOR_DIR/${s}x${s}/apps" "$HICOLOR_DIR/${s}x${s}/mimetypes"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${s}x${s}/apps/${ICON_NAME}.png"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${s}x${s}/mimetypes/${ICON_NAME}.png"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${s}x${s}/mimetypes/${APK_MIME_ICON}.png"
done

# Install @2 HiDPI PNG icons (apps + mimetypes)
for pair in "${HIDPI_PAIRS[@]}"; do
  logical="${pair%%:*}"
  px="${pair##*:}"
  src_png="$SCRIPT_DIR/assets/icons/hicolor/${px}x${px}/apps/${ICON_NAME}.png"
  mkdir -p "$HICOLOR_DIR/${logical}x${logical}@2/apps" "$HICOLOR_DIR/${logical}x${logical}@2/mimetypes"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${logical}x${logical}@2/apps/${ICON_NAME}.png"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${logical}x${logical}@2/mimetypes/${ICON_NAME}.png"
  install -m 0644 "$src_png" "$HICOLOR_DIR/${logical}x${logical}@2/mimetypes/${APK_MIME_ICON}.png"
done

# Install Shared MIME-info icon override for .apk files
install -m 0644 "$SCRIPT_DIR/assets/org.droidloom.ApkInstaller.xml" \
  "$MIME_PKG_DIR/org.droidloom.ApkInstaller.xml"

sed "s|@SETTINGS_EXEC@|$SETTINGS_BIN|g" \
  "$SCRIPT_DIR/assets/org.droidloom.Settings.desktop" > "$APPS_DIR/org.droidloom.Settings.desktop"
chmod 0644 "$APPS_DIR/org.droidloom.Settings.desktop"

sed "s|@INSTALLER_EXEC@|$INSTALLER_BIN|g" \
  "$SCRIPT_DIR/assets/org.droidloom.ApkInstaller.desktop" > "$APPS_DIR/org.droidloom.ApkInstaller.desktop"
chmod 0644 "$APPS_DIR/org.droidloom.ApkInstaller.desktop"

sed "s|@SETTINGS_EXEC@|$SETTINGS_BIN|g" \
  "$SCRIPT_DIR/assets/droidloom-orientation-sync.service" > "$USER_UNIT_DIR/droidloom-orientation-sync.service"
chmod 0644 "$USER_UNIT_DIR/droidloom-orientation-sync.service"

if command -v update-mime-database >/dev/null 2>&1; then
  update-mime-database "$MIME_DIR" || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HICOLOR_DIR" >/dev/null 2>&1 || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime default org.droidloom.ApkInstaller.desktop "$APK_MIME"
fi
rm -f "$CACHE_HOME/icon-cache.kcache"
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 || kbuildsycoca6 >/dev/null 2>&1 || true
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

mime_icon_line="$(grep -m1 "^${APK_MIME}:" "$MIME_DIR/icons" 2>/dev/null || echo unknown)"
echo "Installed Droidloom Settings to $SETTINGS_BIN"
echo "Installed Droidloom APK Installer to $INSTALLER_BIN"
echo "Default APK handler: $(xdg-mime query default "$APK_MIME" 2>/dev/null || echo unknown)"
echo "APK MIME icon: $mime_icon_line"
