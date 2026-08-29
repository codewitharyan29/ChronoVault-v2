# Security Considerations

## What's actually enforced

- **Content integrity**: every object's SHA-256 hash is re-verified
  on every read (`ObjectStore.get()`), not just on write — a
  corrupted or tampered object is detected and rejected before its
  bytes are ever returned to a caller.
- **Path traversal protection**: tree entry names are validated at
  both serialization and deserialization — a name containing `/`,
  `\`, a null byte, or being exactly `.`/`..` is rejected as a
  corrupted object. This was **found as a real, exploitable bug**
  during manual testing while writing this file, not designed in from
  the start: a hand-crafted tree entry named `../../evil.txt` was
  confirmed (via a live reproduction, since fixed and turned into a
  regression test) to cause `restore` to write a file outside the
  target directory. Fixed by validating every entry name at the point
  untrusted bytes become a trusted path component, in both
  `serialize_tree()` and `deserialize_tree()`. See
  `tests/test_snapshot.py` / `tests/test_restore.py` for the
  regression tests (including one
  that verifies the fix at the actual `restore` layer, not just the
  validator in isolation).
- **No arbitrary code execution on read**: object and snapshot
  serialization deliberately avoids `pickle` (whose `load()` can
  execute arbitrary code embedded in the data) in favor of a
  hand-rolled binary format for objects and plain JSON for snapshot
  metadata — reading a ChronoVault repository, even a maliciously
  crafted one, cannot execute code just by being read.
- **Atomic writes**: every write (objects, snapshot records, restored
  files, the tag map, the id counter) goes through a temp-file-then-
  `os.replace()` pattern, so a process crash or power loss mid-write
  never leaves a half-written file in place of a good one.

## What's explicitly out of scope

- **No encryption.** Objects on disk are readable by anyone with
  filesystem access — the same threat model as an unencrypted Git
  repository. Adding encryption correctly (key management, IV/nonce
  handling, authentication tags) is real, careful work; doing it
  hastily would be worse than not doing it. Out of scope for v1.
- **No authentication or access control.** ChronoVault is a local,
  single-user tool. `vault serve`'s inspector binds to `localhost`
  only and has no auth — it's meant to be run on your own machine,
  not exposed to a network.
- **Single-writer model, no locking.** Running `vault snapshot` from
  multiple processes against the same repository concurrently is
  unsupported and could corrupt repository state. Documented, not
  solved — see the README's Known Limitations.
- **Symlinks are skipped**, not followed. This was also a genuine bug
  fix, not a day-one decision: the code originally claimed to skip
  symlinks in a comment, but the check was never actually implemented
  — a symlinked directory pointing at an ancestor could have caused
  infinite recursion during a snapshot walk. Fixed, with a regression
  test covering the cycle case directly.

## Responsible disclosure

This is a hackathon project, not a maintained security-critical
product. If you find something beyond what's listed here, open an
issue on the repository.
