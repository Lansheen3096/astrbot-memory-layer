#!/usr/bin/env bash
# 记忆条目复核工具（包装层）
#   读操作（list/stats）直接在跑着的容器里执行；
#   写操作（keep/suspect/unsuspect/delete）先停 AstrBot → 用镜像起临时容器改 → 再起回来，
#   避免两边同时写 index.faiss 打架。脚本退出时一定会把容器起回来。
#
# 用法： ./review.sh list [--all|--sid 0063]
#        ./review.sh stats
#        ./review.sh keep 0063 0064
#        ./review.sh suspect 0070 | unsuspect 0070
#        ./review.sh delete 0065 [--yes]
#        ./review.sh batch  < plan.json      # 一批一次做完（只停起一次容器）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="${ASTRBOT_COMPOSE:-$HOME/astrbot/astrbot.yml}"
DATA="${ASTRBOT_DATA:-$HOME/astrbot/data}"
IMAGE="${ASTRBOT_IMAGE:-soulter/astrbot:latest}"

if [[ $# -lt 1 ]]; then
  sed -n '2,25p' "$DIR/review.py"
  exit 1
fi
CMD="$1"; shift

# ★ KB_NAME 必须显式传进容器。容器里 KB_ROOT 固定是 /AstrBot/data/knowledge_base
#   （由 -v $DATA:/AstrBot/data 决定），所以不传；但 KB_NAME 不传的话，容器里用的是
#   review.py 里的默认值 —— 你在这儿设的 KB_NAME 会被静默忽略，表现是「找不到知识库」。
ENVFWD=(-e "KB_NAME=${KB_NAME:-长期记忆}")
[[ -n "${EMBED_DIM:-}" ]] && ENVFWD+=(-e "EMBED_DIM=$EMBED_DIM")

case "$CMD" in
  list|stats)
    docker cp "$DIR/review.py" astrbot:/tmp/kb_review.py >/dev/null
    docker exec -i "${ENVFWD[@]}" astrbot python /tmp/kb_review.py "$CMD" "$@"
    ;;
  keep|suspect|unsuspect|delete|batch)
    echo "[复核] 停 AstrBot…"
    docker compose -f "$COMPOSE" stop astrbot >/dev/null
    trap 'echo "[复核] 起 AstrBot…"; docker compose -f "$COMPOSE" start astrbot >/dev/null' EXIT
    docker run --rm -i -e TZ=Asia/Shanghai "${ENVFWD[@]}" \
      -v "$DATA":/AstrBot/data -v "$DIR/review.py":/review.py:ro \
      "$IMAGE" python /review.py "$CMD" "$@"
    ;;
  *)
    echo "未知命令：$CMD" >&2
    exit 1
    ;;
esac
