#!/usr/bin/env bash
# token-usage-dash 一行安装/升级（第四轨：curl 下载不打 quarantine，零 Gatekeeper 摩擦）
#
#   curl -fsSL https://raw.githubusercontent.com/Cuizy949/token-usage-dash/main/scripts/install.sh | bash
#
# 行为：查 GitHub 最新 Release → 下载 DMG → 退出运行中的旧实例 → 整体替换
#       /Applications/token-usage-dash.app → 卸载 DMG。curl 下载的文件不带
#       quarantine 属性，安装后直接双击/open 启动，无需 Gatekeeper 放行。
set -euo pipefail

REPO_SLUG="Cuizy949/token-usage-dash"
APP_NAME="token-usage-dash"
DEST="/Applications/$APP_NAME.app"
DMG_URL="https://github.com/$REPO_SLUG/releases/latest/download/$APP_NAME.dmg"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ausd-install.XXXXXX")"
cleanup() { hdiutil detach "$TMP/mnt" -force >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

echo "↓ 下载最新 Release..."
curl -fL --progress-bar -o "$TMP/app.dmg" "$DMG_URL"

echo "→ 校验 SHA256..."
SHA_URL="https://github.com/$REPO_SLUG/releases/latest/download/$APP_NAME.dmg.sha256"
if curl -fsL -o "$TMP/app.dmg.sha256" "$SHA_URL"; then
  (cd "$TMP" && shasum -a 256 -c --status app.dmg.sha256) \
    || { echo "✗ SHA256 校验失败，中止" >&2; exit 1; }
  echo "  ✓ 校验通过"
else
  echo "  ⚠️ Release 未附 sha256（旧版本？），跳过校验"
fi

# 先挂载、确认卷内 app 存在，再动 /Applications 里的旧版（防半安装）
hdiutil attach -nobrowse -readonly -mountpoint "$TMP/mnt" "$TMP/app.dmg" >/dev/null
[[ -d "$TMP/mnt/$APP_NAME.app" ]] || { echo "✗ DMG 内未找到 $APP_NAME.app" >&2; exit 1; }

echo "→ 安装到 $DEST ..."
if pgrep -f "$DEST/Contents/MacOS" >/dev/null 2>&1; then
  echo "  退出运行中的旧实例..."
  pkill -f "$DEST/Contents/MacOS" || true
  sleep 1
fi
rm -rf "$DEST"
ditto "$TMP/mnt/$APP_NAME.app" "$DEST"

echo "✓ 完成：$DEST"
echo "  启动：open \"$DEST\"   （常驻请在 app 菜单：设置 → 登录时自动启动）"
