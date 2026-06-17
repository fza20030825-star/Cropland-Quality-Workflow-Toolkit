"""Path helpers for the local project layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    """Common project paths resolved from the repository root."""

    workflow_root: Path
    course_root: Path
    configs_dir: Path
    data_dir: Path
    raw_data_dir: Path
    interim_data_dir: Path
    processed_data_dir: Path
    outputs_dir: Path
    logs_dir: Path


def find_workflow_root(start: Path | None = None) -> Path:
    """Find the folder that contains pyproject.toml."""

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current


def resolve_paths(start: Path | None = None) -> ProjectPaths:
    """Resolve all well-known project directories."""

    workflow_root = find_workflow_root(start)
    env_root = os.getenv("CQE_PROJECT_ROOT")
    course_root = Path(env_root).resolve() if env_root else workflow_root.parent.resolve()

    data_dir = workflow_root / "data"
    outputs_dir = workflow_root / "outputs"
    return ProjectPaths(
        workflow_root=workflow_root,
        course_root=course_root,
        configs_dir=workflow_root / "configs",
        data_dir=data_dir,
        raw_data_dir=data_dir / "raw",
        interim_data_dir=data_dir / "interim",
        processed_data_dir=data_dir / "processed",
        outputs_dir=outputs_dir,
        logs_dir=outputs_dir / "logs",
    )


def ensure_project_dirs(paths: ProjectPaths) -> None:
    """Create runtime folders that are safe to create locally."""

    for directory in (
        paths.configs_dir,
        paths.raw_data_dir,
        paths.interim_data_dir,
        paths.processed_data_dir,
        paths.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
