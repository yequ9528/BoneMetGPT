"""Strict readonly environment-manifest binding for LatticeGPT V1."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .manifest_io import read_verified_json, sha256_file, validate_relative_path
from .environment_validation import verify_environment_control_files, verify_python_lock


ENVIRONMENT_MANIFEST_SCHEMA_VERSION = "latticegpt_environment_manifest_v1"
ENVIRONMENT_MANIFEST_NAME = "environment_manifest_v1.json"
ENVIRONMENT_MANIFEST_SHA_NAME = "environment_manifest_v1.json.sha256"
ENVIRONMENT_STATUS = {"PASS", "HOLD_OFFLINE_BACKUP_MISSING"}
ENVIRONMENT_FILE_LAYOUT = {
    "direct_requirements": "python-direct-requirements-v1.in",
    "python_lock": "python-requirements-v1.lock.txt",
    "node_runtime": "node-runtime-v1.json",
    "windows_cpu_reference": "windows-cpu-reference-v1.json",
    "package_lock": "viewer-package-lock.json",
    "env_example": ".env.example",
}
REFERENCE_PROFILE = {
    "windows_build": "19045",
    "architecture": "x86_64",
    "execution_profile": "CPU_ONLY",
    "cuda_required": False,
    "python_version": "3.10.17",
    "python_implementation": "CPython",
    "torch_version": "2.9.1+cpu",
    "node_version": "18.19.0",
    "npm_version": "10.2.3",
}
ENV_EXAMPLE_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "LATTICEGPT_CONFIG",
    "LATTICEGPT_PYTHON",
)
OFFLINE_RECORD_FIELDS = {
    "status",
    "archive_relative_path",
    "archive_size_bytes",
    "archive_sha256",
    "manifest_relative_path",
    "manifest_size_bytes",
    "manifest_sha256",
}
OFFLINE_LAYOUT = {
    "python": {
        "archive": "offline/python-wheelhouse-v1.zip",
        "manifest": "offline/python-wheelhouse-v1.manifest.json",
    },
    "node": {
        "archive": "offline/node-npm-cache-v1.zip",
        "manifest": "offline/node-npm-cache-v1.manifest.json",
    },
}


@dataclass(frozen=True)
class EnvironmentFile:
    logical_id: str
    path: Path
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class EnvironmentBindingV1:
    environment_id: str
    status: str
    manifest_path: Path
    manifest_sha256: str
    source_manifest_sha256: str
    artifact_manifest_sha256: str
    reference_profile: Mapping[str, object]
    files: Mapping[str, EnvironmentFile]
    offline: Mapping[str, object]
    validation: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )


def _require_sha(value: object, label: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 64 or any(ch not in "0123456789ABCDEF" for ch in normalized):
        raise ValueError(f"{label} must be an uppercase SHA256")
    return normalized


def _validate_env_example(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    expected = [f"{key}=" for key in ENV_EXAMPLE_KEYS]
    if lines != expected:
        raise ValueError(".env.example must contain only approved empty values")


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def validate_offline_records(
    release_root: Path,
    offline: object,
    *,
    python_lock_path: Path,
) -> Mapping[str, object]:
    if not isinstance(offline, dict) or set(offline) != set(OFFLINE_LAYOUT):
        raise ValueError("offline environment record is invalid")
    verified: dict[str, object] = {}
    for kind, layout in OFFLINE_LAYOUT.items():
        record = offline[kind]
        if not isinstance(record, dict) or set(record) != OFFLINE_RECORD_FIELDS:
            raise ValueError(f"offline {kind} record is invalid")
        status = record.get("status")
        identity_fields = OFFLINE_RECORD_FIELDS - {"status"}
        if status in {
            "HOLD_OFFLINE_BACKUP_MISSING",
            "NOT_REQUIRED_BY_V1_SCOPE",
        }:
            if any(record[field] is not None for field in identity_fields):
                raise ValueError(
                    f"offline {kind} {status} record must not claim identity"
                )
            verified[kind] = MappingProxyType(dict(record))
            continue
        if status != "PASS":
            raise ValueError("offline environment status is invalid")
        archive_relative = validate_relative_path(
            str(record["archive_relative_path"]), label=f"offline {kind} archive path"
        )
        manifest_relative = validate_relative_path(
            str(record["manifest_relative_path"]), label=f"offline {kind} manifest path"
        )
        if archive_relative != layout["archive"] or manifest_relative != layout["manifest"]:
            raise ValueError(f"offline {kind} fixed layout mismatch")
        archive_path = (release_root / archive_relative).resolve()
        manifest_path = (release_root / manifest_relative).resolve()
        for path, label in ((archive_path, "archive"), (manifest_path, "manifest")):
            try:
                path.relative_to(release_root)
            except ValueError as exc:
                raise ValueError(f"offline {kind} {label} escapes release root") from exc
            if not path.is_file() or _is_reparse_or_symlink(path) or path.stat().st_nlink != 1:
                raise ValueError(f"offline {kind} {label} must be an unlinked regular file")
        archive_size = record["archive_size_bytes"]
        manifest_size = record["manifest_size_bytes"]
        if (
            isinstance(archive_size, bool)
            or not isinstance(archive_size, int)
            or archive_size < 0
            or isinstance(manifest_size, bool)
            or not isinstance(manifest_size, int)
            or manifest_size < 0
        ):
            raise ValueError(f"offline {kind} size is invalid")
        archive_sha = _require_sha(record["archive_sha256"], f"offline {kind} archive SHA256")
        manifest_sha = _require_sha(
            record["manifest_sha256"], f"offline {kind} manifest SHA256"
        )
        if archive_path.stat().st_size != archive_size or sha256_file(archive_path) != archive_sha:
            raise ValueError(f"offline {kind} archive identity mismatch")
        if manifest_path.stat().st_size != manifest_size or sha256_file(manifest_path) != manifest_sha:
            raise ValueError(f"offline {kind} manifest identity mismatch")
        try:
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"offline {kind} manifest is invalid") from exc
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"schema_version", "archive_kind", "archive", "entries"}
            or metadata.get("schema_version") != "latticegpt_offline_archive_manifest_v1"
            or metadata.get("archive_kind") != kind
        ):
            raise ValueError(f"offline {kind} manifest schema mismatch")
        archive_metadata = metadata.get("archive")
        if archive_metadata != {
            "relative_path": archive_relative,
            "size_bytes": archive_size,
            "sha256": archive_sha,
        }:
            raise ValueError(f"offline {kind} manifest archive binding mismatch")
        entries = metadata.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"offline {kind} manifest entries are invalid")
        if kind == "python":
            verify_python_lock(python_lock_path.read_text(encoding="utf-8"), entries)
        verified[kind] = MappingProxyType(dict(record))
    return MappingProxyType(verified)


def load_environment_binding(
    environment_root: str | Path,
    *,
    source_manifest_sha256: str,
    artifact_manifest_sha256: str,
    manifest_path: str | Path | None = None,
    manifest_sha_path: str | Path | None = None,
) -> EnvironmentBindingV1:
    unresolved_root = Path(environment_root).absolute()
    if _is_reparse_or_symlink(unresolved_root):
        raise ValueError("environment root cannot be a reparse point or symlink")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise ValueError(f"environment root is missing: {root}")
    selected_manifest = (
        Path(manifest_path) if manifest_path is not None else root / ENVIRONMENT_MANIFEST_NAME
    )
    selected_sha = (
        Path(manifest_sha_path)
        if manifest_sha_path is not None
        else root / ENVIRONMENT_MANIFEST_SHA_NAME
    )
    payload, manifest_sha = read_verified_json(selected_manifest, selected_sha)
    if set(payload) != {
        "schema_version",
        "environment_id",
        "status",
        "source_manifest_sha256",
        "artifact_manifest_sha256",
        "reference_profile",
        "files",
        "offline",
    }:
        raise ValueError("environment manifest fields are invalid")
    if payload.get("schema_version") != ENVIRONMENT_MANIFEST_SCHEMA_VERSION:
        raise ValueError("environment manifest schema_version is invalid")

    environment_id = str(payload.get("environment_id") or "").strip()
    if not environment_id or any(token in environment_id for token in (":", "\\", "/")):
        raise ValueError("environment manifest contains private or absolute path data")
    status = str(payload.get("status") or "")
    if status not in ENVIRONMENT_STATUS:
        raise ValueError("environment status is invalid")
    source_sha = _require_sha(payload.get("source_manifest_sha256"), "source_manifest_sha256")
    artifact_sha = _require_sha(
        payload.get("artifact_manifest_sha256"), "artifact_manifest_sha256"
    )
    if source_sha != _require_sha(source_manifest_sha256, "expected source manifest SHA256"):
        raise ValueError("source manifest identity mismatch")
    if artifact_sha != _require_sha(
        artifact_manifest_sha256, "expected artifact manifest SHA256"
    ):
        raise ValueError("artifact manifest identity mismatch")

    profile = payload.get("reference_profile")
    if not isinstance(profile, dict) or profile != REFERENCE_PROFILE:
        raise ValueError("reference profile does not match Windows/CPU V1")

    entries = payload.get("files")
    if not isinstance(entries, list) or tuple(
        entry.get("logical_id") if isinstance(entry, dict) else None for entry in entries
    ) != tuple(ENVIRONMENT_FILE_LAYOUT):
        raise ValueError("environment file order is invalid")
    verified: dict[str, EnvironmentFile] = {}
    for entry in entries:
        if set(entry) != {"logical_id", "relative_path", "size_bytes", "sha256"}:
            raise ValueError("environment file record is invalid")
        logical_id = str(entry["logical_id"])
        relative = validate_relative_path(
            str(entry["relative_path"]), label=f"{logical_id}.relative_path"
        )
        if relative != ENVIRONMENT_FILE_LAYOUT[logical_id]:
            raise ValueError(f"{logical_id} relative path mismatch")
        unresolved_path = root / relative
        if _is_reparse_or_symlink(unresolved_path):
            raise ValueError(f"{logical_id} cannot be a reparse point or symlink")
        path = unresolved_path.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{logical_id} is outside environment root") from exc
        if not path.is_file():
            raise ValueError(f"{logical_id} is missing")
        if _is_reparse_or_symlink(path) or path.stat().st_nlink != 1:
            raise ValueError(f"{logical_id} must be an unlinked regular file")
        size = entry["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{logical_id} size is invalid")
        expected_sha = _require_sha(entry["sha256"], f"{logical_id}.sha256")
        if path.stat().st_size != size or sha256_file(path) != expected_sha:
            raise ValueError(f"{logical_id} identity mismatch")
        verified[logical_id] = EnvironmentFile(
            logical_id=logical_id,
            path=path,
            relative_path=relative,
            size_bytes=size,
            sha256=expected_sha,
        )
    control_validation = verify_environment_control_files(
        {logical_id: item.path for logical_id, item in verified.items()},
        reference_profile=REFERENCE_PROFILE,
    )
    offline = validate_offline_records(
        root.parent,
        payload.get("offline"),
        python_lock_path=verified["python_lock"].path,
    )
    offline_statuses = {record["status"] for record in offline.values()}
    if len(offline_statuses) != 1:
        raise ValueError("environment requires matching offline states")
    offline_status = next(iter(offline_statuses))
    if status == "PASS" and offline_status not in {
        "PASS",
        "NOT_REQUIRED_BY_V1_SCOPE",
    }:
        raise ValueError("environment PASS requires verified or optional offline state")
    if (
        status == "HOLD_OFFLINE_BACKUP_MISSING"
        and offline_status != "HOLD_OFFLINE_BACKUP_MISSING"
    ):
        raise ValueError("environment HOLD requires both offline records to be HOLD")

    return EnvironmentBindingV1(
        environment_id=environment_id,
        status=status,
        manifest_path=selected_manifest.resolve(),
        manifest_sha256=manifest_sha,
        source_manifest_sha256=source_sha,
        artifact_manifest_sha256=artifact_sha,
        reference_profile=MappingProxyType(dict(profile)),
        files=MappingProxyType(verified),
        offline=offline,
        validation=control_validation,
    )


__all__ = [
    "ENVIRONMENT_FILE_LAYOUT",
    "ENVIRONMENT_MANIFEST_NAME",
    "ENVIRONMENT_MANIFEST_SCHEMA_VERSION",
    "ENVIRONMENT_MANIFEST_SHA_NAME",
    "EnvironmentBindingV1",
    "EnvironmentFile",
    "OFFLINE_LAYOUT",
    "load_environment_binding",
    "validate_offline_records",
]
