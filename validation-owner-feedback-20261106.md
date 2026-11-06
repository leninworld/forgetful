# Forgetful #188 - owner-feedback validation

Summary of retained hosted Docker results; not a fresh terminal capture.
Run: https://github.com/leninworld/forgetful/actions/runs/42290805000
Published baseline: e363d99efc84bb67a516c5e39793399f5233da8d
Tested repair SHA256: 8519c20b6dd249d5ebf97876551e527c6e25f667d9ee740ad9797c69efd958fb
Linux x86-64 / Python 3.13 / PostgreSQL 16; application limit 8 GiB.

## Verified checks

| Check | Result |
|---|---|
| Published-head source baseline | 1961 passed, 11 skipped, 12 deselected |
| Published-head PostgreSQL baseline | 419 passed, 12 deselected |
| Fail-before regression lane | 8 expected failures; 4 passing controls |
| Candidate full source suite | 1974 passed, 11 skipped, 12 deselected |
| Candidate PostgreSQL suite | 432 passed, 12 deselected |
| Architecture included in source suite | 104 passed |
| Focused CLI/coexistence tests | 9 passed |
| Tiny passive-recorder/classifier check | Positive accepted; 5 bad/missing cases rejected |
| Ruff, sdist and wheel builds | Passed |
| Installed-wheel API and console | Passed |
| Shipping-image offline smoke | 1 passed |

## Behavior checked

- Adding or reopening a task during policy wait preserves task success and keeps the plan active.
- Cancelled plugin children return an advisory warning in both modes, for transition and deletion.
- Genuine request cancellation and manual before-policy cancellation propagate.
- Manual completion still permits unfinished tasks; eligible automatic completion still succeeds.

All four changed-file fingerprints match the tested repair; the parent is immutable.
Elapsed: 1633.5 seconds. All 10 owned containers were stopped.
Synthetic data and pinned models; no external provider calls.
Linux hosted Docker only; native macOS/Windows and live deployment were not tested by this run.
The post-policy task recheck narrows the race window; it is not an atomic/CAS guarantee.
