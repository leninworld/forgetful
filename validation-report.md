# Forgetful #168 — Memory Link Plugin validation

**PASS — fresh agent-controlled hosted Docker (Linux AMD64).**

[Run 43335315150](https://github.com/leninworld/forgetful/actions/runs/43335315150)
Baseline: `83d088e76840c07d4995e9a4df7d4f3e1b2e06e2`.

| Check | Result |
| --- | --- |
| Early real SQLite / PostgreSQL feature cases | 43 / 5 passed |
| Full architecture, integration, SQLite, provider sessions | 2,224 passed; 11 skipped |
| Full native PostgreSQL and provider sessions | 526 passed; 1 skipped |
| Provider-session coverage | All 12 passed in each baseline/candidate backend lane |
| Wheel, Alembic modules, installed CLI and policy demo | Passed |
| Real offline embedding/rerank/search/restart/recreate | Passed |
| Exact tested source / owned cleanup | All 25 hashes verified / all owned containers stopped |

All full suites: zero failures or errors. Baselines: 2,181 / 521 passed, with the same 11 / 1 skips.
Whole-repository Ruff: no new or changed-file violations; inherited benchmark I001 remains in both
baseline and candidate (not a clean lint pass). Helper success/failure and bounded-kill controls passed.
Host workload: 2,235.87 seconds; peak app cgroup 3,752,951,808 bytes under 8 GiB.

Native Windows NOT_RUN; hosted Linux is not claimed as full upstream-CI equivalence.
