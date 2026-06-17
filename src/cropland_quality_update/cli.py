"""Command-line interface for the workflow project."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from .paths import ensure_project_dirs, resolve_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cropland-quality-workflow-toolkit",
        description="Cropland Quality Workflow Toolkit entry point.",
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Print environment and project path diagnostics.",
    )
    return parser


def has_module(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def print_environment_report() -> None:
    paths = resolve_paths(Path.cwd())
    ensure_project_dirs(paths)

    print("Cropland Quality Workflow Toolkit.")
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")
    print(f"Workflow root: {paths.workflow_root}")
    print(f"Course root: {paths.course_root}")
    print(f"Config dir: {paths.configs_dir}")
    print(f"Raw data dir: {paths.raw_data_dir}")
    print(f"Interim data dir: {paths.interim_data_dir}")
    print(f"Processed data dir: {paths.processed_data_dir}")
    print(f"Outputs dir: {paths.outputs_dir}")
    print(f"arcpy available: {has_module('arcpy')}")
    print(f"openpyxl available: {has_module('openpyxl')}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_env:
        print_environment_report()
        return 0

    print_environment_report()
    print("")
    print("GUI workflow entry points:")
    print("  1. python run_merge_shp_ui.py")
    print("  2. python run_membership_ui.py")
    print("  3. python run_update_scores_ui.py")
    print("  4. python run_update_land_blocks_ui.py")
    print("  5. python run_area_balance_ui.py")
    print("")
    print("Read README.md for quick use and docs/workflow_manual.qmd for detailed audit rules.")
    return 0
