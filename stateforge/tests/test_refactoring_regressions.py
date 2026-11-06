"""Regression scenarios reproduced during the architecture audit."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from stateforge.adapters.generic_web import GenericWebAdapter
from stateforge.analysis.source_index import SourceTestIndex
from stateforge.analysis.static.icfg import build_icfg
from stateforge.core.model import AppModel, Endpoint, Param, PlanStep
from stateforge.fuzzing.candidates import CandidateCollector
from stateforge.fuzzing.corpus_manager import CorpusManager
from stateforge.fuzzing.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.frontier_collector import FrontierCollector
from stateforge.fuzzing.guidance.campaign import CampaignConfig
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.fuzzing.seed_publisher import SeedPublisher
from stateforge.runtime.execution import execute_traced
from stateforge.runtime.execution_context import ExecutionContext
from stateforge.runtime.tracer import TraceSession
from stateforge.solving.guard_recovery import recover_guard
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.goals import GoalSpec
from stateforge.state.planning.policies import policy_for


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_flask_write_requests_reach_the_view(method):
    from flask import Flask, jsonify, request
    app = Flask(__name__)

    @app.route("/echo", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def echo():
        return jsonify(method=request.method, body=request.get_json(silent=True),
                       authorization=request.headers.get("Authorization"))

    adapter = GenericWebAdapter(client=app.test_client(),
                                auth_headers={"Authorization": "Bearer sample"})
    response = adapter.execute(json.dumps({
        "method": method, "path": "/echo", "body": {"x": 1}}).encode())
    assert response.status_code == 200
    assert response.json == {"method": method, "body": None if method == "GET" else {"x": 1},
                             "authorization": "Bearer sample"}


def test_django_client_receives_json_and_headers():
    class Client:
        def generic(self, method, path, data, content_type, **extra):
            assert method == "POST" and path == "/api/items"
            assert json.loads(data) == {"title": "测试"}
            assert content_type == "application/json"
            assert extra["HTTP_X_API_KEY"] == "key"
            return 201

    adapter = GenericWebAdapter(client=Client(), base_url="/api")
    assert adapter.execute(json.dumps({"method": "POST", "path": "/items",
        "body": {"title": "测试"}, "headers": {"X-Api-Key": "key"}}).encode()) == 201


def test_adapter_can_delegate_exception_reporting():
    class Client:
        def open(self, *args, **kwargs):
            raise LookupError("view failed")
    assert GenericWebAdapter(client=Client()).execute(b"{}") is None
    with pytest.raises(LookupError, match="view failed"):
        GenericWebAdapter(client=Client(), suppress_errors=False).execute(b"{}")


def test_guards_with_identical_names_and_lines_stay_in_their_files(tmp_path):
    for name, value in [("a", 1), ("b", 2)]:
        (tmp_path / f"{name}.py").write_text(
            f"def gate(x):\n    if x == {value}:\n        return True\n    return False\n")
    ir = build_icfg(tmp_path)
    index = SourceTestIndex(ir)
    guards = {}
    for block in ir.blocks.values():
        if block.kind == "branch":
            guards[Path(block.file).stem] = recover_guard(
                ir, block.id, "unused", "true", index).expression
    assert guards == {"a": "x == 1", "b": "x == 2"}
    assert index.get("gate", 2) is None  # ambiguous legacy lookup fails closed


def test_scoped_predicate_index_does_not_attribute_inner_tests_to_outer(tmp_path):
    (tmp_path / "app.py").write_text(
        "def outer(x):\n    def inner(y):\n        if y == 2:\n            return y\n"
        "    if x == 1:\n        return inner(x)\n")
    index = SourceTestIndex(build_icfg(tmp_path))
    assert index.get("outer", 3, file=str(tmp_path / "app.py")) is None
    assert index.get("outer.inner", 3, file=str(tmp_path / "app.py")) is not None


def test_nested_sessions_restore_the_actual_previous_tracer(tmp_path):
    previous = sys.gettrace()
    def existing(frame, event, arg):
        return existing
    try:
        sys.settrace(existing)
        session = TraceSession(tmp_path)
        with session:
            outer_request = session._current_request()
            with session:
                assert session._current_request() != outer_request
            assert sys.gettrace() == session._trace
            assert session._current_request() == outer_request
        assert sys.gettrace() is existing
    finally:
        sys.settrace(previous)


@pytest.mark.parametrize("scheduler,selection,energy", [
    ("guided", "uniform", "fixed"), ("uniform", "distance", "distance"),
])
def test_explicit_policies_override_the_legacy_scheduler(scheduler, selection, energy):
    config = CampaignConfig(scheduler_name=scheduler, selection_policy=selection,
                            energy_policy=energy)
    assert (config.selection_policy, config.energy_policy) == (selection, energy)
    assert CampaignConfig(scheduler_name="uniform").energy_policy == "fixed"


def _target(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def target(data):\n    return len(data)\n")
    scope = {}
    exec(compile(source.read_text(), str(source), "exec"), scope)
    return scope["target"], build_icfg(tmp_path)


@pytest.mark.parametrize("frontier", [False, True])
def test_collectors_drain_processed_requests_and_preserve_feedback(tmp_path, frontier):
    target, ir = _target(tmp_path)
    session = TraceSession(tmp_path)
    recorder = ExperimentRecorder(tmp_path / "out", "test", 0)
    corpus = CorpusManager(tmp_path / "spool")
    collector = (FrontierCollector(ir, session, FrontierDirectedFeedback(ir), corpus,
                                  recorder, 0) if frontier else
                 CandidateCollector(ir, session, StructuralFeedback(), corpus, recorder, 0))
    for _ in range(100):
        assert collector.handle(target, b"input") is not None
        assert session.events == [] and session._request_indices == {}
    assert collector.last_mapped.block_hits
    assert collector.last_execution.events
    assert len({e.request_id for e in collector.last_execution.events}) == 1


def test_traced_execution_restores_state_on_process_control_exception(tmp_path):
    session = TraceSession(tmp_path)
    calls = []
    class Lifecycle:
        name, isolation = "test", "reset"
        def establish_baseline(self): pass
        def begin_run(self): calls.append("begin")
        def end_run(self): calls.append("restore")
    def target(data):
        raise KeyboardInterrupt
    context = ExecutionContext(Lifecycle())
    with pytest.raises(KeyboardInterrupt):
        execute_traced(target, b"x", session, context)
    assert calls == ["begin", "restore"]
    assert not context._in_run
    assert not session.events and not session._request_indices


def test_canonical_llm_configuration_is_used_by_the_real_provider(monkeypatch):
    from stateforge.analysis.semantics.provider import Provider
    monkeypatch.setenv("STATEFORGE_LLM_API_KEY", "canonical-key")
    monkeypatch.setenv("STATEFORGE_LLM_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setenv("STATEFORGE_LLM_MODEL", "test-model")
    monkeypatch.setenv("STATEFORGE_LLM_TIMEOUT", "7")
    provider = Provider.from_env()
    assert (provider.base_url, provider.model, provider.timeout) == (
        "https://example.invalid/v1", "test-model", 7)
    assert provider.headers == {"Authorization": "Bearer canonical-key"}
    assert importlib.import_module("stateforge.solving.llm.provider").Provider is Provider


def test_generic_planning_does_not_prepend_shop_routes_or_defaults():
    import networkx as nx
    goal = GoalSpec(name="custom", vuln_class="x", kind="reach_state",
                    step_template=[PlanStep(action="POST /custom",
                                           headers={"Authorization": "Bearer ?token"})])
    model = AppModel(app_root="custom", framework="flask")
    assert [s.action for s in plan_backward(model, nx.MultiDiGraph(), goal).steps] == ["POST /custom"]
    ep = Endpoint(func="custom", route="/custom", methods=["POST"],
                  params=[Param(name="amount", location="json")])
    index = {("POST", "/custom"): ep}
    assert policy_for("generic").render_step(index, "POST /custom", goal).json == {"amount": "x"}
    assert policy_for("vulnshop").render_step(index, "POST /custom", goal).json == {"amount": "100"}


def test_seed_publisher_reuses_content_ids_and_lineage(tmp_path):
    publisher = SeedPublisher(tmp_path / "corpus", "test_spool")
    cid = publisher.save(b"seed", origin="STATE", interesting_reason="state_plan")
    assert (tmp_path / "corpus" / cid).read_bytes() == b"seed"
    assert publisher.save(b"seed", origin="STATE", interesting_reason="state_plan") is None
    assert publisher.spool.entries()[0].origin == "STATE"


def test_snapshot_supervisor_accepts_custom_factory(monkeypatch):
    from stateforge.runtime.snapshot.supervisor import build_app
    factory = SimpleNamespace(create=lambda args: (args.marker, None))
    monkeypatch.setitem(sys.modules, "custom_snapshot_app", factory)
    assert build_app(SimpleNamespace(app_factory="custom_snapshot_app:create", marker="app")) == ("app", None)


def test_experiment_aliases_and_suite_scoped_labels():
    from stateforge.evaluation.experiment.arms import RUNNER_ARMS, FOUR_ARM_ARMS
    assert RUNNER_ARMS["B"].mode == "measure"
    assert FOUR_ARM_ARMS["B"].mode == "feedback"
    assert {RUNNER_ARMS[n].canonical_policy for n in ("D1", "D2R", "D2P")} == {"reinjection"}


def test_seed_and_analysis_consumers_import_without_optional_engines():
    script = '''
import sys
class BlockOptional:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'atheris', 'httpx'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockOptional())
from stateforge.analysis.analyzer import extract_model
from stateforge.solving import goal_checkpoint, state_seed_injector
from stateforge.fuzzing import session_online, entry_driver
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
