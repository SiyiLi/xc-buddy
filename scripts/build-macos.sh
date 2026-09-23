#!/bin/bash
# Build XC Buddy for macOS as a universal app bundle.
#
# Produces:
#   build/XC-Buddy-<version>.app
#   build/XC-Buddy-<version>.zip
#   build/XC-Buddy-<version>.signature  (when Sparkle sign_update is available)
#
# Optional environment:
#   XC_BUDDY_APPCAST_URL=https://raw.githubusercontent.com/SiyiLi/xc-buddy/main/website/public/appcast.xml
#   SPARKLE_PUBLIC_ED_KEY=<public key from Sparkle generate_keys>
#   SPARKLE_PRIVATE_ED_KEY=<private key exported by Sparkle generate_keys -x>
#   CODESIGN_IDENTITY=<Developer ID identity; defaults to ad-hoc signing>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$SCRIPT_DIR/.."
DESKTOP_DIR="$ROOT_DIR/desktop/macos"
BUILD_DIR="$ROOT_DIR/build"
PLIST="$DESKTOP_DIR/Sources/XCBuddy/Info.plist"
VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
CONFIG="${1:---release}"
TARGET_ARCHS="arm64 x86_64"
DEFAULT_APPCAST_URL="https://raw.githubusercontent.com/SiyiLi/xc-buddy/main/website/public/appcast.xml"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

case "$CONFIG" in
    --release)
        SWIFT_CONFIG="release"
        REQUIRE_UPDATE_SIGNATURE=1
        ;;
    --debug)
        SWIFT_CONFIG="debug"
        REQUIRE_UPDATE_SIGNATURE=0
        ;;
    *)
        echo "Usage: $0 [--release|--debug]"
        exit 1
        ;;
esac

if [ -z "$VERSION" ]; then
    echo "Error: VERSION is empty"
    exit 1
fi

APPCAST_URL="${XC_BUDDY_APPCAST_URL:-$(/usr/libexec/PlistBuddy -c 'Print :SUFeedURL' "$PLIST")}"
SPARKLE_PUBLIC_KEY="${SPARKLE_PUBLIC_ED_KEY:-$(/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$PLIST")}"
APPCAST_URL="${APPCAST_URL:-$DEFAULT_APPCAST_URL}"

if [ "$REQUIRE_UPDATE_SIGNATURE" -eq 1 ]; then
    if [[ "$APPCAST_URL" != https://* ]]; then
        echo "Error: a release build requires an HTTPS Sparkle appcast URL."
        exit 1
    fi
    if ! [[ "$SPARKLE_PUBLIC_KEY" =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
        echo "Error: a release build requires a valid SPARKLE_PUBLIC_ED_KEY."
        echo "Generate it once with Sparkle's generate_keys tool."
        exit 1
    fi
fi

mkdir -p "$BUILD_DIR"

echo "===================================="
echo " XC Buddy macOS Build v$VERSION"
echo " Universal Binary: $TARGET_ARCHS"
echo "===================================="

for ARCH in $TARGET_ARCHS; do
    echo ""
    echo "Building XCBuddy for $ARCH..."
    SCRATCH="$DESKTOP_DIR/.build-$ARCH"
    rm -rf "$SCRATCH"
    swift build \
        --package-path "$DESKTOP_DIR" \
        -c "$SWIFT_CONFIG" \
        --arch "$ARCH" \
        --scratch-path "$SCRATCH"
done

APP_DIR="$BUILD_DIR/XC-Buddy-${VERSION}.app"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources" "$APP_DIR/Contents/Frameworks"

ARM_BUILD="$DESKTOP_DIR/.build-arm64/arm64-apple-macosx/$SWIFT_CONFIG"
X86_BUILD="$DESKTOP_DIR/.build-x86_64/x86_64-apple-macosx/$SWIFT_CONFIG"

echo ""
echo "Creating universal executable..."
lipo -create \
    "$ARM_BUILD/XCBuddy" \
    "$X86_BUILD/XCBuddy" \
    -output "$APP_DIR/Contents/MacOS/XCBuddy"

cp "$PLIST" "$APP_DIR/Contents/Info.plist"
BUNDLED_PLIST="$APP_DIR/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$BUNDLED_PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$BUNDLED_PLIST"
/usr/libexec/PlistBuddy -c "Set :SUFeedURL $APPCAST_URL" "$BUNDLED_PLIST"
if [ -n "$SPARKLE_PUBLIC_KEY" ]; then
    /usr/libexec/PlistBuddy -c "Set :SUPublicEDKey $SPARKLE_PUBLIC_KEY" "$BUNDLED_PLIST"
fi

ICON_PATH="$DESKTOP_DIR/Resources/AppIcon.icns"
if [ -f "$ICON_PATH" ]; then
    cp "$ICON_PATH" "$APP_DIR/Contents/Resources/AppIcon.icns"
else
    echo "WARNING: App icon was not found: $ICON_PATH"
fi

SPARKLE_FRAMEWORK="$(find -L "$DESKTOP_DIR/.build-arm64/artifacts" -name Sparkle.framework -type d 2>/dev/null | head -1 || true)"
if [ -n "$SPARKLE_FRAMEWORK" ]; then
    ditto "$SPARKLE_FRAMEWORK" "$APP_DIR/Contents/Frameworks/Sparkle.framework"
    install_name_tool -add_rpath "@loader_path/../Frameworks" "$APP_DIR/Contents/MacOS/XCBuddy" 2>/dev/null || true
else
    echo "Error: Sparkle.framework was not found in SwiftPM artifacts."
    exit 1
fi

SIGN_OPTIONS=()
if [ "$CODESIGN_IDENTITY" != "-" ]; then
    SIGN_OPTIONS+=(--options runtime --timestamp)
fi

sign_code() {
    local path="$1"
    shift
    if [ -e "$path" ]; then
        codesign --force "${SIGN_OPTIONS[@]}" "$@" \
            --sign "$CODESIGN_IDENTITY" "$path"
    fi
}

echo ""
echo "Signing app..."
xattr -cr "$APP_DIR" 2>/dev/null || true
if [ "$CODESIGN_IDENTITY" = "-" ]; then
    echo "Using ad-hoc signature."
else
    echo "Using: $CODESIGN_IDENTITY"
fi

SPARKLE_BUNDLE="$APP_DIR/Contents/Frameworks/Sparkle.framework"
SPARKLE_VERSION="$SPARKLE_BUNDLE/Versions/B"
sign_code "$SPARKLE_VERSION/XPCServices/Installer.xpc"
sign_code "$SPARKLE_VERSION/XPCServices/Downloader.xpc" \
    --preserve-metadata=entitlements
sign_code "$SPARKLE_VERSION/Autoupdate"
sign_code "$SPARKLE_VERSION/Updater.app"
sign_code "$SPARKLE_BUNDLE"
sign_code "$APP_DIR"

echo "Verifying app signature..."
codesign --verify --deep --strict --verbose=2 "$APP_DIR"

ZIP_PATH="$BUILD_DIR/XC-Buddy-${VERSION}.zip"
SIGNATURE_PATH="${ZIP_PATH%.zip}.signature"
STAGING_DIR="$BUILD_DIR/.sparkle-staging"
rm -rf "$STAGING_DIR" "$ZIP_PATH" "$SIGNATURE_PATH"
mkdir -p "$STAGING_DIR"
ditto --norsrc --noextattr "$APP_DIR" "$STAGING_DIR/XC Buddy.app"

echo ""
echo "Creating Sparkle ZIP..."
ditto -c -k --norsrc --noextattr --keepParent "$STAGING_DIR/XC Buddy.app" "$ZIP_PATH"
rm -rf "$STAGING_DIR"

SIGN_TOOL="$(find -L "$DESKTOP_DIR/.build-arm64/artifacts" -name sign_update -type f 2>/dev/null | head -1 || true)"
if [ "$REQUIRE_UPDATE_SIGNATURE" -eq 1 ]; then
    if [ -z "$SIGN_TOOL" ] || [ ! -x "$SIGN_TOOL" ]; then
        echo "Error: Sparkle sign_update tool was not found."
        exit 1
    fi
    echo "Signing Sparkle ZIP..."
    if [ -n "${SPARKLE_PRIVATE_ED_KEY:-}" ]; then
        if ! SIGN_OUTPUT="$(printf '%s' "$SPARKLE_PRIVATE_ED_KEY" | "$SIGN_TOOL" --ed-key-file - "$ZIP_PATH" 2>&1)"; then
            echo "Error: Sparkle could not sign the update archive."
            echo "$SIGN_OUTPUT"
            exit 1
        fi
    else
        if ! SIGN_OUTPUT="$("$SIGN_TOOL" "$ZIP_PATH" 2>&1)"; then
            echo "Error: Sparkle could not sign the update archive."
            echo "$SIGN_OUTPUT"
            exit 1
        fi
    fi
    ED_SIGNATURE="$(printf '%s\n' "$SIGN_OUTPUT" | sed -nE 's/.*sparkle:edSignature="([^"]+)".*/\1/p' | head -1)"
    if ! [[ "$ED_SIGNATURE" =~ ^[A-Za-z0-9+/]{86}==$ ]]; then
        echo "Error: Sparkle returned an invalid EdDSA signature."
        exit 1
    fi
    printf '%s\n' "$ED_SIGNATURE" > "$SIGNATURE_PATH"
fi

echo ""
echo "Build complete:"
echo "  App: $APP_DIR"
echo "  ZIP: $ZIP_PATH"
if [ -f "$SIGNATURE_PATH" ]; then
    echo "  Sig: $SIGNATURE_PATH"
fi
echo ""
if [ "$REQUIRE_UPDATE_SIGNATURE" -eq 1 ]; then
    echo "Next: attach the ZIP to the app release and update the appcast."
else
    echo "Debug builds do not produce a signed Sparkle update."
fi
