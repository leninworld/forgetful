Forgetful #194 — recorded Docker validation
Run 43049392590: FULL_VALIDATION_PASS (31m 18s workload)
Linux x86_64 hosted Docker; source Python 3.13; shipping Python 3.12
Base: 583cedb5121e4aefe2b3d83ab3a3f3c475ab048a
Tested repair SHA256:
06cec5c1d8d32be023412e83c32412d30b442f1bf56a86fa3db187340b313157
SQLite: 2,086 passed / 11 skipped; PostgreSQL: 498 passed / 1 skipped
Both baselines passed; four expected old-code failures and one control verified
All three optional-tools boundaries and both prior PostgreSQL failures passed
Ruff, architecture, nested help/examples, wheel/demo and shipping checks passed
Installed demo: documents/code/binary round-trip across two databases; binary bytes intact
All 25 tested source hashes verified; owned containers stopped; no external provider calls
Limits: native Windows untested; in-memory SQLite requires serialized callers
Evidence: https://github.com/leninworld/forgetful/actions/runs/43049392590
