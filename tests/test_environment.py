from cropland_quality_update.paths import resolve_paths


def test_paths_resolve():
    paths = resolve_paths()
    assert paths.workflow_root.name
    assert paths.course_root.exists()
