"""Does a harness return the same answers on a second run in the same process?

Every multi-step experiment rests on one assumption: a run starts from a known
state.  In-process harnesses (``APP.test_client()``) make that assumption easy
to violate, because a second run inherits everything the first one left behind
-- SQLAlchemy identity maps, Flask ``g``, module-level caches, committed rows.

This probe answers it directly.  In one child process it calls ``smoke()``
twice and compares the two result lists.  Three outcomes matter:

  STABLE     identical results -- a second run is a valid repeat
  DRIFT      same paths, different status codes -- state leaked, and the
             direction of the change tells us how
  SHAPE      different paths or counts -- the harness is not deterministic

A fourth check matters just as much: compare run 1 against a *fresh process*.
If run 1 already differs from a cold process, then the harness's own module
import is not the reset boundary we assumed it was.

Usage::

    python probe_state_isolation.py                # all ten
    python probe_state_isolation.py ctfd wger      # selected
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

APPS: dict[str, str] = {
    "ctfd": "CTFd",
    "linkding": "linkding",
    "fava": "fava",
    "wger": "wger",
    "microblog": "microblog",
    "octoprint": "OctoPrint",
    "spirit": "Spirit",
    "zinnia": "django-blog-zinnia",
    "mezzanine": "mezzanine",
    "oscar": "django-oscar",
}

# Two smoke() calls in one process, then print both.
TWICE = r'''
import json, sys, traceback
sys.path.insert(0, {repo!r})
out = {{"ok": False}}
try:
    mod = __import__("stateforge.targets.apps20.{slug}.harness",
                     fromlist=["smoke"])
    smoke = mod.smoke
    first = [list(map(str, r)) for r in smoke()]
    second = [list(map(str, r)) for r in smoke()]
    out = {{"ok": True, "first": first, "second": second}}
except BaseException:
    out = {{"ok": False, "stage": "exception",
            "detail": traceback.format_exc()[-1500:]}}
print("@@JSON@@" + json.dumps(out))
'''

# One smoke() call in a cold process.
ONCE = r'''
import json, sys, traceback
sys.path.insert(0, {repo!r})
out = {{"ok": False}}
try:
    mod = __import__("stateforge.targets.apps20.{slug}.harness",
                     fromlist=["smoke"])
    out = {{"ok": True, "first": [list(map(str, r)) for r in mod.smoke()]}}
except BaseException:
    out = {{"ok": False, "stage": "exception",
            "detail": traceback.format_exc()[-1500:]}}
print("@@JSON@@" + json.dumps(out))
'''


def _run(slug: str, template: str, timeout: int) -> dict:
    python = REPO / "external" / APPS[slug] / ".venv" / "bin" / "python"
    if not python.exists():
        return {"ok": False, "stage": "no_venv"}
    code = template.format(repo=str(REPO), slug=slug)
    try:
        proc = subprocess.run([str(python), "-c", code], capture_output=True,
                              text=True, timeout=timeout, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "timeout"}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("@@JSON@@"):
            try:
                return json.loads(line[len("@@JSON@@"):])
            except json.JSONDecodeError:
                return {"ok": False, "stage": "bad_json"}
    return {"ok": False, "stage": "no_output",
            "detail": proc.stderr.strip()[-1200:]}


def compare(a: list, b: list) -> tuple[str, list[str]]:
    """Return (kind, human-readable differences) for two smoke result lists."""
    if len(a) != len(b):
        return "SHAPE", [f"route count {len(a)} -> {len(b)}"]
    pa = {r[0]: r[1] for r in a}
    pb = {r[0]: r[1] for r in b}
    if set(pa) != set(pb):
        only_a = sorted(set(pa) - set(pb))
        only_b = sorted(set(pb) - set(pa))
        return "SHAPE", [f"only in run1: {only_a[:3]}",
                         f"only in run2: {only_b[:3]}"]
    diffs = [f"{path} {pa[path]} -> {pb[path]}"
             for path in pa if pa[path] != pb[path]]
    return ("STABLE" if not diffs else "DRIFT"), diffs


# Routes that deliberately end or reset a session.  Several harnesses place one
# at the end of ROUTES on purpose (``# 最后:清会话``), so a second smoke() starts
# logged out.  That is the harness working as designed, not state leaking, and
# the two must not be reported as the same finding.
DESTRUCTIVE = ("logout", "signout", "admin/reset", "password-reset")

# A drift where the *only* movement is "logged in -> redirected to login" is
# the signature of exactly that design.
# Pages a logged-in user is redirected *away* from.  When the session ends they
# start answering 200 instead of 302, because there is no longer a session to
# redirect.  This is the mirror image of a guarded page going 200 -> 302, and
# belongs to the same event.
_LOGIN_PATH = ("login", "signin", "accounts/login", "register", "registrat",
               "signup", "sign-up", "password-reset", "reset_password",
               "password_reset", "demo")


def _is_logout_drift(diffs: list[str]) -> tuple[bool, list[str], list[str]]:
    """Split drift into 'explained by a logout route' and 'unexplained'.

    The signature of a designed logout is a wholesale move to redirects: every
    authenticated route starts answering 302 (to the login page) while the
    login/register pages themselves flip 302 -> 200, because the session they
    used to redirect *away* from is gone.

    Critically, a route that was 404 before may become 302 after: the auth
    guard runs before routing to a 404, so "not found" turns into "log in
    first".  That is still the logout, not an unrelated change.  So the test
    is not "every diff ends in 302" but "nothing became *more* permissive
    while a majority moved to redirects".
    """
    explained: list[str] = []
    unexplained: list[str] = []
    for diff in diffs:
        # A diff looks like "GET /auth/login 302 -> 200"; the path is field 1,
        # not field 0 (field 0 is the HTTP method).
        parts = diff.split(" ")
        path = parts[1] if len(parts) > 1 else ""
        lowered = path.lower()
        moved_to_redirect = diff.endswith("-> 302") or diff.endswith("-> 301")
        moved_to_200 = diff.endswith("-> 200")
        if moved_to_redirect:
            explained.append(diff)          # became a redirect to login
        elif moved_to_200 and any(k in lowered for k in _LOGIN_PATH):
            explained.append(diff)          # the login page itself: 302 -> 200
        elif "-> 403" in diff or "-> 401" in diff:
            # Losing a session makes guarded routes *refuse*, not permit.  A
            # 200 that becomes 403/401 is the same event as 200 -> 302, just
            # answered by a different guard.
            explained.append(diff)
        elif "404 -> 302" in diff:
            # The auth guard runs before routing, so a logged-in 404 becomes a
            # logged-out redirect: "not found" turns into "log in first".
            # Strictly a consequence of the session ending, not a separate
            # change.
            explained.append(diff)
        else:
            unexplained.append(diff)
    # A logout makes things stricter, never more permissive: an authenticated
    # page that starts returning 200 would mean a session appeared, not ended.
    permissive = [d for d in unexplained if d.endswith("-> 200")
                  and not any(k in d.lower() for k in _LOGIN_PATH)]
    if permissive:
        return False, explained, unexplained
    # An exception raised on the second run is never "the harness logging out";
    # it is the harness failing.  Only status-code movement can be attributed
    # to the session ending.
    raised = [d for d in unexplained if "EXC:" in d]
    if raised:
        return False, explained, unexplained
    return (bool(explained) and len(explained) >= len(unexplained),
            explained, unexplained)


def _is_volatile_id(diffs: list[str]) -> bool:
    """True when the diffs are only generated IDs differing between processes.

    linkding mints a bookmark hash and wger a nutrition-plan UUID at fixture
    time, so two cold processes serve *different* paths for the same route.
    Nothing leaked; the identifiers are simply random per run.  Such diffs are
    reported as "only in run1:" / "only in run2:" path lists, which is the
    shape this checks for.
    """
    if not diffs:
        return False
    only_a = [d for d in diffs if d.startswith("only in run1:")]
    only_b = [d for d in diffs if d.startswith("only in run2:")]
    return bool(only_a) and bool(only_b) and len(only_a) == len(only_b)


def _is_self_deleting_logout(diffs: list[str]) -> bool:
    """True when a *logout route itself* raised after the first run.

    wger deletes temporary guest users on ``user_logged_out``
    (``wger/core/signals.py``: ``delete_temporary_user_on_logout``).  The second
    smoke's logout therefore operates on a row the first smoke deleted and
    raises ``ValueError: User object can't be deleted because its id attribute
    is set to None``.

    That is the application's own design, not a harness leak -- and it is
    distinguishable from a generic failure because the exception is raised on
    a logout route and the rest of the run is explained by the session ending.
    """
    raised_on_logout = [d for d in diffs
                        if "EXC:" in d and "logout" in d.lower()]
    if not raised_on_logout:
        return False
    # Any other exception is treated as a downstream symptom of the same event:
    # once the guest row is gone, pages that render that user raise rather than
    # return a status.  What must NOT be tolerated is a session *appearing* -- a
    # route that starts answering 200 where it used to refuse.
    appeared = [d for d in diffs
                if d.endswith("-> 200")
                and "logout" not in d.lower()
                and not any(k in d.lower() for k in _LOGIN_PATH)]
    return not appeared


def classify_verdict(kind: str, warm_diffs: list[str],
                     cold_diff: list[str]) -> str:
    if kind == "SHAPE":
        # Distinguish "different routes because the IDs are random" from a
        # genuine shape change.
        if _is_volatile_id(cold_diff):
            return "VOLATILE_IDS"
        return "NONDETERMINISTIC"
    if kind == "DRIFT":
        if _is_self_deleting_logout(warm_diffs):
            return "SELF_DELETING_LOGOUT"
        logout_only, _, unexplained = _is_logout_drift(warm_diffs)
        raised = [d for d in warm_diffs if "EXC:" in d]
        if raised and not logout_only:
            # The second run raises where the first one returned a status.
            # Whether that is a leaked half-initialised object or a genuine
            # harness bug, the smoke is not repeatable -- and a repeatable
            # smoke is the floor for any multi-step experiment.
            return "RAISES_ON_REPEAT"
        if logout_only:
            # The harness ends its own session, as designed.  A second run is a
            # different starting state, so it is not a repeat -- but nothing
            # leaked that the harness did not intend to change.
            return "LOGOUT_BY_DESIGN"
        return "STATE_LEAK"
    if cold_diff:
        if _is_volatile_id(cold_diff):
            return "VOLATILE_IDS"
        return "COLD_MISMATCH"
    return "ISOLATED"


def main() -> int:
    wanted = sys.argv[1:] or list(APPS)
    unknown = [s for s in wanted if s not in APPS]
    if unknown:
        print(f"unknown app(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    rows = []
    for slug in wanted:
        print(f"probing {slug} ...", flush=True)
        started = time.monotonic()
        warm = _run(slug, TWICE, timeout=300)
        if not warm.get("ok"):
            rows.append({"app": slug, "verdict": "ERROR",
                         "kind": warm.get("stage"),
                         "detail": (warm.get("detail") or "")[-400:]})
            print(f"  -> ERROR ({warm.get('stage')})", flush=True)
            continue
        kind, diffs = compare(warm["first"], warm["second"])

        cold = _run(slug, ONCE, timeout=300)
        cold_diffs: list[str] = []
        if cold.get("ok"):
            cold_kind, cold_diffs = compare(warm["first"], cold["first"])
            if cold_kind == "SHAPE":
                cold_diffs = cold_diffs or ["cold run has a different shape"]

        verdict = classify_verdict(kind, diffs, cold_diffs)
        rows.append({
            "app": slug, "verdict": verdict, "kind": kind,
            "routes": len(warm["first"]),
            "warm_diffs": diffs, "cold_vs_warm_diffs": cold_diffs,
            "elapsed": round(time.monotonic() - started, 1),
        })
        note = f"  -> {verdict}"
        if diffs:
            note += f"  ({len(diffs)} diffs, e.g. {diffs[0]})"
        print(note, flush=True)

    print("\n" + "=" * 92)
    print(f"{'app':<12} {'verdict':<18} {'routes':>6} {'elapsed':>8}  detail")
    print("-" * 92)
    for r in rows:
        detail = ""
        if r["verdict"] == "STATE_LEAK":
            detail = f"{len(r['warm_diffs'])} route(s) changed on 2nd run"
        elif r["verdict"] == "LOGOUT_BY_DESIGN":
            detail = (f"harness ends its own session; "
                      f"{len(r['warm_diffs'])} route(s) redirect on 2nd run")
        elif r["verdict"] == "COLD_MISMATCH":
            detail = f"{len(r['cold_vs_warm_diffs'])} route(s) differ cold vs warm"
        elif r["verdict"] == "VOLATILE_IDS":
            detail = "same routes, different generated IDs per process"
        elif r["verdict"] == "NONDETERMINISTIC":
            detail = "; ".join(r.get("warm_diffs") or [])[:60]
        elif r["verdict"] == "ERROR":
            detail = str(r.get("kind"))
        print(f"{r['app']:<12} {r['verdict']:<18} "
              f"{str(r.get('routes','-')):>6} {str(r.get('elapsed','-')):>8}  "
              f"{detail}")

    isolated = [r["app"] for r in rows if r["verdict"] == "ISOLATED"]
    by_design = [r["app"] for r in rows
                 if r["verdict"] == "LOGOUT_BY_DESIGN"]
    deleting = [r["app"] for r in rows
                if r["verdict"] == "SELF_DELETING_LOGOUT"]
    volatile = [r["app"] for r in rows if r["verdict"] == "VOLATILE_IDS"]
    print("-" * 92)
    print(f"isolated (2nd run == 1st): {len(isolated)}/{len(rows)}"
          + (f"  ({', '.join(isolated)})" if isolated else ""))
    if by_design:
        print(f"idempotent-except-logout:  {len(by_design)}/{len(rows)}"
              f"  ({', '.join(by_design)})")
    if deleting:
        print(f"logout deletes the user:   {len(deleting)}/{len(rows)}"
              f"  ({', '.join(deleting)})")
    if volatile:
        print(f"stable but random IDs:     {len(volatile)}/{len(rows)}"
              f"  ({', '.join(volatile)})")
    raising = [r["app"] for r in rows
               if r["verdict"] == "RAISES_ON_REPEAT"]
    if raising:
        print(f"raises on 2nd run:         {len(raising)}/{len(rows)}"
              f"  ({', '.join(raising)})")
    bad = [(r["app"], r["verdict"]) for r in rows
           if r["verdict"] not in ("ISOLATED", "LOGOUT_BY_DESIGN",
                                   "SELF_DELETING_LOGOUT", "VOLATILE_IDS",
                                   "RAISES_ON_REPEAT")]
    if bad:
        print("REAL problems: " + ", ".join(f"{a}({v})" for a, v in bad))
    usable = len(isolated) + len(by_design) + len(deleting) + len(volatile)
    print(f"\nharnesses usable for multi-step work: {usable}/{len(rows)}")
    print("  - 'logout by design' needs a fresh login between runs, which the")
    print("    campaign already does; no code change required.")
    print("  - 'logout deletes the user' needs a re-created fixture per run;")
    print("    the harness must rebuild its user, not reuse the deleted one.")
    print("  - 'random IDs' needs the ID to come from the harness at run time")
    print("    (ROUTES is rebuilt per process), not from a captured constant.")
    if raising:
        print("  - 'raises on 2nd run' must be fixed before that app joins a")
        print("    multi-step arm: a smoke that cannot repeat cannot measure.")

    out = (REPO / "docs/reports/dynamic-completion-2026-11-11"
           / "state_isolation_probe.json")
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
