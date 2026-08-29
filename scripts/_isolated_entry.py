#!/usr/bin/env python3
"""
scripts/_isolated_entry.py

Invokes ChronoVault's real CLI under Python's fully isolated mode
(-I): no PYTHONPATH, no user site-packages, no system site-packages
influence -- the strongest proof Python itself offers that nothing
outside the standard library is being used.

One real quirk, found while building this, not assumed: -I also
drops the invoked script's OWN directory from sys.path (normally
auto-added), so `python3 -I chronovault.py ...` fails with
ModuleNotFoundError before even reaching any real dependency check --
not a third-party dependency problem, a Python isolated-mode
behavior. This wrapper adds the project root explicitly, which -I
does not prevent (it isolates against PYTHONPATH and site-packages,
not against an explicit sys.path.insert of the project's own code).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vault.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
