# ChronoVault v2 — Demo Video Script

Target length ~5–6 minutes. One coherent engineering story, not a
command tour. Every number shown is produced by an actual run — run
`python chronovault.py demo demo-project` first, then `cd demo-project`,
so your figures match.

`vault` below = `python chronovault.py` (use `python`, not `python3`,
on a stock Windows install).

Narration in *italics*; on-screen commands in `code`.

---

## BEAT 1 — The problem  ·  0:00–0:25

*"A backup tells you a copy exists. It doesn't tell you the copy is
intact, or that you can actually get your data back. ChronoVault is a
snapshot engine where **recovery is verifiable** — built entirely from
the Python standard library, so there's no storage engine to trust but
this one."*

---

## BEAT 2 — Zero dependency, proven  ·  0:25–0:55

```
$ python scripts/check_dependencies.py
```
```
Dynamic imports (importlib / __import__ with a literal name):
  (none)
Subprocess / os.system executables observed:
  python           the interpreter itself
External dependencies found:
  NONE
Status: ZERO DEPENDENCY VERIFIED
```

*"Every import in every file is parsed from the AST — including
function-body imports, `importlib`, and `__import__`. No third-party
package, static or dynamic."*

```
$ python -I scripts/_isolated_entry.py verify
```

*"And it runs under `python -I` — no site-packages, no PYTHONPATH, no
working-directory tricks."*

---

## BEAT 3 — The snapshot model  ·  0:55–1:45

```
$ vault init .
$ vault snapshot -m "initial project"
```
```
✓ Snapshot created
  ID: 1   Files: 11   New objects: 10   Reused objects: 1
```

*"Eleven files, ten new objects — the eleventh is a byte-identical
duplicate on disk, deduplicated automatically. Here's the model, made
visible:"*

```
$ vault show 1
```
```
Snapshot 1 — "initial project"
  Parent:     (none — first snapshot)
  Root tree:  b9b418f9a8ff...
  Files:      11

  PATH                     KIND   SIZE     OBJECT
  src/utils.py             file   1.3 KB   e8876ce159538f53...
  src/legacy_utils.py      file   1.3 KB   e8876ce159538f53...
  ...
```

*"`src/utils.py` and `src/legacy_utils.py` are different paths pointing
at **the same object hash** — same content, stored once. That's content
addressing. Directories are content-addressed objects too."*

```
$ vault status --json
```

*"Every read-only command has a `--json` mode for automation — same
data, deterministic keys."*

---

## BEAT 4 — Understanding change  ·  1:45–2:30

```
$ vim src/database.py        # (edit already made)
$ vault snapshot -m "tune connection pool"
```
```
✓ Snapshot created
  ID: 2   New objects: 1   Reused objects: 10
```

*"One new object. The ten unchanged files are referenced, not
re-stored."*

```
$ vault diff 1 2
$ vault explain 2
```

*"`diff` is a real tree diff. `explain` shows the dedup and compression
this snapshot actually achieved."*

---

## BEAT 5 — Rename-aware history  ·  2:30–3:05

```
$ git mv src/database.py src/db.py      # or: Rename-Item / mv
$ vault snapshot -m "rename database module"
$ vault log src/db.py
```
```
History for src/db.py
  Snapshot 1   219b7a53...   "initial project"   (as src/database.py)
  Snapshot 2   4c1e90a1...   "tune connection pool"   (as src/database.py)
  Snapshot 3   4c1e90a1...   "rename database module"
3 entries across a rename (also known as: src/database.py).
```

*"History follows the file across the rename — matched by content and
history, not by guessing from the filename. A pure rename creates no
new blob: `vault show 3` proves `src/db.py` has the exact object
`src/database.py` had in snapshot 2."*

---

## BEAT 6 — Recovery safety  ·  3:05–4:00

```
$ vault verify
✓ All objects verified   Repository healthy.

$ python3 -c "corrupt_one_byte('.vault/objects/…')"   # flip one real byte
$ vault verify
✗ 1 corrupted object(s) found
Repository integrity FAILED.
```

*"Detected by re-hashing — a cryptographic check, not a guess."*

```
$ vault recover-check 3
Snapshot 3 is NOT fully recoverable
  ✗ src/db.py   <hash>   object is present but fails hash/decode verification
```

*"`recover-check` answers one question before you touch anything: could
this snapshot actually be restored right now? It names the exact fault."*

```
$ vault restore 3 --preview      # shows the plan, writes nothing
$ vault restore 3
⚠ Integrity check failed — restore aborted before any changes were made.
```

*"It refuses. Nothing is written. Silently restoring corrupt data would
be worse than stopping."* (Restore the object, show a clean `restore`.)

---

## BEAT 7 — Storage engineering  ·  4:00–4:40

```
$ vault pack
✓ Packed N object(s)
  Full entries: …   Delta entries: …  (saved … vs. storing whole)

$ vault verify
✓ All objects verified   Repository healthy.

$ vault snapshot-rm 1
$ vault gc
✓ Collected M unreachable object(s)   Reclaimed: … B
```

*"`pack` finds files that evolved from an earlier version using real
tree diffs and stores the delta. GC walks every live snapshot's tree —
and it's delta-aware: it will never drop a base object a surviving
snapshot still needs. `verify` stays green through all of it."*

---

## BEAT 8 — The thesis, as one executable proof  ·  4:40–5:15

```
$ python scripts/content_addressing_proof.py
```
```
CONTENT-ADDRESSING PROOF
[PASS] identical content -> identical object hash
[PASS] two paths with equal bytes share one object
[PASS] unchanged files across snapshots reference the same object
[PASS] modification creates a new object; unchanged files keep theirs
[PASS] pack preserves every object's logical identity
[PASS] delta-encoded objects reconstruct the original bytes exactly
[PASS] GC keeps reachable shared objects intact
[PASS] verify confirms full integrity after pack + gc
RESULT: PASS
```

*"That's the whole design in one command — it runs the real storage
operations in a throwaway directory and checks eight invariants."*

---

## BEAT 9 — Engineering proof  ·  5:15–5:40

```
$ make test
Ran <N> tests … OK (skipped=2)
```

*"Full suite on Python 3.11 and 3.14, Ubuntu and Windows in CI. The two
skips are a Windows symlink-privilege limitation — documented, not
hidden. Plus a live concurrency stress test and a security demo."*

```
$ python chronovault.py stress-test --processes 8
  Processes launched: 8   Snapshots created: 8   Unique IDs: 8   Result: PASS
```

---

## BEAT 10 — Bonuses  ·  5:40–6:00

```
$ python scripts/build_single_file.py      # → dist/chronovault_single.py, runs standalone
$ python scripts/prove_reproducible.py     # two independent builds, identical SHA-256
$ python scripts/benchmark_vs_diskcache.py # Package Killer: vs diskcache, in a throwaway venv
```

*"Single-file build, reproducible build, a fair comparison against
`diskcache`, and STDLIB.md documenting every avoided package. The
diskcache package is installed only inside a temporary venv for the
benchmark — never a runtime dependency."*

---

## BEAT 11 — One command  ·  6:00–6:15

```
$ python scripts/judge_mode.py
          CHRONOVAULT: VERIFIED ✓
```

*(Optionally: `python scripts/judge_mode.py --json` for the machine
scorecard.)*

*"One command runs all of it as real subprocesses. This isn't a toy
key-value store — it's a small storage engine, built from the standard
library, whose important claims you can verify yourself."*

---

## Recording notes

- Numbers drift as the suite grows — say "the live count" and let
  `make test` / `judge_mode` show it, rather than freezing a figure.
- The corruption beat needs a genuine one-byte flip on a real stored
  object (a Python one-liner), so the hash `verify` flags matches.
- If you must cut to ~4:30: fold BEAT 4's `explain` into `diff`, and
  drop the `--preview` line in BEAT 6.
