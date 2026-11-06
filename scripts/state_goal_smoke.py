"""Derive and execute the shop's state goal without an LLM or web framework."""

from pathlib import Path
import sys

# Also work when invoked as ``python scripts/state_goal_smoke.py`` from a checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.plan_executor import execute_plan_for_block
from stateforge.state.extractor import extract_state_facts


def main() -> int:
    shop = ROOT / "stateforge" / "targets" / "stateful"
    block = "t_state_shop.py::_handle::91-92::block::1"
    ir = build_icfg(shop)
    facts = extract_state_facts(shop, ir=ir)
    result = execute_plan_for_block(
        ir, facts, block, str(shop / "t_state_shop.py"), max_replan=0)
    print("\n   ↓\n".join(s.action.removeprefix("POST op=") for s in result.steps))
    print(f"\ntarget_covered: {result.target_covered}")
    for step in result.steps:
        if step.blocking:
            print(f"{step.action}: {step.blocking}", file=sys.stderr)
    for note in result.notes:
        print(note, file=sys.stderr)
    return 0 if result.target_covered and result.steps and all(
        s.ok for s in result.steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
