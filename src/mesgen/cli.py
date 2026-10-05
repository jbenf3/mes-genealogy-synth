"""mesgen command line: mesgen generate --scenario NAME --out FOLDER."""

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    """Run the mesgen command line.

    Args:
        argv: Command-line arguments. Defaults to sys.argv.

    Returns:
        Exit code: 0 on success, 2 if the output directory is not empty.
    """
    ap = argparse.ArgumentParser(
        prog="mesgen", description="Deterministic synthetic MES genealogy data."
    )
    ap.add_argument("--version", action="version", version=f"mesgen {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser(
        "generate",
        help="simulate a scenario into a new folder: event files, inputs, churn log, manifest",
    )
    g.add_argument(
        "--scenario",
        required=True,
        help="bundled scenario name (official_1x, smoke) or path to a YAML file",
    )
    g.add_argument("--out", required=True)
    g.add_argument(
        "--seed", type=int, default=None, help="use another seed (changes the data)"
    )
    g.add_argument(
        "--profile",
        default=None,
        help="use another profile, by name or path (changes the data)",
    )
    g.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    from .generate import generate

    try:
        generate(
            args.scenario,
            args.out,
            seed=args.seed,
            profile_ref=args.profile,
            quiet=args.quiet,
        )
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
