"""CLI entry: python -m homefinder [--config config.yaml] [--seed] [--dry-run]"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .pipeline import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homefinder")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="populate state without scoring or notifying (run this first)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the pipeline but print notifications and roll back state writes",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process at most N listings (debug)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    stats = run(cfg, seed=args.seed, dry_run=args.dry_run, limit=args.limit)
    logging.getLogger(__name__).info(
        "run %s finished: fetched=%d geo=%d filtered=%d new=%d changed=%d scored=%d notified=%d",
        stats.run_id, stats.n_fetched, stats.n_after_geo, stats.n_after_filters,
        stats.n_new, stats.n_changed, stats.n_scored, stats.n_notified,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
