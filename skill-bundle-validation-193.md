# Forgetful #193 — portable skill resource bundles
Retained hosted Docker results; this summary is not a fresh terminal capture.
Base: 8c63e23e97559ed45663c43ef32c4af4b2f3f4c2
Tested patch SHA256:
1334549bb979c31ec96ad957237129aed7509bf7f82c36abb4733f2b2ddcfe82
Scope: SKILL.md plus documents, code artifacts and binary files; legacy Markdown APIs preserved.
Behavior: atomic SQLite/PostgreSQL import, stable export, resource paths, scopes and ownership.
SQLite: 2,065 collected; zero failures/errors; 11 existing skips.
PostgreSQL: 475 collected; zero failures/errors; one SQLite-only skip.
Baselines: SQLite 1,986 and PostgreSQL 432 collected; zero failures/errors.
Checks: full Ruff and architecture, installed wheel, offline demo and console passed.
Shipping: actual image, offline embedding/rerank, restart and recreation persistence passed.
Oracle: two expected missing-tool failures on unmodified source; legacy control passed.
Provenance: all 37 tested source hashes verified; complete framed evidence; owned containers stopped.
Limits: first run had a test-fixture setup failure; full corrected run passed. Native Windows not run.
Job: https://github.com/leninworld/forgetful/actions/runs/42404832179/job/128500172948
