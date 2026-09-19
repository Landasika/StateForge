"""flask_adapter.py —— SF1-T2:Flask 路由适配(装饰器在工厂函数内也能提)。"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.analyzer.patterns import EndpointExtractor
from stateforge.core.model import AppModel

_SKIP_DIRS = {"tests", "__pycache__", "golden", "probes", "database"}
_SKIP_FILES = {"adapter.py", "__init__.py", "app_anota.py"}  # app_anota = 注解工件,非分析对象


def extract_flask(app_root: Path) -> AppModel:
    model = AppModel(app_root=str(app_root), framework="flask")
    for py in sorted(app_root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        if py.name in _SKIP_FILES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        ex = EndpointExtractor(str(py), model.config_flags)
        ex.visit(tree)
        model.endpoints.extend(ex.endpoints)
        model.candidates.extend(ex.candidates)
        model.config_flags.update(ex.config_flags)
    return model
