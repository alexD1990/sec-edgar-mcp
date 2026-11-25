"""Command line for the funnel.

    python -m sec_edgar_mcp.funnel stage1                 # screening -> runs/<today>/stage1.csv
    python -m sec_edgar_mcp.funnel stage2                 # light analysis -> stage2.csv + sheets/
    python -m sec_edgar_mcp.funnel all                    # both
    python -m sec_edgar_mcp.funnel params                 # show default thresholds

Options:
    --stockdocs PATH     stockdocs repo (default: $STOCKDOCS_DIR or ../../stockdocs next to this repo)
    --date YYYY-MM-DD    run folder name and "today" for look-back windows (default: today)
    --set key=value      override a threshold, e.g. --set min_revenue_growth_pct=25 (repeatable)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import anyio

from ..config import PROJECT_ROOT, load_env
from .params import Params


def stockdocs_dir(arg: str | None) -> Path:
    path = Path(arg or os.environ.get("STOCKDOCS_DIR") or PROJECT_ROOT.parent.parent / "stockdocs").expanduser()
    if not path.is_dir():
        sys.exit(f"Fant ikke stockdocs-mappen: {path}. Bruk --stockdocs PATH eller sett STOCKDOCS_DIR.")
    return path


def latest_run_with_stage1(runs: Path) -> Path | None:
    candidates = sorted(p for p in runs.glob("*") if (p / "stage1.csv").is_file())
    return candidates[-1] if candidates else None


async def main_async(args: argparse.Namespace, p: Params) -> None:
    from ..apiclient import get_api_client
    from ..market import all_closes
    from ..sec_client import get_client
    from .run import run_stage1, run_stage2

    today = date.fromisoformat(args.date) if args.date else date.today()
    runs = stockdocs_dir(args.stockdocs) / "runs"
    run_dir = runs / today.isoformat()
    client = get_client()

    if args.command in ("stage1", "all"):
        if not os.environ.get("MASSIVE_API_KEY"):
            sys.exit("Steg 1 trenger kurs: legg MASSIVE_API_KEY i .env.")
        prices = await all_closes(get_api_client("massive"), today=today)
        await run_stage1(client, prices, p, run_dir, today)

    if args.command in ("stage2", "all"):
        if not (run_dir / "stage1.csv").is_file():
            found = latest_run_with_stage1(runs)
            if found is None:
                sys.exit("Fant ingen stage1.csv. Kjør steg 1 først.")
            print(f"Bruker steg 1 fra {found.name}", file=sys.stderr)
            run_dir = found
        await run_stage2(client, p, run_dir, today)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m sec_edgar_mcp.funnel", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["stage1", "stage2", "all", "params"])
    parser.add_argument("--stockdocs")
    parser.add_argument("--date")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    p = Params()
    try:
        for assignment in args.set:
            p.set(assignment)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.command == "params":
        print(p.to_json())
        return

    load_env()
    if not os.environ.get("SEC_USER_AGENT"):
        sys.exit(f"SEC_USER_AGENT mangler (i miljøet eller {PROJECT_ROOT / '.env'}).")
    try:
        anyio.run(main_async, args, p)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
