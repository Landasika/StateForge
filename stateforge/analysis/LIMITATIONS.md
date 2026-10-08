# SA-PR v1 已知局限(SF1 §3,不静默)

- **无跨文件数据流**(VAmPI 的 `token_validator`/`decode_auth_token` 经惯用法
  调用名分派识别,未做通用过程间分析;T5 单层内联推后——今天用 idioms 捷径覆盖)
- 参数化 SQL 的动态赋值不可见(`SET status=?`)——迁移由"无入边值按首现序
  回填"的线性序兜底补全(已写入 analyzer.derive_invariants)
- Jinja 模板内逻辑不分析;动态分发(getattr/dict→func)跳过。**class-based view/register_view 式路由已支持**(2026-09-20,FlaskBB 实测 147 端点);但 Flask-Login(login_user/current_user)与 SQLAlchemy Model 类字段习语未覆盖——真实项目的语义密度取决于习语表扩充
- VAmPI 的 SQLAlchemy 查询链只识别 `X.attr = <expr>` 写与构造器字面 kwargs;
  `db.session.add` 的非常量参数不产 Effect
- 路径转换器缺失由 planner `@probe` 兜底(SF3)
- `if vuln:`/`if GUARDS:` 双态提取:守卫事实来自两分支并集,fixed/vuln 行为
  分化由运行时旗标决定,静态不裁剪
- ORM 构造器实体识别有黑名单(Response/Request 等);未知类名会被当实体
- request_data 等非常量下标变量保留为非归一候选(如 request_data.admin),
  实体归一交给提升层(SF6 relate pass 或 v2)
