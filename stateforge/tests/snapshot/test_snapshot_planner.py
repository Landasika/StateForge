"""SF3-T5 后半:规划链 × 快照 epoch(PlanSegment = 一个 epoch = 一个叶)。

5 goal × {vuln, fixed} 十格阵,与直跑矩阵(test_replay_e2e)同期望,
但执行面是 fork COW + L2 模板恢复——同一套 GoalSpec/规划/绑定/判定,
两种执行器可互换(检测语义一致 = B2 与 B2' 对齐的先决条件)。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from stateforge.analysis.analyzer import analyze
from stateforge.runtime.snapshot_replay import SnapshotReplay
from stateforge.analysis.builder import build_sstg
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.goals import VULNSHOP_GOALS
from stateforge.runtime.snapshot.client import CheckpointHandle

pytestmark = pytest.mark.anota

REPO = Path(__file__).parents[3]
ANOTA_PY = REPO / "anota" / "ANOTA" / "ANOTA-interpreter" / "python"
VULNSHOP = REPO / "stateforge" / "targets" / "vulnshop"

EXPECT = {
    ("goal_workflow_bypass", 0): True, ("goal_workflow_bypass", 1): False,
    ("goal_coupon_reuse", 0): True, ("goal_coupon_reuse", 1): False,
    ("goal_idor", 0): True, ("goal_idor", 1): False,
    ("goal_payment_bypass", 0): True, ("goal_payment_bypass", 1): False,
    ("goal_privilege", 0): True, ("goal_privilege", 1): False,
}


@pytest.fixture(scope="module")
def sstg_model():
    m = analyze(VULNSHOP, "flask")
    return m, build_sstg(m)


@pytest.fixture(scope="module")
def checkpoints(tmp_path_factory):
    handles = {}
    procs = {}
    for guards in (0, 1):
        tmp = tmp_path_factory.mktemp(f"snap-g{guards}")
        sock, db = tmp / "sf.sock", tmp / "shop.db"
        procs[guards] = subprocess.Popen(
            [str(ANOTA_PY), "-m", "stateforge.runtime.snapshot.supervisor",
             "--target", "vulnshop", "--sock", str(sock), "--port", "0",
             "--guards", str(guards), "--db", str(db)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        handles[guards], _ = CheckpointHandle.connect(str(sock))
    yield handles
    for h in handles.values():
        h.exit()
    for p in procs.values():
        p.terminate()
        p.wait(timeout=5)


@pytest.mark.parametrize("goal_name,guards", sorted(EXPECT))
def test_goal_on_snapshot_epoch(goal_name, guards, checkpoints, sstg_model):
    m, g = sstg_model
    goal = next(x for x in VULNSHOP_GOALS if x.name == goal_name)
    seg = plan_backward(m, g, goal)
    result = SnapshotReplay(checkpoints[guards]).run_segment(seg, goal)
    want = EXPECT[(goal_name, guards)]
    assert result.hit == want, (
        f"{goal_name} guards={guards}: hit={result.hit} reason={result.reason} "
        f"steps={[(t.route, t.status) for t in result.steps]}"
    )
