# Forgetful #188 — Plan Plugin merge validation

Summary of retained results from agent-controlled hosted Docker; not a fresh terminal capture.
Run: https://github.com/leninworld/forgetful/actions/runs/41603446215
Base: e35034247ca9341a0abe6fd75f9eee751ad2da7a
Tested patch SHA256: c730354a716d2aee1fbdabb35865066e533e562202f2dcc38fdd87f8433cc7b0
Environment: Linux x86-64, Python 3.13, app limit 8 GiB; PostgreSQL 16.

## Recorded checks

| Check | Result |
|---|---|
| Original source baseline | 1,845 passed; 11 skipped; 12 deselected |
| Original PostgreSQL baseline | 395 passed; 12 deselected |
| Regression checks on original code | 4 expected failures; 1 positive control |
| Focused CLI and plugin coexistence | 9 passed |
| Complete candidate source suite | 1,961 passed; 11 skipped; 12 deselected |
| Candidate PostgreSQL suite | 419 passed; 12 deselected |
| Architecture tests included above | 104 passed |
| Full Ruff, source distribution and wheel | Passed |
| Installed wheel import, CLI and API demo | Passed |
| Shipping-image offline smoke | 1 passed |

## Public API behavior checked

- Memory, Entity and Plan plugins load together and dispatch their own create hooks.
- Installed-wheel Plan create/update/delete and both task-completion paths passed.
- Rejected secondary completion preserves the committed task; successful completion returns warnings.
- Real remote CLI plain/warn/blocked/error cases and isolated platform-directory lookups passed on Linux.

All 35 tested file hashes match the reviewed patch; normalization changed zero source files.
The run took 1385.4 seconds; all eight lane containers exited 0 without OOM, and all nine owned containers, including PostgreSQL, were stopped.
Synthetic data and pinned models only; no external provider calls.
Linux hosted Docker only: native Windows/macOS and live deployment were not tested.
Shipping recipe: immutable Python 3.12 base, verified uv and isolated build constraints.
