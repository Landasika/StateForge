"""vulnshop target adapter: auth seed + byte-driven bodies."""

from __future__ import annotations

from stateforge.adapters.flask import FlaskAdapter


def vulnshop_adapter(db_path, guards: int = 0,
                     username: str = "alice", password: str = "pw") -> FlaskAdapter:
    from stateforge.targets.vulnshop.app import create_app, make_token

    app = create_app(db_path=str(db_path), guards=guards, init_db=True)
    client = app.test_client()
    client.post("/auth/register", json={"username": username, "password": password})
    headers = {"Authorization": f"Bearer {make_token(username)}"}

    def _positive(data: bytes, default: int = 1) -> int:
        return data[1] if len(data) > 1 and data[1] > 0 else default

    bodies = {
        "register": lambda d: {"username": f"u{d[1] if len(d) > 1 else 0}",
                               "password": "pw"},
        "login": lambda d: {"username": username, "password": password},
        "create_order": lambda d: {"items": [{"price": _positive(d), "qty": 1}]},
        "pay_order": lambda d: {"amount": _positive(d)},
        "set_role": lambda d: {"role": "admin"},
    }
    return FlaskAdapter(app, headers=headers, bodies=bodies)
