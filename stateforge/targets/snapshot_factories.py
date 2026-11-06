"""Application-specific snapshot bootstrapping, outside the runtime engine."""


def vulnshop(args):
    from stateforge.targets.vulnshop import app
    return app.create_app(db_path=args.db, guards=args.guards, init_db=True), args.db


def vampi(args):
    from stateforge.targets.vampi import adapter
    app = adapter.make_app(vulnerable=args.guards, token_ttl=600)
    return app, str(adapter.VAMPI_DIR / "database" / "database.db")
