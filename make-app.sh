#!/bin/zsh
# Build (or rebuild) "MIST Console.app" — a double-clickable launcher that runs
# desktop.py (Flask + native WKWebView) via uv. Idempotent.
set -e

PROJ="/Users/alexhedtke/Documents/mist-console"
APP="$HOME/Desktop/Apps/MIST Console.app"
ICON_SRC="/Users/alexhedtke/Documents/mist-console/static/mist-logo.png"  # MIST logo (from ~/Downloads/MIST.png)

echo "Building $APP ..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/MacOS/launch" <<EOF
#!/bin/zsh
cd "$PROJ" || exit 1
export PATH="/Users/alexhedtke/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
exec uv run --script desktop.py >> "$PROJ/desktop.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/launch"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MIST Console</string>
  <key>CFBundleDisplayName</key><string>MIST Console</string>
  <key>CFBundleIdentifier</key><string>com.exobrain.mist-console</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Icon from the evolved-form portrait (optional; skip cleanly on any failure).
if [ -f "$ICON_SRC" ]; then
  TMP="$(mktemp -d)"
  if sips -s format png "$ICON_SRC" --out "$TMP/src.png" >/dev/null 2>&1; then
    W=$(sips -g pixelWidth  "$TMP/src.png" | awk '/pixelWidth/{print $2}')
    H=$(sips -g pixelHeight "$TMP/src.png" | awk '/pixelHeight/{print $2}')
    S=$(( W > H ? W : H ))
    sips --padToHeightWidth "$S" "$S" --padColor 0E1C2B "$TMP/src.png" --out "$TMP/sq.png" >/dev/null 2>&1
    ICONSET="$TMP/MIST.iconset"; mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
      sips -z $sz $sz "$TMP/sq.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
      sips -z $((sz*2)) $((sz*2)) "$TMP/sq.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" >/dev/null 2>&1 && echo "  icon: built from mist-logo.png"
  fi
  rm -rf "$TMP"
fi
[ -f "$APP/Contents/Resources/AppIcon.icns" ] || echo "  icon: skipped (default)"
touch "$APP"
echo "Done. Double-click ~/Desktop/Apps/MIST Console.app"
