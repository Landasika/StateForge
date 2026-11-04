"""定向灰盒(guidance)测试包。

靶场固定用 ``stateforge.targets.guidance_m1``:纯 Python、无 web 依赖,
``A`` → ``B`` → ``C`` 三道门构成可验证的距离阶梯(3→2→1→0),
使"距离是否真的在收敛"能被断言而不是被观察。
"""
