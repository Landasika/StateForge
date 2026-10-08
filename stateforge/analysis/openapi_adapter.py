"""openapi_adapter.py —— SF1-T2(ADR-0003):VAmPI 路由 100% 在 openapi3.yml。

paths → (method, operationId, path 参数) → 反查 api_views 同名函数 AST →
复用 patterns 的端点体提取器。$ref 只展平一层(complex schema → 宽松 str)。
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from stateforge.analysis.patterns import EndpointExtractor, _path_params
from stateforge.core.model import AppModel, Endpoint, Param

_SKIP_FILES = {"adapter.py", "__init__.py", "app.py", "config.py"}


def extract_connexion(app_root: Path, spec_name: str = "openapi3.yml") -> AppModel:
    model = AppModel(app_root=str(app_root), framework="connexion")
    spec_path = next(app_root.rglob(spec_name))
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    funcs = _load_functions(app_root)  # {(module, func): FunctionDef}
    ex = EndpointExtractor(str(spec_path), model.config_flags)

    for path, item in (spec.get("paths") or {}).items():
        for method, op in item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            op_id = op.get("operationId", "")
            module, _, func_name = op_id.rpartition(".")
            fdef = funcs.get((module, func_name))
            params = _dedup_params(_spec_params(path, op, spec))
            ep = Endpoint(
                func=func_name or op_id,
                route=path,
                methods=[method.upper()],
                params=params,
                file=str(fdef and fdef.lineno),
                line=(fdef.lineno if fdef else 0),
            )
            if fdef is not None:
                ex._extract_endpoint_into(fdef, path, [method.upper()], ep)
            model.endpoints.append(ep)

    model.candidates.extend(ex.candidates)
    # 旗标在 app.py(vuln/alive)——单独扫它的模块级 Assign
    for flag_file in ("app.py", "config.py"):
        p = app_root / flag_file
        if p.exists():
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            fx = EndpointExtractor(str(p), model.config_flags)
            fx.visit(tree)
            model.config_flags.update(fx.config_flags)
    return model


def _dedup_params(params: list) -> list:
    seen: set[tuple[str, str]] = set()
    out = []
    for p in params:
        key = (p.name, p.location)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _spec_params(path: str, op: dict, spec: dict) -> list[Param]:
    params: list[Param] = _path_params(path)
    for p in op.get("parameters", []) or []:
        if p.get("in") == "query":
            params.append(Param(name=p["name"], location="query"))
    body = op.get("requestBody")
    if body:
        schema = _resolve(body.get("content", {}).get("application/json", {}).get("schema"), spec)
        for name in (schema or {}).get("properties", {}):
            params.append(Param(name=name, location="json"))
    if op.get("security"):
        params.append(Param(name="Authorization", location="header"))
    return params


def _resolve(schema: dict | None, spec: dict) -> dict | None:
    if not schema:
        return None
    ref = schema.get("$ref")
    if ref:  # 只展平一层
        node = spec
        for part in ref.lstrip("#/").split("/"):
            node = node.get(part, {})
        return node
    return schema


def _load_functions(app_root: Path) -> dict[tuple[str, str], ast.FunctionDef]:
    out: dict[tuple[str, str], ast.FunctionDef] = {}
    for py in sorted(app_root.rglob("*.py")):
        if py.name in _SKIP_FILES:
            continue
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        module = ".".join(py.relative_to(app_root).with_suffix("").parts)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                out.setdefault((module, node.name), node)
                # 相对模块名兜底(api_views.users ↔ users)
                short = module.rpartition(".")[2]
                out.setdefault((short, node.name), node)
    return out


_SKIP_DIRS = {"__pycache__", "database", "openapi_specs", "probes"}
