from pathlib import Path

def process():
    if Path("/tmp/approved").exists():
        return run_job()
    return "not approved"

def run_job():
    return "job done"
