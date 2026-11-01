#!/usr/bin/env bash
# 构建内嵌 Python 运行时（python-build-standalone，可重定位，免 install_name_tool）
#
# 产物：build/python-runtime/（bin/ lib/ share/）——由 build_app.sh 拷入
#       .app/Contents/Resources/python-runtime/
# 幂等：stamp 文件记录两个 sha256，匹配则直接跳过（强制重建删 build/python-runtime/）。
#
# 选型说明：
# - pbs（astral/python-build-standalone）install_only 发行版设计为可重定位，
#   与 python.org framework 不同，无需重写 dylib install_name 即可随 bundle 移动。
# - stripped 变体去掉调试符号，体积更小。
# - pycryptodome 是唯一硬 pip 依赖（Trae 3.4.x SQLCipher 解密），abi3 wheel 兼容 3.12；
#   wheel 走 curl 预下载 + 本地安装（离线可复现），不走 pip 在线解析。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DL="$REPO/build/dl"
OUT="$REPO/build/python-runtime"
STAMP="$OUT/.stamp"

PBS_VER="3.12.16+20261026"
PBS_SHA="353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-${PBS_VER}-aarch64-apple-darwin-install_only_stripped.tar.gz"

CRYPTO_VER="4.2.0"
CRYPTO_FILE="pycryptodome-${CRYPTO_VER}-cp39-abi3-macosx_10_9_universal2.whl"
CRYPTO_SHA="3e430168f767ecec60c1f850df4abbd0b9f94786b9cc8041a00e976f6d5190c6"
CRYPTO_URL="https://files.pythonhosted.org/packages/41/4c/954a9a1451f7bcc89d66db39bf6499ef5701e8ff35b6c898c659a7ec8546/pycryptodome-${CRYPTO_VER}-cp39-abi3-macosx_10_9_universal2.whl"

STAMP_WANT="pbs=${PBS_SHA}
crypto=${CRYPTO_SHA}"

fetch() {  # fetch <url> <dest> <sha256>
  local url="$1" dest="$2" want="$3"
  if [[ -f "$dest" ]] && [[ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" == "$want" ]]; then
    echo "  缓存命中: $(basename "$dest")"
    return
  fi
  echo "  下载: $(basename "$dest")"
  curl -fsSL -o "$dest" "$url"
  local got
  got="$(shasum -a 256 "$dest" | cut -d' ' -f1)"
  [[ "$got" == "$want" ]] || { echo "sha256 校验失败: $dest" >&2; exit 1; }
}

if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$STAMP_WANT" ]]; then
  echo "✓ runtime 已就绪（stamp 匹配）: $OUT"
  exit 0
fi

mkdir -p "$DL"
fetch "$PBS_URL"    "$DL/pbs.tar.gz"      "$PBS_SHA"
fetch "$CRYPTO_URL" "$DL/$CRYPTO_FILE"    "$CRYPTO_SHA"

echo "  解压与精简..."
rm -rf "$OUT"
tar -xzf "$DL/pbs.tar.gz" -C "$REPO/build"
mv "$REPO/build/python" "$OUT"

# 精简：标准库测试套件/IDLE/文档/字节码缓存（约省 15-25MB）
rm -rf "$OUT/lib/python3.12/test" \
       "$OUT/lib/python3.12/idlelib" \
       "$OUT/lib/python3.12/lib2to3/tests" \
       "$OUT/share/doc" \
       "$OUT/bin/idle3"
find "$OUT" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "  安装 pycryptodome（本地 wheel）..."
# --no-index --no-deps：纯本地安装，无网络解析；pip 自身依赖已随 pbs 附带
"$OUT/bin/python3" -m pip install --no-index --no-deps --no-warn-script-location \
  "$DL/$CRYPTO_FILE" >/dev/null

# 二次精简（安装完成后）：pip/ensurepip 仅安装期需要；include 为编译头文件；
# Crypto wheel 是 universal2，单架构分发剔除 x86_64 半边（约省 5MB）。
rm -rf "$OUT/lib/python3.12/site-packages/pip" \
       "$OUT/lib/python3.12/site-packages/pip-".*.dist-info \
       "$OUT/lib/python3.12/ensurepip" \
       "$OUT/include" \
       "$OUT/lib/python3.12/pydoc_data" \
       "$OUT/lib/python3.12/lib2to3" \
       "$OUT/bin/2to3" "$OUT/bin/2to3-3.12" "$OUT/bin/idle3.12" \
       "$OUT/bin/pydoc3" "$OUT/bin/pydoc3.12" "$OUT/bin/python3.12-config" \
       "$OUT/bin/python3-config"
find "$OUT" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$OUT/lib/python3.12/site-packages/Crypto" -name '*.so' | while read -r f; do
  if lipo -info "$f" 2>/dev/null | grep -q x86_64; then
    lipo -remove x86_64 "$f" -o "$f.arm64only" && mv "$f.arm64only" "$f"
  fi
done

echo "$STAMP_WANT" > "$STAMP"
du -sh "$OUT" | awk '{print "✓ " $2 " (" $1 ")"}'
