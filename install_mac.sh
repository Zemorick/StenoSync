#!/bin/bash
# Creates a macOS launcher app for StenoSync in ~/Applications.
#
# Unlike a PyInstaller build (which freezes a copy of the code into the
# .app), this launcher runs stenosync.py straight from this repo folder —
# so after a `git pull`, the next launch IS the new version. Run once;
# re-run only if you move the repo or change Python installs.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="StenoSync"
PY="$(command -v python3)"

if [ -z "$PY" ]; then
    echo "python3 not found on PATH" >&2
    exit 1
fi
if ! "$PY" -c "import PyQt6" 2>/dev/null; then
    echo "PyQt6 not installed for $PY — run: pip3 install PyQt6" >&2
    exit 1
fi

APP_DIR="$HOME/Applications/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"

echo "Creating launcher app at $APP_DIR..."
mkdir -p "$MACOS"

cat > "$MACOS/$APP_NAME" << LAUNCHER
#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH"
export QT_MAC_WANTS_LAYER=1
cd "$SCRIPT_DIR"
exec /usr/bin/arch -arm64 "$PY" "$SCRIPT_DIR/stenosync.py" 2>>"$SCRIPT_DIR/crash.log"
LAUNCHER
chmod +x "$MACOS/$APP_NAME"

cat > "$CONTENTS/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>StenoSync</string>
    <key>CFBundleDisplayName</key>
    <string>StenoSync</string>
    <key>CFBundleExecutable</key>
    <string>StenoSync</string>
    <key>CFBundleIdentifier</key>
    <string>com.zemorick.stenosync</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo "Done. $APP_DIR now launches the live repo code."
echo "You can delete any old PyInstaller build (dist/, build/, *.spec)."
