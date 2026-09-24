# Phase-1 限制清单(封版快照,2026-09-24)

冻结范围:`core/ir.py` schema、块/CS/RS ID 规则(`relpath::func::lo-hi::kind::n`)、
mapper 归一化规则、SF-Micro 真值。Phase-2 起原则上不得为结果好看而改动以上
任何一项;确需变更须开新的真值版本并保留旧版对照。

## 静态图

1. **Try/except 保守近似**:handler 自 try 入口可达(may 语义),不是从真实
   抛出点出发的 exception 边;正常流与异常派发在静态上都自 try_entry 出发。
2. **raise 只连函数出口**:`raise` 直连 exit,不路由到 enclosing except
   handler(与 Try 近似同口径);`assert` 失败路径未单独建模。
3. **嵌套表达式求值序**:`foo(bar())` 真实求值序先 bar;块级图按语句序
   (line, col) 排链,两个 callsite 都在链上但顺序不区分。
4. **同名方法歧义**:方法按类限定(app.A.get/app.B.get);属性调用
   `obj.get(x)` 在多候选时解析为 NONE(unresolved),不造假边;
   `self.sibling()` 类内互调同样不解析(裸名查不到限定名)。
5. **构造器/内建/外部调用**全部进 unresolved(含 jsonify、sqlite execute、
   dict.get 等);未解析率本身是静态质量指标,不是缺陷。
6. **callgraph 消歧**只在提供预生成 PyCG JSON 时启用(PyCG 驱动侧 py3.13
   不可运行);vulnshop golden 上零增益(其 unresolved 全为外部调用)。

## 动态映射

7. **py3.10 推导式帧**:`<listcomp>` 等独立帧不在函数表内,推导式内的调用
   动态侧映射不到(静态边正确);comprehension_call 用例静态-only。
8. **settrace 生效范围**:只对设置后进入的函数帧生效;模块级帧无事件。
9. **多行语句中段行**走 range 兜底(精确 line_numbers 优先)。
10. **exception 事件仅诊断**:采集不映射,不产生静态边。
11. **tracer 平台绑定**:CPython 3.10(无字节码重排假设);3.11+ 需重审。

## 度量与基准

12. **块粒度**:同块多 call 的二次进入(RS→host→CS)为邻接对,无静态 kind。
13. **try_finally**:finally 合并进直线块,异常路径穿透 finally 未建模。
14. **动态真值子集**(5 用例)为确定性形态;随机性形态未纳入动态真值。

## 环境备注

15. 评审环境缺 flask 属环境问题(requirements-test.txt 已固化);
    本机驱动侧 anaconda py3.13 + flask 3.1.0 全量 114 passed。
