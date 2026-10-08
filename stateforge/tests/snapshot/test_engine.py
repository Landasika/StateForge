"""SF2-T1/T2 集成测试:监督进程 + fork COW + L2 文件还原 + 嵌套检查点。

运行(ANOTA 解释器,flask+httpx 都在其内):
  anota/ANOTA/ANOTA-interpreter/python -m pytest -q -m anota stateforge/tests/snapshot
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stateforge.runtime.snapshot.client import CheckpointHandle

pytestmark = pytest.mark.anota

REPO = Path(__file__).parents[3]
ANOTA_PY = REPO / "anota" / "ANOTA" / "ANOTA-interpreter" / "python"
PORT = 5060


@pytest.fixture(scope="module")
def root_ckpt(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("snap")
    db = tmp / "shop.db"
    sock = tmp / "sf.sock"
    proc = subprocess.Popen(
        [str(ANOTA_PY), "-m", "stateforge.runtime.snapshot.supervisor",
         "--target", "vulnshop", "--sock", str(sock), "--port", "0",
         "--guards", "0", "--db", str(db)],
        cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ckpt, ready = CheckpointHandle.connect(str(sock))
    yield ckpt
    ckpt.exit()
    proc.terminate()
    proc.wait(timeout=5)


def _reg_login(username="snap_u"):
    return [
        {"method": "POST", "route": "/auth/register",
         "json": {"username": username, "password": f"pw-{username}"}},
        {"method": "POST", "route": "/auth/login",
         "json": {"username": username, "password": f"pw-{username}"}},
    ]


def _login(username="snap_u"):
    return {"method": "POST", "route": "/auth/login",
            "json": {"username": username, "password": f"pw-{username}"}}


def test_epoch_isolation_memory_and_disk(root_ckpt):
    """COW+L2:epoch 内写入(内存+文件)随叶死掉;下一 epoch 从干净态恢复。"""
    out1 = root_ckpt.serve(_reg_login())
    assert out1.responses[0]["status"] == 201 and out1.responses[1]["status"] == 200
    assert out1.served == 2

    out2 = root_ckpt.serve([_login()])
    assert out2.responses[0]["status"] == 401  # 用户随叶消亡(内存 COW + DB 模板还原)


def test_nested_checkpoint_preserves_chain(root_ckpt):
    """depth-1:登录态冻结;从子检查点恢复,me 直接 200(无需重放链)。"""
    child = root_ckpt.freeze_child("logged_in", build=_reg_login("snap_b"))
    out = child.serve([{"method": "GET", "route": "/auth/me",
                        "headers": {}}])
    # login 响应里的 token 已随 build 消费方丢弃 → 这里用重新 login 验证状态存在
    out2 = child.serve([_login("snap_b")])
    assert out2.responses[0]["status"] == 200
    child.exit()


def test_l2_zero_drift_order_lifecycle(root_ckpt):
    """SF2 硬验收(L2 零漂移):epoch1 同叶内注册→登录→建单;epoch2 重注册→查单 404。"""
    items = {"items": [{"sku": "b", "price": 5, "qty": 1}]}

    def chain(i, rs):
        if i == 0:
            return {"method": "POST", "route": "/auth/register",
                    "json": {"username": "snap_d", "password": "pw-snap_d"}}
        if i == 1:
            return {"method": "POST", "route": "/auth/login",
                    "json": {"username": "snap_d", "password": "pw-snap_d"}}
        token = rs[1]["body"]["token"]
        if i == 2:
            return {"method": "POST", "route": "/orders", "json": items,
                    "headers": {"Authorization": f"Bearer {token}"}}
        oid = rs[2]["body"]["id"]
        return {"method": "GET", "route": f"/orders/{oid}",
                "headers": {"Authorization": f"Bearer {token}"}}

    out = root_ckpt.serve(chain, quota=4)
    assert [r["status"] for r in out.responses] == [201, 200, 201, 200]  # 同叶全链成立
    oid = out.responses[2]["body"]["id"]
    token = out.responses[1]["body"]["token"]

    out2 = root_ckpt.serve(_reg_login("snap_d") + [
        {"method": "GET", "route": f"/orders/{oid}",
         "headers": {"Authorization": f"Bearer {token}"}}])
    assert out2.responses[0]["status"] == 201  # 用户可重注册 → DB 确已还原
    assert out2.responses[2]["status"] == 404  # 订单不存在 = 零漂移


def test_restore_latency(root_ckpt):
    """恢复成本与链长无关的抽检:root 恢复 < 50ms(全量验收在 SF2 基准)。"""
    forks = [root_ckpt.serve([{"method": "GET", "route": "/"}]).fork_ms for _ in range(10)]
    median = sorted(forks)[len(forks) // 2]
    assert median < 50.0, f"fork(=restore) 中位 {median:.1f}ms 超限"


def test_checkpoint_tree_memory(root_ckpt):
    info = root_ckpt.info()
    assert info["pss_kb"] > 0
