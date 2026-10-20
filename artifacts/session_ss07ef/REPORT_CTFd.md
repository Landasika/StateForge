# CTFd — SS-07E/F raw report

- status: `SMOKE_PASS`
- reason: `HTTP 200`
- source SHA-256: `c45bd0e0cf9289aa93979c35c22124d4250b3b2ebad15ffd958df8b23417a1ba`
- pre-registered SHA-256: `c45bd0e0cf9289aa93979c35c22124d4250b3b2ebad15ffd958df8b23417a1ba`
- launch command: `/home/landasika/icassp/external/CTFd/.venv/bin/python -m flask run --port 5011`
- health URL: `http://127.0.0.1:5011/`
- elapsed wall seconds: `2.406021`
- raw log: `artifacts/session_ss07ef/CTFd.log`

## Arm execution

This run performed **zero** fuzz arms for this app.  A health
probe is not a blind Session fuzzing result.  The status is
kept separate from `target_reached`; no success is claimed.
