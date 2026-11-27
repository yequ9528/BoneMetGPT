"""Independent, readonly R4-Lite Job evidence verification."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

from .artifacts import PRODUCT_FILE_MAP, resolve_product_package, resolve_product_package_file
from .store import JOB_ID_PATTERN, JobStore


_TERMINAL_FAILURE_STATUSES = {"FAILED", "HOLD_RUNTIME_INTERRUPTED"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _within(path: str | Path, root: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve(strict=True)
    exact_root = root.resolve(strict=True)
    try:
        candidate.relative_to(exact_root)
    except ValueError as exc:
        raise ValueError(f"{label} path escapes its authorized root") from exc
    if candidate.is_symlink():
        raise ValueError(f"{label} symlink is forbidden")
    return candidate


def _recorded_active_identity(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    binding = job.get("formal_bundle_binding")
    if isinstance(binding, Mapping) and isinstance(binding.get("active_identity"), Mapping):
        return binding["active_identity"]
    reproduction = job.get("reproduction")
    if isinstance(reproduction, Mapping):
        context = reproduction.get("execution_context")
        release = context.get("release") if isinstance(context, Mapping) else None
        if isinstance(release, Mapping):
            return {
                "generation": release.get("generation"),
                "config_sha256": release.get("config_sha256"),
                "active_release_binding_sha256": release.get(
                    "active_release_binding_sha256"
                ),
                "source_manifest_sha256": release.get("source_manifest_sha256"),
                "artifact_manifest_sha256": release.get("artifact_manifest_sha256"),
                "environment_manifest_sha256": release.get(
                    "environment_manifest_sha256"
                ),
                "formal_bundle_binding_sha256": release.get(
                    "formal_bundle_binding_sha256"
                ),
                "generation_contract_sha256": release.get(
                    "generation_contract_sha256"
                ),
                "job_reproduction_profile_sha256": context.get(
                    "reproduction", {}
                ).get("profile_sha256"),
                "comparison_contract_sha256": context.get(
                    "reproduction", {}
                ).get("comparison_contract_sha256"),
                "tolerance_schema_sha256": context.get(
                    "reproduction", {}
                ).get("tolerance_schema_sha256"),
            }
        profile = reproduction.get("profile")
        identities = profile.get("identities") if isinstance(profile, Mapping) else None
        if isinstance(identities, Mapping):
            return {
                **dict(identities),
                "generation_contract_sha256": identities.get(
                    "release_generation_contract_sha256"
                ),
                "job_reproduction_profile_sha256": reproduction.get(
                    "profile_sha256"
                ),
                "comparison_contract_sha256": reproduction.get(
                    "comparison_contract_sha256"
                ),
                "tolerance_schema_sha256": profile.get("tolerance", {}).get(
                    "schema_sha256"
                ),
            }
    return None


def _require_identity(
    job: Mapping[str, Any], expected_active_identity: Mapping[str, object]
) -> None:
    if not isinstance(expected_active_identity, Mapping) or not expected_active_identity:
        raise ValueError("expected active identity is required")
    if "identity_sha256" in expected_active_identity:
        from .release_identity import validate_final_active_identity_v1

        validate_final_active_identity_v1(expected_active_identity)
    recorded = _recorded_active_identity(job)
    if not isinstance(recorded, Mapping):
        raise ValueError("Job does not bind an active identity")
    transitive_keys = {
        "config_sha256", "generation_contract_sha256",
        "active_release_binding_sha256", "source_manifest_sha256",
        "artifact_manifest_sha256", "environment_manifest_sha256",
        "formal_bundle_binding_sha256",
        "comparison_contract_sha256", "tolerance_schema_sha256",
    }
    for key in transitive_keys.intersection(expected_active_identity):
        expected = expected_active_identity[key]
        if recorded.get(key) != expected:
            raise ValueError(f"active identity mismatch: {key}")


def _validate_candidate_relationships(job: Mapping[str, Any]) -> None:
    target = job.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("target evidence is missing")
    target_sha = target.get("proposal_sha256")
    if target_sha is not None and re.fullmatch(r"[0-9A-F]{64}", str(target_sha)) is None:
        raise ValueError("target identity is invalid")
    candidate_set = job.get("candidate_set")
    selection = job.get("selection")
    if candidate_set is None and selection is None:
        return
    if not isinstance(candidate_set, Mapping) or not isinstance(selection, Mapping):
        raise ValueError("candidate/selection relationship is incomplete")
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate inventory is invalid")
    ids = [
        item.get("candidate_id")
        for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
    ]
    if len(ids) != len(candidates) or len(ids) != len(set(ids)):
        raise ValueError("candidate identities are invalid or duplicated")
    selected = selection.get("selected_candidate_id")
    if selected not in set(ids):
        raise ValueError("selected candidate is not in the candidate set")
    recommended = selection.get("recommended_candidate_id")
    if recommended and recommended != candidate_set.get("recommended_candidate_id"):
        raise ValueError("recommended candidate relationship mismatch")
    if target_sha is not None:
        candidate_target = candidate_set.get("target_sha256")
        if candidate_target is not None and candidate_target != target_sha:
            raise ValueError("candidate-set target identity mismatch")
        for candidate in candidates:
            value = candidate.get("target_sha256")
            if value is not None and value != target_sha:
                raise ValueError("candidate target identity mismatch")


def _validate_uploads(job: Mapping[str, Any], job_dir: Path) -> tuple[list[dict], dict]:
    inventory: list[dict] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for upload in job.get("mesh_uploads") or []:
        upload_id = upload.get("upload_id") if isinstance(upload, Mapping) else None
        if not isinstance(upload_id, str) or upload_id in by_id:
            raise ValueError("upload identity is invalid or duplicated")
        path = _within(upload.get("stored_path"), job_dir, label="upload")
        if not path.is_file():
            raise ValueError("upload is not a regular file")
        size = path.stat().st_size
        digest = _sha256(path)
        if upload.get("size_bytes") != size or str(upload.get("sha256") or "").upper() != digest:
            raise ValueError("upload size/SHA evidence mismatch")
        relative = path.relative_to(job_dir.resolve()).as_posix()
        inventory.append({"relative_path": relative, "size_bytes": size, "sha256": digest})
        by_id[upload_id] = upload
    return inventory, by_id


def _relationship_value(container: Mapping[str, Any] | None, key: str) -> Any:
    return container.get(key) if isinstance(container, Mapping) else None


def _validate_five_ids(
    job: Mapping[str, Any], uploads: Mapping[str, Mapping[str, Any]]
) -> None:
    mesh = job.get("mesh_binding")
    reservation = job.get("conformal_reservation")
    attempts = job.get("attempts") or []
    relationship = mesh.get("relationship") if isinstance(mesh, Mapping) else None
    selection = job.get("selection")
    selected = _relationship_value(selection, "selected_candidate_id")
    if relationship is not None:
        if _relationship_value(relationship, "job_id") != job["job_id"]:
            raise ValueError("Mesh relationship Job ID mismatch")
        upload_id = _relationship_value(relationship, "upload_id")
        if upload_id not in uploads:
            raise ValueError("Mesh relationship upload ID mismatch")
        if _relationship_value(relationship, "candidate_id") != selected:
            raise ValueError("Mesh relationship candidate ID mismatch")
        source = mesh.get("source")
        upload = uploads[upload_id]
        if (
            not isinstance(source, Mapping)
            or str(source.get("raw_sha256") or "").upper()
            != str(upload.get("sha256") or "").upper()
            or source.get("raw_size_bytes") != upload.get("size_bytes")
        ):
            raise ValueError("Mesh/upload identity mismatch")
    if reservation is not None:
        for key in (
            "job_id",
            "upload_id",
            "candidate_id",
            "reservation_id",
            "attempt_id",
            "package_id",
        ):
            if _relationship_value(reservation, key) != _relationship_value(relationship, key):
                raise ValueError(f"reservation relationship mismatch: {key}")
        mesh_sha = _relationship_value(mesh.get("digests") if isinstance(mesh, Mapping) else None, "mesh_binding_sha256")
        if reservation.get("mesh_binding_sha256") != mesh_sha:
            raise ValueError("reservation Mesh identity mismatch")
    if attempts:
        attempt = attempts[0]
        for key in ("attempt_id", "package_id"):
            if _relationship_value(attempt, key) != _relationship_value(relationship, key):
                raise ValueError(f"attempt relationship mismatch: {key}")


def _verify_package(
    store: JobStore, job: Mapping[str, Any], runtime_root: Path
) -> list[dict]:
    package = resolve_product_package(store, job["job_id"], runtime_root / "conformal")
    conformal_job_root = package.package_dir.parent
    output_objects = {path.name for path in conformal_job_root.iterdir()}
    if output_objects != {package.package_dir.name}:
        raise ValueError("successful Job contains partial or extra output objects")
    selected = _relationship_value(job.get("selection"), "selected_candidate_id")
    package_candidate = package.package_metadata.get("candidate")
    if isinstance(package_candidate, Mapping):
        if package_candidate.get("job_id") != job["job_id"]:
            raise ValueError("package candidate Job relationship mismatch")
        if package_candidate.get("selected_candidate_id") != selected:
            raise ValueError("package selected candidate mismatch")

    allowed = {path.as_posix() for path in PRODUCT_FILE_MAP.values()}
    actual = {
        path.relative_to(package.package_dir).as_posix()
        for path in package.package_dir.rglob("*")
        if path.is_file()
    }
    if actual != allowed:
        raise ValueError("package contains missing or extra files")

    inventory = []
    for key, relative in PRODUCT_FILE_MAP.items():
        path = resolve_product_package_file(package, key)
        size = path.stat().st_size
        digest = _sha256(path)
        if key == "source_mesh":
            evidence = package.package_metadata.get("source_mesh")
            expected_size = (
                evidence.get("package_copy_size_bytes")
                if isinstance(evidence, Mapping)
                else None
            )
        else:
            outputs = package.package_metadata.get("outputs")
            output_key = {
                "b31": "b31",
                "stl": "stl",
                "png": "png",
                "metadata": "legacy_metadata",
                "run_summary": "run_summary",
                "geometry_ledger": "geometry_ledger",
                "source_element_map": "source_element_map",
            }.get(key)
            evidence = outputs.get(output_key) if isinstance(outputs, Mapping) and output_key else None
            expected_size = evidence.get("size_bytes") if isinstance(evidence, Mapping) else None
        if expected_size is not None and expected_size != size:
            raise ValueError(f"package file size mismatch: {relative.as_posix()}")
        inventory.append(
            {"relative_path": relative.as_posix(), "size_bytes": size, "sha256": digest}
        )
    relationship = job.get("mesh_binding", {}).get("relationship", {})
    upload_id = relationship.get("upload_id") if isinstance(relationship, Mapping) else None
    uploads = [
        item
        for item in job.get("mesh_uploads") or []
        if isinstance(item, Mapping) and item.get("upload_id") == upload_id
    ]
    source_items = [
        item for item in inventory if item["relative_path"] == "input/source_c3d8.inp"
    ]
    if (
        len(uploads) != 1
        or len(source_items) != 1
        or source_items[0]["size_bytes"] != uploads[0].get("size_bytes")
        or source_items[0]["sha256"] != str(uploads[0].get("sha256") or "").upper()
    ):
        raise ValueError("package source Mesh differs from the bound upload")
    return inventory


def _successful_package_exists(conformal_job_root: Path) -> bool:
    if not conformal_job_root.is_dir():
        return False
    expected = {path.as_posix() for path in PRODUCT_FILE_MAP.values()}
    for candidate in conformal_job_root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        actual = {
            path.relative_to(candidate).as_posix()
            for path in candidate.rglob("*")
            if path.is_file()
        }
        if actual == expected:
            return True
    return False


def _verify_registered_partial_outputs(
    job: Mapping[str, Any], runtime_root: Path
) -> list[dict[str, object]]:
    conformal_job_root = runtime_root / "conformal" / str(job["job_id"])
    if not conformal_job_root.exists():
        return []
    if not conformal_job_root.is_dir() or conformal_job_root.is_symlink():
        raise ValueError("partial output root is invalid")
    if _successful_package_exists(conformal_job_root):
        raise ValueError("terminal failure Job contains a success package")

    actual_files = {
        path.resolve(strict=True): path
        for path in conformal_job_root.rglob("*")
        if path.is_file()
    }
    registered: dict[Path, dict[str, object]] = {}
    for attempt in job.get("attempts") or []:
        if not isinstance(attempt, Mapping):
            raise ValueError("failure attempt evidence is invalid")
        produced = attempt.get("produced_objects")
        if produced is None:
            continue
        if not isinstance(produced, list):
            raise ValueError("registered partial output inventory is invalid")
        raw_output_dir = attempt.get("conformal_output_dir")
        if not isinstance(raw_output_dir, str) or not raw_output_dir:
            raise ValueError("registered partial output root is missing")
        output_dir = _within(
            raw_output_dir, conformal_job_root, label="partial output root"
        )
        if not output_dir.is_dir():
            raise ValueError("registered partial output root is not a directory")
        for item in produced:
            if not isinstance(item, Mapping):
                raise ValueError("registered partial output is invalid")
            relative = item.get("relative_path")
            if not isinstance(relative, str) or not relative:
                raise ValueError("registered partial output path is invalid")
            path = _within(output_dir / relative, output_dir, label="partial output")
            if not path.is_file():
                raise ValueError("registered partial output is missing")
            if path in registered:
                raise ValueError("registered partial output is duplicated")
            size = path.stat().st_size
            digest = _sha256(path)
            if (
                item.get("size_bytes") != size
                or str(item.get("sha256") or "").upper() != digest
            ):
                raise ValueError("registered partial output size/SHA mismatch")
            registered[path] = {
                "attempt_id": attempt.get("attempt_id"),
                "relative_path": relative,
                "size_bytes": size,
                "sha256": digest,
            }
    unregistered = sorted(
        path.relative_to(conformal_job_root.resolve()).as_posix()
        for path in set(actual_files) - set(registered)
    )
    if unregistered:
        raise ValueError(
            f"terminal failure Job contains unregistered partial output: {unregistered[0]}"
        )
    return [registered[path] for path in sorted(registered, key=lambda value: str(value))]


def verify_job(
    runtime_root: str | Path,
    job_id: str,
    *,
    expected_active_identity: Mapping[str, object],
) -> dict[str, object]:
    """Return a structured verdict without writing or repairing any evidence."""
    result: dict[str, object] = {
        "schema_version": "latticegpt.r4_lite_job_verification.v1",
        "status": "FAIL",
        "job_id": job_id,
        "terminal_evidence": None,
        "reasons": [],
        "job_inventory": [],
        "package_inventory": [],
        "partial_inventory": [],
    }
    try:
        if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError("a complete exact Job ID is required")
        root = Path(runtime_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("runtime root is not a directory")
        job_dir = _within(root / "jobs" / job_id, root / "jobs", label="Job root")
        if job_dir.name != job_id or not job_dir.is_dir():
            raise ValueError("Job root identity mismatch")
        store = JobStore(root)
        job = store.get_consistent(job_id)
        _require_identity(job, expected_active_identity)
        _validate_candidate_relationships(job)
        upload_inventory, uploads = _validate_uploads(job, job_dir)
        _validate_five_ids(job, uploads)

        allowed_job_files = {"job.json", "events.jsonl", *(item["relative_path"] for item in upload_inventory)}
        actual_job_files = {
            path.relative_to(job_dir).as_posix()
            for path in job_dir.rglob("*")
            if path.is_file()
        }
        if actual_job_files != allowed_job_files:
            raise ValueError("Job root contains missing or extra files")
        job_inventory = []
        for relative in sorted(actual_job_files):
            path = job_dir / relative
            job_inventory.append(
                {"relative_path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}
            )
        result.update(
            {
                "job_schema_version": job["schema_version"],
                "revision": job["revision"],
                "job_status": job["status"],
                "active_identity": deepcopy(dict(expected_active_identity)),
                "job_inventory": job_inventory,
            }
        )
        if job["status"] == "VIEWER_READY":
            if job.get("failure") is not None:
                raise ValueError("successful Job contains failure evidence")
            result["package_inventory"] = _verify_package(store, job, root)
            result["terminal_evidence"] = "SUCCESS"
            result["status"] = "PASS"
        elif job["status"] in _TERMINAL_FAILURE_STATUSES:
            result["partial_inventory"] = _verify_registered_partial_outputs(job, root)
            for attempt in job.get("attempts") or []:
                verification = attempt.get("package_verification")
                if isinstance(verification, Mapping) and verification.get("status") in {
                    "passed", "success", "complete"
                }:
                    raise ValueError("terminal failure Job claims successful package verification")
            failure = job.get("failure")
            if not isinstance(failure, Mapping) or any(
                not isinstance(failure.get(key), str) or not failure.get(key)
                for key in ("stage", "code", "message")
            ):
                raise ValueError("terminal failure evidence is incomplete")
            result["terminal_evidence"] = "FAILURE"
            result["status"] = "PASS"
        else:
            if _successful_package_exists(root / "conformal" / job_id):
                raise ValueError("nonterminal Job contains a success package")
            result["status"] = "HOLD"
            result["reasons"] = ["JOB_NOT_TERMINAL"]
    except (OSError, TypeError, ValueError) as exc:
        result["status"] = "FAIL"
        result["reasons"] = [str(exc)]
    return result


__all__ = ["verify_job"]
