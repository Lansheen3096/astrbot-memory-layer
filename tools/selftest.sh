#!/usr/bin/env bash
# 在【副本】上验 review.py：真删一条 + 标一条 suspect + keep 回来，全程不碰线上库。
set -euo pipefail
rm -rf /tmp/kbtest
cp -r /AstrBot/data/knowledge_base /tmp/kbtest
export KB_ROOT=/tmp/kbtest

echo "=== 副本初始状态 ==="
python /kb/review.py stats

echo
echo "=== 删除 0075（三处同步） ==="
python /kb/review.py delete 0075 --yes

echo
echo "=== 删除后一致性 ==="
python /kb/review.py stats

echo
echo "=== 标 0070 存疑 → 应出现在待审列表 ==="
python /kb/review.py suspect 0070
python /kb/review.py list

echo
echo "=== keep 0070 → 列表应清空 ==="
python /kb/review.py keep 0070
python /kb/review.py list

echo
echo "=== stats 收尾 ==="
python /kb/review.py stats
