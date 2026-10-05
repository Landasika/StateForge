def handler(user, session, logger):
    print(user.name)
    logger.info(session.get("theme"))
    return "done"
