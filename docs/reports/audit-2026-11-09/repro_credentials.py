"""Synthetic credentials only; demonstrates cleartext audit artifact storage."""
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace

from stateforge.analysis.static.icfg import build_icfg
from stateforge.core.model import PlanSegment, PlanStep
from stateforge.runtime.http_plan_executor import execute_app_chain
from stateforge.solving.goal_checkpoint import http_checkpoint_from_execution

root = Path(tempfile.mkdtemp(prefix="sf-credential-audit-"))
source = root / "views.py"
source.write_text("def login():\n    return 200\n")
spec = importlib.util.spec_from_file_location("audit_login", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ir = build_icfg(root)
block = next(b.id for b in ir.blocks.values() if b.kind == "terminal")

class Client:
    def post(self, *args, **kwargs):
        return SimpleNamespace(status_code=module.login())

secret = "SYNTHETIC_AUDIT_SECRET"
execution = execute_app_chain(
    PlanSegment(goal="g", steps=[PlanStep(action="POST /login", form={"username": "placeholder"})]),
    Client(), ir, block, root,
    credentials={"username": "audit-user", "password": secret})
checkpoint = http_checkpoint_from_execution(block, str(source), [], execution, ir=ir)
path = checkpoint.save(root / "checkpoints")
print("CREDENTIAL_IN_CHECKPOINT", secret in path.read_text())
print("CHECKPOINT_PERMISSION", oct(path.stat().st_mode & 0o777))
print("CREDENTIAL_IN_EXECUTION_REQUEST", execution.steps[0].request["form"]["password"] == secret)
print("ARTIFACT_ROOT", root)
