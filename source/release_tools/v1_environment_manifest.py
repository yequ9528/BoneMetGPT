"""Readonly environment release validators and explicit manifest writer."""

from __future__ import annotations

import argparse
import json
import copy
from pathlib import Path
from typing import Iterable, Mapping

from product_workflow.environment_manifest import (
    ENVIRONMENT_FILE_LAYOUT,
    ENVIRONMENT_MANIFEST_NAME,
    ENVIRONMENT_MANIFEST_SCHEMA_VERSION,
    ENVIRONMENT_MANIFEST_SHA_NAME,
    REFERENCE_PROFILE,
    validate_offline_records,
)
from product_workflow.manifest_io import (
    canonical_json_bytes,
    detached_sha256_bytes,
    exclusive_create_set_v1,
    sha256_bytes,
    sha256_file,
    validate_relative_path,
)


def write_r3_environment_manifest_pair(
    root: str | Path,
    manifest_bytes: bytes,
    *,
    before_create=None,
) -> dict[str, object]:
    """Create the R3 environment manifest pair once in manifest/SHA order."""

    if not isinstance(manifest_bytes, bytes):
        raise TypeError("manifest_bytes must be bytes")
    return exclusive_create_set_v1(
        root,
        {
            ENVIRONMENT_MANIFEST_NAME: manifest_bytes,
            ENVIRONMENT_MANIFEST_SHA_NAME: detached_sha256_bytes(
                manifest_bytes, ENVIRONMENT_MANIFEST_NAME
            ),
        },
        before_create=before_create,
    )


def rebind_r3_environment_manifest(
    r2_payload: Mapping[str, object],
    *,
    source_manifest_sha256: str,
    artifact_manifest_sha256: str,
) -> dict[str, object]:
    """Rebind only the two upstream identities; retain all environment records."""

    if not isinstance(r2_payload, Mapping):
        raise ValueError("R2 environment manifest payload is invalid")
    required = {"source_manifest_sha256", "artifact_manifest_sha256"}
    if not required.issubset(r2_payload):
        raise ValueError("R2 environment manifest binding fields are missing")
    rebound = copy.deepcopy(dict(r2_payload))
    rebound["source_manifest_sha256"] = _require_upper_sha(source_manifest_sha256)
    rebound["artifact_manifest_sha256"] = _require_upper_sha(artifact_manifest_sha256)
    return rebound
from product_workflow.environment_validation import (
    verify_environment_control_files,
    verify_node_package_lock,
    verify_python_lock,
)


def _require_upper_sha(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 64 or any(ch not in "0123456789ABCDEF" for ch in normalized):
        raise ValueError("archive entry SHA256 is invalid")
    return normalized


def build_offline_archive_manifest(
    archive_kind: str,
    entries: Iterable[Mapping[str, object]],
    *,
    archive: Mapping[str, object],
) -> dict:
    if archive_kind not in {"python", "node"}:
        raise ValueError("archive kind must be python or node")
    normalized = []
    seen: set[str] = set()
    for entry in entries:
        if entry.get("is_link"):
            raise ValueError("offline archive cannot contain a link")
        relative = validate_relative_path(
            str(entry.get("relative_path") or ""), label="archive relative path"
        )
        folded = relative.casefold()
        if folded in seen:
            raise ValueError("offline archive contains a duplicate name")
        seen.add(folded)
        size = entry.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("archive entry size is invalid")
        normalized.append(
            {"relative_path": relative, "size_bytes": size, "sha256": _require_upper_sha(entry.get("sha256"))}
        )
    normalized.sort(key=lambda item: item["relative_path"].casefold())
    expected_archive_path = {
        "python": "offline/python-wheelhouse-v1.zip",
        "node": "offline/node-npm-cache-v1.zip",
    }[archive_kind]
    archive_relative = validate_relative_path(
        str(archive.get("relative_path") or ""), label="offline archive relative path"
    )
    if archive_relative != expected_archive_path:
        raise ValueError("offline archive relative path is invalid")
    archive_size = archive.get("size_bytes")
    if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 0:
        raise ValueError("offline archive size is invalid")
    archive_record = {
        "relative_path": archive_relative,
        "size_bytes": archive_size,
        "sha256": _require_upper_sha(archive.get("sha256")),
    }
    return {
        "schema_version": "latticegpt_offline_archive_manifest_v1",
        "archive_kind": archive_kind,
        "archive": archive_record,
        "entries": normalized,
    }


def build_environment_manifest(
    environment_root: str | Path,
    *,
    source_manifest_sha256: str,
    artifact_manifest_sha256: str,
    offline_mode: str,
    offline_records: Mapping[str, object] | None = None,
) -> dict:
    root = Path(environment_root).resolve()
    control_paths = {
        logical_id: root / relative for logical_id, relative in ENVIRONMENT_FILE_LAYOUT.items()
    }
    for logical_id, path in control_paths.items():
        if not path.is_file():
            raise ValueError(f"{logical_id} is missing")
    verify_environment_control_files(control_paths, reference_profile=REFERENCE_PROFILE)
    files = []
    for logical_id, relative in ENVIRONMENT_FILE_LAYOUT.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"{logical_id} is missing")
        files.append(
            {
                "logical_id": logical_id,
                "relative_path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if offline_mode not in {"verified", "required_missing", "not_required_v1"}:
        raise ValueError("offline mode is invalid")
    empty_record = {
        "status": None,
        "archive_relative_path": None,
        "archive_size_bytes": None,
        "archive_sha256": None,
        "manifest_relative_path": None,
        "manifest_size_bytes": None,
        "manifest_sha256": None,
    }
    if offline_mode == "verified":
        if offline_records is None:
            raise ValueError("verified offline mode requires explicit archive records")
        verified_offline = validate_offline_records(
            root.parent,
            dict(offline_records),
            python_lock_path=control_paths["python_lock"],
        )
        offline_payload = {
            kind: dict(record) for kind, record in verified_offline.items()
        }
        environment_status = "PASS"
    else:
        if offline_records is not None:
            raise ValueError("offline mode without archives cannot contain identity records")
        record_status = (
            "HOLD_OFFLINE_BACKUP_MISSING"
            if offline_mode == "required_missing"
            else "NOT_REQUIRED_BY_V1_SCOPE"
        )
        empty_record["status"] = record_status
        offline_payload = {"python": dict(empty_record), "node": dict(empty_record)}
        environment_status = (
            "HOLD_OFFLINE_BACKUP_MISSING"
            if offline_mode == "required_missing"
            else "PASS"
        )
    return {
        "schema_version": ENVIRONMENT_MANIFEST_SCHEMA_VERSION,
        "environment_id": "windows-cpu-v1",
        "status": environment_status,
        "source_manifest_sha256": _require_upper_sha(source_manifest_sha256),
        "artifact_manifest_sha256": _require_upper_sha(artifact_manifest_sha256),
        "reference_profile": dict(REFERENCE_PROFILE),
        "files": files,
        "offline": offline_payload,
    }


def write_environment_manifest(root: str | Path, payload: Mapping[str, object]) -> None:
    target = Path(root).resolve()
    raw = canonical_json_bytes(dict(payload))
    (target / ENVIRONMENT_MANIFEST_NAME).write_bytes(raw)
    (target / ENVIRONMENT_MANIFEST_SHA_NAME).write_text(
        f"{sha256_bytes(raw)}  {ENVIRONMENT_MANIFEST_NAME}\n",
        encoding="ascii",
        newline="\n",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-root", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--artifact-manifest-sha256", required=True)
    parser.add_argument(
        "--offline-mode",
        choices=("verified", "required_missing", "not_required_v1"),
        required=True,
    )
    parser.add_argument("--offline-records-json")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    offline_records = None
    if args.offline_records_json:
        offline_records = json.loads(
            Path(args.offline_records_json).read_text(encoding="utf-8")
        )
    payload = build_environment_manifest(
        args.environment_root,
        source_manifest_sha256=args.source_manifest_sha256,
        artifact_manifest_sha256=args.artifact_manifest_sha256,
        offline_mode=args.offline_mode,
        offline_records=offline_records,
    )
    if args.write:
        write_environment_manifest(args.environment_root, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
