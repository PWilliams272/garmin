from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.analysis.analysis_pipeline import fit_strength_curves_for_all
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit the hierarchical Bayesian load-repetition model to strength set "
        "detail and write per-exercise e1RM/e5RM/e8RM curves (with credible intervals) to "
        "curated/analyzed/strength/. Kept separate from manual_analyze_metrics.py because "
        "this samples via MCMC: it takes minutes and far more memory than the daily "
        "analyzer Lambda has. Run it after manual_analyze_metrics.py, on its own cadence.",
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated strength inputs are read from and curve outputs are written.",
    )
    parser.add_argument("--draws", type=int, default=1500, help="Posterior draws per chain.")
    parser.add_argument(
        "--tune", type=int, default=2500,
        help="Tuning (warmup) steps per chain. Below ~2500 this model's random-walk "
        "geometry doesn't mix -- see strength_curve.fit_strength_curves' tune default.",
    )
    parser.add_argument("--chains", type=int, default=2, help="Number of MCMC chains.")
    parser.add_argument(
        "--progress", action="store_true",
        help="Show the sampler progress bar (off by default so scheduled runs log cleanly).",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    fm = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=fm)
    fit_strength_curves_for_all(
        curated_store,
        draws=args.draws,
        tune=args.tune,
        chains=args.chains,
        progressbar=args.progress,
    )


if __name__ == "__main__":
    main()
