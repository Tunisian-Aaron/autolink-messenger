#!/usr/bin/env python3
"""Set up and start AutoLink Messenger with one command:

    python3 start.py          (Windows: py start.py)

The first run creates a private Python environment in .venv, installs what the tool needs
(Playwright and its Chromium browser, about 300 MB, and the Anthropic SDK) and opens the web
page. Later runs skip straight to the page. Nothing is installed outside this folder, apart
from Playwright's browser, which goes in its own cache folder.

Anything after `start.py` is passed on to the web page's server, e.g. `python3 start.py --port 9000`.
`python3 start.py --setup-only` installs without starting.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
REQUIREMENTS = HERE / "requirements.txt"
STAMP = VENV / ".autolink-ready"  # holds the hash of the requirements that are installed
MIN_PYTHON = (3, 9)


def venv_python(venv: Path = VENV) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirements_hash(path: Path = REQUIREMENTS) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def needs_setup(venv: Path = VENV, stamp: Path = STAMP, requirements: Path = REQUIREMENTS) -> bool:
    """True until everything in requirements.txt (as it is now) has been installed."""
    return not (venv_python(venv).exists() and stamp.exists()
                and stamp.read_text(encoding="utf-8").strip() == requirements_hash(requirements))


def run(cmd, what: str):
    print(f"\n== {what} ==", flush=True)
    if subprocess.call([str(c) for c in cmd]) != 0:
        raise SystemExit(f"\nThat step failed ({what}). Fix what it reports above and run start.py again; "
                         "it continues where it stopped.")


def setup():
    if not venv_python().exists():
        try:
            import venv  # noqa: F401
        except ImportError:
            raise SystemExit("This Python can't create environments. On Debian/Ubuntu: sudo apt install python3-venv")
        run([sys.executable, "-m", "venv", VENV], "Creating a private Python environment in .venv")
    py = venv_python()
    run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", REQUIREMENTS],
        "Installing the tool's Python packages (a minute or two)")
    run([py, "-m", "playwright", "install", "chromium"],
        "Installing the automation browser (Chromium, a one-time download)")
    STAMP.write_text(requirements_hash(), encoding="utf-8")
    print("\nSetup is done.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"AutoLink Messenger needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
                         f"{sys.version.split()[0]}. Install a newer one from https://www.python.org/downloads/")
    setup_only = "--setup-only" in argv
    argv = [a for a in argv if a != "--setup-only"]
    if needs_setup():
        print("First run: setting things up. This takes a few minutes and happens once.")
        setup()
    if setup_only:
        print("Start the tool any time with: python3 start.py")
        return
    try:
        raise SystemExit(subprocess.call([str(venv_python()), str(HERE / "webapp.py"), *argv]))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
