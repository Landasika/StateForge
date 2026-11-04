"""Directed greybox guidance:目标块、距离调度、变异与 campaign 编排。

这一层只做"往某个静态基本块走"的定向反馈,与 A 既有的状态/会话线相互独立:
输入是已有的 ``ProgramIR`` + 一个适配器,输出是可复现的 campaign 结果与
落盘证据(metrics.jsonl / summary.json / 目标输入二进制)。它不假设任何 web
或安全语义,也不改写 IR 与映射层的既有口径。
"""
