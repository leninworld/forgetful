# Forgetful #212 — retained hosted Docker results

Run: https://github.com/leninworld/forgetful/actions/runs/43752061808
Diagnostic commit: 66d9a774d7c2d55b10a5c910aa90fa6798376217
Baseline: 46b45dc2a42076a4c220b35770ad30fc9fa4c450 (current main merged; no repair)
Tested repair SHA256: f967c26260c74a166373ec2478d92d89c44c7978d19ac6c1b4f85bf908e59249

Real SQLite regression/control cases: baseline 10 expected assertion failures + 7 passes;
candidate 17 passes, zero failures/errors/skips. Both reported bugs reproduced before repair.
Full baseline: 2244 tests, zero failures/errors, 11 skips.
Full candidate: 2261 tests, zero failures/errors, 11 skips.
Full PostgreSQL baseline/candidate: 527 tests each, zero failures/errors, 1 skip each.
Early SQLite 43/43 and native PostgreSQL 5/5 passed.
Wheel, Alembic, CLI and offline Docker startup/search/restart/recreation passed.
Eight real-pytest classifier controls passed; signed APT installed-freeze evidence retained.
All 27 tested file hashes match; 292 framed evidence records verified; owned containers stopped.

Fixes: retain rebuild outcomes across batches; emit existing activity before advisory after-hooks.
Registered response shape and child-cancellation semantics remain explicit contract choices.
Limits: no atomic/durable event-delivery or global-order guarantee; Windows NOT_RUN.
Inherited benchmark I001 remains; no new/changed-file lint findings (not a clean full lint pass).
This is a rendered summary of recorded results, not a terminal capture or new execution.
