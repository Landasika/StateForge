"""Distance must measure decisions, not traffic.

Directed web fuzzing sorts candidates by how close they got to the target.
That only works if distance distinguishes inputs. It did not:

* Every request to linkding -- logged in or not, on any URL -- runs Django's
  ``_get_user_session_key``, whose exit block sits exactly 2 hops from the
  login write. All 200 attempts therefore scored distance 2 and the metric
  carried no information at all.
* Dropping non-divergent blocks fixed that but over-corrected: a function
  ``exit`` block has several return edges, so some land on a target-reaching
  path and some do not, which passes a naive "has both kinds of edge" test.
  ``get_user_model``'s exit is executed by every request, so every endpoint
  then scored distance 4 -- the same failure in a different disguise.

The rule these tests pin: a distance signal exists only at branch/terminal
blocks that both stay on and leave a target-reaching path.
"""

from __future__ import annotations

from stateforge.core.ir import BasicBlock, FlowEdge, ProgramIR
from stateforge.fuzzing.session_guided import SessionGuidedSearch


def _ir(blocks, edges):
    ir = ProgramIR()
    for bid, kind in blocks:
        ir.blocks[bid] = BasicBlock(id=bid, file="app.py", module="app",
                                    function="f", start_line=1, end_line=1,
                                    kind=kind)
    for src, dst in edges:
        ir.add_edge(FlowEdge(src=src, dst=dst))
    return ir


def _search(ir, target):
    obj = SessionGuidedSearch.__new__(SessionGuidedSearch)
    obj.ir = ir
    obj.target_block = target
    return obj


def test_branch_that_decides_is_kept():
    ir = _ir([("entry", "entry"), ("branch", "branch"), ("target", "block"),
              ("fail", "terminal")],
             [("entry", "branch"), ("branch", "target"), ("branch", "fail")])
    dist = _search(ir, "target")._distance_to_target()
    assert dist.get("branch") is not None


def test_shared_plumbing_block_carries_no_distance():
    """A block every request passes through cannot discriminate."""
    ir = _ir([("entry", "entry"), ("shared", "block"), ("branch", "branch"),
              ("target", "block"), ("fail", "terminal")],
             [("entry", "shared"), ("shared", "branch"),
              ("branch", "target"), ("branch", "fail")])
    dist = _search(ir, "target")._distance_to_target()
    # `shared` is on the path but has a single outgoing edge, so it is not a
    # decision point and must not contribute a distance.
    assert "shared" not in dist


def test_function_exit_is_not_a_decision_point():
    """A multi-edge exit passes a naive test but is not a fork."""
    ir = _ir([("entry", "entry"), ("exit", "exit"), ("a", "block"),
              ("target", "block"), ("dead", "terminal")],
             [("entry", "exit"), ("exit", "a"), ("exit", "dead"),
              ("a", "target")])
    dist = _search(ir, "target")._distance_to_target()
    assert "exit" not in dist


def test_target_itself_is_always_ranked_first():
    ir = _ir([("entry", "entry"), ("target", "block")],
             [("entry", "target")])
    dist = _search(ir, "target")._distance_to_target()
    assert dist.get("target") == 0


def test_no_reachable_decision_yields_no_distance():
    """Disconnected target: nothing to sort by, and that must be explicit."""
    ir = _ir([("entry", "entry"), ("orphan", "branch"), ("target", "block")],
             [("entry", "orphan")])
    dist = _search(ir, "target")._distance_to_target()
    assert set(dist) == {"target"}
