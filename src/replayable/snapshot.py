"""Deterministic workspace snapshots and file-level comparisons."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXED_MTIME = 1_577_836_800  # 2020-01-01 00:00:00 UTC
WORKSPACE_ARCHIVE = "workspace.tar.gz"
WORKSPACE_HASH = "workspace.sha256"
WORKSPACE_FILES = "workspace.files.json"


class SnapshotError(RuntimeError):
    """A workspace could not be snapshotted or compared."""


@dataclass(frozen=True)
class SnapshotResult:
    archive_path: Path
    sha256: str
    files: list[dict[str, Any]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_entries(root: Path) -> list[Path]:
    return sorted(
        root.rglob("*"),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _file_manifest(root: Path, entries: list[Path]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in entries:
        relative = path.relative_to(root).as_posix()
        mode = f"{stat.S_IMODE(path.lstat().st_mode):04o}"
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8")
            files.append(
                {
                    "path": relative,
                    "size": len(target),
                    "sha256": hashlib.sha256(target).hexdigest(),
                    "type": "symlink",
                    "mode": mode,
                }
            )
        elif path.is_dir():
            files.append(
                {
                    "path": relative,
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "type": "directory",
                    "mode": mode,
                }
            )
        elif path.is_file():
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "type": "file",
                    "mode": mode,
                }
            )
        else:
            files.append(
                {
                    "path": relative,
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "type": "special",
                    "mode": mode,
                }
            )
    return files


def create_snapshot(
    workspace: Path,
    output_directory: Path,
    *,
    archive_name: str = WORKSPACE_ARCHIVE,
    hash_name: str = WORKSPACE_HASH,
    files_name: str = WORKSPACE_FILES,
) -> SnapshotResult:
    """Write a byte-stable tar.gz and a content manifest for ``workspace``."""

    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise SnapshotError(f"workspace directory does not exist at {workspace}")
    output_directory.mkdir(parents=True, exist_ok=True)
    archive_path = output_directory / archive_name
    entries = _workspace_entries(workspace)

    try:
        with archive_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                mtime=0,
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for path in entries:
                        relative = path.relative_to(workspace).as_posix()
                        info = archive.gettarinfo(str(path), arcname=relative)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = FIXED_MTIME
                        if info.isfile():
                            with path.open("rb") as source:
                                archive.addfile(info, source)
                        else:
                            archive.addfile(info)

        digest = _sha256_file(archive_path)
        files = _file_manifest(workspace, entries)
        (output_directory / hash_name).write_text(f"{digest}\n", encoding="ascii")
        (output_directory / files_name).write_text(
            json.dumps(files, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SnapshotError(f"cannot snapshot workspace {workspace}: {exc}") from exc

    return SnapshotResult(archive_path=archive_path, sha256=digest, files=files)


def load_recorded_snapshot(cassette: Path) -> tuple[str, list[dict[str, Any]]]:
    """Load and minimally validate a cassette's recorded workspace metadata."""

    hash_path = cassette / WORKSPACE_HASH
    files_path = cassette / WORKSPACE_FILES
    try:
        digest = hash_path.read_text(encoding="ascii").strip()
        files = json.loads(files_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"recorded workspace metadata is unreadable: {exc}") from exc
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise SnapshotError(f"invalid workspace hash at {hash_path}")
    if not isinstance(files, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
        for item in files
    ):
        raise SnapshotError(f"invalid workspace file manifest at {files_path}")
    return digest, files


def diff_file_manifests(
    recorded: list[dict[str, Any]],
    replayed: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Return added, removed, and content/metadata-changed paths."""

    expected = {item["path"]: item for item in recorded}
    actual = {item["path"]: item for item in replayed}
    expected_paths = set(expected)
    actual_paths = set(actual)
    return {
        "added": sorted(actual_paths - expected_paths),
        "removed": sorted(expected_paths - actual_paths),
        "changed": sorted(
            path
            for path in expected_paths & actual_paths
            if expected[path] != actual[path]
        ),
    }
