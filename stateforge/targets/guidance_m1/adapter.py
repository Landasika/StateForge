"""guidance_m1 适配器:把输入字节直接喂给三道阈值门。

纯 Python、无 web 依赖,故既能在 CI 里跑,也能作为"距离是否收敛"的最小
可断言靶;web 侧对照见 ``targets/toy_app`` 等 Flask 靶。
"""

from stateforge.adapters.callable import CallableAdapter
from stateforge.targets.guidance_m1.target import target


def build_adapter() -> CallableAdapter:
    return CallableAdapter(target)
