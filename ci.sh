#!/usr/bin/env bash
# StateForge 本地 CI:P0 fresh-clone 验收链脚本化。
#   临时目录 clone 本仓(仅已提交内容)→ 全新 venv → pip install -e ".[dev,test,fuzz]"
#   → pytest(fresh clone 口径)→ micro_benchmark → 尾部打印三验收数字。
# LLM 环境变量对 pytest/demo 屏蔽:语义通道必须走"无 key"路径,fresh-clone 口径才成立。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SF_CI_DIR="$(mktemp -d /tmp/sf-ci.XXXXXX)"
trap 'rm -rf "$SF_CI_DIR"' EXIT

# pytest/demo 运行时屏蔽 LLM 凭据(无 key 路径 = fresh-clone 验收口径)
# 屏蔽 legacy 族:ANTHROPIC_* + SF_LLM_{PROVIDER,BASE_URL,MODEL,MAX_CALLS,SSL_VERIFY}
# + OPENAI_API_KEY(=provider.py 实际变量名);STATEFORGE_LLM_* 不屏蔽——
# 验收链自身 config 面由测试构造 fake,不依赖宿主环境。
SANITIZE_ENV=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL
              -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_MODEL
              -u SF_LLM_PROVIDER -u SF_LLM_BASE_URL -u SF_LLM_MODEL
              -u SF_LLM_MAX_CALLS -u SF_LLM_SSL_VERIFY -u OPENAI_API_KEY)

echo "== [1/5] clone $REPO_ROOT -> $SF_CI_DIR/src"
git clone -q "$REPO_ROOT" "$SF_CI_DIR/src"
cd "$SF_CI_DIR/src"

echo "== [2/5] venv(python3 = $(python3 --version))"
python3 -m venv "$SF_CI_DIR/venv"
source "$SF_CI_DIR/venv/bin/activate"

echo "== [3/5] pip install -e \".[dev,test,fuzz]\""
python -m pip install -e ".[dev,test,fuzz]"

echo "== [4/5] pytest(fresh clone:预期全绿;数字随阶段测试数增长,以 pytest 汇总行为准)"
if ! PYTEST_TAIL="$("${SANITIZE_ENV[@]}" python -m pytest 2>&1 \
        | tee "$SF_CI_DIR/pytest.log" | tail -n 1)"; then
    echo "pytest FAILED(末 40 行见下)" >&2
    tail -n 40 "$SF_CI_DIR/pytest.log" >&2
    exit 1
fi
echo "   $PYTEST_TAIL"

echo "== [5/5] micro_benchmark(预期 静态 21/21 + 动态 5/5 全 1.00)"
if ! "${SANITIZE_ENV[@]}" python - "$SF_CI_DIR/demo.verdict" <<'PYEOF'
import sys

from stateforge.evaluation.micro_benchmark import evaluate

report = evaluate()  # 打印全表
static, dynamic = report["static"], report["dynamic"]
bad_static = [c for c, m in static.items()
              if any(m[k]["precision"] != 1.0 or m[k]["recall"] != 1.0
                     for k in ("CFG", "CALL", "RETURN"))]
bad_dynamic = [c for c, m in dynamic.items() if m["accuracy"] != 1.0]
verdict = (f"静态 {len(static) - len(bad_static)}/{len(static)}"
           f" + 动态 {len(dynamic) - len(bad_dynamic)}/{len(dynamic)} 全 1.00")
failed = bad_static or bad_dynamic
print(verdict + ("" if not failed else f"  FAIL: {sorted(failed)}"))
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(verdict)
sys.exit(1 if failed else 0)
PYEOF
then
    echo "micro_benchmark FAILED" >&2
    exit 1
fi

echo
echo "==================== SF-CI 验收数字 ===================="
echo "pytest          : $PYTEST_TAIL"
echo "micro_benchmark : $(cat "$SF_CI_DIR/demo.verdict")"
echo "========================================================"
