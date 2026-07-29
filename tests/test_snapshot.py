from __future__ import annotations

import json

from replayable.snapshot import (
    create_snapshot,
    diff_file_manifests,
    load_recorded_snapshot,
)


def test_snapshot_is_deterministic_across_mtime_and_creation_order(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "b.txt").write_text("same\n", encoding="utf-8")
    (first / "a.txt").write_text("also same\n", encoding="utf-8")
    (second / "a.txt").write_text("also same\n", encoding="utf-8")
    (second / "b.txt").write_text("same\n", encoding="utf-8")
    (second / "a.txt").touch()

    first_result = create_snapshot(first, tmp_path / "out-one")
    second_result = create_snapshot(second, tmp_path / "out-two")

    assert first_result.sha256 == second_result.sha256
    assert first_result.archive_path.read_bytes() == second_result.archive_path.read_bytes()
    assert first_result.files == second_result.files


def test_snapshot_round_trip_and_file_diff(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "kept.txt").write_text("before", encoding="utf-8")
    (workspace / "removed.txt").write_text("gone", encoding="utf-8")
    baseline = create_snapshot(workspace, tmp_path / "cassette")

    (workspace / "kept.txt").write_text("after", encoding="utf-8")
    (workspace / "removed.txt").unlink()
    (workspace / "added.txt").write_text("new", encoding="utf-8")
    replay = create_snapshot(workspace, tmp_path / "replay")

    digest, files = load_recorded_snapshot(tmp_path / "cassette")
    assert digest == baseline.sha256
    assert files == baseline.files
    assert diff_file_manifests(files, replay.files) == {
        "added": ["added.txt"],
        "removed": ["removed.txt"],
        "changed": ["kept.txt"],
    }
    assert json.loads(
        (tmp_path / "cassette" / "workspace.files.json").read_text(encoding="utf-8")
    ) == baseline.files


def test_snapshot_records_symlink_target_without_following_it(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("contents", encoding="utf-8")
    (workspace / "link.txt").symlink_to("target.txt")

    result = create_snapshot(workspace, tmp_path / "cassette")

    link = next(item for item in result.files if item["path"] == "link.txt")
    assert link["type"] == "symlink"
    assert link["size"] == len("target.txt")


def test_snapshot_diff_reports_empty_directories_and_mode_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "existing-empty"
    existing.mkdir(mode=0o755)
    baseline = create_snapshot(workspace, tmp_path / "cassette")

    existing.chmod(0o700)
    (workspace / "added-empty").mkdir()
    replay = create_snapshot(workspace, tmp_path / "replay")

    assert diff_file_manifests(baseline.files, replay.files) == {
        "added": ["added-empty"],
        "removed": [],
        "changed": ["existing-empty"],
    }
