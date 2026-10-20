"""SS-06B T1: renamed key/routes/functions and parameterized passcode.

No StateForge production code may branch on these names.  The target is only a
ground-truth fixture for the generic analyzer.
"""

from __future__ import annotations

from flask import Flask, current_app, request, session

DEFAULT_PASSCODE = "gate-t1"


def target_block() -> None:
    current_app.config.setdefault("T1_HITS", []).append("target")


def truthy_target_block() -> None:
    current_app.config.setdefault("T1_HITS", []).append("truthy")


def create_app(passcode: str = DEFAULT_PASSCODE) -> Flask:
    app = Flask(__name__)
    app.secret_key = "stateforge-t1-dev-only"
    app.config["PASSCODE"] = passcode

    @app.post("/p1")
    def open_gate():
        if request.form.get("code") != current_app.config["PASSCODE"]:
            return "deny", 403
        session["ticket"] = 7
        return "ok"

    @app.get("/p2")
    def vault():
        ticket = session.get("ticket")
        if ticket is None:
            return "deny", 401
        target_block()
        return "T1_TARGET"

    @app.get("/p3")
    def vault_truthy():
        ticket = session.get("ticket")
        if ticket:
            truthy_target_block()
            return "T1_TRUTHY"
        return "deny", 401

    @app.post("/p4")
    def close_gate():
        session.pop("ticket", None)
        return "ok"

    @app.post("/p5")
    def wipe():
        session.clear()
        return "ok"

    @app.post("/p6")
    def noise():
        session["noise"] = 1
        return "ok"

    return app
