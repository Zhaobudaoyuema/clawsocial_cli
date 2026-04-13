# clawsocial/__main__.py
"""python -m clawsocial 入口：默认走 CLI，--daemon 走 daemon"""
from __future__ import annotations

import argparse
import sys

from clawsocial import cli


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--workspace", type=str)
    parser.add_argument("--port", type=int)
    args, remaining_argv = parser.parse_known_args()

    if args.daemon:
        if not args.workspace:
            print("error: --workspace required for daemon mode", file=sys.stderr)
            sys.exit(1)
        import os
        from pathlib import Path
        from clawsocial.daemon import _main
        _main(Path(os.path.expanduser(args.workspace)), args.port)
    else:
        sys.exit(cli.main(remaining_argv))


if __name__ == "__main__":
    main()
