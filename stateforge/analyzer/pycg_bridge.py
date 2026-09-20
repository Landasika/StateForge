"""pycg_bridge.py —— PyCG 调用图构建器(七步配方 ①②③ 的正式实现)。

流程:入口探测(模拟应用加载)→ 逐入口隔离执行(崩者入缺口清单)→ 合并去重 → 缓存。
执行环境:ANOTA 解释器(py3.10;py3.13 会炸 ImportManagerError),tuna 镜像无关
(纯本地分析)。前置:site-packages 需 pycg→PyCG 符号链接(0.0.8 大小写 bug),
本模块自检并自动修复。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ANOTA_PY = Path(__file__).parents[2] / "anota" / "ANOTA" / "ANOTA-interpreter" / "python"

# 入口探测:应用加载点 + 各功能模块的注册点(插件式框架靠 views/routes 入口覆盖)
_ENTRY_NAMES = {"app.py", "manage.py", "factory.py", "wsgi.py", "asgi.py", "config.py"}
_ENTRY_HINTS = ("views", "routes", "controllers", "urls", "api", "handlers")
_SKIP_DIRS = {"tests", "__pycache__", "migrations", "docs", "node_modules", ".git"}


def detect_entries(root: Path, max_entries: int = 12) -> list[Path]:
    """入口 = 加载点文件 + 名字含 views/routes 等的功能文件(按优先级截断)。"""
    primary: list[Path] = []
    secondary: list[Path] = []
    for py in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        if py.name in _ENTRY_NAMES:
            primary.append(py)
        elif any(h in py.name.lower() for h in _ENTRY_HINTS):
            secondary.append(py)
    # views/routes 优先于 __init__(后者常只是 import 汇聚点,信息量低)
    primary.sort(key=lambda p: (p.name == "__init__.py", len(p.parts)))
    return (primary + secondary)[:max_entries]


def _ensure_pycg_importable(python: Path) -> None:
    """修 PyPI pycg 0.0.8 大小写 bug:顶层 PyCG/ 内部 import pycg。"""
    probe = subprocess.run(
        [str(python), "-c", "import pycg"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        site = subprocess.run(
            [str(python), "-c", "import site;print(site.getsitepackages()[0])"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        link = Path(site) / "pycg"
        if not link.exists() and (Path(site) / "PyCG").exists():
            link.symlink_to("PyCG")


def run_pycg(entry: Path, python: Path = ANOTA_PY, timeout: int = 120) -> dict | None:
    """单入口隔离执行;崩/超时返回 None(缺口,由调用方记录)。"""
    entry = Path(entry).resolve()
    _ensure_pycg_importable(python)
    out = entry.parent / f".pycg-{entry.stem}.json"
    try:
        r = subprocess.run(
            [str(python), "-m", "PyCG", str(entry), "-o", str(out)],
            capture_output=True, timeout=timeout, cwd=str(entry.parent.parent),
        )
        if r.returncode == 0 and out.exists():
            return json.loads(out.read_text(encoding="utf-8"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    finally:
        out.unlink(missing_ok=True)
    return None


def merge_callgraphs(parts: list[dict]) -> dict:
    """并集合并:节点保留、边列表拼接去重(嵌套限定名保持原样,消费侧 _canon 拍平)。"""
    merged: dict[str, list[str]] = {}
    for part in parts:
        for k, v in part.items():
            bucket = merged.setdefault(k, [])
            bucket.extend(x for x in v if x not in bucket)
    return merged


def build_callgraph(root: str | Path, cache_path: str | Path | None = None,
                    max_entries: int = 12, force: bool = False,
                    python: Path = ANOTA_PY) -> tuple[dict, list[str]]:
    """全项目调用图:探测→隔离→合并→缓存。返回 (调用图, 缺口清单)。"""
    root = Path(root)
    cache = Path(cache_path) if cache_path else root / ".pycg-callgraph.json"
    if cache.exists() and not force and time.time() - cache.stat().st_mtime < 86400:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data.get("graph", {}), data.get("gaps", [])

    entries = detect_entries(root, max_entries)
    parts: list[dict] = []
    gaps: list[str] = []
    for e in entries:
        part = run_pycg(e, python=python)
        if part:
            parts.append(part)
        else:
            gaps.append(str(e))
    graph = merge_callgraphs(parts)
    cache.write_text(json.dumps({"graph": graph, "gaps": gaps,
                                 "entries": [str(e) for e in entries],
                                 "ok": len(parts), "generated": time.strftime("%F %T")}),
                     encoding="utf-8")
    return graph, gaps
