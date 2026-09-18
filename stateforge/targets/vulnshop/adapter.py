"""vulnshop TargetSpec —— SF0 §3 契约。

make_server 用 werkzeug 同步单线程(fork-per-leaf 前提);计数版
CountingWSGIServer 在 SF2-T1 替换(SF0 只需可服务 + 健康检查)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import app as vshop


@dataclass
class TargetSpec:
    module: str                 # WSGI app 所在模块(有 .app 或工厂 create_app)
    init: Callable              # 开 DB + schema + seed(ANOTA 侧执行)
    db_path: Path | None        # L2 模板指向;None=:memory:(纯 COW)
    make_server: Callable       # (app, host, port) -> 同步 WSGI server
    healthy_path: str           # 健康检查路由
    observes: dict = field(default_factory=dict)  # 端点 → 可读回字段(规划器 verify 用)


def _make_server(app, host: str, port: int):
    from werkzeug.serving import make_server

    return make_server(host, port, app, threaded=False)


def _init(db_path: str = None) -> str:
    path = db_path or vshop.DB_PATH
    vshop.init_database(path)
    return path


SPEC = TargetSpec(
    module="stateforge.targets.vulnshop.app",
    init=_init,
    db_path=Path(vshop.DB_PATH),
    make_server=_make_server,
    healthy_path="/",
    observes={
        "GET /orders/<int:oid>": ["status", "total", "paid_amount"],
        "GET /coupons/<code>": ["used", "discount"],
        "GET /auth/me": ["id", "username", "role"],
    },
)
