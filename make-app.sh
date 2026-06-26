#!/bin/zsh
# Build (or rebuild) "MIST Console.app" — a double-clickable launcher for
# desktop.py (Flask + native WKWebView). Idempotent.
#
# Dock-icon correctness (the reason this is more than a one-line `uv run` shim):
# macOS ties a Dock tile to the running process's executable. `uv run` spawns a
# CHILD python (the uv-cache python3.13), a brand-new PID that is NOT the bundle,
# so the tile bound to ".../uv/.../python3.13". Keeping that in the Dock / quitting
# left a tile pointing at a bare binary -> "There is no application set to open the
# document python3.13". The fix proven by experiment: ship the interpreter INSIDE
# the bundle and `exec` it IN PLACE from Contents/MacOS/launch (CFBundleExecutable).
# exec keeps the same PID the bundle was launched as, so LaunchServices records
# bundle path = MIST Console.app. desktop.py stays a plain editable script.
set -e

PROJ="/Users/alexhedtke/Documents/mist-console"
APP="$HOME/Desktop/Apps/MIST Console.app"
ICON_SRC="/Users/alexhedtke/Documents/mist-console/static/mist-logo.png"  # MIST logo (from ~/Downloads/MIST.png)

echo "Building $APP ..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- bundle a self-contained CPython (so the GUI process lives in the .app) ----
# Copy the uv-managed standalone interpreter tree (relocatable; loads its dylib +
# stdlib via @executable_path/../lib, so it works at any path incl. one w/ spaces).
PYBASE="$(dirname "$(dirname "$(uv python find 3.13)")")"
# `uv python find` can resolve to an ephemeral venv; walk to the real standalone.
case "$PYBASE" in
  *"/uv/python/"*) ;;                                  # already the standalone
  *) PYBASE="$(/bin/ls -d /Users/alexhedtke/.local/share/uv/python/cpython-3.13* 2>/dev/null | sort | tail -1)" ;;
esac
echo "  bundling python from: $PYBASE"
cp -R "$PYBASE" "$APP/Contents/Resources/python"
BUNDLE_PY="$APP/Contents/Resources/python/bin/python3.13"
echo "  installing deps (flask pywebview setproctitle) into the bundle ..."
"$BUNDLE_PY" -m pip install -q --disable-pip-version-check --break-system-packages \
  flask pywebview setproctitle >/dev/null

cat > "$APP/Contents/MacOS/launch" <<EOF
#!/bin/zsh
# exec the in-bundle python IN PLACE (same PID as the bundle launch) so the Dock
# tile stays "MIST Console.app", never the bare interpreter. Do NOT use uv run here
# (it forks a child PID and re-breaks the Dock association).
HERE="\${0:A:h}"
cd "$PROJ" || exit 1
exec "\$HERE/../Resources/python/bin/python3.13" desktop.py >> "$PROJ/desktop.log" 2>&1
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
