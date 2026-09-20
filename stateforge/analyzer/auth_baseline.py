"""auth_baseline.py —— auth 链的降级基线(挖掘层纯语法后,auth 语义在此补)。

**定位**:这不是白名单挖掘——挖掘层已产出全部语法候选(含调用形状);
本模块在**标注层**基于关键词对已有候选做语义解释,作为与 LLM 列对照的
heuristic 基线。LLM `relate()` 的 auth_chains 输出是正式通道。

规则(关键词标注,非挖掘):
  - 端点 observes 含 token/session/key → 可能产 auth → Session.authed_as=set
  - 端点 preconditions 引用 auth 关键词候选 → Session.authed_as=truthy
  - 候选名含 login/token/auth → authn 标签(由 label_rule_based 完成)
"""

from __future__ import annotations

from stateforge.core.model import AppModel, Effect, Precondition

_AUTH_OBSERVE_KEYS = {"token", "session", "auth_token", "access_token", "jwt"}
_AUTH_CAND_KEYWORDS = ("token", "session", "login", "auth", "jwt", "credential")


def apply_auth_baseline(model: AppModel) -> int:
    """对端点补 Session.authed_as 事实(标注层基线)。返回补了多少条。"""
    added = 0
    for ep in model.endpoints:
        # 产:observes 含 auth 类键,且端点有产生行为
        if any(k in _AUTH_OBSERVE_KEYS for k in map(str.lower, ep.observes)):
            has = any(e.var == "Session.authed_as" for e in ep.effects)
            if not has:
                ep.effects.append(Effect(
                    var="Session.authed_as", op="set", key="username",
                    file=ep.file, line=ep.line,
                ))
                added += 1
        # 需:路由/函数名含受保护资源痕迹,或引用已知实体(宽松补——宁多勿漏,
        # 运行时 401 会自动过滤误补)
        route_l = ep.route.lower()
        func_l = ep.func.lower()
        auth_hint = any(k in route_l or k in func_l
                        for k in ("me", "order", "pay", "ship", "cancel",
                                  "admin", "coupon", "book", "post", "topic",
                                  "reply", "user", "profile", "password", "email"))
        touches_entity = any(
            p.var and any(p.var.startswith(e) for e in
                          ("Order", "User", "Coupon", "Book", "Session", "Post", "Topic"))
            for p in ep.preconditions
        ) or any(
            e.var and any(e.var.startswith(e2) for e2 in
                          ("Order", "User", "Coupon", "Book", "Post", "Topic"))
            for e in ep.effects
        )
        has_pre = any(p.var == "Session.authed_as" for p in ep.preconditions)
        if (auth_hint or touches_entity) and not has_pre:
            ep.preconditions.append(Precondition(
                var="Session.authed_as", op="truthy",
                file=ep.file, line=ep.line,
            ))
            added += 1
    # Session.authed_as 候选若不存在,从 effects 反推一个
    from stateforge.core.model import StateCandidate
    has_cand = any(c.var == "Session.authed_as" for c in model.candidates)
    if not has_cand and added:
        model.candidates.append(StateCandidate(
            var="Session.authed_as", entity="Session", attr="authed_as",
            ops=["write", "read"],
            evidence="auth baseline: observes/effects",
            source="session_access", file="", line=0,
        ))
    return added
