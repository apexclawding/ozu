#!/bin/bash
# Build WHISPER.app from icon.svg + launcher.sh + Info.plist and install it.
# Usage: macos/make-app.sh [destination]   (default: /Applications)
# Rerun after moving the repo or editing the icon/launcher. Needs only
# macOS built-ins (qlmanage, sips, iconutil, codesign) plus uv for the
# one Pillow step that restores the icon's transparent corners.
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(cd .. && pwd)"
DEST="${1:-/Applications}"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

# 1. Render the SVG at 1024px. QuickLook flattens transparency to white,
#    so restore the alpha channel with the same squircle the SVG draws.
qlmanage -t -s 1024 -o "$BUILD" icon.svg >/dev/null
uv run --with pillow python - "$BUILD" <<'EOF'
import sys
from PIL import Image, ImageDraw
build = sys.argv[1]
im = Image.open(f"{build}/icon.svg.png").convert("RGBA")
S = 4  # supersample for an antialiased edge
mask = Image.new("L", (1024 * S, 1024 * S), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [100 * S, 100 * S, 924 * S, 924 * S], radius=186 * S, fill=255)
im.putalpha(mask.resize((1024, 1024), Image.LANCZOS))
im.save(f"{build}/icon_1024.png")
EOF

# 2. Every Dock/Finder size, then compile the .icns.
mkdir "$BUILD/whisper.iconset"
for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" \
            "64 icon_32x32@2x" "128 icon_128x128" "256 icon_128x128@2x" \
            "256 icon_256x256" "512 icon_256x256@2x" "512 icon_512x512" \
            "1024 icon_512x512@2x"; do
    set -- $spec
    sips -z "$1" "$1" "$BUILD/icon_1024.png" \
        --out "$BUILD/whisper.iconset/$2.png" >/dev/null
done
iconutil -c icns "$BUILD/whisper.iconset" -o "$BUILD/whisper.icns"

# 3. Assemble the bundle with the repo path baked into the launcher.
APP="$BUILD/WHISPER.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
cp "$BUILD/whisper.icns" "$APP/Contents/Resources/whisper.icns"
sed "s|@APP_DIR@|$APP_DIR|" launcher.sh > "$APP/Contents/MacOS/WHISPER"
chmod +x "$APP/Contents/MacOS/WHISPER"
codesign --force -s - "$APP"

# 4. Install.
rm -rf "$DEST/WHISPER.app"
cp -R "$APP" "$DEST/"
echo "Installed $DEST/WHISPER.app (launches $APP_DIR)"
