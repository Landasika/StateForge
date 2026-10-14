"""Phase 1 MVP 验收:统一状态读写索引(t_state_shop 旗舰靶)。

顾问 Phase-1 验收口径:给定一个状态读取点,能找到静态上可能影响它的
写入点。t_state_shop 是 v0.4 留档的"5A DB 域守卫不可见"能力边界靶
(容器 dict 库词汇);本文件钉死四个历史盲区全部入索引 + 归属守卫的
读→写对闭合。
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.extractor import extract_state_facts
from stateforge.state.index import build_index
from stateforge.state.model import StateDomain, StateOperation, Relevance

SHOP = Path(__file__).parents[2] / "targets" / "stateful"


def _shop_facts():
    facts = extract_state_facts(SHOP)
    return [f for f in facts if "t_state_shop" in f.source_file]


def _shop_index():
    return build_index(extract_state_facts(SHOP))


class TestDictStoreVocabulary:
    """四个历史盲区(v0.4 零注入的机制级解释)全部可见。"""

    def test_users_row_write_visible(self):
        """L71:`_DB["users"][user] = {...}` → db.users WRITE。"""
        facts = _shop_facts()
        ws = [f for f in facts if f.object_ref == "db.users"
              and f.operation == StateOperation.WRITE]
        assert ws and ws[0].lineno == 71

    def test_orders_row_write_visible(self):
        """L79:`_DB["orders"][oid] = {...}` → db.orders WRITE。"""
        facts = _shop_facts()
        ws = [f for f in facts if f.object_ref == "db.orders"
              and f.operation == StateOperation.WRITE]
        assert ws and ws[0].lineno == 79

    def test_orders_get_read_visible(self):
        """L86:`_DB["orders"].get(oid)` → db.orders READ。"""
        facts = _shop_facts()
        rs = [f for f in facts if f.object_ref == "db.orders"
              and f.operation == StateOperation.READ]
        assert any(f.lineno == 86 for f in rs)

    def test_ownership_field_read_visible(self):
        """L90:`order["owner"]` → db.orders.owner READ(EQ 谓词)。"""
        facts = _shop_facts()
        rs = [f for f in facts if f.object_ref == "db.orders.owner"]
        assert rs
        f = rs[0]
        assert f.lineno == 90
        assert f.relevance == Relevance.CONTROL
        assert f.structured_predicate is not None
        assert f.structured_predicate.operator.value == "EQ"

    def test_membership_and_none_guards(self):
        """L77 `in` 检查与 L87 `is None` 检查 → EXISTS 谓词读。"""
        facts = _shop_facts()
        exists = [f for f in facts
                  if f.structured_predicate is not None
                  and f.structured_predicate.operator.value == "EXISTS"
                  and f.domain == StateDomain.DATABASE]
        lines = {f.lineno for f in exists}
        assert 77 in lines and 87 in lines


class TestReadToWriteQuery:
    """读点 → 写点查询(Phase 1 核心交付)。"""

    def test_owner_read_finds_orders_write(self):
        """L90 owner 读 → L79 订单行写(行写覆盖字段)。"""
        idx = _shop_index()
        writers = idx.writers_of("db.orders.owner")
        assert any(w["line"] == 79 for w in writers)

    def test_table_read_not_polluted_by_other_tables(self):
        """db.users 读点不受 db.orders 写影响(粒度隔离)。"""
        idx = _shop_index()
        for w in idx.writers_of("db.users"):
            assert w["object_ref"].startswith("db.users")

    def test_session_uid_write_reachable(self):
        """L90 session.uid 读的写点 = L72(register 建会话)。"""
        idx = _shop_index()
        ws = idx.writers_of("session.uid")
        assert any(w["line"] == 72 for w in ws)


class TestOrmTransactionPoints:
    """ORM 事务点(commit/flush/rollback)提取。"""

    def test_commit_flush_rollback(self):
        import ast
        import tempfile

        src = ("def save(obj):\n"
               "    db.session.add(obj)\n"
               "    db.session.commit()\n"
               "    db.session.rollback()\n")
        fn = ast.parse(src).body[0]
        from stateforge.state.extractor import _extract_dict_store

        with tempfile.TemporaryDirectory() as d:
            facts = _extract_dict_store(fn, str(Path(d, "x.py")), False)
        refs = {f.object_ref: f.operation for f in facts}
        assert refs.get("db.session.commit") == StateOperation.WRITE
        assert refs.get("db.session.rollback") == StateOperation.DELETE


class TestBlockBindingOnShop:
    """ir 给定时,dict 库事实同样锚到 CFG 基本块(诊断 3 联动)。"""

    def test_shop_facts_bind_blocks(self):
        ir = build_icfg(SHOP)
        facts = extract_state_facts(SHOP, ir=ir)
        shop = [f for f in facts if "t_state_shop" in f.source_file]
        bound = [f for f in shop if f.block_id is not None]
        assert bound, "t_state_shop 事实应携带 block_id"
        for f in bound:
            blk = ir.blocks[f.block_id]
            assert blk.start_line <= f.lineno <= blk.end_line
