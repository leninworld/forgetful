# Forgetful #171 — Plan Plugin validation

Summary of retained results from agent-controlled hosted Docker; not a fresh terminal capture.
Run: https://github.com/leninworld/forgetful/actions/runs/41527297768
Base: 38710e64b97604ce391a6533b54ff283a33e98d8
Tested patch SHA256: 24b06a77662fccb787645a97f704edb5f735581aca4f0ce8d926991f84741408
Environment: Linux x86-64, Python 3.13, app limit 8 GiB / 3 CPU; PostgreSQL 16.

## Recorded checks

| Check | Result |
|---|---|
| Original source baseline | 1,795 passed; 11 skipped; 12 deselected |
| Original PostgreSQL baseline | 395 passed; 12 deselected |
| Regression checks on original code | 4 expected behavior failures; 1 positive control |
| Complete candidate source suite | 1,910 passed; 11 skipped; 12 deselected |
| Candidate PostgreSQL suite | 419 passed; 12 deselected |
| Architecture tests included above | 104 passed |
| Full Ruff, source distribution and wheel | Passed |
| Installed wheel import, CLI and API demo | Passed |
| Shipping-image offline smoke | 1 passed |

## Public API behavior checked

- Installed wheel 0.0.0+plan171 loads the new modules and runs real Plan create/update/delete.
- Rejected secondary plan completion preserves the committed task; successful completion returns warnings.
- Task deletion also invokes automatic plan completion with the verified result.
- Real remote CLI plain/warn/blocked/error cases all passed over HTTP; REST and MCP contracts passed.
- Read/list payloads remain unchanged; warnings appear only on applicable successful writes.

All 35 tested file hashes match the reviewed patch; freeze made no further changes.
The run took 1,629.8 seconds; all owned containers stopped, with no OOM.
Tests used synthetic data and pinned model files; no external provider calls.
Linux hosted Docker only: native macOS/Windows and live cloud deployment were not tested.
Shipping recipe used an immutable Python 3.12 base, verified uv and isolated build constraints.
