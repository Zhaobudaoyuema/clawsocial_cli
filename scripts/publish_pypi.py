#!/usr/bin/env python3
"""
Build sdist/wheel and upload to PyPI (production).

  pip install -e ".[dev]"   # needs build + twine

  python scripts/publish_pypi.py              # bump patch → build + upload PyPI
  python scripts/publish_pypi.py --build-only # build only（默认不改版本号）

版本号：以 pyproject.toml 的 version 为准，同步写入 clawsocial/__init__.py。
正式上传 PyPI 时默认将 patch +1，避免重复上传同版本失败。

For upload, set (recommended):

  TWINE_USERNAME=__token__
  TWINE_PASSWORD=<pypi-API-token>

If upload fails with "license-file" / "license-expression" metadata errors, upgrade
packaging (twine uses it to validate Metadata 2.4): pip install -U "packaging>=25"
"""
from __future__ import annotations

import argparse
import glob
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT_PY = ROOT / "clawsocial" / "__init__.py"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd or ROOT)
    if r.returncode != 0:
        sys.exit(r.returncode)


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        sys.exit("error: pyproject.toml has no version = \"...\" line")
    return m.group(1)


def write_versions(new_version: str) -> None:
    pt = PYPROJECT.read_text(encoding="utf-8")
    pt_new, n = re.subn(
        r'(^version\s*=\s*")([^"]+)(")',
        rf"\g<1>{new_version}\g<3>",
        pt,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        sys.exit("error: failed to replace version in pyproject.toml")
    PYPROJECT.write_text(pt_new, encoding="utf-8")

    it = INIT_PY.read_text(encoding="utf-8")
    it_new, n = re.subn(
        r'(^__version__\s*=\s*")([^"]+)(")',
        rf"\g<1>{new_version}\g<3>",
        it,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        sys.exit("error: failed to replace __version__ in clawsocial/__init__.py")
    INIT_PY.write_text(it_new, encoding="utf-8")


def parse_semver(v: str) -> tuple[int, int, int]:
    # 仅支持 X.Y.Z 三段数字（与当前项目一致）
    parts = v.strip().split(".")
    if len(parts) != 3:
        sys.exit(f"error: version must be MAJOR.MINOR.PATCH (got {v!r})")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        sys.exit(f"error: invalid version segment in {v!r}")


def bumped_version(current: str, kind: str) -> str:
    major, minor, patch = parse_semver(current)
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "major":
        return f"{major + 1}.0.0"
    sys.exit(f"error: unknown bump kind {kind!r}")


def resolve_bump_action(
    *,
    build_only: bool,
    no_bump: bool,
    bump: str | None,
) -> str | None:
    """返回 'patch'|'minor'|'major' 或 None（不修改版本文件）。"""
    if no_bump:
        return None
    if bump is not None:
        return bump
    if build_only:
        return None
    return "patch"


def main() -> None:
    p = argparse.ArgumentParser(description="Build and upload to PyPI.")
    p.add_argument(
        "--build-only",
        action="store_true",
        help="只构建，不上传。",
    )
    p.add_argument(
        "--no-clean",
        action="store_true",
        help="构建前不删除 dist/。",
    )
    p.add_argument(
        "--no-bump",
        action="store_true",
        help="不修改版本号（上传已发布的同一版本仍会遭 PyPI 拒绝）。",
    )
    p.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default=None,
        metavar="LEVEL",
        help="发布前递增版本：patch / minor / major。"
        " 省略时：上传 PyPI 默认 patch；仅 --build-only 时不递增除非写明本选项。",
    )
    args = p.parse_args()

    bump_kind = resolve_bump_action(
        build_only=args.build_only,
        no_bump=args.no_bump,
        bump=args.bump,
    )
    if bump_kind:
        old_v = read_pyproject_version()
        new_v = bumped_version(old_v, bump_kind)
        write_versions(new_v)
        print(f"Version {old_v} → {new_v} ({bump_kind})", flush=True)

    dist = ROOT / "dist"
    if not args.no_clean and dist.exists():
        shutil.rmtree(dist)

    run([sys.executable, "-m", "build"], cwd=ROOT)

    if args.build_only:
        print(f"Done. Artifacts: {dist}", flush=True)
        return

    artifacts = sorted(glob.glob(str(dist / "*")))
    if not artifacts:
        print("error: dist/ is empty after build", file=sys.stderr)
        sys.exit(1)

    upload_cmd = [sys.executable, "-m", "twine", "upload"] + artifacts

    run(upload_cmd, cwd=ROOT)


if __name__ == "__main__":
    main()
