"""Controlled two-run Gate 7 orchestration primitives.

This module never starts a service or Viewer.  It validates fresh output roots,
executes Run A then Run B exactly once, and keeps every failure root intact.
"""

from __future__ import annotations

import os
import copy
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_latticegpt_config, require_within
from .contracts import (
    STIFFNESS_NAMES,
    build_frozen_replay_model_runtime_profile_v1,
    target_from_physical_9d,
)
from .factory import create_product_workflow_service
from .manifest_io import (
    canonical_json_object_v1,
    exclusive_create_set_v1,
    parse_closed_json_object_v1,
    sha256_file,
)
from .mesh_identity import parse_mesh_identity_v1
from .preflight import require_preflight
from .reproducibility import (
    TOLERANCE_ROWS,
    build_deterministic_projection_v2,
    build_execution_context_comparison_contract_v1,
    build_layered_comparison_report_v2,
    build_visual_layer_evidence_v1,
    build_operational_failure_evidence_v2,
    build_operational_result_content_v1,
    build_reproduction_profile_v1,
    build_volatile_audit_records_v2,
    compare_numeric_inputs_v1,
    relationship_check_registry_v1,
    seal_operational_result_manifest_v2,
    validate_deterministic_projection_v2,
    validate_numeric_comparison_input_v1,
    validate_operational_result_manifest_v2,
    validate_layered_comparison_external_evidence_v2,
    validate_tolerance_registry_v1,
)
from .r3_execution_context import (
    build_r3_execution_context_v1,
    preflight_execution_output_roots_v1,
    validate_r3_execution_context_v1,
)


FROZEN_SEED = 20260811
FROZEN_TARGET_SHA256 = "D08EB2BDFFF79760C60C886BE8CF2B4B3F5313E143354ED4CD2FC1E16871F777"
FROZEN_TARGET_IDENTITY_POINTER = "/source_context/target_sha256"
FROZEN_TARGET_VALUES_POINTER = "/source_context/physical_target_9d/values"
FROZEN_TARGET_UNITS_POINTER = "/source_context/physical_target_9d/units"
_SHA256_UPPER = re.compile(r"^[0-9A-F]{64}$")
PACKAGE_FILES = (
    "input/source_c3d8.inp",
    "core/conformal_b31.inp",
    "visualization/conformal_preview.stl",
    "visualization/conformal_preview.png",
    "package_metadata.json",
    "geometry_ledger.json",
    "source_element_map.csv",
)


def _is_reparse_or_symlink(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _resolved_absent_root(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = target.absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Gate 7 output root already exists: {target}")
    parent = target.parent
    if not parent.is_dir() or _is_reparse_or_symlink(parent):
        raise ValueError("Gate 7 output parent must be a normal directory")
    resolved_parent = parent.resolve(strict=True)
    resolved = resolved_parent / target.name
    if resolved.parent != resolved_parent:
        raise ValueError("Gate 7 output root escapes its parent")
    return resolved


def _root_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return int(info.st_dev), int(info.st_ino)


def _validate_success(result: Mapping[str, Any], *, label: str, root: Path) -> dict[str, Any]:
    if not isinstance(result, Mapping) or result.get("status") != "SUCCESS":
        raise RuntimeError(f"Run {label} did not return SUCCESS evidence")
    value = dict(result)
    if value.get("operational_evidence_kind") != "SUCCESS_MANIFEST":
        raise RuntimeError(f"Run {label} did not return a SUCCESS_MANIFEST")
    identity = value.get("run_identity")
    if not isinstance(identity, (list, tuple)) or len(identity) != 5 or any(
        not isinstance(item, str) or not item for item in identity
    ):
        raise RuntimeError(f"Run {label} five-ID tuple is invalid")
    if not root.is_dir() or Path(value.get("output_root_absolute", "")).resolve() != root.resolve():
        raise RuntimeError(f"Run {label} output-root evidence mismatch")
    evidence_sha = value.get("operational_evidence_sha256")
    if not isinstance(evidence_sha, str) or len(evidence_sha) != 64:
        raise RuntimeError(f"Run {label} operational evidence identity is invalid")
    return value


def run_pair_controlled(
    run_a_output_root: str | Path,
    run_b_output_root: str | Path,
    *,
    execute_run: Callable[[str, Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """Execute two fresh, non-aliased roots once, in A-then-B order."""

    if not callable(execute_run):
        raise TypeError("execute_run must be callable")
    run_a = _resolved_absent_root(run_a_output_root)
    run_b = _resolved_absent_root(run_b_output_root)
    if os.path.normcase(str(run_a)) == os.path.normcase(str(run_b)):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: aliased output roots")
    result_a = _validate_success(execute_run("A", run_a), label="A", root=run_a)
    result_b = _validate_success(execute_run("B", run_b), label="B", root=run_b)
    if _root_identity(run_a) == _root_identity(run_b):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: equal directory object identity")
    if tuple(result_a["run_identity"]) == tuple(result_b["run_identity"]):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: repeated five-ID tuple")
    if result_a["operational_evidence_sha256"] == result_b["operational_evidence_sha256"]:
        raise ValueError("SELF_COMPARISON_FORBIDDEN: equal operational evidence")
    return {"status": "SUCCESS_PAIR", "run_a": result_a, "run_b": result_b}


def compare_controlled(
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
    report_root: str | Path,
    *,
    build_report: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one comparison root without retry, cleanup, or self-comparison."""

    if not isinstance(run_a, Mapping) or not isinstance(run_b, Mapping):
        raise TypeError("run evidence must be objects")
    if run_a.get("operational_evidence_sha256") == run_b.get("operational_evidence_sha256"):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: equal operational evidence")
    identity_a = tuple(run_a.get("run_identity", ()))
    identity_b = tuple(run_b.get("run_identity", ()))
    if len(identity_a) != 5 or len(identity_b) != 5 or identity_a == identity_b:
        raise ValueError("SELF_COMPARISON_FORBIDDEN: invalid or repeated five-ID tuple")
    root_a = Path(str(run_a.get("output_root_absolute", ""))).resolve(strict=True)
    root_b = Path(str(run_b.get("output_root_absolute", ""))).resolve(strict=True)
    if _root_identity(root_a) == _root_identity(root_b):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: equal directory object identity")
    report = _resolved_absent_root(report_root)
    result = build_report(run_a, run_b, report)
    if not isinstance(result, Mapping):
        raise RuntimeError("comparison builder returned invalid evidence")
    return dict(result)


def _strict_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("identity JSON must not contain BOM")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    value = json.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=unique
    )
    if not isinstance(value, dict):
        raise ValueError("identity JSON root must be an object")
    return value


def _pointer(document: Any, pointer: str) -> Any:
    value = document
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be absolute")
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError(f"JSON pointer does not resolve: {pointer}")
    return copy.deepcopy(value)


def _sha_object(value: Any) -> str:
    wrapper = value if isinstance(value, Mapping) else {"value": value}
    return hashlib.sha256(canonical_json_object_v1(wrapper)).hexdigest().upper()


def _require_file_identity(path: Path, expected_sha256: str, label: str) -> None:
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise ValueError(f"{label} identity mismatch")


def load_frozen_target_values_v1(
    source_path: str | Path,
    *,
    source_sha256: str,
    values_json_pointer: str,
    target_sha256: str,
) -> dict[str, float]:
    """Load the one frozen Gate 7 target without conflating its values digest."""

    path = Path(source_path).resolve(strict=True)
    if not isinstance(source_sha256, str) or _SHA256_UPPER.fullmatch(source_sha256) is None:
        raise ValueError("frozen target source identity mismatch")
    _require_file_identity(path, source_sha256, "frozen target source")
    if values_json_pointer != FROZEN_TARGET_VALUES_POINTER:
        raise ValueError("frozen target values JSON pointer mismatch")
    if (
        not isinstance(target_sha256, str)
        or _SHA256_UPPER.fullmatch(target_sha256) is None
        or target_sha256 != FROZEN_TARGET_SHA256
    ):
        raise ValueError("frozen target identity mismatch")

    document = _strict_json_object(path)
    try:
        manifest_target_sha = _pointer(document, FROZEN_TARGET_IDENTITY_POINTER)
    except ValueError as exc:
        raise ValueError("frozen target identity mismatch") from exc
    if (
        not isinstance(manifest_target_sha, str)
        or manifest_target_sha.upper() != FROZEN_TARGET_SHA256
    ):
        raise ValueError("frozen target identity mismatch")
    try:
        units = _pointer(document, FROZEN_TARGET_UNITS_POINTER)
    except ValueError as exc:
        raise ValueError("frozen target units mismatch") from exc
    if units != "GPa":
        raise ValueError("frozen target units mismatch")
    try:
        values = _pointer(document, FROZEN_TARGET_VALUES_POINTER)
    except ValueError as exc:
        raise ValueError("frozen target values must be a closed finite 9D object") from exc
    if not isinstance(values, Mapping) or set(values) != set(STIFFNESS_NAMES):
        raise ValueError("frozen target values must be a closed finite 9D object")
    result: dict[str, float] = {}
    for name in STIFFNESS_NAMES:
        item = values[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or item <= 0
        ):
            raise ValueError("frozen target values must be a closed finite 9D object")
        result[name] = float(item)
    return result


def _numeric_input(job: Mapping[str, Any], mesh_bytes: bytes) -> dict[str, Any]:
    selected = next(
        item for item in job["candidate_set"]["candidates"]
        if item["candidate_id"] == job["selection"]["selected_candidate_id"]
    )
    parsed = parse_mesh_identity_v1(mesh_bytes)
    node_rows = parsed["nodes"]
    coordinates = [float.fromhex(value) for row in node_rows for value in row[1:]]
    node_ids = [f"node:{row[0]}:{axis}" for row in node_rows for axis in ("x", "y", "z")]
    candidate_nodes = selected["nodes"]
    candidate_coordinates = [float(value) for row in candidate_nodes for value in row]
    candidate_ids = [
        f"node:{index}:{axis}"
        for index in range(1, len(candidate_nodes) + 1)
        for axis in ("x", "y", "z")
    ]
    precheck = job["mesh_uploads"][-1]["precheck"]
    quality = precheck["geometry_quality"]
    xyz = list(zip(*(tuple(float.fromhex(value) for value in row[1:]) for row in node_rows)))
    minimum = [min(axis) for axis in xyz]
    maximum = [max(axis) for axis in xyz]
    bounds = [*minimum, *maximum, *(hi - lo for lo, hi in zip(minimum, maximum, strict=True))]
    reference_length = quality["reference_length"]
    normalized_diameter = float(selected["normalized_diameter"])
    groups_data = (
        (["score"], [float(selected["score"])], [1]),
        (candidate_ids, candidate_coordinates, [len(candidate_nodes), 3]),
        (list(STIFFNESS_NAMES), [float(selected["predicted_stiffness"][key]) for key in STIFFNESS_NAMES], [9]),
        (["relative_density"], [float(selected["relative_density"])], [1]),
        (["normalized_diameter"], [normalized_diameter], [1]),
        (["physical_diameter_mm"], [normalized_diameter * float(reference_length)], [1]),
        (node_ids, coordinates, [len(node_rows), 3]),
        (["min_x", "min_y", "min_z", "max_x", "max_y", "max_z", "extent_x", "extent_y", "extent_z"], bounds, [9]),
        (["total_volume", "average_element_volume"], [quality["total_volume"], quality["average_element_volume"]], [2]),
        (["minimum_scaled_jacobian", "maximum_jacobian_condition_2"], [quality["minimum_scaled_jacobian"], quality["maximum_jacobian_condition_2"]], [2]),
    )
    groups = []
    for row, (component_ids, values, shape) in zip(TOLERANCE_ROWS, groups_data, strict=True):
        order, group_id, pointer, units, _atol, _rtol, _shape_policy, _component_policy = row
        groups.append({
            "group_order": order,
            "group_id": group_id,
            "source_json_pointer": pointer,
            "units": units,
            "shape": shape,
            "component_ids": component_ids,
            "values": values,
        })
    return validate_numeric_comparison_input_v1({
        "schema_version": "latticegpt_numeric_comparison_input_v1",
        "source_result_schema_version": "latticegpt_operational_result_content_v1",
        "candidate_id": selected["candidate_id"],
        "groups": groups,
    })


def _package_inventory(package_dir: Path) -> dict[str, Any]:
    records = []
    for relative in PACKAGE_FILES:
        target = package_dir.joinpath(*relative.split("/"))
        if not target.is_file():
            raise ValueError(f"required package file is missing: {relative}")
        records.append({
            "relative_path": relative,
            "size_bytes": target.stat().st_size,
            "sha256": sha256_file(target),
        })
    return {
        "schema_version": "latticegpt_gate7_package_inventory_v1",
        "files": records,
    }


def _relationship_evidence(
    *,
    job: Mapping[str, Any],
    manifest: Mapping[str, Any],
    result_content_sha256: str,
) -> dict[str, Any]:
    reservation = job["conformal_reservation"]
    mesh = job["mesh_binding"]
    relationship = mesh["relationship"]
    attempt = {
        "reservation_id": reservation["reservation_id"],
        "attempt_id": reservation["attempt_id"],
        "package_id": reservation["package_id"],
        "mesh_binding_sha256": mesh["digests"]["mesh_binding_sha256"],
    }
    request = dict(attempt)
    result = {
        **attempt,
        "result_content_sha256": result_content_sha256,
        "relationship_integrity_status": "PASS",
    }
    objects = {
        "JOB_SNAPSHOT": job,
        "RESERVATION": reservation,
        "MESH_BINDING": mesh,
        "ATTEMPT": attempt,
        "PACKAGE_REQUEST": request,
        "PACKAGE_RESULT": result,
        "OPERATIONAL_MANIFEST": manifest,
    }
    checks = []
    for registry in relationship_check_registry_v1()["checks"]:
        expected = _pointer(objects[registry["expected_object_kind"]], registry["expected_json_pointer"])
        actual = _pointer(objects[registry["actual_object_kind"]], registry["actual_json_pointer"])
        expected_sha = _sha_object(expected)
        actual_sha = _sha_object(actual)
        if expected_sha != actual_sha:
            raise ValueError(f"relationship mismatch: {registry['check_id']}")
        checks.append({
            **registry,
            "expected_value_sha256": expected_sha,
            "actual_value_sha256": actual_sha,
            "status": "PASS",
        })
    return {
        "schema_version": "latticegpt_relationship_integrity_evidence_v1",
        "operational_evidence_sha256": manifest["operational_manifest_sha256"],
        "reservation_sha256": reservation["reservation_sha256"],
        "mesh_binding_sha256": mesh["digests"]["mesh_binding_sha256"],
        "result_content_sha256": result_content_sha256,
        "status": "PASS",
        "checks": checks,
    }


def _controlled_identities(profile: Mapping[str, Any]) -> dict[str, Any]:
    identities = profile["identities"]
    return {
        "source_manifest_sha256": identities["source_manifest_sha256"],
        "artifact_manifest_sha256": identities["artifact_manifest_sha256"],
        "environment_manifest_sha256": identities["environment_manifest_sha256"],
        "formal_bundle_binding_sha256": identities["formal_bundle_binding_sha256"],
        "reproduction_profile_sha256": profile["profile_sha256"],
        "comparison_contract_sha256": profile["tolerance"]["schema_sha256"],
        "tolerance_schema_path": profile["tolerance"]["schema_path"],
        "tolerance_schema_sha256": profile["tolerance"]["schema_sha256"],
    }


def _failure_run_identity(
    job: Mapping[str, Any] | None, *, proposed_job_id: str
) -> dict[str, Any]:
    result = {
        "job_id": proposed_job_id,
        "upload_id": None,
        "reservation_id": None,
        "attempt_id": None,
        "package_id": None,
    }
    if not isinstance(job, Mapping):
        return result
    result["job_id"] = job.get("job_id") or proposed_job_id
    uploads = job.get("mesh_uploads")
    if isinstance(uploads, list) and uploads and isinstance(uploads[-1], Mapping):
        result["upload_id"] = uploads[-1].get("upload_id")
    reservation = job.get("conformal_reservation")
    if isinstance(reservation, Mapping):
        result["reservation_id"] = reservation.get("reservation_id")
        result["attempt_id"] = reservation.get("attempt_id")
        result["package_id"] = reservation.get("package_id")
    return result


def _failure_produced_objects(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(root).as_posix()
        if relative == "evidence/operational_failure_evidence_v2.json":
            continue
        if relative == "execution_context_v1.json":
            kind, producer = "EXECUTION_CONTEXT", "GATE7_RUNNER"
        elif relative == "run_contract_v2.json":
            kind, producer = "RUN_CONTRACT", "GATE7_RUNNER"
        elif relative.endswith("/job.json"):
            kind, producer = "JOB_SNAPSHOT", "JOB_STORE"
        elif "/events/" in relative:
            kind, producer = "JOB_EVENT", "JOB_STORE"
        elif "/adapter/" in relative or relative.startswith("adapter/"):
            kind, producer = "RUNTIME_OUTPUT", "ADAPTER"
        elif "/conformal/" in relative or relative.startswith("conformal/"):
            kind, producer = "RUNTIME_OUTPUT", "CONFORMAL_RUNTIME"
        else:
            kind, producer = "RUNTIME_OUTPUT", "JOB_STORE"
        rows.append({
            "object_kind": kind,
            "relative_path": relative,
            "object_type": "REGULAR_FILE",
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "producer": producer,
            "ownership": "CREATED_BY_CURRENT_INVOCATION",
            "quarantine_status": "QUARANTINED_NO_AUTOMATIC_CLEANUP",
        })
    return sorted(rows, key=lambda row: (row["relative_path"].encode("utf-8"), row["object_kind"].encode("utf-8")))


def _persist_failure_evidence(
    *,
    root: Path,
    execution_context: Mapping[str, Any],
    job: Mapping[str, Any] | None,
    proposed_job_id: str,
    stage: str,
    error: Exception,
) -> dict[str, Any]:
    produced = _failure_produced_objects(root)
    created = sorted(
        {row["relative_path"] for row in produced}, key=lambda item: item.encode("utf-8")
    )
    expected = sorted(
        {
            "execution_context_v1.json",
            "run_contract_v2.json",
            "evidence/operational_failure_evidence_v2.json",
            "evidence/operational_result_manifest_v2.json",
            "evidence/deterministic_result_projection_v2.json",
            "evidence/numeric_comparison_input_v1.json",
            "evidence/relationship_integrity_evidence_v1.json",
            "evidence/package_inventory_v1.json",
        },
        key=lambda item: item.encode("utf-8"),
    )
    evidence = build_operational_failure_evidence_v2(
        execution_context=execution_context,
        run=_failure_run_identity(job, proposed_job_id=proposed_job_id),
        status="FAIL",
        stage=stage,
        error_code="CONTROLLED_RUN_FAILED" if stage == "CONTROLLED_RUN" else "SCHEMA_REJECTED",
        message=f"{type(error).__name__}: {error}",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        produced_objects=produced,
        partial_state={
            "status": "PARTIAL_QUARANTINED",
            "expected_targets": expected,
            "preexisting_targets": [],
            "builder_created_targets": created,
            "race_created_targets": [],
            "cleanup_authorized": False,
        },
        relationship_integrity_status="NOT_REACHED",
        persistence={
            "status": "PERSISTED_EXCLUSIVE",
            "relative_path": "evidence/operational_failure_evidence_v2.json",
            "error_code": None,
        },
        non_claims=["NOT_A_SCIENTIFIC_RESULT", "NO_AUTOMATIC_RETRY"],
    )
    try:
        exclusive_create_set_v1(
            root,
            {"evidence/operational_failure_evidence_v2.json": canonical_json_object_v1(evidence)},
        )
    except Exception as persistence_error:
        in_memory = build_operational_failure_evidence_v2(
            execution_context=execution_context,
            run=_failure_run_identity(job, proposed_job_id=proposed_job_id),
            status="HOLD",
            stage=stage,
            error_code="EXCLUSIVE_CREATE_RACE",
            message=f"{type(persistence_error).__name__}: {persistence_error}",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            produced_objects=_failure_produced_objects(root),
            partial_state={
                "status": "PARTIAL_QUARANTINED",
                "expected_targets": expected,
                "preexisting_targets": [],
                "builder_created_targets": created,
                "race_created_targets": [],
                "cleanup_authorized": False,
            },
            relationship_integrity_status="NOT_REACHED",
            persistence={
                "status": "PERSISTENCE_FAILED",
                "relative_path": "evidence/operational_failure_evidence_v2.json",
                "error_code": "EXCLUSIVE_CREATE_RACE",
            },
            non_claims=["FAILURE_EVIDENCE_NOT_PERSISTED", "NO_AUTOMATIC_RETRY"],
        )
        raise RuntimeError(
            "failure evidence exclusive-create failed: "
            + json.dumps(in_memory, ensure_ascii=False, sort_keys=True)
        ) from persistence_error
    return evidence


def _persist_success_evidence(
    *,
    root: Path,
    job: Mapping[str, Any],
    mesh_bytes: bytes,
    execution_context: Mapping[str, Any],
) -> dict[str, Any]:
    context = validate_r3_execution_context_v1(execution_context)
    selected = next(
        item for item in job["candidate_set"]["candidates"]
        if item["candidate_id"] == job["selection"]["selected_candidate_id"]
    )
    binding = job["mesh_binding"]
    reservation = job["conformal_reservation"]
    attempt = job["attempts"][0]
    package_dir = Path(attempt["conformal_result"].get("package_dir", attempt["conformal_output_dir"])).resolve()
    inventory = _package_inventory(package_dir)
    numeric = _numeric_input(job, mesh_bytes)
    relationship = {
        "reservation_id": reservation["reservation_id"],
        "job_id": job["job_id"],
        "upload_id": binding["relationship"]["upload_id"],
        "attempt_id": reservation["attempt_id"],
        "package_id": reservation["package_id"],
        "candidate_id": selected["candidate_id"],
        "selection_sha256": job["selection"]["selection_sha256"],
    }
    artifacts = copy.deepcopy(inventory["files"])
    result_content = build_operational_result_content_v1(
        relationship=relationship,
        candidate_identity={
            "candidate_id": selected["candidate_id"],
            "source_candidate_id": selected["source_candidate_id"],
            "rank": selected["rank"],
        },
        mesh_binding_sha256=binding["digests"]["mesh_binding_sha256"],
        artifacts=artifacts,
        producer_status="VIEWER_READY",
        non_claims=["NOT_FORMAL_150W_PROVENANCE", "TECHNICAL_REFERENCE_ONLY"],
    )
    inventory["result_content"] = result_content
    precheck = binding["precheck"]
    domain = binding["design_domain"]
    b31_sha = next(item["sha256"] for item in artifacts if item["relative_path"] == "core/conformal_b31.inp")
    candidate_topology_sha = _sha_object({"nodes": selected["nodes"], "connections": selected["connections"]})
    structure_sha = _sha_object({"candidate": selected})
    numeric_sha = _sha_object(numeric)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    candidate_manifest = {
        "candidate_id": selected["candidate_id"],
        "candidate_order": int(selected.get("candidate_order", selected["rank"] - 1)),
        "eligibility": "ELIGIBLE",
        "score": float(selected["score"]),
        "rank": int(selected["rank"]),
        "candidate_topology_sha256": candidate_topology_sha,
        "structure_sha256": structure_sha,
        "numeric_values_sha256": numeric_sha,
        "cad_selection": {
            "policy": "DIAMETER_DESC_CANDIDATE_ID_ASC",
            "selected_candidate_id": selected["candidate_id"],
        },
        "cad_selection_key": {
            "diameter_float64_hex": float(selected["normalized_diameter"]).hex(),
            "candidate_id": selected["candidate_id"],
        },
    }
    mesh_manifest = {
        "raw_size_bytes": binding["source"]["raw_size_bytes"],
        "raw_sha256": binding["source"]["raw_sha256"],
        "precheck_evidence_sha256": precheck["evidence_sha256"],
        "mesh_semantic_sha256": binding["digests"]["mesh_semantic_sha256"],
        "mesh_binding_sha256": binding["digests"]["mesh_binding_sha256"],
        "design_domain_semantic_sha256": domain["semantic_sha256"],
        "canonical_b31_edges_sha256": b31_sha,
    }
    package_manifest = {
        "package_schema_version": "latticegpt_conformal_package_v1",
        "result_schema_version": result_content["schema_version"],
        "result_content_sha256": result_content["result_content_sha256"],
        "relationship_integrity_status": "PASS",
    }
    frozen = {
        "replay_start": "FROZEN_QUERY_PLAN_OR_TARGET",
        "query_plan_sha256": context["inputs"]["target"]["target_sha256"],
        "target_sha256": context["inputs"]["target"]["target_sha256"],
        "random_seed": context["execution"]["seed"],
        "execution_profile": "CPU_ONLY",
        "dtype": "float32",
    }
    manifest = seal_operational_result_manifest_v2({
        "schema_version": "latticegpt_operational_result_manifest_v2",
        "generation": {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1"},
        "controlled_identities": _controlled_identities(context["reproduction"]["profile"]),
        "run": {
            "job_id": job["job_id"],
            "upload_id": relationship["upload_id"],
            "reservation_id": relationship["reservation_id"],
            "attempt_id": relationship["attempt_id"],
            "package_id": relationship["package_id"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "recorded_at": now,
            "elapsed_ms": 0,
        },
        "frozen_input": frozen,
        "candidate": candidate_manifest,
        "mesh": mesh_manifest,
        "package": package_manifest,
        "numeric_comparison_input_sha256": numeric_sha,
        "runtime": {"pid": None, "ports": None, "log_locations": None, "process_exit_code": 0},
        "paths": {
            "temporary_root": None,
            "work_root_absolute": str(root),
            "output_root_absolute": str(root),
            "mesh_source_absolute": job["mesh_uploads"][-1]["stored_path"],
            "source_job_dir_absolute": str(root / "jobs" / job["job_id"]),
            "work_dir_absolute": str(package_dir),
        },
        "events": [{"event_id": f"{job['job_id']}-success", "event_type": "RUN_SUCCEEDED", "recorded_at": now, "evidence_sha256": result_content["result_content_sha256"]}],
        "non_claims": ["NOT_FORMAL_150W_PROVENANCE", "TECHNICAL_REFERENCE_ONLY"],
    }, execution_context=context, numeric_comparison_input=numeric)
    relationship_evidence = _relationship_evidence(
        job=job,
        manifest=manifest,
        result_content_sha256=result_content["result_content_sha256"],
    )
    projection = build_deterministic_projection_v2(
        execution_context=context,
        frozen_input=frozen,
        candidate={
            key: candidate_manifest[key]
            for key in ("candidate_id", "candidate_order", "eligibility", "rank", "candidate_topology_sha256", "cad_selection")
        },
        mesh={
            key: mesh_manifest[key]
            for key in ("raw_size_bytes", "raw_sha256", "mesh_semantic_sha256", "design_domain_semantic_sha256", "canonical_b31_edges_sha256")
        },
        package={
            "package_schema_version": package_manifest["package_schema_version"],
            "result_schema_version": package_manifest["result_schema_version"],
        },
        non_claims=["NOT_FORMAL_150W_PROVENANCE", "TECHNICAL_REFERENCE_ONLY"],
    )
    evidence_outputs = {
        "evidence/operational_result_manifest_v2.json": canonical_json_object_v1(manifest),
        "evidence/deterministic_result_projection_v2.json": canonical_json_object_v1(projection),
        "evidence/numeric_comparison_input_v1.json": canonical_json_object_v1(numeric),
        "evidence/relationship_integrity_evidence_v1.json": canonical_json_object_v1(relationship_evidence),
        "evidence/package_inventory_v1.json": canonical_json_object_v1(inventory),
    }
    exclusive_create_set_v1(root, evidence_outputs)
    return {
        "status": "SUCCESS",
        "output_root_absolute": str(root.resolve()),
        "operational_evidence_kind": "SUCCESS_MANIFEST",
        "operational_evidence_sha256": manifest["operational_manifest_sha256"],
        "deterministic_projection_sha256": projection["projection_sha256"],
        "numeric_comparison_input_sha256": numeric_sha,
        "run_identity": [relationship[key] for key in ("job_id", "upload_id", "reservation_id", "attempt_id", "package_id")],
        "success_manifest_path": str((root / "evidence/operational_result_manifest_v2.json").resolve()),
    }


def _build_profile(config, preflight, *, seed: int) -> dict[str, Any]:
    import numpy
    import torch

    active = preflight.active_release_binding
    if not isinstance(active, Mapping):
        raise ValueError("Gate 7 requires ActiveReleaseBindingV1")
    versions = {
        "windows_build": platform.version(),
        "architecture": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "numpy": str(numpy.__version__),
        "torch": str(torch.__version__),
    }
    return build_reproduction_profile_v1(
        random_seed=seed,
        versions=versions,
        identities={
            "config_sha256": config.paths.config_sha256,
            "release_generation_contract_sha256": config.paths.generation_contract_sha256_value,
            "active_release_binding_sha256": preflight.active_release_binding_sha256,
            "source_manifest_sha256": preflight.source_manifest_sha256,
            "artifact_manifest_sha256": preflight.artifact_manifest_sha256,
            "environment_manifest_sha256": preflight.environment_manifest_sha256,
            "formal_bundle_binding_sha256": active["formal_bundle_binding_sha256"],
        },
        llm={
            "provider": "replay",
            "model": "frozen-target",
            "interface_version": "gate7-frozen-target-v1",
            "prompt_template_version": "not-used-frozen-target-v1",
            "temperature": 0.0,
            "top_p": None,
            "provider_seed": None,
            "online_text_determinism": "NOT_CLAIMED",
            "replay_start": "FROZEN_QUERY_PLAN_OR_TARGET",
        },
        tolerance_schema_sha256=preflight.reproduction_tolerances_sha256,
    )


def _build_execution_context(
    *,
    config,
    preflight,
    config_path: str | Path,
    target_path: Path,
    target_source_sha256: str,
    target_values: Mapping[str, float],
    mesh_path: Path,
    mesh_sha256: str,
    mesh_bytes: bytes,
    case_path: Path,
    case_manifest_sha256: str,
    seed: int,
    run_a_output_root: str | Path,
    run_b_output_root: str | Path,
    comparison_output_root: str | Path,
) -> dict[str, Any]:
    profile = _build_profile(config, preflight, seed=seed)
    model_profile = build_frozen_replay_model_runtime_profile_v1(profile)
    tolerance_registry = validate_tolerance_registry_v1(
        _strict_json_object(config.paths.reproduction_tolerances)
    )
    comparison_contract = build_execution_context_comparison_contract_v1(
        tolerance_schema_path=profile["tolerance"]["schema_path"],
        tolerance_schema_sha256=profile["tolerance"]["schema_sha256"],
    )

    case_document = _strict_json_object(case_path)
    case_schema = case_document.get("schema_version")
    case_identity = case_document.get("case")
    case_source = case_document.get("source")
    mesh_contract = case_document.get("mesh_contract")
    design_domain = case_document.get("design_domain")
    if not all(isinstance(item, Mapping) for item in (case_identity, case_source, mesh_contract, design_domain)):
        raise ValueError("case manifest identity contract is incomplete")
    if case_schema != "latticegpt_9x_case_manifest_v1" or case_identity.get("case_id") != "gugu5mm_engineering_pilot_v1":
        raise ValueError("case manifest identity mismatch")
    if (
        Path(str(case_source.get("path"))).resolve() != mesh_path
        or case_source.get("sha256") != mesh_sha256
        or case_source.get("size_bytes") != len(mesh_bytes)
    ):
        raise ValueError("case manifest source identity mismatch")
    parsed_mesh = parse_mesh_identity_v1(mesh_bytes)
    semantics = parsed_mesh["semantics"]
    declared_types = mesh_contract.get("element_types_original")
    if (
        not isinstance(declared_types, list)
        or declared_types != sorted(set(declared_types), key=lambda item: item.encode("utf-8"))
        or semantics["element_type_original"] not in declared_types
        or any(item not in {"C3D8", "C3D8R"} for item in declared_types)
    ):
        raise ValueError("case manifest mesh element-type contract mismatch")
    if (
        mesh_contract.get("expected_node_count") != semantics["node_count"]
        or mesh_contract.get("expected_element_count") != semantics["element_count"]
        or mesh_contract.get("units") != "mm"
        or mesh_contract.get("frame") != "source_native_cartesian_frame"
        or design_domain.get("selector_type") != "whole_file_implant_domain"
        or design_domain.get("expected_selected_element_count") != semantics["element_count"]
    ):
        raise ValueError("case manifest mesh semantics mismatch")

    output_values: dict[str, str] = {}
    for key, raw in (
        ("run_a_output_root", run_a_output_root),
        ("run_b_output_root", run_b_output_root),
        ("comparison_output_root", comparison_output_root),
    ):
        resolved = require_within(raw, config.paths.runtime_root, label=key)
        if resolved == config.paths.runtime_root:
            raise ValueError(f"{key} must be a child of configured runtime root")
        output_values[key] = str(resolved)

    generation_contract = _strict_json_object(config.paths.generation_contract)
    active = preflight.active_release_binding
    if not isinstance(active, Mapping):
        raise ValueError("Gate 7 requires ActiveReleaseBindingV1")
    context = build_r3_execution_context_v1(
        release={
            "generation": config.paths.active_generation,
            "config_path": str(Path(config_path).resolve()),
            "config_sha256": config.paths.config_sha256,
            "generation_contract_path": str(config.paths.generation_contract.resolve()),
            "generation_contract_schema_version": generation_contract.get("schema_version"),
            "generation_contract_sha256": config.paths.generation_contract_sha256_value,
            "active_release_binding_path": str(config.paths.active_release_binding.resolve()),
            "active_release_binding_sha256": preflight.active_release_binding_sha256,
            "source_manifest_sha256": preflight.source_manifest_sha256,
            "artifact_manifest_sha256": preflight.artifact_manifest_sha256,
            "environment_manifest_sha256": preflight.environment_manifest_sha256,
            "formal_bundle_binding_sha256": active["formal_bundle_binding_sha256"],
        },
        reproduction_profile=profile,
        model_runtime_profile=model_profile,
        comparison_contract=comparison_contract,
        tolerance_registry=tolerance_registry,
        inputs={
            "target": {
                "source_path": str(target_path),
                "source_file_sha256": target_source_sha256,
                "identity_json_pointer": FROZEN_TARGET_IDENTITY_POINTER,
                "target_sha256": FROZEN_TARGET_SHA256,
                "values_json_pointer": FROZEN_TARGET_VALUES_POINTER,
                "units_json_pointer": FROZEN_TARGET_UNITS_POINTER,
                "units": "GPa",
                "physical_target_9d": dict(target_values),
                "diagnostic_values_sha256": _sha_object(dict(target_values)),
                "reference_classification": "DETERMINISM_TECHNICAL_REFERENCE_TARGET",
                "non_claim": "NOT_FORMAL_150W_PROVENANCE",
            },
            "mesh": {
                "path": str(mesh_path),
                "original_filename": mesh_path.name,
                "raw_size_bytes": len(mesh_bytes),
                "raw_sha256": mesh_sha256,
                "element_types": declared_types,
                "units": mesh_contract["units"],
                "coordinate_system": mesh_contract["frame"],
                "design_domain": design_domain["selector_type"],
            },
            "case": {
                "path": str(case_path),
                "size_bytes": case_path.stat().st_size,
                "sha256": case_manifest_sha256,
                "schema_version": case_schema,
                "case_id": case_identity["case_id"],
            },
        },
        execution={
            "seed": seed,
            "execution_profile": "CPU_ONLY",
            "device_type": "cpu",
            "cuda_allowed": False,
            "dtype": "float32",
            "deterministic_algorithms": True,
            "warn_only": False,
            "overwrite": False,
            "no_retry": True,
        },
        outputs=output_values,
    )
    return validate_r3_execution_context_v1(context)


def run_product_pair(
    *,
    config_path: str | Path,
    frozen_target_source: str | Path,
    frozen_target_source_sha256: str,
    frozen_target_json_pointer: str,
    frozen_target_sha256: str,
    mesh_path: str | Path,
    mesh_sha256: str,
    case_manifest: str | Path,
    case_manifest_sha256: str,
    seed: int,
    run_a_output_root: str | Path,
    run_b_output_root: str | Path,
    comparison_output_root: str | Path,
    compute_providers: Mapping[str, object] | None = None,
    preflight_bindings: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    # Re-enable only with an explicit, version-bound selection authorization contract.
    return {
        "status": "HOLD",
        "code": "CONTROLLED_SELECTION_AUTHORIZATION_REQUIRED",
        "message_zh": "此版本暂停新的受控重放/Gate7运行：尚无绑定候选集合版本的有效选择授权。已有结果仍可按原合同读取。",
        "entrypoint": "gate7.run_product_pair",
        "side_effects": "NONE",
    }


def compare_product_runs(
    *,
    config_path: str | Path,
    run_a_success_manifest: str | Path,
    run_b_success_manifest: str | Path,
    run_a_output_root: str | Path,
    run_b_output_root: str | Path,
    report_root: str | Path,
    preflight_bindings: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    config = load_latticegpt_config(config_path=config_path, environ={})
    if preflight_bindings is None:
        preflight = require_preflight(config, profile="full_product")
    else:
        expected_binding_keys = {"source_binding", "artifact_binding", "environment_binding"}
        if not isinstance(preflight_bindings, Mapping) or set(preflight_bindings) != expected_binding_keys:
            raise ValueError("preflight bindings must contain the exact three-key fixture contract")
        preflight = require_preflight(
            config,
            profile="full_product",
            source_binding=preflight_bindings["source_binding"],
            artifact_binding=preflight_bindings["artifact_binding"],
            environment_binding=preflight_bindings["environment_binding"],
        )

    def load_run(manifest_path: str | Path, output_root: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
        root = Path(output_root).resolve(strict=True)
        expected_manifest_path = (root / "evidence/operational_result_manifest_v2.json").resolve(strict=True)
        if Path(manifest_path).resolve(strict=True) != expected_manifest_path:
            raise ValueError("success manifest path does not belong to the declared output root")
        context = validate_r3_execution_context_v1(
            _strict_json_object(root / "execution_context_v1.json")
        )
        if (
            context["release"]["config_sha256"] != config.paths.config_sha256
            or context["release"]["active_release_binding_sha256"] != preflight.active_release_binding_sha256
            or context["release"]["source_manifest_sha256"] != preflight.source_manifest_sha256
            or context["release"]["artifact_manifest_sha256"] != preflight.artifact_manifest_sha256
            or context["release"]["environment_manifest_sha256"] != preflight.environment_manifest_sha256
        ):
            raise ValueError("execution context no longer matches the active release identity")
        numeric = parse_closed_json_object_v1(
            (root / "evidence/numeric_comparison_input_v1.json").read_bytes(),
            "latticegpt_numeric_comparison_input_v1",
        )
        manifest = validate_operational_result_manifest_v2(
            _strict_json_object(expected_manifest_path),
            execution_context=context,
            numeric_comparison_input=numeric,
        )
        projection = validate_deterministic_projection_v2(
            _strict_json_object(root / "evidence/deterministic_result_projection_v2.json"),
            execution_context=context,
        )
        relationship = _strict_json_object(
            root / "evidence/relationship_integrity_evidence_v1.json"
        )
        inventory = _strict_json_object(root / "evidence/package_inventory_v1.json")
        return {
            "status": "SUCCESS",
            "output_root_absolute": str(root),
            "operational_evidence_kind": "SUCCESS_MANIFEST",
            "operational_evidence_schema_version": manifest["schema_version"],
            "operational_evidence_sha256": manifest["operational_manifest_sha256"],
            "deterministic_projection_sha256": projection["projection_sha256"],
            "numeric_comparison_input_sha256": _sha_object(numeric),
            "run_identity": [manifest["run"][key] for key in ("job_id", "upload_id", "reservation_id", "attempt_id", "package_id")],
            "success_manifest_path": str(Path(manifest_path).resolve()),
        }, {
            "context": context,
            "manifest": manifest,
            "projection": projection,
            "numeric": numeric,
            "relationship": relationship,
            "inventory": inventory,
        }

    left, left_objects = load_run(run_a_success_manifest, run_a_output_root)
    right, right_objects = load_run(run_b_success_manifest, run_b_output_root)
    if canonical_json_object_v1(left_objects["context"]) != canonical_json_object_v1(right_objects["context"]):
        raise ValueError("Run A and Run B execution contexts differ")
    context = left_objects["context"]
    if Path(report_root).resolve() != Path(context["outputs"]["comparison_output_root"]):
        raise ValueError("comparison output root does not match the execution context")
    preflight_execution_output_roots_v1(context, phase="COMPARE")

    def build_report(_left, _right, root):
        numeric_comparisons = compare_numeric_inputs_v1(
            left_objects["numeric"], right_objects["numeric"]
        )
        byte_status = "PASS" if canonical_json_object_v1(left_objects["projection"]) == canonical_json_object_v1(right_objects["projection"]) else "FAIL"
        numeric_status = "PASS" if all(item["status"] == "PASS" for item in numeric_comparisons) else "FAIL"
        semantic_status = "PASS" if left_objects["projection"]["stable_result_content"] == right_objects["projection"]["stable_result_content"] else "FAIL"
        run_a_ref = {key: left[key] for key in ("operational_evidence_kind", "operational_evidence_schema_version", "operational_evidence_sha256", "deterministic_projection_sha256", "numeric_comparison_input_sha256")}
        run_b_ref = {key: right[key] for key in ("operational_evidence_kind", "operational_evidence_schema_version", "operational_evidence_sha256", "deterministic_projection_sha256", "numeric_comparison_input_sha256")}
        def inventory_sha(objects: Mapping[str, Any], relative_path: str) -> str:
            matches = [
                item["sha256"] for item in objects["inventory"].get("files", [])
                if isinstance(item, Mapping) and item.get("relative_path") == relative_path
            ]
            if len(matches) != 1:
                raise ValueError(f"package inventory is missing unique {relative_path}")
            return matches[0]

        visual_status = "PASS" if inventory_sha(left_objects, "visualization/conformal_preview.png") == inventory_sha(right_objects, "visualization/conformal_preview.png") else "FAIL"
        report = build_layered_comparison_report_v2(
            execution_context=context,
            run_a=run_a_ref,
            run_b=run_b_ref,
            byte_status=byte_status,
            numeric_status=numeric_status,
            semantic_status=semantic_status,
            run_a_relationship_status=left_objects["relationship"]["status"],
            run_b_relationship_status=right_objects["relationship"]["status"],
            visual_status=visual_status,
            non_claims=["NOT_FORMAL_150W_PROVENANCE", "TECHNICAL_REFERENCE_ONLY"],
            numeric_comparisons=numeric_comparisons,
            run_a_relationship_evidence=left_objects["relationship"],
            run_b_relationship_evidence=right_objects["relationship"],
            run_a_volatile_records=build_volatile_audit_records_v2(left_objects["manifest"]),
            run_b_volatile_records=build_volatile_audit_records_v2(right_objects["manifest"]),
        )
        byte_evidence = {
            "schema_version": "latticegpt_byte_layer_evidence_v1",
            "run_a_projection_sha256": run_a_ref["deterministic_projection_sha256"],
            "run_b_projection_sha256": run_b_ref["deterministic_projection_sha256"],
            "status": byte_status,
            "compared_json_pointers": ["/"],
            "mismatch_json_pointers": [] if byte_status == "PASS" else ["/"],
        }
        numeric_evidence = {
            "schema_version": "latticegpt_numeric_layer_evidence_v2",
            "execution_context_sha256": context["execution_context_sha256"],
            "comparison_contract_sha256": context["reproduction"]["comparison_contract_sha256"],
            "tolerance_schema_sha256": context["reproduction"]["tolerance_schema_sha256"],
            "run_a_numeric_comparison_input_sha256": run_a_ref["numeric_comparison_input_sha256"],
            "run_b_numeric_comparison_input_sha256": run_b_ref["numeric_comparison_input_sha256"],
            "status": numeric_status,
            "comparisons": numeric_comparisons,
        }
        semantic_evidence = {
            "schema_version": "latticegpt_semantic_layer_evidence_v1",
            "run_a_projection_sha256": run_a_ref["deterministic_projection_sha256"],
            "run_b_projection_sha256": run_b_ref["deterministic_projection_sha256"],
            "status": semantic_status,
            "stable_semantic_equality": semantic_status == "PASS",
        }
        visual_evidence = build_visual_layer_evidence_v1(
            run_a=run_a_ref, run_b=run_b_ref, visual_status=visual_status
        )
        external_outputs = {
            "byte_layer_evidence_v1.json": canonical_json_object_v1(byte_evidence),
            "numeric_layer_evidence_v1.json": canonical_json_object_v1(numeric_evidence),
            "semantic_layer_evidence_v1.json": canonical_json_object_v1(semantic_evidence),
            "run_a_relationship_integrity_evidence_v1.json": canonical_json_object_v1(left_objects["relationship"]),
            "run_b_relationship_integrity_evidence_v1.json": canonical_json_object_v1(right_objects["relationship"]),
            "visual_layer_evidence_v1.json": canonical_json_object_v1(visual_evidence),
        }
        validate_layered_comparison_external_evidence_v2(
            report,
            external_evidence=external_outputs,
            execution_context=context,
        )
        outputs = {
            **external_outputs,
            "latticegpt_layered_comparison_report_v2.json": canonical_json_object_v1(report),
        }
        exclusive_create_set_v1(root, outputs)
        return report

    return compare_controlled(
        left,
        right,
        report_root,
        build_report=build_report,
    )


__all__ = [
    "FROZEN_SEED", "FROZEN_TARGET_SHA256", "compare_controlled",
    "compare_product_runs", "load_frozen_target_values_v1",
    "run_pair_controlled", "run_product_pair",
]
