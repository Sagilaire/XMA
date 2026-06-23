#!/usr/bin/env bash
# Container entrypoint: lazily downloads /tools on first run and forwards
# to uvicorn. All downloaded artifacts persist in the /tools volume so that
# subsequent boots are near-instant.

set -euo pipefail

: "${TOOLS_DIR:=/tools}"
: "${ANDROID_BUILD_TOOLS_VERSION:=34.0.0}"
: "${APKTOOL_VERSION:=2.10.0}"

mkdir -p "$TOOLS_DIR"

log() { printf "[entrypoint] %s\n" "$*"; }

# --- apktool.jar -------------------------------------------------------------------
if [ ! -s "$TOOLS_DIR/apktool.jar" ]; then
    log "Downloading apktool ${APKTOOL_VERSION}..."
    wget -q \
        "https://github.com/iBotPeaches/Apktool/releases/download/v${APKTOOL_VERSION}/apktool_${APKTOOL_VERSION}.jar" \
        -O "$TOOLS_DIR/apktool.jar"
fi

# --- Android SDK build-tools -------------------------------------------------------
# We install under $TOOLS_DIR/android-sdk/build-tools/<version>/. apktool,
# zipalign and apksigner are all referenced by the absolute path inside the
# build-tools directory (see app/xapk_processor.py) so we DO NOT copy the
# binaries: modern build-tools ship apksigner as a Python wrapper that
# needs sibling apksigner.jar, and copying the wrapper alone breaks signing.
NEED_BUILD_TOOLS=0
BT_DIR="$TOOLS_DIR/android-sdk/build-tools/${ANDROID_BUILD_TOOLS_VERSION}"
if [ ! -d "$BT_DIR" ] || [ ! -x "$BT_DIR/zipalign" ] || [ ! -x "$BT_DIR/apksigner" ]; then
    NEED_BUILD_TOOLS=1
fi

if [ "$NEED_BUILD_TOOLS" = "1" ]; then
    log "Installing Android SDK command-line tools + build-tools ${ANDROID_BUILD_TOOLS_VERSION}..."
    ANDROID_SDK="$TOOLS_DIR/android-sdk"
    CMDLINE_TOOLS_DIR="$ANDROID_SDK/cmdline-tools/latest"
    mkdir -p "$ANDROID_SDK/cmdline-tools"

    if [ ! -d "$CMDLINE_TOOLS_DIR" ]; then
        TMP_ZIP="$(mktemp /tmp/cmdline-tools-XXXX.zip)"
        wget -q "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip" -O "$TMP_ZIP"
        unzip -q "$TMP_ZIP" -d "$ANDROID_SDK/cmdline-tools"
        # The bundle extracts to .../cmdline-tools/cmdline-tools; promote to .../latest.
        if [ -d "$ANDROID_SDK/cmdline-tools/cmdline-tools" ]; then
            rm -rf "$CMDLINE_TOOLS_DIR"
            mv "$ANDROID_SDK/cmdline-tools/cmdline-tools" "$CMDLINE_TOOLS_DIR"
        fi
        rm -f "$TMP_ZIP"
    fi

    export ANDROID_HOME="$ANDROID_SDK"
    export PATH="$PATH:$CMDLINE_TOOLS_DIR/bin"

    if [ ! -d "$BT_DIR" ]; then
        # Accept all licenses and install only what we need.
        yes | sdkmanager --sdk_root="$ANDROID_SDK" --licenses >/dev/null 2>&1 || true
        sdkmanager --sdk_root="$ANDROID_SDK" --install "build-tools;${ANDROID_BUILD_TOOLS_VERSION}" >/dev/null 2>&1
    fi
fi

# --- debug.keystore ----------------------------------------------------------------
if [ ! -s "$TOOLS_DIR/debug.keystore" ]; then
    log "Generating debug.keystore..."
    keytool -genkeypair \
        -keystore "$TOOLS_DIR/debug.keystore" \
        -storepass android \
        -alias androiddebugkey \
        -keypass android \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=Android Debug,O=Android,C=US"
fi

# --- Temp directory ----------------------------------------------------------------
mkdir -p "${TEMP_DIR:-/temp/workflows}"

log "Tools ready in ${TOOLS_DIR}."
exec "$@"
