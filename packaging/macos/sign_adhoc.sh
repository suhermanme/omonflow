#!/bin/sh
# Ad-hoc signing for development builds (SPEC §18): no Developer ID, no
# notarization. macOS ties privacy permissions to this signature, so each
# rebuild may ask for Microphone/Accessibility again.
# Release builds will replace "-" with a Developer ID identity and add
# --options runtime --entitlements packaging/macos/entitlements.plist + notarytool.
set -eu
APP="${1:-dist/OmonFlow.app}"
codesign --force --deep --sign - --identifier io.github.suhermanme.omonflow "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature|Format"
