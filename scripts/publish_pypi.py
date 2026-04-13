#!/usr/bin/env python3
"""
Build sdist/wheel and optionally upload to PyPI / TestPyPI.

  pip install -e ".[dev]"   # needs build + twine

  python scripts/publish_pypi.py              # build only
  python scripts/publish_pypi.py --upload     # build + upload to PyPI
  python scripts/publish_pypi.py --testpypi   # build + upload to TestPyPI

For upload, set (recommended):

  TWINE_USERNAME=__token__
  TWINE_PASSWORD=<pypi-API-token>

If upload fails with "license-file" / "license-expression" metadata errors, upgrade
packaging (twine uses it to validate Metadata 2.4): pip install -U "packaging>=25"
"""
from __future__ import annotations

import argparse
import glob
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd or ROOT)
    if r.returncode != 0:
        sys.exit(r.returncode)


def main() -> None:
    p = argparse.ArgumentParser(description="Build and optionally publish to PyPI.")
    p.add_argument(
        "--upload",
        action="store_true",
        help="Upload dist/* to PyPI (production).",
    )
    p.add_argument(
        "--testpypi",
        action="store_true",
        help="Upload dist/* to TestPyPI instead of PyPI.",
    )
    p.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not remove dist/ before build.",
    )
    args = p.parse_args()

    if args.upload and args.testpypi:
        print("error: use only one of --upload or --testpypi", file=sys.stderr)
        sys.exit(2)

    dist = ROOT / "dist"
    if not args.no_clean and dist.exists():
        shutil.rmtree(dist)

    run([sys.executable, "-m", "build"], cwd=ROOT)

    if not args.upload and not args.testpypi:
        print(f"Done. Artifacts: {dist}", flush=True)
        return

    artifacts = sorted(glob.glob(str(dist / "*")))
    if not artifacts:
        print("error: dist/ is empty after build", file=sys.stderr)
        sys.exit(1)

    upload_cmd = [sys.executable, "-m", "twine", "upload"]
    if args.testpypi:
        upload_cmd += ["--repository", "testpypi"]
    upload_cmd += artifacts

    run(upload_cmd, cwd=ROOT)


if __name__ == "__main__":
    main()
