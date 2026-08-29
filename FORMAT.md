# Object & Snapshot Format

The exact on-disk byte layout, for anyone verifying the zero-dependency
claim or reading the storage engine's actual output.

## Object storage layout

```
.vault/objects/<hash[:2]>/<hash[2:]>
```

An object's SHA-256 hash (hex, 64 characters) is split at the first 2
characters to form a subdirectory — the same fan-out convention Git
uses, to avoid one directory holding tens of thousands of files.

## Object file format

```
+----------------+------------------+------------------------+
| version (1B)   | encoding (1B)    | payload (N bytes)       |
+----------------+------------------+------------------------+
```

- **version**: currently `0x01`. Lets a future format change branch on
  this byte without breaking the ability to read objects written by
  this version.
- **encoding**: `'Z'` (compressed, zlib) or `'R'` (raw, uncompressed).
  Each object independently picks whichever is smaller — zlib adds
  ~11 bytes of header/footer overhead per object, which means naive
  "always compress" can make *small* objects larger than the
  original. Rather than accept that, `put()` compares both and stores
  the smaller one.
- **payload**: the compressed or raw bytes.

An object's identity (its filename/hash) is always computed over the
**original, uncompressed** content — never over the stored bytes. This
means verification doesn't depend on compression being deterministic
across runs or platforms.

### Reading (`get()`)

```
read stored bytes
check version byte matches what this code understands
branch on encoding byte:
    'Z' -> zlib.decompress(payload)
    'R' -> payload as-is
recompute SHA-256 of the result
compare to the object's hash (its filename)
    mismatch -> ObjectCorruptedError
    match    -> return the bytes
```

This means `get()` is a **strict read**: any caller either receives
provably-correct data, or an exception. There's no path that returns
silently-corrupted bytes.

## Tree object format

A tree object's *payload* (before the version/encoding header above —
trees are objects like any other, stored through the same `put()`)
is a sequence of entries, each:

```
+--------+------------------+------------------+------------------------+
| kind   | name_len (2B,BE) | name (UTF-8,      | object hash             |
| (1B)   |                  |  name_len bytes)  | (ASCII hex, 64 bytes)   |
+--------+------------------+------------------+------------------------+
```

- **kind**: `'b'` (blob) or `'t'` (tree). Any other byte is rejected
  as corrupted — not silently treated as one or the other.
- **name_len**: big-endian 16-bit length of the name that follows.
- **name**: the file or directory's name, UTF-8 encoded. **Validated
  on both write and read**: must be non-empty, must not contain `/`,
  `\`, or a null byte, and must not be exactly `.` or `..`. This
  isn't a formatting nicety — it closes a real path-traversal bug
  found during manual testing (see `SECURITY.md`), where a hand-
  crafted entry name like `../../evil.txt` was confirmed to make
  `restore` write outside the target directory before this check
  existed.
- **object hash**: 64 ASCII hex characters (the referenced blob or
  sub-tree's SHA-256). Validated as actual hex on read — a malformed
  hash is rejected rather than silently accepted.

Entries are written **sorted by name** before serialization. This is
what makes a tree's hash a pure function of its *contents* — the same
directory contents always produce the identical tree object,
regardless of what order the filesystem happened to return entries in
during the walk.

No length prefix or delimiter is needed between entries because every
field either has a fixed size or is itself length-prefixed — the
format is self-delimiting.

## Snapshot record format

Stored as UTF-8 JSON, not binary — snapshot records are small,
created infrequently (once per `vault snapshot`, not once per file),
and benefit more from being human-inspectable than from being compact.

```json
{
  "id": 2,
  "timestamp": 1754417178.234,
  "parent": 1,
  "root_tree_hash": "8a6a17e561cf...",
  "message": "after refactor",
  "stats": {
    "files": 11,
    "new_objects": 1,
    "reused_objects": 10,
    "original_bytes": 11340,
    "compressed_bytes": 5988
  }
}
```

Stored at `.vault/snapshots/<id>` (a plain file named by the numeric
id, not hashed — snapshot records aren't content-addressed the way
objects are, since their id is their identity).

## Supporting files

```
.vault/next_id     plain text integer — the next snapshot id to assign.
                    Monotonic: never decreases, even across deletions,
                    so ids stay unique for the life of the repository.

.vault/tags.json   {"tag-name": snapshot_id, ...} — a simple name-to-id
                    map for `vault tag` / tag-based references.
```

Both are written the same crash-safe way as objects: temp file, then
`os.replace()`.

## Why not just use `pickle`?

`pickle.load()` can execute arbitrary code embedded in the pickled
data — a real risk even for a tool that only reads its own files,
since a corrupted or tampered `.vault` directory shouldn't be able to
run code just by being read. The hand-rolled binary format above has
no such risk, and designing it was more in the spirit of a
zero-dependency storage-engine hackathon besides.
