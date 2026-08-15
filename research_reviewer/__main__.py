# -*- coding: utf-8 -*-
"""CLI: python -m research_reviewer [batch|yield]"""
from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "yield":
        from .yield_report import main as yield_main
        return yield_main(argv[1:])
    from .batch import main as batch_main
    # Let argparse read sys.argv directly
    return batch_main(None)


if __name__ == "__main__":
    raise SystemExit(main())
