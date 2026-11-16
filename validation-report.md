Forgetful #215 - retained composite validation report

Source base: ccd53fcb696b7e8f803ca20211e451c4e2f60731
Final tested 46-file diff SHA256: a5c32100daf2a73393acf3c72de26fb0a6ea7e9e86d133b8c15a4d78a7fad486
Candidate: I5 run 43922460906 / job 133493041150 / commit 2ca85e4fb443263d54f0305aadd056bae8f0eb53
Result: COMPOSITE_FULL_VALIDATION_PASS (926.26 seconds; target 3000 / hard 3600)

Full SQLite: 2402 passed, 11 inherited activity skips, 12 marker deselections.
Full PostgreSQL: 655 passed, 1 SQLite-only skip, 12 marker deselections.
Early checks: 105 PostgreSQL + 2 SQLite and 2 PostgreSQL existing Plans cases passed.
3178 product-test executions; all 218 new cases passed in full suites; 323 new-case executions.
Installed wheel: seven real paged GETs and 29 production-module hashes verified.
Installed API: all seven GETs with limit=1&offset=1 returned 1 item, total=3, limit=1, offset=1.
18 composite controls passed. All owned containers stopped; no cleanup error.
Linux/amd64, Python 3.13.18; app 3 CPU / 8 GiB, native PostgreSQL 1 CPU / 1 GiB.

Historical I3 run 43901923810: baseline reference + shipping/offline/restart/recreation.
I3 remains partial; shipping passed with a retained 20-second faulthandler dump.
Two environments bridged by versions/pins; APT equality is version/architecture only.
Two existing test oracles now require the full limit/offset envelope; all privacy checks retained.
Inherited benchmark I001 remains. Windows, external UI and malformed foreign DB case untested.
This is a report of recorded results, not a terminal screenshot or a fresh application UI run.
