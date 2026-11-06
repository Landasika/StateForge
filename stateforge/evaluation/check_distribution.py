"""Build and smoke-test the wheel outside the source tree.

Run with python -m stateforge.evaluation.check_distribution. The isolated
installation intentionally omits optional engines and HTTP/web dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile


def check_distribution(repo: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="stateforge-wheel-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        # Include the working refactor as well as tracked release files; omit
        # deleted files and ignored local environments/results.
        listing = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=repo)
        for relative in set(listing.decode().split("\0")) - {""}:
            path = repo / relative
            if not path.is_file():
                continue
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                        "--no-build-isolation", "--wheel-dir", str(root / "wheels"),
                        str(source)], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wheel = next((root / "wheels").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert "stateforge/analysis/semantics/prompts/v1.md" in names
            caches = [n for n in names if n.startswith("stateforge/analysis/semantics/cache/")
                      and n.endswith(".json")]
            assert caches, "release is missing frozen semantic caches"
            assert not any(n.startswith("stateforge/tests/") for n in names)
            assert "stateforge/pipeline/goal_runner.py" in names
            assert "stateforge/solving/checkpoint_replay.py" in names
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv" / "bin" / "python"
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        smoke = '''
import importlib.util
from pathlib import Path
import tempfile
import stateforge
from stateforge.core.seeds import encode_sequence_seed, decode_sequence_seed
from stateforge.analysis.analyzer import analyze, extract_model
from stateforge.analysis.semantics import lifting, cache
from stateforge.solving import goal_checkpoint, state_seed_injector
from stateforge.fuzzing import session_online, entry_driver
from stateforge.solving.llm import lifting as legacy_lifting
assert legacy_lifting is lifting
assert decode_sequence_seed(encode_sequence_seed([{'json': {}}])) == [{'json': {}}]
assert cache.stats()['entries'] > 0
assert importlib.util.find_spec('atheris') is None
assert importlib.util.find_spec('httpx') is None
assert importlib.util.find_spec('flask') is None
from stateforge.pipeline import GoalRunner
with tempfile.TemporaryDirectory() as output:
    shop = Path(stateforge.__file__).parent / 'targets' / 'stateful'
    report = GoalRunner().run(shop, 't_state_shop.py::_handle::91-92::block::1',
                              source='t_state_shop.py', out=output)
    assert report.success, report.summary
    assert report.summary['cold_replay']['verified']
'''
        subprocess.run([str(python), "-I", "-c", smoke], cwd=root, check=True)
        return {"wheel": wheel.name, "prompt": True,
                "frozen_caches": len(caches), "bundled_tests": 0,
                "isolated_base_install": "passed",
                "goal_runner_cold_replay": "passed without optional dependencies"}


def main() -> None:
    print(json.dumps(check_distribution(Path(__file__).resolve().parents[2]),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
