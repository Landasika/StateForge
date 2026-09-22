"""vulnshop adapter factory end-to-end (no atheris needed)."""
from pathlib import Path

from stateforge.fuzz.adapters.flask import FlaskAdapter
from stateforge.fuzz.adapters.vulnshop import vulnshop_adapter

ENDPOINTS = {"index", "register", "login", "me", "create_order", "read_order",
             "pay_order", "ship_order", "cancel_order", "read_coupon", "set_role"}


def _idx(adapter, endpoint):
    return next(i for i, r in enumerate(adapter.routes) if r.endpoint == endpoint)


def test_vulnshop_adapter_covers_all_routes(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    assert isinstance(adapter, FlaskAdapter)
    assert {r.endpoint for r in adapter.routes} == ENDPOINTS


def test_all_seed_inputs_execute_without_escape(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    statuses = []
    for b in range(len(adapter.routes)):
        resp = adapter.execute(bytes([b]))
        statuses.append(None if resp is None else resp.status_code)
    assert all(s is not None for s in statuses)
    assert 200 in statuses          # happy path 至少可达


def test_post_bodies_driven_by_bytes(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    resp = adapter.execute(bytes([_idx(adapter, "create_order"), 5]))
    assert resp.status_code == 201  # items 合法,创建成功
    resp2 = adapter.execute(bytes([_idx(adapter, "pay_order"), 3]))
    assert resp2.status_code == 200
