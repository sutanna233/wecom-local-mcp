"""Create an isolated virtual environment for the WeCom MCP package.

The .venv directory is intentionally created on the target machine rather than
copied between machines. No project source or message data is modified.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def venv_python(destination: Path) -> Path:
    return destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=ROOT / ".venv")
    parser.add_argument("--skip-install", action="store_true", help="create only; used by package tests")
    parser.add_argument("--offline", action="store_true", help="install only from the local wheels directory")
    args = parser.parse_args()
    destination = args.dest.resolve()
    python = venv_python(destination)
    if not python.exists():
        print(f"CREATE_VENV={destination}")
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(destination)
    if not python.exists():
        raise RuntimeError(f"virtual environment Python was not created: {python}")
    if not args.skip_install:
        requirements = ROOT / "requirements.txt"
        command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        install_env = os.environ.copy()
        install_env.update({
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        })
        if args.offline:
            wheels = ROOT / "wheels"
            if not wheels.is_dir() or not any(wheels.glob("*.whl")):
                raise RuntimeError("--offline requires at least one wheel in the wheels directory")
            command += ["--no-index", "--find-links", str(wheels), "--only-binary", ":all:"]
            install_env["PIP_NO_INDEX"] = "1"
        command += ["-r", str(requirements)]
        subprocess.run(command, check=True, env=install_env)
        subprocess.run(
            [str(python), "-c", "import Crypto; print('PYCRYPTODOME=OK')"],
            check=True,
            env=install_env,
        )
    print(f"VENV_PYTHON={python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
