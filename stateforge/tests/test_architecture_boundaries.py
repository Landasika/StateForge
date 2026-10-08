"""架构分层边界测试 —— ARCHITECTURE.md 例外登记制的机器执行面。

规范文见 stateforge/ARCHITECTURE.md:L0 core | L1 analysis | L1.5 state |
L2 runtime | L2.5 fuzzing/adapters | L3 solving | L4 evaluation, cli。
规则:只允许向下引用(高层 → 严格更低层);同层跨域仅允许两个有向对
(adapters→fuzzing、cli→evaluation,见 PEER_EDGES);向上引用一律违规,
除非登记于 ARCHITECTURE.md 例外登记表。
扫描范围 stateforge/**/*.py,排除 targets/ fixtures/ tests/(数据域与测试
可任意引用);动态边(subprocess -m / __import__)AST 不可见,不在此测。
"""

import ast
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]          # .../stateforge
REPO_DIR = PACKAGE_DIR.parent
ARCHITECTURE_MD = PACKAGE_DIR / "ARCHITECTURE.md"

# 分层表(数值只用于比较;与 ARCHITECTURE.md 分层图一致)。
LAYERS = {
    "core": 0,
    "analysis": 1,
    "state": 1.5,
    "runtime": 2,
    "fuzzing": 2.5,
    "adapters": 2.5,
    "solving": 3,
    "evaluation": 4,
    "cli": 4,
}

# 不入分层的域:config=配置叶(importer 为 solving/llm/real_provider 与
# solving/llm/provider 两处),targets=靶场数据域(引用须登记,见
# ARCHITECTURE.md 例外表第 3 行)。
# fixtures/tests 整目录不在扫描范围。
UNLAYERED_DOMAINS = {"config", "targets"}
EXCLUDED_TOP_DIRS = {"targets", "fixtures", "tests", "__pycache__"}

# 同层有向白名单(规则 2):同层跨域引用仅此两对放行——adapters→fuzzing
# (适配器取插桩器)、cli→evaluation(viz 子命令);反向(fuzzing→adapters、
# evaluation→cli)为违规,重新引入须登记例外表。
PEER_EDGES = {("adapters", "fuzzing"), ("cli", "evaluation")}

# 依赖汇(③):evaluation/adapters 不得被管线域反向依赖,入边只许来自
# 白名单对等入口(cli);evaluation 面 -m 驱动如 acceptance.py 属
# L4 评测入口,其对 adapters 的引用按 T1 裁定 3 放行。
SINK_DOMAINS = {"evaluation", "adapters"}
SINK_ALLOWED_SOURCES = SINK_DOMAINS | {"cli", "tests"}

# ARCHITECTURE.md 例外登记表第 1-3 行(静态可查的豁免边):
# (来源模块, 指向域或完整模块)。第 4-5 行(动态边、targets 域内自引用)
# 静态扫描不可见,仅入表不入本集合——互锁:表行数 == len(本集合) + 2。
ALLOWED_EXCEPTIONS = {
    ("stateforge.analysis.analyzer", "solving"),                          # 行1 标注边界
    ("stateforge.state.planning.checkers", "stateforge.runtime.replay"),  # 行2 GoalResult 仅类型
    # 行3 仅锁/登记用:targets 不入分层,规则②不会过滤到它;此行撑起条数锁与来源锁
    ("stateforge.runtime.snapshot.supervisor", "targets"),
}


def _module_of(py_path: Path) -> str:
    parts = list(py_path.relative_to(REPO_DIR).with_suffix("").parts)
    return ".".join(parts[:-1]) if parts[-1] == "__init__" else ".".join(parts)


def _domain_of(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "stateforge"   # 裸 import stateforge → 包根


def _is_internal(module: str) -> bool:
    # 精确段匹配,避免误吞 stateforge_extra 等兄弟包
    return module == "stateforge" or module.startswith("stateforge.")


def _imports(py_path: Path):
    """yield (lineno, imported_module);AST 级提取,动态边天然不可见。

    包根形式 `from stateforge import X` 逐名解析为 `stateforge.X`,
    使其同样产生域级边(否则 _domain_of 落在包根、绕过全部规则)。
    """
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_internal(alias.name):
                    yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if not _is_internal(node.module or ""):
                continue
            if node.module == "stateforge":
                for alias in node.names:
                    yield node.lineno, f"stateforge.{alias.name}"
            else:
                yield node.lineno, node.module


def _edges():
    """全扫描文件的引用边 [(来源模块, 行号, 被引模块, 来源域, 指向域)]。"""
    for py in sorted(PACKAGE_DIR.rglob("*.py")):
        if set(py.relative_to(PACKAGE_DIR).parts[:-1]) & EXCLUDED_TOP_DIRS:
            continue
        src = _module_of(py)
        for lineno, target in _imports(py):
            yield src, lineno, target, _domain_of(src), _domain_of(target)


def _loc(src: str, lineno: int, target: str) -> str:
    return f"  {src.replace('.', '/')}.py:{lineno}  import {target}"


def test_core_imports_nothing_internal_outside_core():
    bad = [(s, n, t) for s, n, t, fd, td in _edges() if fd == "core" and td != "core"]
    assert not bad, (
        "core 必须是纯数据契约层(不得 import 任何其他内部域):\n"
        + "\n".join(_loc(s, n, t) for s, n, t in bad)
    )


def test_no_upward_import_outside_registered_exceptions():
    violations = []
    for src, lineno, target, fdom, tdom in _edges():
        if tdom == fdom:                               # 自域(含子包)
            continue
        if fdom not in LAYERS or tdom not in LAYERS:   # config/targets/包根不入分层
            continue
        if LAYERS[tdom] < LAYERS[fdom]:                # 严格向下
            continue
        if (fdom, tdom) in PEER_EDGES:                 # 同层有向白名单
            continue
        if any(src == es and (tdom == et or target == et or target.startswith(et + "."))
               for es, et in ALLOWED_EXCEPTIONS):      # 已登记例外
            continue
        violations.append(
            f"{_loc(src, lineno, target)}"
            f"  ({fdom}[L{LAYERS[fdom]}] → {tdom}[L{LAYERS[tdom]}] 向上引用)"
        )
    assert not violations, (
        "发现未登记的向上引用边:\n"
        + "\n".join(violations)
        + "\n  → 修复该 import,或在 stateforge/ARCHITECTURE.md 例外登记表登记"
          "(并同步本测试 ALLOWED_EXCEPTIONS,条数互锁)"
    )


def test_sink_domains_take_no_inbound_from_pipeline():
    # evaluation/adapters 是依赖汇:管线域(fuzzing/solving/runtime/…)不得反向
    # 依赖它们——正是 P2-T1 解环消除的 fuzzing→adapters、fuzzing→evaluation 回归面。
    violations = [
        _loc(s, n, t) + f"  (→ {td} 入边)"
        for s, n, t, fd, td in _edges()
        if td in SINK_DOMAINS and fd not in SINK_ALLOWED_SOURCES
    ]
    assert not violations, (
        "依赖汇被管线域反向依赖(evaluation/adapters 只许 cli/tests/对等入口引用):\n"
        + "\n".join(violations)
    )


def test_architecture_md_exception_table_lock():
    lines = ARCHITECTURE_MD.read_text(encoding="utf-8").splitlines()
    header = next((i for i, l in enumerate(lines)
                   if l.strip().startswith("|") and "来源文件" in l and "指向" in l), None)
    assert header is not None, "ARCHITECTURE.md 缺少例外登记表(| 来源文件 | 指向 | 理由 |)"
    rows = []
    for line in lines[header + 1:]:
        if not line.strip().startswith("|"):
            break                                      # 表格结束
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue                                   # 分隔行
        rows.append(cells)

    # 互锁:表行数 == len(ALLOWED_EXCEPTIONS) + 2(第 4、5 行为不可静态扫描的留痕行)
    assert len(rows) == len(ALLOWED_EXCEPTIONS) + 2, (
        f"例外登记表行数({len(rows)})与测试例外集({len(ALLOWED_EXCEPTIONS)})+2 不同步:"
        f"md 与 ALLOWED_EXCEPTIONS 必须同时增删(ARCHITECTURE.md『例外登记表』)"
    )
    # 前 3 行的来源文件须与 ALLOWED_EXCEPTIONS 的来源模块一一对应
    single = re.compile(r"^stateforge/[\w/]+\.py$")
    md_static = {c[0].replace("/", ".").removesuffix(".py") for c in rows if single.match(c[0])}
    assert md_static == {s for s, _ in ALLOWED_EXCEPTIONS}, (
        "例外表静态行与 ALLOWED_EXCEPTIONS 来源不一致:\n"
        f"  仅 md 有:{sorted(md_static - {s for s, _ in ALLOWED_EXCEPTIONS})}\n"
        f"  仅测试有:{sorted({s for s, _ in ALLOWED_EXCEPTIONS} - md_static)}"
    )
