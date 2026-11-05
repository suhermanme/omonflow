#!/bin/sh
# Compiles AppIcon.icon (Icon Composer document: light, dark, tinted and clear
# appearances) into the files the app bundle uses. Needs Xcode 26+; the
# compiled output in icon/ is committed so builds without Xcode still work.
#   Assets.car   -> CFBundleIconName (macOS 26+ draws the right appearance)
#   AppIcon.icns -> CFBundleIconFile (older macOS)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/icon"
rm -rf "$OUT" && mkdir -p "$OUT"
xcrun actool "$HERE/AppIcon.icon" --compile "$OUT" --platform macosx --minimum-deployment-target 13.0 \
  --app-icon AppIcon --output-partial-info-plist "$OUT/partial.plist" --errors --warnings >/dev/null
rm -f "$OUT/partial.plist"
ls -l "$OUT"
