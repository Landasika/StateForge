# microblog — SS-07E/F raw report

- status: `SMOKE_PASS`
- reason: `HTTP 200`
- source SHA-256: `d3380292e88717a10d0e7acc46dffd94c45abbb8b0fcd468314e5045a7f20ace`
- pre-registered SHA-256: `d3380292e88717a10d0e7acc46dffd94c45abbb8b0fcd468314e5045a7f20ace`
- launch command: `/home/landasika/icassp/external/microblog/.venv/bin/python -m flask --app microblog.py run --host 127.0.0.1 --port 5010`
- health URL: `http://127.0.0.1:5010/auth/login`
- elapsed wall seconds: `0.206725`
- raw log: `artifacts/session_ss07ef/microblog.log`

## Arm execution

This run performed **zero** fuzz arms for this app.  A health
probe is not a blind Session fuzzing result.  The status is
kept separate from `target_reached`; no success is claimed.
