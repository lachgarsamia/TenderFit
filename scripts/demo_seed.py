"""Kept for existing instructions: load the fictional demo and apply its amendment. See docs/DEMO.md."""
import argparse

from tenderfit.db import connect, migrate
from tenderfit.demo.__main__ import amend, load, reset


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--fast", action="store_true", help="hashing embedder instead of the real models")
    a = p.parse_args()
    args = argparse.Namespace(fast=a.fast, fail_first=False, audit=True)
    with connect() as conn:
        migrate(conn)
        if a.reset:
            reset(conn, args)
        load(conn, args)
        amend(conn, args)


if __name__ == "__main__":
    main()
