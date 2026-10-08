"""ARTIFACT.md / make artifact 入口一致性测试(轻量,P5-T1)。

守卫面:
- ARTIFACT.md 存在,六个双语主节 + 复现小节齐全;
- 文中每个 `python -m <module>` 都是可解析的真实模块(importlib.util.find_spec,
  文档命令"实际可跑"的最低执行面);
- Makefile 有 artifact target(.PHONY 登记),recipe = git ls-files → sha256sum
  → artifact-manifest.sha256,且带符号链接守卫(直接 sha256sum 会顺着目录
  符号链接炸——仓内真实存在 flaskbb2 主题 static 链接);
- 真实跑一遍 `make artifact`(清单为 gitignored 构建产物,跑完即删,不污染
  仓库):非空、含 ARTIFACT.md/Makefile 本身、排除规则成立(git ls-files 只列
  tracked 文件=排除规则,anota//external//results/ 绝不入清单)、符号链接条目
  以链接文本哈希落账;
- .gitignore 覆盖 manifest(构建产物不入库),ARTIFACT.md 本身入库。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_MD = REPO_ROOT / "ARTIFACT.md"
MAKEFILE = REPO_ROOT / "Makefile"

REQUIRED_SECTIONS = (
    "## Overview / 概览",
    "## Requirements / 环境要求",
    "## Install / 安装",
    "## Reproducing the tables / 复现实验表",
    "## Badges / 徽章映射",
    "## Known limitations / 已知限制",
)

REQUIRED_SUBSECTIONS = (
    "### 五臂 spot(four_arm)",
    "### 正式战役(campaign --groups D1,E)",
    "### 消融(state_ablation)",
    "### 分析(p4_analysis)",
    "### SF-Micro 微基准(make demo)",
    "### CI 一键(ci.sh / make verify)",
)


def _artifact_text() -> str:
    assert ARTIFACT_MD.exists(), "仓库根缺 ARTIFACT.md(评估者入口)"
    return ARTIFACT_MD.read_text(encoding="utf-8")


def test_artifact_md_has_required_sections() -> None:
    text = _artifact_text()
    for header in REQUIRED_SECTIONS + REQUIRED_SUBSECTIONS:
        assert header in text, f"ARTIFACT.md 缺节: {header}"


def test_artifact_md_commands_reference_real_modules() -> None:
    text = _artifact_text()
    modules = sorted(set(re.findall(r"python -m ([A-Za-z_][\w.]*)", text)))
    assert len(modules) >= 5, f"ARTIFACT.md 命令块异常少: {modules}"
    for mod in modules:
        assert importlib.util.find_spec(mod) is not None, (
            f"ARTIFACT.md 引用了不可运行的模块: {mod}"
        )


def test_makefile_has_artifact_target() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(r"^artifact:\n((?:\t[^\n]*\\\n)*\t[^\n]*\n)", text, re.M)
    assert m is not None, "Makefile 缺 artifact target"
    recipe = m.group(1)
    assert "git ls-files" in recipe, "recipe 缺 git ls-files(tracked=排除规则)"
    assert "sha256sum" in recipe, "recipe 缺 sha256sum"
    assert "artifact-manifest.sha256" in recipe, "recipe 缺清单输出路径"
    # 符号链接守卫:目录链接直灌 sha256sum 会 "Is a directory" 炸管道
    assert "-L" in recipe and "readlink" in recipe, (
        "recipe 缺符号链接分支(链接文本哈希)"
    )
    phony = re.search(r"^\.PHONY:(.*)$", text, re.M)
    assert phony is not None and "artifact" in phony.group(1).split(), (
        "artifact 未登记进 .PHONY"
    )


def test_make_artifact_produces_manifest() -> None:
    """真实执行 make artifact(端到端),产物为 gitignored 构建文件,跑完即删。"""
    manifest_path = REPO_ROOT / "artifact-manifest.sha256"
    try:
        subprocess.run(["make", "artifact"], cwd=REPO_ROOT, check=True,
                       capture_output=True, text=True)
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) > 100, "清单条目异常少"
        listed = {line.split(maxsplit=1)[1].strip() for line in lines}
        assert "ARTIFACT.md" in listed and "Makefile" in listed
        assert not any(
            p.startswith(("anota/", "external/", "results/", "corpus/"))
            for p in listed
        ), "未入库目录混进了清单(ls-files 排除规则失效)"
        assert any(
            p.endswith("stateforge/targets/flaskbb2/flaskbb"
                       "/themes/aurora/static") for p in listed
        ), "符号链接条目缺失(链接文本哈希分支未生效)"
    finally:
        manifest_path.unlink(missing_ok=True)


def test_manifest_gitignored_and_artifact_md_tracked() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "artifact-manifest.sha256"],
        cwd=REPO_ROOT, capture_output=True,
    )
    assert ignored.returncode == 0, "artifact-manifest.sha256 未被 .gitignore 覆盖"
    tracked = subprocess.run(
        ["git", "ls-files", "ARTIFACT.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert tracked.strip() == "ARTIFACT.md", "ARTIFACT.md 未入库"
