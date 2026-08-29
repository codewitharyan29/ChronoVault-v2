# Architecture

## Layers

```
                    vault CLI (argparse)
                            |
        +-------------------+--------------------+
        |                                         |
  Snapshot Engine                       Repository Inspector
  (vault/snapshot.py)                   (vault/inspector.py,
        |                                stdlib http.server)
        |                                         |
        +-------------------+--------------------+
                            |
        +-------------------+--------------------+
        |                   |                     |
  Diff Engine          GC / Trace            Reporting
  (vault/diff.py)      (vault/gc.py)         (vault/reporting.py)
                            |
                            v
              Content-Addressable Object Store
                    (vault/objects.py)
                SHA-256 + zlib-or-raw + atomic rename
                            |
                           Disk
```

Every higher layer reads and writes through the Object Store — there
is no layer that touches the filesystem directly except the object
store itself (for objects) and restore.py (for reconstructing real
files, which is a distinct concern from object storage).

## The object store (`vault/objects.py`)

The foundation everything else is built on. Content-addressed: a
piece of data's identity *is* its SHA-256 hash, computed over the
original (uncompressed) bytes. Two pieces of identical content always
produce the same hash, which is what makes whole-file deduplication
essentially free — storing a file that already exists is a no-op
lookup, not a write.

```
put(data):
    hash = SHA-256(data)
    if hash already exists on disk:
        return (deduplicated, no write)
    compressed = zlib.compress(data)
    stored = compressed if smaller than raw else raw   (+ 2-byte header:
                                                          version + encoding marker)
    write to temp file in the same directory
    os.replace(temp file, final path)     <- atomic rename, this IS the
                                              crash-safety mechanism
```

```
get(hash):
    read stored bytes from disk
    decompress if marked compressed
    verify SHA-256(decompressed) == hash    <- get() is a STRICT read;
                                                it never returns
                                                unverified data
    return decompressed bytes
```

Storage layout mirrors Git's fan-out convention to avoid one huge flat
directory:

```
.vault/
  objects/
    4f/89ab...     <- object hash "4f89ab..." split at 2 chars
    9d/67...
  snapshots/
    1
    2
  next_id           <- monotonic counter for snapshot ids
  tags.json         <- name -> snapshot id map
```

## The snapshot layer (`vault/snapshot.py`)

Turns a directory on disk into an immutable, addressable tree.

```
_walk_into_tree(directory):
    for each entry in directory, sorted by name:
        if symlink: skip (see README's Known Limitations)
        if directory: recurse, get back a tree hash
        if file: read bytes, store as a blob, get back a blob hash
    serialize the (name, kind, hash) list deterministically
    store that serialized bytes as a tree object
    return the tree object's hash
```

Sorting entries by name before serializing is what makes the tree
hash a pure function of *contents* — the same directory contents
always produce the same tree hash regardless of the order the
filesystem happened to return entries in.

A snapshot record is `{id, timestamp, parent, root_tree_hash, message,
stats}`, stored as JSON (not binary — snapshot records are small,
infrequent, and benefit more from being human-readable than from
being compact). `parent` chains snapshots into a linear history.

Concretely, for a small project, a snapshot's object graph looks like:

```
Snapshot #3
    |
    v
Root Tree
    |
    +-- file.py    -> Blob (SHA-256 of file.py's content)
    +-- README.md  -> Blob (SHA-256 of README.md's content)
    +-- src/       -> Tree
    |       +-- main.py  -> Blob
    |       `-- utils.py -> Blob (same hash as an identical file
    |                        elsewhere -- this IS the dedup)
```

Every arrow is a hash reference, not a copy — the snapshot object
doesn't contain file contents, it contains one hash (the root tree's),
and everything below is reached by following hashes recursively.

## Diff engine (`vault/diff.py`)

One comparison algorithm, two callers — this is deliberate, not
incidental:

```
              diff_trees(A, B)
                     |
        +------------+------------+
        |                         |
  vault diff <a> <b>      restore --preview
  (two stored snapshots)  (current working dir
                            vs. one snapshot)
```

Both reduce to "given two {path: content_hash} maps, what's added,
removed, or modified?" — comparing hashes, not file contents, so a
diff never needs to read a file's actual bytes to know it's unchanged.

## GC and trace (`vault/gc.py`)

Also one shared computation:

```
compute_reachable_objects():
    for every snapshot that currently exists:
        walk its tree, recording every object hash reached

  vault gc                          vault trace <hash>
  delete everything NOT             report which snapshots'
  in the reachable set              walks reached this hash
```

Deletion in ChronoVault is two-phase, matching how Git handles it:
`snapshot-rm` removes a snapshot's *record* only; the objects it
uniquely owned aren't freed until a subsequent `gc` run recomputes
reachability from whatever snapshots remain. This keeps deletion fast
(no reachability walk needed just to remove a pointer) and safe (gc
always recomputes fresh from current state, so it can never be tricked
by stale cached reachability info).

## Restore (`vault/restore.py`)

```
preview_restore(snapshot):
    diff = current working directory vs. snapshot's tree
    for every object the restore would need to WRITE
    (added + modified files only):
        verify its integrity
    return (diff, any integrity issues found)

apply_restore(snapshot):
    preview_restore first -- if anything failed integrity, ABORT,
    write nothing
    otherwise, for each added/modified file:
        read the (already-verified) object
        write to a temp file, then atomic rename into place
```

Restore is non-destructive by design — files that exist on disk but
aren't part of the target snapshot are left alone, never deleted.

## Repository Inspector (`vault/inspector.py`)

A single HTML page (dark theme, no framework, no CDN) plus five JSON
endpoints, served by stdlib `http.server`/`socketserver`. The routing
logic is a pure function (`route()`) separate from the actual
`BaseHTTPRequestHandler` subclass, so it's unit-testable without
opening a real socket — only a handful of manual/live tests actually
exercise the real HTTP layer.

## Why this shape

Every layer above the object store exists to answer one question
about the same underlying data, not to introduce a new subsystem:
diff and restore-preview share a comparison; gc and trace share a
reachability walk; status and explain both just read snapshot
metadata differently. The object store is the only place with
genuinely new logic per layer — everything above it is composition.
