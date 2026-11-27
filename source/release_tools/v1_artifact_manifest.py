"""Deterministic construction and guarded assembly of V1 artifact bundles."""

from __future__ import annotations

import shutil
import stat
import uuid
import copy
from pathlib import Path
from typing import Mapping

from product_workflow.artifact_manifest import (
    APPROVED_ARTIFACT_NON_CLAIMS,
    APPROVED_BUNDLE_ID,
    APPROVED_BUNDLE_VERSION,
    APPROVED_BUNDLE_IDENTITIES,
    APPROVED_MANIFEST_NON_CLAIMS,
    APPROVED_MANIFEST_STATUS,
    APPROVED_PROVENANCE_STATUS,
    ARTIFACT_LAYOUT,
    ARTIFACT_MANIFEST_NAME,
    ARTIFACT_MANIFEST_SCHEMA_VERSION,
    ARTIFACT_MANIFEST_SHA_NAME,
    REDISTRIBUTION_STATUS,
    load_artifact_binding,
    require_approved_artifact_identity,
)
from product_workflow.manifest_io import (
    canonical_json_bytes,
    detached_sha256_bytes,
    exclusive_create_set_v1,
    sha256_bytes,
    sha256_file,
)


def write_r3_artifact_manifest_pair(
    root: str | Path,
    manifest_bytes: bytes,
    *,
    before_create=None,
) -> dict[str, object]:
    """Create the R3 artifact manifest pair once in manifest/SHA order."""

    if not isinstance(manifest_bytes, bytes):
        raise TypeError("manifest_bytes must be bytes")
    return exclusive_create_set_v1(
        root,
        {
            ARTIFACT_MANIFEST_NAME: manifest_bytes,
            ARTIFACT_MANIFEST_SHA_NAME: detached_sha256_bytes(
                manifest_bytes, ARTIFACT_MANIFEST_NAME
            ),
        },
        before_create=before_create,
    )


def rebind_r3_artifact_manifest(
    r2_payload: Mapping[str, object],
    *,
    source_release_sha256: str,
) -> dict[str, object]:
    """Rebind only the source identity; every controlled artifact record is retained."""

    if not isinstance(r2_payload, Mapping) or "source_release_sha256" not in r2_payload:
        raise ValueError("R2 artifact manifest payload is invalid")
    rebound = copy.deepcopy(dict(r2_payload))
    rebound["source_release_sha256"] = _require_upper_sha(
        source_release_sha256, label="source_release_sha256"
    )
    return rebound


def _require_upper_sha(value: object, *, label: str) -> str:
    normalized = str(value or "").strip()
    if (
        len(normalized) != 64
        or normalized != normalized.upper()
        or any(character not in "0123456789ABCDEF" for character in normalized)
    ):
        raise ValueError(f"{label} must be an uppercase SHA256")
    return normalized


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _require_regular_unlinked_path(path: Path, *, label: str) -> Path:
    unresolved = path.absolute()
    current = Path(unresolved.anchor)
    for part in unresolved.parts[1:]:
        current = current / part
        if _is_reparse_or_symlink(current):
            raise ValueError(f"{label} path cannot contain a reparse point or symlink")
    if not unresolved.is_file():
        raise ValueError(f"{label} path must be an absolute regular file")
    if unresolved.stat().st_nlink != 1:
        raise ValueError(f"{label} hardlink is forbidden")
    return unresolved.resolve()


def _require_regular_root(path: Path, *, label: str) -> Path:
    unresolved = path.absolute()
    if _is_reparse_or_symlink(unresolved):
        raise ValueError(f"{label} cannot be a reparse point or symlink")
    if not unresolved.is_dir():
        raise ValueError(f"{label} must be an existing regular directory")
    return unresolved.resolve()


def _require_safety_margin(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("safety margin must be a nonnegative integer")
    return value


def _verified_source_specs(
    source_specs: Mapping[str, Mapping[str, object]],
    *, bundle_version: str = APPROVED_BUNDLE_VERSION,
) -> dict[str, dict[str, object]]:
    if not isinstance(source_specs, Mapping) or set(source_specs) != set(ARTIFACT_LAYOUT):
        raise ValueError("source specs must contain the exact seven artifact logical IDs")
    verified: dict[str, dict[str, object]] = {}
    for logical_id in ARTIFACT_LAYOUT:
        spec = source_specs[logical_id]
        if not isinstance(spec, Mapping):
            raise ValueError(f"{logical_id} source spec must be an object")
        path = Path(str(spec.get("path") or ""))
        if not path.is_absolute():
            raise ValueError(f"{logical_id} source path must be an absolute regular file")
        path = _require_regular_unlinked_path(path, label=f"{logical_id} source")
        expected_size = spec.get("size_bytes")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int):
            raise ValueError(f"{logical_id} source size_bytes is invalid")
        expected_sha = _require_upper_sha(
            spec.get("sha256"), label=f"{logical_id}.source_sha256"
        )
        require_approved_artifact_identity(logical_id, expected_size, expected_sha, bundle_version=bundle_version)
        if path.stat().st_size != expected_size:
            raise ValueError(f"source {logical_id} size mismatch")
        if sha256_file(path) != expected_sha:
            raise ValueError(f"source {logical_id} SHA256 mismatch")
        provenance_status = str(spec.get("provenance_status") or "").strip()
        non_claims = spec.get("non_claims")
        if provenance_status != APPROVED_PROVENANCE_STATUS:
            raise ValueError(f"{logical_id} provenance_status is not approved")
        if non_claims != list(APPROVED_ARTIFACT_NON_CLAIMS):
            raise ValueError(f"{logical_id} non_claims are invalid")
        verified[logical_id] = {
            "path": path,
            "size_bytes": expected_size,
            "sha256": expected_sha,
            "provenance_status": provenance_status,
            "non_claims": list(non_claims),
        }
    return verified


def build_artifact_manifest(
    source_specs: Mapping[str, Mapping[str, object]],
    *,
    source_release_sha256: str,
    bundle_id: str = APPROVED_BUNDLE_ID,
    bundle_version: str = APPROVED_BUNDLE_VERSION,
) -> dict[str, object]:
    if bundle_id != APPROVED_BUNDLE_ID or bundle_version not in APPROVED_BUNDLE_IDENTITIES:
        raise ValueError("artifact bundle identity is not approved")
    verified = _verified_source_specs(source_specs, bundle_version=bundle_version)
    artifacts = []
    for logical_id, layout in ARTIFACT_LAYOUT.items():
        source = verified[logical_id]
        artifacts.append(
            {
                "logical_id": logical_id,
                "role": layout["role"],
                "relative_path": layout["relative_path"],
                "size_bytes": source["size_bytes"],
                "sha256": source["sha256"],
                "required": True,
                "loader_policy": layout["loader_policy"],
                "redistribution_status": REDISTRIBUTION_STATUS,
                "provenance_status": source["provenance_status"],
                "non_claims": source["non_claims"],
            }
        )
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "bundle_id": str(bundle_id),
        "bundle_version": str(bundle_version),
        "status": APPROVED_MANIFEST_STATUS,
        "source_release_sha256": _require_upper_sha(
            source_release_sha256, label="source_release_sha256"
        ),
        "artifacts": artifacts,
        "trusted_source_boundary": {
            "status": APPROVED_PROVENANCE_STATUS,
            "public_release": "DENY_UNTIL_EXPLICIT_OWNER_DECISION",
        },
        "non_claims": list(APPROVED_MANIFEST_NON_CLAIMS),
    }


def plan_artifact_assembly(
    source_specs: Mapping[str, Mapping[str, object]],
    *,
    release_root: str | Path,
    source_release_sha256: str,
    safety_margin_bytes: int,
    bundle_version: str = APPROVED_BUNDLE_VERSION,
) -> dict[str, object]:
    root = _require_regular_root(Path(release_root), label="release root")
    margin = _require_safety_margin(safety_margin_bytes)
    verified = _verified_source_specs(source_specs, bundle_version=bundle_version)
    manifest = build_artifact_manifest(
        source_specs, source_release_sha256=source_release_sha256, bundle_version=bundle_version
    )
    final_root = root / "artifacts_v1"
    artifact_bytes = sum(int(spec["size_bytes"]) for spec in verified.values())
    required_bytes = artifact_bytes + margin
    free_bytes = shutil.disk_usage(root).free
    artifacts = [
        {
            "logical_id": logical_id,
            "destination_relative_path": (
                Path("artifacts_v1") / ARTIFACT_LAYOUT[logical_id]["relative_path"]
            ).as_posix(),
            "size_bytes": verified[logical_id]["size_bytes"],
            "sha256": verified[logical_id]["sha256"],
        }
        for logical_id in ARTIFACT_LAYOUT
    ]
    return {
        "schema_version": "latticegpt_artifact_assembly_plan_v1",
        "status": "READY" if not final_root.exists() and free_bytes >= required_bytes else "HOLD",
        "ready": not final_root.exists() and free_bytes >= required_bytes,
        "destination_exists": final_root.exists(),
        "artifact_bytes": artifact_bytes,
        "safety_margin_bytes": margin,
        "required_bytes": required_bytes,
        "available_bytes": free_bytes,
        "artifact_manifest_sha256": sha256_bytes(canonical_json_bytes(manifest)),
        "artifacts": artifacts,
    }


def assemble_artifact_bundle(
    source_specs: Mapping[str, Mapping[str, object]],
    *,
    release_root: str | Path,
    source_release_sha256: str,
    safety_margin_bytes: int,
    failure_policy: str,
    overwrite: bool = False,
    bundle_version: str = APPROVED_BUNDLE_VERSION,
) -> dict[str, object]:
    if overwrite:
        raise ValueError("overwrite=true is forbidden for the V1 artifact bundle")
    if failure_policy not in {"cleanup_created_staging", "retain_created_staging"}:
        raise ValueError("failure policy is invalid")
    root = _require_regular_root(Path(release_root), label="release root")
    final_root = root / "artifacts_v1"
    if final_root.exists():
        raise FileExistsError(f"artifact destination already exists: {final_root}")
    plan = plan_artifact_assembly(
        source_specs,
        release_root=root,
        source_release_sha256=source_release_sha256,
        safety_margin_bytes=safety_margin_bytes,
        bundle_version=bundle_version,
    )
    if not plan["ready"]:
        raise ValueError("artifact assembly plan is not ready")
    verified = _verified_source_specs(source_specs, bundle_version=bundle_version)
    manifest = build_artifact_manifest(
        source_specs, source_release_sha256=source_release_sha256, bundle_version=bundle_version
    )
    staging_root = root / f".artifacts_v1.assembly-{uuid.uuid4().hex}"
    staging_root.mkdir()
    try:
        for logical_id, layout in ARTIFACT_LAYOUT.items():
            destination = staging_root / layout["relative_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(verified[logical_id]["path"], destination)
            if sha256_file(destination) != verified[logical_id]["sha256"]:
                raise ValueError(f"staged {logical_id} SHA256 mismatch")
            if destination.stat().st_size != verified[logical_id]["size_bytes"]:
                raise ValueError(f"staged {logical_id} size mismatch")

        manifest_raw = canonical_json_bytes(manifest)
        manifest_sha = sha256_bytes(manifest_raw)
        (staging_root / ARTIFACT_MANIFEST_NAME).write_bytes(manifest_raw)
        (staging_root / ARTIFACT_MANIFEST_SHA_NAME).write_text(
            f"{manifest_sha}  {ARTIFACT_MANIFEST_NAME}\n", encoding="ascii"
        )
        load_artifact_binding(staging_root)
        staging_root.replace(final_root)
    except Exception:
        if failure_policy == "cleanup_created_staging" and staging_root.exists():
            shutil.rmtree(staging_root)
        raise
    return {
        "schema_version": "latticegpt_artifact_assembly_report_v1",
        "status": "ASSEMBLED_AND_VERIFIED",
        "artifact_manifest_sha256": manifest_sha,
        "artifact_count": len(ARTIFACT_LAYOUT),
        "artifact_root": str(final_root),
    }


__all__ = [
    "assemble_artifact_bundle",
    "build_artifact_manifest",
    "plan_artifact_assembly",
]
