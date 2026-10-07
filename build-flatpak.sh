#!/bin/bash
# Build and install DesuIndex as a Flatpak.
#
# FIRST-TIME SETUP (run once):
#   Fedora:        sudo dnf install flatpak-builder
#   Ubuntu/Debian: sudo apt install flatpak flatpak-builder
#   The GNOME runtime/SDK named in the manifest are installed automatically.
#
# BUILD & INSTALL on this machine:
#   ./build-flatpak.sh
#
# BUILD A SINGLE-FILE INSTALLER for another machine (same CPU architecture):
#   ./build-flatpak.sh --bundle          → desuindex-<version>-<arch>.flatpak
#   then, on the other machine:  flatpak install --user desuindex-<version>-<arch>.flatpak
#
# RUN:
#   flatpak run io.github.hexvi.DesuIndex
#
# UNINSTALL:
#   flatpak uninstall --user io.github.hexvi.DesuIndex
#
# PERMISSIONS:
#   By default the app can only access ~/Pictures. Folders picked in the app's
#   file dialogs are still reachable through the portal. To grant more, e.g.:
#     flatpak override --user --filesystem=home io.github.hexvi.DesuIndex
#   (or use Flatseal). Undo with: flatpak override --user --reset io.github.hexvi.DesuIndex
#
# PYTHON DEPENDENCIES:
#   The build is fully offline: every wheel is pinned with its sha256 in
#   python3-requirements.json. After changing requirements-flatpak.txt or the
#   runtime version, regenerate it with ./generate-python-deps.py

set -euo pipefail
cd "$(dirname "$0")"

APP_ID="io.github.hexvi.DesuIndex"
MANIFEST="${APP_ID}.json"
BUILD_DIR=".flatpak-build"
REPO_DIR=".flatpak-repo"
FLATHUB_REPO="https://flathub.org/repo/flathub.flatpakrepo"

BUNDLE=false
[[ "${1:-}" == "--bundle" ]] && BUNDLE=true

# The metainfo's newest release is the single source of truth for the version.
APP_VERSION=$(python3 -c 'import sys, xml.etree.ElementTree as ET; print(ET.parse(sys.argv[1]).find("releases/release").get("version"))' "${APP_ID}.metainfo.xml")

# ── prerequisite checks ──────────────────────────────────────────────────────

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: '$1' not found. Install with: $2"
        exit 1
    fi
}

check_cmd flatpak         "sudo dnf install flatpak  (Ubuntu/Debian: sudo apt install flatpak)"
check_cmd flatpak-builder "sudo dnf install flatpak-builder  (Ubuntu/Debian: sudo apt install flatpak-builder)"

# The GNOME runtime and SDK come from Flathub; flatpak-builder installs or
# updates them itself (--install-deps-from).
flatpak remote-add --if-not-exists --user flathub "$FLATHUB_REPO"

# ── pinned python sources ────────────────────────────────────────────────────

if [[ ! -f python3-requirements.json ]]; then
    echo ">>> python3-requirements.json missing — generating …"
    ./generate-python-deps.py
fi

# ── bundle ───────────────────────────────────────────────────────────────────

if $BUNDLE; then
    BUNDLE_FILE="desuindex-${APP_VERSION}-$(flatpak --default-arch).flatpak"
    echo ">>> Building $APP_ID $APP_VERSION into $REPO_DIR …"
    flatpak-builder --user --install-deps-from=flathub --force-clean \
        --repo="$REPO_DIR" "$BUILD_DIR" "$MANIFEST"
    echo ">>> Writing $BUNDLE_FILE …"
    # --runtime-repo lets the other machine fetch the GNOME runtime from Flathub.
    flatpak build-bundle --runtime-repo="$FLATHUB_REPO" "$REPO_DIR" "$BUNDLE_FILE" "$APP_ID"
    echo ""
    echo "══════════════════════════════════════════"
    echo "  Bundle: $BUNDLE_FILE ($(du -h "$BUNDLE_FILE" | cut -f1))"
    echo "  Install on another machine with:"
    echo "    flatpak install --user $BUNDLE_FILE"
    echo "══════════════════════════════════════════"
    exit 0
fi

# ── build ────────────────────────────────────────────────────────────────────

echo ">>> Building $APP_ID …"
flatpak-builder --user --install-deps-from=flathub --install --force-clean \
    "$BUILD_DIR" "$MANIFEST"

echo ""
echo "══════════════════════════════════════════"
echo "  Done! Launch with:"
echo "    flatpak run $APP_ID"
echo ""
echo "  The first run downloads the AI model (~1.3 GB)."
echo "  Sorted images go to ~/Pictures/Sorted/<expression>/ (changeable in the app)"
echo "══════════════════════════════════════════"
