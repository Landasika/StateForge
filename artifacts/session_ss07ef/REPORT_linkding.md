# linkding — SS-07E/F raw report

- status: `SMOKE_PASS`
- reason: `HTTP 200`
- source SHA-256: `4926d735a2d5c38bf5533883c1ec18d68b0eb2793465fd5e4eca3080a4396ce2`
- pre-registered SHA-256: `4926d735a2d5c38bf5533883c1ec18d68b0eb2793465fd5e4eca3080a4396ce2`
- launch command: `/home/landasika/icassp/external/linkding/.venv/bin/python manage.py runserver 127.0.0.1:5013 --noreload`
- health URL: `http://127.0.0.1:5013/login`
- elapsed wall seconds: `0.733702`
- raw log: `artifacts/session_ss07ef/linkding.log`

## Arm execution

This run performed **zero** fuzz arms for this app.  A health
probe is not a blind Session fuzzing result.  The status is
kept separate from `target_reached`; no success is claimed.
