"""SS-06B: two structurally different Session targets, no name branches."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.session_guided import (STATUS_VERIFIED,
                                               SessionGuidedSearch)
from stateforge.state.session_access import extract_session_accesses
from stateforge.state.session_index import (build_session_index,
                                            query_session_frontier)
from stateforge.state.session_observation import snapshot_view
from stateforge.solving.session_replay import (PREPARATION_NONE,
                                               find_verified_preparation_prefix)
from stateforge.targets.session_gate.app import observe_session

VARIANTS = Path(__file__).resolve().parents[3] / "targets" / "session_variants"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _line_of(source: Path, marker: str) -> int:
    for i, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() == marker:
            return i
    raise AssertionError(f"call not found: {marker}")


def _block_at(ir, function: str, line: int) -> str:
    hits = [b for b in ir.blocks.values()
            if b.function == function and b.start_line <= line <= b.end_line]
    exact = [b for b in hits if b.start_line == line]
    return (exact or hits)[0].id


def _runner(app, appmod):
    holder = {"client": app.test_client()}

    def reset():
        holder["client"] = app.test_client()

    def target(data):
        step = json.loads(data)
        method = str(step.get("method", "GET")).upper()
        kwargs = {}
        for section in ("form", "query", "json"):
            body = step.get(section)
            if isinstance(body, dict):
                kwargs["data" if section == "form" else
                       "query_string" if section == "query" else "json"] = body
        holder["client"].open(str(step.get("path", "/")), method=method,
                              **kwargs)

    def probe():
        return snapshot_view(
            observe_session(holder["client"], app),
            client_id="variant-client", observation_point="after_response",
            source="variant-test")

    return reset, target, probe, holder


def test_t1_renamed_key_routes_and_functions_are_indexed_and_found():
    t1 = VARIANTS / "t1"
    appmod = _load(t1, "ss06_t1")
    ir = build_icfg(t1)
    accesses = extract_session_accesses(t1, ir=ir)
    index = build_session_index(accesses)
    line = _line_of(t1 / "app.py", "target_block()")
    target = _block_at(ir, "vault", line)
    frontier = query_session_frontier(ir, index, target, accesses)

    keys = {a.key_expr for a in accesses if a.key_expr}
    assert "ticket" in keys and "noise" in keys
    assert any(a.key_expr == "ticket" and a.operation == "READ"
               for a in accesses)
    assert any("open_gate" in b for b in frontier.candidate_blocks)
    assert all(a.effect_status == "STATIC_MAY" for a in accesses)

    app = appmod.create_app(passcode="gate-t1-9f3a")
    reset, call, probe, _holder = _runner(app, appmod)
    pool = [
        {"method": "POST", "path": "/p1", "form": {"code": "gate-t1-9f3a"}},
        {"method": "GET", "path": "/p2"},
        {"method": "GET", "path": "/p3"},
        {"method": "POST", "path": "/p4"},
        {"method": "POST", "path": "/p5"},
        {"method": "POST", "path": "/p6"},
    ]
    result = SessionGuidedSearch(
        call, ir, target, frontier, pool, max_execs=60, max_steps=3,
        seed=0, guided=True, reset_hook=reset, trace_root=t1,
        session_probe=probe).run()
    assert result.found is True
    assert result.status == STATUS_VERIFIED
    assert result.steps[0]["path"] == "/p1"
    assert result.steps[1]["path"] == "/p2"
    assert result.session_witness["values"].get("ticket") == 7


def test_t2_helper_writer_two_targets_and_client_isolation():
    t2 = VARIANTS / "t2"
    appmod = _load(t2, "ss06_t2")
    ir = build_icfg(t2)
    accesses = extract_session_accesses(t2, ir=ir)
    index = build_session_index(accesses)
    line_a = _line_of(t2 / "app.py", "target_a()")
    line_b = _line_of(t2 / "app.py", "target_b()")
    target_a = _block_at(ir, "vault_a", line_a)
    target_b = _block_at(ir, "vault_b", line_b)
    frontier_a = query_session_frontier(ir, index, target_a, accesses)
    frontier_b = query_session_frontier(ir, index, target_b, accesses)

    writer_functions = {a.block_id.split("::")[1] for a in accesses
                        if a.operation == "SET" and a.key_expr == "token"}
    assert "save_state" in writer_functions
    assert any(a.key_kind == "SYMBOLIC"
               and a.block_id.split("::")[1] == "save_dynamic"
               for a in accesses)
    assert any("save_state" in b for b in frontier_a.candidate_blocks)
    assert any("save_state" in b for b in frontier_b.candidate_blocks)

    app = appmod.create_app(passcode="gate-t2-77")
    client_a = app.test_client()
    client_b = app.test_client()
    client_a.post("/login", data={"code": "gate-t2-77"})
    assert observe_session(client_a, app).status == "decoded"
    assert observe_session(client_b, app).status == "absent"
    assert client_a.get("/a").status_code == 200
    assert client_b.get("/b").status_code == 401

    reset, call, probe, _holder = _runner(app, appmod)
    pool = [
        {"method": "POST", "path": "/login", "form": {"code": "gate-t2-77"}},
        {"method": "GET", "path": "/a"},
        {"method": "GET", "path": "/b"},
    ]
    result = SessionGuidedSearch(
        call, ir, target_a, frontier_a, pool, max_execs=60, max_steps=3,
        seed=0, guided=True, reset_hook=reset, trace_root=t2,
        session_probe=probe).run()
    assert result.found is True
    assert result.status == STATUS_VERIFIED
    assert result.steps[0]["path"] == "/login"
    assert result.steps[1]["path"] == "/a"


def test_t1_prefix_that_already_hits_target_is_not_reusable():
    t1 = VARIANTS / "t1"
    appmod = _load(t1, "ss06_t1_direct")
    ir = build_icfg(t1)
    accesses = extract_session_accesses(t1, ir=ir)
    index = build_session_index(accesses)
    line = _line_of(t1 / "app.py", "target_block()")
    # The call inside direct() also matches; use the vault line explicitly.
    target_line = _line_of(t1 / "app.py", "target_block()")
    target = _block_at(ir, "vault", target_line)
    frontier = query_session_frontier(ir, index, target, accesses)
    groups = [[c for c in g] for g in frontier.path_constraint_groups]

    app = appmod.create_app(passcode="gate-t1-direct")
    reset, call, probe, _holder = _runner(app, appmod)
    prep = find_verified_preparation_prefix(
        [{"method": "GET", "path": "/p8"},
         {"method": "GET", "path": "/p2"}],
        groups, call, ir, target, reset_hook=reset, session_probe=probe,
        trace_root=t1)
    assert prep.status == PREPARATION_NONE
    assert prep.reason == "no-prefix-verifies-conditions"
