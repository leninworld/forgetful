# Forgetful #194 — recorded hosted Docker validation

Run: https://github.com/leninworld/forgetful/actions/runs/43138272366
Base: 9f85ddcf2ca1ae0264358e0b4b50860948ec1e03; 51 tested source files.
Tested patch SHA256: 64b4d4314a803859589bbac30aa58a9fdbe6db2d691b4583f2a28a629a417a3d

SQLite/integration/architecture: 2,181 passed, 11 skipped. PostgreSQL: 521 passed, 1 skipped.
Unchanged baselines: SQLite 2,050 passed / 11 skipped; PostgreSQL 446 passed.
Eight real migration/startup paths and branch-scoped search downgrade/restore passed.
Overlapping MCP exports, session cancellation and empty-task-update compatibility passed.
Required-kind schemas and native numeric-metadata exact-cap/no-write regressions passed.
Built wheel: document/script/binary round-trip across independent databases passed.
Offline shipping-image test passed; all owned containers stopped without OOM.

Ruff: no new/changed findings; inherited benchmark I001 remains (not clean full lint).
Limits: hosted Linux only; native Windows unrun; one synthetic SQLite vec_skills setup.
The old-source oracle does not reproduce all four new owner findings.
This is a summary of retained results, not a fresh terminal execution.
