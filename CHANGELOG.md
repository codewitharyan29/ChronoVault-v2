# Changelog

Notable changes to ChronoVault. The live test count and verification
status are whatever `make test` / `python scripts/judge_mode.py`
report — numbers here are point-in-time.

## v2 — final hardening

**Reliability**
- Fixed a Windows `PermissionError` race in the repository lock and in
  the `next_id` / snapshot-record atomic writes: under many concurrent
  `vault snapshot` processes a virus-scanner/indexer handle on a
  just-closed `.vault/` file made `open`/`rename` fail (POSIX never
  does this). Now retried with a bounded, Windows-only backoff
  (`vault/objects.py: windows_retry` / `atomic_replace`); the lock's
  `acquire()` treats a delete-pending `repo.lock` as "held, wait",
  never as stale. `vault stress-test --processes 8` is clean across
  hundreds of runs on real Windows.

**New (all read-only, opt-in — no existing behaviour changed)**
- `vault show <id>` — lists every path in a snapshot with its entry
  kind, logical size, and content-addressed object hash. Makes
  deduplication directly visible (two paths → one hash). `--json` too.
- `--json` on `status`, `list`, `info`, `diff`, `explain`, `show`,
  `log`, `verify`, `recover-check` — one deterministic JSON document
  (sorted keys, stable ordering), same exit codes; errors are
  machine-readable in `--json` mode.
- `scripts/content_addressing_proof.py` — executable proof of the core
  thesis: identical content → one object, shared across snapshots,
  modification → new object, pack/delta preserve identity and bytes,
  GC keeps reachable shared objects, verify confirms integrity.
  `--json` supported.
- `scripts/judge_mode.py --json` — deterministic machine scorecard;
  human output unchanged. Now also runs the demo-regression, the
  `--json` contract, and the content-addressing proof.
- `tests/test_demo_regression.py` — end-to-end lock on the full
  recorded-demo workflow (snapshot IDs, rename lineage, byte-correct
  restore, `gc → verify → restore` invariant).

**Zero-dependency proof**
- `scripts/check_dependencies.py` now also inspects
  `importlib.import_module` / `__import__` with literal arguments and
  classifies every `subprocess` / `os.system` executable, with its
  scope stated honestly. `make verify-deps` tees the live output to
  `deps-proof.txt`.

**Cleanup**
- Removed `vault/experimental/fast_walk.py` (an unused alternative
  directory walker — imported only by its own test) and a now-dead
  `import os` in `snapshot.py`.
- `--version` → `2.0.0`.
- Documentation counts synchronised to the live suite.

## v2 — feature work

Concurrency-safe repository locking, pack files with tree-diff-derived
delta compression, delta-aware garbage collection, a persistent
path-history index with content/history-based rename detection,
`recover-check` (read-only pre-restore audit), pack quarantine for
corrupt/truncated pack files, and a single-file amalgamation build.

## v1

Content-addressable object store (SHA-256 → zlib/raw, atomic writes),
snapshot graph, tree diff, integrity `verify`, `restore` with preview
and integrity gating, mark-and-sweep GC, `trace`, tags, a stdlib
`http.server` Repository Inspector, and the STDLIB.md substitution log.
