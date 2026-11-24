#!/usr/bin/env bash
# 限内存检索演示：进程地址空间限制在 256 MiB，语料 4500 篇、每篇 512 词
# （>=510 个互异词 + 2 个常见词）。
#
# 场景：
#   A. 限内存下首查两个常见词 -> 200、total_hits 准确，续页与独立全榜探针
#      逐元素一致、remaining_hits 精确；
#   B. 撤稿 + 同号重刊后，用变更前签发的旧游标续页：跳过被撤/重刊版本、
#      当页补满、remaining_hits 精确，最后新开检索能看到重刊版本。
#
# 用法：bash demo_memory.sh
# 依赖：python3 + fastapi/uvicorn（检索/撤稿/重刊/续页均走真实 HTTP API）
set -euo pipefail

LIMIT_MIB="${LIMIT_MIB:-256}"
N_DOCS="${N_DOCS:-4500}"
SIZE=10

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "目标：地址空间上限 ${LIMIT_MIB} MiB，在刊稿 ${N_DOCS} 篇，每篇 512 词（510 互异 + 抗战/北平）"
echo

LIMIT_MIB="$LIMIT_MIB" N_DOCS="$N_DOCS" SIZE="$SIZE" \
  python3 "$SCRIPT_DIR/demo_memory_impl.py"
