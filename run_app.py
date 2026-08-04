"""Launch the Servey OCR desktop app.

Starts the local FastAPI server and opens the UI in the default browser. Run:

    python run_app.py

The app is served at http://127.0.0.1:8000 and is never exposed to the network
(host is loopback only). Close the terminal window to stop it.
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import webbrowser
from pathlib import Path

# Make sure the repo root is importable when launched by double-click.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "127.0.0.1"
PORT = 8000


def _open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


def main() -> None:
    import uvicorn

    from app.server import app  # imported lazily so a frozen build resolves cleanly

    print("=" * 56)
    print("  Servey OCR  —  http://%s:%d" % (HOST, PORT))
    print("  Close this window to stop the app.")
    print("=" * 56)
    # Open the browser shortly after the server starts accepting connections.
    threading.Timer(1.2, _open_browser).start()
    # Pass the app object (not an import string) so PyInstaller does not need to
    # re-import by name from a frozen module.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # no-op in dev; required for a frozen build
    main()
