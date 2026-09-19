"""VAmPI TargetSpec —— SF0-T5(源码零改动,ADR-0003:路由 100% 在 openapi3.yml)。

VAmPI 事实(2026-09-18 核实):
- 入口是 ``app.py``(非 vampi.py);``config.py`` 造 ``vuln_app``(connexion.App)
- Flask WSGI = ``config.vuln_app.app``;connexion.run() 绕过,直接包 WSGI server
- ``vulnerable`` / ``tokentimetolive`` 环境变量在 ``app.py`` import 时读取
  ——必须在 import 前设置(本 adapter 的 make_app 负责)
- DB = ``database/database.db``;``/createdb`` 端点 drop+create+seed(users)
- 挂载前缀 /v1;GET /v1/ 返回 JSON(含 vulnerable 旗标)= healthy_path
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from stateforge.targets.vulnshop.adapter import TargetSpec

VAMPI_DIR = Path(__file__).parent


def make_app(vulnerable: int = 1, token_ttl: int = 600):
    """环境变量先于 import 设置,再加载 VAmPI 的 Flask app(返回 flask.App)。

    必须 import ``config`` 而非 ``app``:VAmPI 的链条是
    config(add_api→api_views→models→``from app import vuln``)——若先 import
    ``app``,它在第 1 行反过来依赖 config,触发 partially-initialized 循环;
    原版靠 ``python app.py`` 时模块名为 ``__main__`` 才幸免。
    """
    os.environ["vulnerable"] = str(vulnerable)
    os.environ["tokentimetolive"] = str(token_ttl)
    if str(VAMPI_DIR) not in sys.path:
        sys.path.insert(0, str(VAMPI_DIR))
    import config  # noqa: F401  (import 即完成 connexion 装配)

    return config.vuln_app.app


def _make_server(flask_app, host: str, port: int):
    from werkzeug.serving import make_server

    return make_server(host, port, flask_app, threaded=False)


def _init() -> Path:
    """程序化建库(db.drop_all/create_all/User.init_db_users),等价 POST /createdb。"""
    if str(VAMPI_DIR) not in sys.path:
        sys.path.insert(0, str(VAMPI_DIR))
    from config import db
    from models.books_model import Book  # noqa: F401
    from models.user_model import User

    db.drop_all()
    db.create_all()
    User.init_db_users()
    return VAMPI_DIR / "database" / "database.db"


def make_spec(vulnerable: int = 1) -> TargetSpec:
    return TargetSpec(
        module="app",  # VAmPI 入口模块(sys.path 指向 vampi 目录)
        init=_init,
        db_path=VAMPI_DIR / "database" / "database.db",
        make_server=lambda flask_app, host, port: _make_server(flask_app, host, port),
        healthy_path="/",
        observes={
            "GET /users/v1/{username}": ["username", "email"],
            "GET /books/v1/{book_title}": ["secret"],
            "GET /me": ["data.username", "data.admin"],
        },
    )


SPEC = make_spec()
