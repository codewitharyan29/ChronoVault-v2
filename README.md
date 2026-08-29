# ChronoVault v2

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![Tests](https://img.shields.io/badge/tests-202%20passing-brightgreen)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

**A Git-like snapshot/recovery engine built from scratch with zero
dependencies, where storage correctness is the project — not
something delegated to Git or SQLite.**

```
$ rm -rf src/
$ vault restore 1
✓ Integrity check passed
✓ Restoration completed — 5 file(s) restored (75 B)

$ vault verify
✗ 1 corrupted object(s) found
Repository integrity FAILED.

$ vault restore 1
⚠ Integrity check failed — restore aborted before any changes were made.
```

(Real output from an actual run, corrupting one object on disk between
the two `restore` calls — not invented for the README. `vault` above
is shorthand for `python3 chronovault.py` — or `python chronovault.py`
on Windows, where `python3` isn't on `PATH` by default; see the Judge
checklist below for exact, copy-pasteable commands.)

Delete everything, get it back exactly. Corrupt one object, and the
system refuses to silently restore bad data — it tells you and stops.

```
✓ 204 tests            ✓ Concurrent-process safe
✓ Corruption-safe restore   ✓ Tree-diff-derived delta compression
```

```
   Directory
       │
       ▼
   Snapshot ──────────► Content Addressing
       │                      │
       ▼                      ▼
   Tree History          SHA-256 Objects
       │                      │
       ├───────────────┐      │
       ▼                ▼     ▼
   Real Tree Diff    Dedup  Compression
       │
       ▼
   Delta Base
       │
       ▼
   Pack ──────► Verify ──────► Restore
                          │
                          ▼
                    Byte-perfect data
```

**One command for everything above, run fresh, right now:**

```
$ make judge
```

## 1. What is ChronoVault?

A zero-dependency, content-addressable snapshot engine, built
entirely on Python's standard library. It stores point-in-time
snapshots of any directory, deduplicates and compresses them, and
restores them — with the storage, snapshot, concurrency, and delta
mechanisms implemented in this repository rather than delegated to
third-party libraries. (Compression uses Python's standard-library
`zlib`; hashing uses standard-library `hashlib` — "zero dependency"
means no third-party packages, not that every algorithm was
reinvented from scratch.)

Delete something important? Restore it directly from a snapshot.

```
$ vault demo --snapshot
$ rm -rf src/
$ vault restore 1 --preview
$ vault restore 1
```

v2 adds concurrency safety, pack files with delta compression, and a
path-history index — each proven correct against real, reproduced
failure scenarios, not just unit tests written to pass.

## 2. Why is it different?

A zero-dependency, content-addressable snapshot engine focused on
point-in-time recovery, where the storage mechanics are implemented
and measurable rather than outsourced to SQLite or hidden behind Git.
Every claim in this README — the hashing, the deduplication, the
pack format, the delta encoding, the concurrency guarantee — is
something you can read the source of and re-run the proof for
yourself. Nothing here is "and then a library does the hard part."

**What this is NOT:** ChronoVault is not trying to be a Git
replacement. It intentionally doesn't implement branching, merging,
staging, or distributed collaboration. The project focuses on one
question: can we build a reliable, inspectable snapshot/recovery
engine ourselves, using only Python's standard library?

**Why this isn't just "another Git clone":** the goal here was never
to reimplement Git's feature set — it's to build the storage
mechanics from scratch and see what a from-scratch object model
makes *possible* that copying Git's design wouldn't. One concrete
example, measured not asserted: Git has to *guess* which earlier
object a new one evolved from (name/size heuristics), because its
pack-building process doesn't carry path history forward.
ChronoVault's snapshot model makes that relationship provable — a
real tree diff between two snapshots directly names the earlier blob
a later one evolved from. Built a naive Git-style heuristic as a
controlled baseline and ran both against identical data
(`scripts/compare_delta_base_selection.py`, reproducible): path-aware
selection was right 100% of the time, by construction; the
heuristic's extra guesses were wrong more than half the time. That's
the difference between building your own storage engine and copying
one — see "What v2 proves" below for the full result.

### The zero-dependency achievement, made concrete

"Zero dependencies" is easy to claim and easy to under-prove. Here,
it's backed by five separately-verified claims, not one assertion:

1. **14 real stdlib substitutions**, each naming the specific
   third-party package that would normally be reached for and why the
   stdlib equivalent was used instead — see `STDLIB.md`.
2. **Reproducible builds, proven not asserted**: identical input
   produces byte-for-byte identical object hashes and stored bytes
   across completely independent runs — 5 dedicated tests, including
   one that fails loudly if anything *other* than the timestamp ever
   diverges.
3. **A working single-file build**, generated (not hand-maintained)
   from the real modular source, with an automated 11-step proof that
   it's functionally equivalent to the real CLI — including 8 real
   concurrent subprocesses correctly self-invoking the single file.
4. **A harder proof than an empty `requirements.txt`**: the real CLI
   runs correctly under Python's fully isolated mode (`python -I`),
   which disables `PYTHONPATH`, user site-packages, and system
   site-packages — there is no way for a third-party package to be
   silently available. If it still works there, "zero dependencies"
   means nothing was *reachable*, not just nothing listed
   (`make prove-isolated`, `scripts/prove_isolated_mode.sh`).
5. **Package Killer, benchmarked**: `vault/objects.py` replaces a
   whole package — `diskcache` — not one function call, and
   `scripts/benchmark_vs_diskcache.py` proves it against the real
   package on an identical workload (installed into a throwaway venv,
   never a ChronoVault dependency): ~5x smaller on disk out of the
   box, ~47x smaller on a realistic repeated-snapshot workload thanks
   to content-addressed dedup — at an honestly-reported throughput
   cost. See `STDLIB.md`'s "Package Killer" section.

Every one of those five is independently checkable in under a minute
— see the Judge checklist below.

## 3. 30-second demo

### Judge checklist

```
One command, everything     → make judge
Zero dependencies           → make verify-deps
Zero deps, hard proof       → make prove-isolated
Reproducible storage        → make prove-reproducible
Security, live attacks      → make security-demo
Differentiation, live       → make demo-differentiation
Single-file build           → make verify-single
Package Killer benchmark    → python scripts/benchmark_vs_diskcache.py
Full test suite             → make test
End-to-end demo             → make demo-v2
Video demo script            → DEMO_VIDEO_SCRIPT.md
Security proof (written)     → SECURITY.md
Storage format                → FORMAT.md
Architecture                   → ARCHITECTURE.md
Benchmarks                       → BENCHMARKS.md
Bonus claims (STDLIB Log,      → STDLIB.md
  Package Killer, Single File,
  Reproducible Build)
```

No `make` on your system (e.g. plain Windows)? Every target above is a
one-line wrapper — see the `Makefile` for the exact command it runs
and call that directly, e.g. `make judge` → `python scripts/judge_mode.py`
(use `python`, not `python3`, on a default Windows Python install).

### Try the whole thing in one command

```bash
bash scripts/demo_v2.sh
```

`make demo-v2` creates a temporary repository and demonstrates:

```
✓ snapshots           ✓ pack + delta compression
✓ tree diff            ✓ integrity verification
✓ file history          ✓ measured performance
                        ✓ concurrent-process safety
```

Excerpt of real output from one run:

```
▶ 6/9  vault pack — delta compression + consolidation
✓ Packed 5 object(s) in 0.003s
  Delta entries: 1  (saved 420 B vs. storing them whole)

▶ 9/9  vault stress-test — concurrency safety + corruption recovery, proven live
  Processes launched:   10
  Unique IDs:            10
  Result: PASS
```

Runs init → snapshot → snapshot → diff → log → pack → verify →
benchmark → stress-test end to end, in a throwaway directory, with
real numbers printed at every step — nothing pre-computed or faked.

## 4. What v2 proves

v1 was already feature-complete, tested, and security-reviewed. v2
adds four capabilities, each built as a genuine experiment first —
theory, real-system comparison, implementation, adversarial testing,
honest verdict — and only merged after clearing that bar with real
evidence:

| Capability | What it proves | Real, measured result |
|---|---|---|
| **Concurrent-write locking** | v1 had an actual, reproducible data-corruption bug | 10 concurrent `vault snapshot` processes → 10/10 unique IDs, all 10 snapshots persisted and readable via `vault list` (0 lost writes; was 7/8 unique without the fix) |
| **Pack files (corrected)** | Consolidating objects can beat loose storage on *both* speed and space | 7.6x faster reads, 57.7x lower disk usage, in the benchmark workload |
| **Delta compression** | ChronoVault's tree-diff-derived base selection is deterministic and path-aware, with a genuine size-based fallback | Real space savings on evolving files (never forces a worse encoding than plain compression) |
| **Path-history index** | Answers "show me every version of this file" with a direct index instead of re-walking snapshot history on every query | 574x faster indexed lookup (0.0169 ms vs. 9.68 ms, 200 snapshots, benchmark workload) |

**Command count:** v1: 15 commands. v2: +4 commands. Total: 19.

All 15 v1 commands retain their original behavior and continue to
work in v2 (see Testing below). v2 adds 4:
`pack`, `log`, `benchmark`, `stress-test`.

### The bug-finding story

Five real bugs were found during development — not hypothetical edge
cases, each reproduced against the live implementation, fixed, and
locked in with a regression test:

| Problem discovered | How it was found | Fix | Proof |
|---|---|---|---|
| Concurrent snapshot ID collision | Real `multiprocessing` run, 8 processes | Repository lock (`O_EXCL` + stale-lock detection) | 10/10 unique IDs (was 7/8 without the lock) |
| Delta base disappeared after packing/GC | End-to-end pack → verify → GC → restore | Delta-aware reachability + pack-first base resolution | Byte-for-byte restore after GC |
| Symlink recursion | Real filesystem test, 2000-level nesting | Skip symlinks (a comment claimed this but the check didn't exist) | Cycle terminates, no `RecursionError` |
| Path traversal | Live crafted malicious tree object | Restore path validation | Exploit blocked, regression-tested |
| Demo path resolution | `make demo-v2`, first real run | Absolute script path instead of relative `$0` | Full demo succeeds |

This is the part worth reading even if nothing else is: the most
interesting result in v2 wasn't a benchmark number, it was the
delta/GC bug, found by actually running the software end-to-end, not
by reasoning about it in the abstract.

**What happened:** after wiring delta compression into `vault pack`,
`vault verify` started reporting a corrupted object on a repository
that was actually completely healthy. Root cause: a delta-encoded
object's *base* gets deleted from loose storage once it's packed —
but the code resolving a delta's base only knew to look in loose
storage, never inside the pack the base was actually sitting in.
**Fixed**, then the fix was proven against the exact failure
scenario, live, through the real CLI:

```
vault pack                     → delta entries created, base packed
vault verify                   → ✓ All objects verified (after the fix)
vault snapshot-rm <old>        → deletes the delta base's ONLY owning snapshot
vault gc                       → "delta-aware: base objects for live
                                   delta-encoded data were protected"
vault restore <snapshot>       → EXACT byte-for-byte match, still correct
vault verify                   → ✓ All objects verified
```

Two more real bugs were found the same way during v2's development —
a pack-format incompatibility caught by tracing formats before
wiring (never shipped), and a test that initially "passed" for the
wrong reason (it hadn't actually constructed the danger condition it
claimed to test) — both corrected before being trusted.

### Security

ChronoVault's restore path has been tested against malformed and
malicious repository data, including path traversal. A real
traversal vulnerability was discovered during development,
reproduced against the live implementation, fixed, and
regression-tested. See `SECURITY.md`.

### What's actually novel here

Not "invented a new concept from scratch" — that's rare in any
hackathon project, including the ones ChronoVault is compared
against. The specific claims:

**Tree-diff-derived delta base selection, measured against a controlled
baseline, not just argued.** Built a naive size-proximity heuristic
(the shape of Git's real heuristic, minus name-matching, since
ChronoVault has no comparable string-similarity layer to compare
fairly) and ran both against identical test data —
`scripts/compare_delta_base_selection.py`, reproducible with real
numbers on any machine:

```
Path-aware (tree-diff):  8 candidates, ALL genuinely correct
                           relationships (100% precision, by
                           construction — every one traces to a real
                           tree diff of a modified path)
Size-heuristic (no path): 23 candidates found, only 8 match
                           path-aware's picks; of the 15 extra, 8
                           (53%) provably pair GENUINELY UNRELATED
                           content that only happens to share a size
```

The size heuristic isn't just "sometimes picks a worse base" — over
half its extra guesses, in this test, are objects with *zero* real
relationship, confirmed by checking they don't even share a filename.
Path-aware's precision isn't empirical luck; it's structural — a real
tree diff cannot report a false "this evolved from that" relationship
the way a size coincidence can.

**A genuinely new limitation, found by this same round of testing, not
hidden:** the delta algorithm's fixed 64-byte block matching finds
*zero* copyable bytes when changes are densely scattered (a small
edit on every line, rather than one localized region) — confirmed
directly: a single localized edit correctly copies 99% of a file;
per-line scattered edits copy 0%, falling back to fully literal
encoding. Every earlier benchmark in this project used localized
edits (the realistic case), so this didn't show up until deliberately
tested against a harder, less-common editing pattern.

**Delta-aware garbage collection is a systems-integration problem most
projects that bolt delta compression onto object storage get wrong
silently.** A delta-encoded object depends on its base to be
reconstructed; if GC doesn't know that, it can delete a base a live
object still needs, and the failure is invisible until someone tries
to restore. This isn't hypothetical here — `tests/test_v2_delta_gc.py`
reproduces the exact disaster (v1's unmodified GC provably *would*
delete a needed base) before proving the fix, which is a stronger
claim than most systems make about this exact failure mode.

## 5. Evidence / reproducibility

```bash
make verify-deps    # confirm zero external dependencies, from source
make test            # full 204-test suite (202 pass, 2 skipped -- Windows symlink-privilege limitation, not a failure)
make demo-v2          # reproduce the v2 demo and benchmark numbers above
```

All v2 benchmark figures shown in this README are generated by the
executable `vault benchmark` command; they are not hard-coded demo
output. Run it yourself and you'll get your own machine's numbers,
not these ones.

### Testing

204 tests (202 passing, 2 skipped — a Windows-only symlink-privilege
limitation, not a failure), `python3 -m unittest discover tests -v`
(`python` instead of `python3` on Windows). Covers everything v1
covered, plus:

- **The actual disaster scenario for delta-aware GC**, proven in two
  stages: first confirming v1's unmodified GC *would* delete a needed
  base (the real danger), then confirming the delta-aware version
  protects it — not just "the fix works," but "the bug it fixes is real."
- **Real concurrent-process proof for locking** — `multiprocessing`
  (not threads, since the race is in file I/O, which threads wouldn't
  exercise the same way), reproducing an actual ID collision without
  the lock and its absence with it.
- **Pack correctness parity** between the corrected and original
  implementations, plus dedicated tests for each of the four specific
  read-path fixes (wasted `stat()` calls, file-handle churn, O(packs)
  lookup, decode-logic duplication).
- **Two tests that initially passed for the wrong reason**, caught
  and rewritten to construct the actual danger condition instead of
  a scenario where the existing code would have been fine anyway.

### Benchmarks

Real numbers, printed fresh every run by `vault benchmark` — not
copied from a prior session. Representative output:

```
Object storage (3000 objects, 1000 random reads):
  Loose  read:   18.7 ms   disk: 11.7 MB
  Packed read:   2.5 ms    disk: 208.0 KB
  Read speedup:    7.6x   (measured on this workload)
  Disk reduction:  57.7x  (measured on this workload)

Path history (200 snapshots):
  Brute-force walk: 9.68 ms
  Indexed lookup:   0.0169 ms
  Speedup:          574x  (measured on this workload)
```

See `BENCHMARKS.md` for v1's original large-scale benchmarks
(100–20,000 files), reproducible with `python3 scripts/benchmark.py`.

## 6. Architecture

```
                         vault CLI (19 commands)
                                 |
        +------------------------+------------------------+
        |                        |                         |
  Snapshot Engine         Repository Inspector      Repository Lock
  (tree walk, diff)       (http.server, stdlib)     (O_EXCL + stale-
        |                        |                   lock detection)
        |                        |                         |
        +------------------------+-------------------------+
                                 |
        +------------------------+------------------------+
        |                        |                         |
   Pack-Aware Store       Delta-Aware GC            Path-History Index
   (loose OR packed,      (protects delta bases     (direct indexed
    transparent to         even with no direct       lookup for "every
    every command)          tree reference)           version of this
                                                        file")
        |                        |
        +------------------------+
                                 |
              Content-Addressable Object Store
             SHA-256 + zlib-or-raw + atomic rename
                                 |
                                Disk
```

```
File
  |
  v
SHA-256 hash --> already stored? --yes--> reference existing object
  |no
  v
Is there a natural delta base? (same path, earlier snapshot,
found via real tree diffs -- not name/size guessing)
  |yes                              |no
  v                                 v
try delta encoding          compress (or store raw,
  |                          whichever is smaller)
  v
keep delta ONLY if
smaller than plain
compression, else
fall back to plain
  |
  v
write to temp file, atomic rename into place
```

## 7. Detailed documentation

### Commands

**Core workflow:**

| Command | What it does |
|---|---|
| `init` | Initialize a repository |
| `snapshot -m "msg"` | Snapshot the current directory |
| `list` | List snapshot history |
| `diff <a> <b>` | Show changes between two snapshots |
| `restore <id> [--preview]` | Restore files from a snapshot |
| `verify` | Re-hash every object, report corruption |

**Inspection:**

| Command | What it does |
|---|---|
| `status` | Fast repository overview |
| `info` | Repository format version, hash algorithm, object encoding |
| `explain <id>` | Dedup/compression breakdown for one snapshot |
| `tag <id> <name>` | Name a snapshot for easy reference |
| `log <path>` *(v2)* | History of one file across all snapshots — 574x faster indexed lookup (0.0169 ms vs. 9.68 ms on the benchmark workload) |

**Storage internals:**

| Command | What it does |
|---|---|
| `trace <hash>` | Show which snapshots (and file paths) reference an object |
| `gc` | Delete unreachable objects — automatically delta-aware when needed |
| `snapshot-rm <id>` | Delete a snapshot's record |
| `pack <name>` *(v2)* | Consolidate loose objects into a pack, with delta compression |

**Proof & demo:**

| Command | What it does |
|---|---|
| `benchmark` *(v2)* | Real, fresh performance measurements — never touches your actual repo |
| `stress-test` *(v2)* | Proves concurrency safety and corruption-recovery live, with real concurrent processes and a real corrupted object |
| `demo [--init] [--snapshot]` | Generate a realistic sample repository |
| `serve` | Local web Repository Inspector |

Any command that takes a snapshot id also accepts a tag name in its place.

### Why not Git?

Git is one of the best-engineered pieces of software that exists —
this isn't trying to replace it. ChronoVault solves a narrower
problem (point-in-time recovery for any directory) with a much
smaller surface area: no branches, no merges, no staging area.

### Why not SQLite?

Because building the storage engine is the point. Using SQLite would
mean outsourcing the thing this project exists to demonstrate.

### Design decisions

**Why is `vault pack` delta-aware by default, not a separate flag?**
Delta compression only fires when a real "same path, later version"
relationship exists (found via genuine tree diffs), and it's kept
*only* if it's actually smaller than plain compression — worst case,
it costs nothing over plain packing. For the current implementation,
delta encoding is only retained when it improves on plain
compression, so there's no practical need for a disable flag.

**Why does `vault gc` need to know about deltas at all?** Because a
delta-encoded object cannot be reconstructed without its base. If a
snapshot referencing that base directly is deleted, ordinary tree-walk
reachability has no way to know the base is still needed — proven as
a real, reproducible bug scenario (see The bug-finding story above),
not just reasoned about.

**Why single-level deltas only, no chains?** Enforced independently in
two places (candidate selection *and* base resolution refuses to
recurse into another delta) — a chain would mean a lost/corrupted
object cascades to every object delta-encoded against it, transitively.
Not worth the compression gain at this project's scale.

**Why does locking wrap only 5 commands, not all 19?** Only
`snapshot`, `snapshot-rm`, `restore`, `gc`, and `tag` mutate
repository state. Read-only commands (`list`, `diff`, `status`,
`explain`, `trace`, `verify`, `info`, `serve`, `log`) gain nothing
from serialization and would only pay latency for no correctness
benefit.

**Why do `benchmark` and `stress-test` never touch the real
repository?** So they're safe to run repeatedly, including live in
front of a judge, without any risk to actual data — both operate
entirely in throwaway temp directories.

See `STDLIB.md` and `SECURITY.md` for v1's original design decisions
(WAL vs. atomic rename, no chunk-level dedup, no encryption, etc.) —
all still accurate for v2's foundation.

### Trade-offs

ChronoVault deliberately does not try to compete with Git's full
feature set. It gives up:

- branching and merging
- staging
- rename detection
- chunk-level deduplication
- encryption

In exchange, the implementation stays small, dependency-free, and
focused on one job: reliable point-in-time recovery, implemented and
measurable end to end rather than delegated to a library.

### Known limitations

Everything in v1's Known Limitations still applies (whole-file
dedup only, symlinks skipped, no encryption). v2 adds:

- **Delta compression is single-level, transaction-scoped to one
  `vault pack` run** — deltas aren't re-evaluated against newer bases
  in later `pack` runs.
- **The path-history index has no rename detection** — a file moved
  to a new path shows as two unrelated histories. Git can detect
  renames heuristically during history analysis (`diff`/`log`), but
  does not store renames as first-class metadata either.

### Project layout

```
chronovault/
|-- chronovault.py         entry point
|-- Makefile
|-- scripts/
|   |-- demo_v2.sh          one-command end-to-end v2 demo
|   |-- benchmark.py        v1's large-scale benchmark script
|   `-- check_dependencies.py
|-- vault/
|   |-- objects.py          content-addressable object store (v1, unmodified)
|   |-- snapshot.py         tree walking, snapshot records (v1, unmodified)
|   |-- diff.py             shared diff engine (v1, unmodified)
|   |-- restore.py          restore with preview/confirm/integrity check (v1)
|   |-- gc.py               mark-and-sweep GC (v1, unmodified)
|   |-- reporting.py        status, explain, tags (v1)
|   |-- demo.py             sample repository generator (v1)
|   |-- inspector.py        vault serve (v1)
|   |-- cli.py              argparse entry point -- the only v1 file v2 changes,
|   |                       since that's where every command gets wired
|   `-- experimental/
|       |-- lock.py                repository-wide locking
|       |-- packfile.py, packfile_v2.py   pack file formats (v1 exploration + fix)
|       |-- delta.py               rolling-hash delta algorithm
|       |-- delta_pack.py          delta-aware pack writer/reader (the real fix lives here)
|       |-- delta_gc.py            the delta-aware GC integration
|       |-- pack_aware_store.py    transparent loose+packed reads
|       |-- path_history.py        the `vault log` index
|       |-- benchmark_cmd.py       powers `vault benchmark`
|       `-- stress_test_cmd.py     powers `vault stress-test`
|-- tests/                  204 tests, stdlib unittest only
|-- STDLIB.md               every stdlib-for-package substitution (v1, still accurate)
|-- FORMAT.md               binary object/snapshot format (v1, still accurate)
|-- ARCHITECTURE.md         v1's original per-layer breakdown
|-- BENCHMARKS.md           v1's large-scale benchmark results
`-- SECURITY.md             v1's security review (path traversal fix, etc.)
```
