#!/usr/bin/env bash
# 长期记忆管线：按固定 tag 拉两个插件，装进 AstrBot 的插件目录。
#
#   ./install.sh                      # 拉到 ./plugins/（先看再拷）
#   ./install.sh --dest /path/plugins # 直接拉到 AstrBot 的 data/plugins
#   ./install.sh --print              # 只打印本次会拉的版本，不动磁盘
#
# 版本和聚合 tag 的对应关系写在 README 的「版本兼容矩阵」里。
set -euo pipefail

# ── 本次聚合发行版锁定的版本（改动这里就要打新的 memory-layer-v* tag）──
DIGEST_REPO="https://github.com/Lansheen3096/astrbot_plugin_memory_digest.git"
DIGEST_TAG="v0.2.2"
RECALL_REPO="https://github.com/Lansheen3096/astrbot_plugin_memory_recall.git"
RECALL_TAG="v0.2.0"

DEST="./plugins"
PRINT_ONLY=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--dest) DEST="${2:?--dest 需要一个目录}"; shift 2 ;;
    -p|--print) PRINT_ONLY=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "未知参数：$1" >&2; usage 1 ;;
  esac
done

fetch() {
  local repo="$1" tag="$2" name="$3" target="$DEST/$3"
  if [ -d "$target/.git" ]; then
    echo "→ $name：已有仓库，切到 $tag"
    git -C "$target" fetch --quiet --depth 1 --tags origin "$tag"
    git -C "$target" -c advice.detachedHead=false checkout --quiet --detach "$tag"
  else
    echo "→ $name：克隆 $tag"
    git clone --quiet --depth 1 --branch "$tag" "$repo" "$target"
  fi
  local got
  got="$(grep -E '^version:' "$target/metadata.yaml" | head -1 | awk '{print $2}')"
  echo "  $name 版本：${got:-?}（tag $tag）"
}

echo "可观测的安装计划："
echo "  astrbot_plugin_memory_digest  $DIGEST_TAG"
echo "  astrbot_plugin_memory_recall  $RECALL_TAG"
echo "  目标目录：$DEST"

if [ "$PRINT_ONLY" -eq 1 ]; then
  exit 0
fi

command -v git >/dev/null 2>&1 || { echo "缺少 git" >&2; exit 1; }
mkdir -p "$DEST"
fetch "$DIGEST_REPO" "$DIGEST_TAG" "astrbot_plugin_memory_digest"
fetch "$RECALL_REPO" "$RECALL_TAG" "astrbot_plugin_memory_recall"

cat <<'TIP'

装完还要做三件事（详见 README）：
  1. 两个插件的「知识库名字」必须填同一个（默认 长期记忆），否则链路断了。
  2. 知识库要有一个 rerank provider，没有就只注入状态层。
  3. 重启 AstrBot（docker compose restart astrbot）让插件加载。
TIP
