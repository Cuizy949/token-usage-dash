#!/usr/bin/env bash
# 构建 token-usage-dash.app（自包含 .app bundle，含 Python 采集层与图标资源）
#
# 用法：
#   scripts/build_app.sh               # 本机架构
#   scripts/build_app.sh --universal   # arm64 + x86_64 通用二进制
#   scripts/build_app.sh --dmg         # 追加打包 DMG（用于 Release 分发）
#
# 产物：build/token-usage-dash.app（+ 可选 build/token-usage-dash.dmg）
# 自编译产物不带 quarantine 属性，双击即开，Gatekeeper 不介入。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="token-usage-dash"
BUNDLE_ID="app.tokenusage.dash"
VERSION="0.1.0"
BUILD_DIR="$REPO/build"
APP="$BUILD_DIR/$APP_NAME.app"

UNIVERSAL=0; DMG=0
for arg in "$@"; do
  case "$arg" in
    --universal) UNIVERSAL=1 ;;
    --dmg) DMG=1 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" \
         "$APP/Contents/collectors" "$APP/Contents/assets/icons"

# ---- Swift 主程序（arm64 产物自动 ad-hoc 签名；x86_64 需显式 --签名=adhoc）----
ARCH_FLAGS=()
if [[ "$UNIVERSAL" == 1 ]]; then
  ARCH_FLAGS=(-arch arm64 -arch x86_64)
fi
xcrun swiftc -O -parse-as-library ${ARCH_FLAGS[@]+"${ARCH_FLAGS[@]}"} \
  "$REPO/dash/AiUsageDash.swift" -o "$APP/Contents/MacOS/$APP_NAME"
if [[ "$(uname -m)" == "x86_64" ]]; then
  codesign --force --sign - "$APP/Contents/MacOS/$APP_NAME"
fi

# ---- 数据桥 + 采集层 + 配置（bundle 内相对定位与仓库布局同构）----
# Swift 以 <execDir>/../collectors 与 <execDir>/../assets/icons 定位；
# Python 以 Contents/ 为 REPO 根 import aiusd_config。
cp "$REPO/dash/ai_usage_fetch.py"    "$APP/Contents/MacOS/"
cp "$REPO/aiusd_config.py"           "$APP/Contents/"
cp "$REPO"/collectors/*.py           "$APP/Contents/collectors/"
cp "$REPO"/assets/icons/*.png        "$APP/Contents/assets/icons/"
chmod +x "$APP/Contents/collectors/"*.py

# ---- Info.plist（LSUIElement：无 Dock 图标，与 setActivationPolicy(.accessory) 一致）----
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>Token Usage Dash</string>
    <key>CFBundleDisplayName</key>       <string>Token Usage Dash</string>
    <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key>        <string>$APP_NAME</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key>           <string>$VERSION</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSMinimumSystemVersion</key>    <string>14.0</string>
    <key>LSUIElement</key>               <true/>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>NSHumanReadableCopyright</key>  <string>MIT License</string>
</dict>
</plist>
PLIST

# ---- 图标（Assets/app-icon.png 1024px → icns；缺失则跳过）----
if [[ -f "$REPO/assets/app-icon.png" ]]; then
  ICONSET="$BUILD_DIR/$APP_NAME.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for size in 16 32 64 128 256 512 1024; do
    sips -z $size $size "$REPO/assets/app-icon.png" \
      --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    if [[ $size -le 512 ]]; then
      d=$((size * 2))
      sips -z $d $d "$REPO/assets/app-icon.png" \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    fi
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$ICONSET"
  /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist"
fi

codesign --force --sign - "$APP" 2>/dev/null || true   # bundle 级 ad-hoc（arm64 兜底）

echo "✓ $APP"
if [[ "$DMG" == 1 ]]; then
  DMG="$BUILD_DIR/$APP_NAME.dmg"
  rm -f "$DMG"
  hdiutil create -volname "$APP_NAME" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null
  echo "✓ $DMG  （浏览器下载会带 quarantine：需拖入 /Applications 后按 README 流程放行）"
fi
