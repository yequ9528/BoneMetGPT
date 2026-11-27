"""Closed R3 determinism identities and comparison primitives."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .manifest_io import canonical_json_object_v1


PROFILE_SCHEMA = "latticegpt_reproduction_profile_v1"
SHA256_UPPER_RE = re.compile(r"^[0-9A-F]{64}$")

TOLERANCE_ROWS = (
    (0, "CANDIDATE_SCORE", "/candidate/score", "dimensionless", 1e-9, 1e-8, "SCALAR_1_V1:[1]", "EXACT_SCORE_V1:score"),
    (1, "CANDIDATE_COORDINATES", "/candidate/numeric_values/node_coordinates", "dimensionless", 1e-7, 1e-6, "CANDIDATE_NODES_NX3_V1:[N,3]", "CANDIDATE_NODE_AXIS_ID_V1:node:<n>:<axis>"),
    (2, "CANDIDATE_STIFFNESS", "/candidate/numeric_values/predicted_stiffness_gpa", "GPa", 1e-5, 1e-6, "STIFFNESS_9_V1:[9]", "EXACT_STIFFNESS_9_V1:C11,C12,C13,C22,C23,C33,C44,C55,C66"),
    (3, "CANDIDATE_RELATIVE_DENSITY", "/candidate/numeric_values/relative_density", "dimensionless", 1e-8, 1e-6, "SCALAR_1_V1:[1]", "EXACT_RELATIVE_DENSITY_V1:relative_density"),
    (4, "CANDIDATE_NORMALIZED_DIAMETER", "/candidate/numeric_values/normalized_diameter", "dimensionless", 1e-7, 1e-6, "SCALAR_1_V1:[1]", "EXACT_NORMALIZED_DIAMETER_V1:normalized_diameter"),
    (5, "CANDIDATE_PHYSICAL_DIAMETER", "/candidate/numeric_values/physical_diameter_mm", "mm", 1e-6, 1e-6, "SCALAR_1_V1:[1]", "EXACT_PHYSICAL_DIAMETER_MM_V1:physical_diameter_mm"),
    (6, "MESH_COORDINATES", "/mesh/nodes/coordinates_mm", "mm", 1e-9, 0, "MESH_NODES_NX3_V1:[N,3]", "MESH_NODE_AXIS_ID_V1:node:<n>:<axis>"),
    (7, "GEOMETRY_LENGTHS_BOUNDS", "/mesh/geometry/lengths_bounds_mm", "mm", 1e-8, 1e-8, "GEOMETRY_LENGTHS_BOUNDS_9_V1:[9]", "EXACT_LENGTHS_BOUNDS_9_V1:min_x,min_y,min_z,max_x,max_y,max_z,extent_x,extent_y,extent_z"),
    (8, "VOLUMES", "/mesh/geometry/volumes_mm3", "mm^3", 1e-8, 1e-8, "VOLUMES_2_V1:[2]", "EXACT_VOLUMES_2_V1:total_volume,average_element_volume"),
    (9, "QUALITY_METRICS", "/mesh/geometry/quality_metrics", "dimensionless", 1e-9, 1e-8, "QUALITY_METRICS_2_V1:[2]", "EXACT_QUALITY_METRICS_2_V1:minimum_scaled_jacobian,maximum_jacobian_condition_2"),
)

VOLATILE_JSON_POINTERS = (
    "/run/job_id", "/run/upload_id", "/run/reservation_id", "/run/attempt_id",
    "/run/package_id", "/run/created_at", "/run/updated_at", "/run/recorded_at",
    "/run/elapsed_ms", "/events/*/event_id", "/events/*/recorded_at",
    "/runtime/pid", "/runtime/ports", "/runtime/log_locations",
    "/paths/temporary_root", "/paths/work_root_absolute", "/paths/output_root_absolute",
    "/paths/mesh_source_absolute", "/paths/source_job_dir_absolute", "/paths/work_dir_absolute",
    "/mesh/mesh_binding_sha256", "/package/result_content_sha256",
    "/operational_manifest_sha256",
)

RELATIONSHIP_CHECK_REGISTRY = (
    ("R01_JOB_RESERVATION_JOB", "JOB_SNAPSHOT", "/job_id", "RESERVATION", "/job_id"),
    ("R02_JOB_RESERVATION_UPLOAD", "JOB_SNAPSHOT", "/mesh_binding/relationship/upload_id", "RESERVATION", "/upload_id"),
    ("R03_JOB_RESERVATION_CANDIDATE", "JOB_SNAPSHOT", "/selection/selected_candidate_id", "RESERVATION", "/candidate_id"),
    ("R04_JOB_RESERVATION_ID", "JOB_SNAPSHOT", "/conformal_reservation/reservation_id", "RESERVATION", "/reservation_id"),
    ("R05_JOB_RESERVATION_ATTEMPT", "JOB_SNAPSHOT", "/conformal_reservation/attempt_id", "RESERVATION", "/attempt_id"),
    ("R06_JOB_RESERVATION_PACKAGE", "JOB_SNAPSHOT", "/conformal_reservation/package_id", "RESERVATION", "/package_id"),
    ("R07_RESERVATION_MESH_JOB", "RESERVATION", "/job_id", "MESH_BINDING", "/relationship/job_id"),
    ("R08_RESERVATION_MESH_UPLOAD", "RESERVATION", "/upload_id", "MESH_BINDING", "/relationship/upload_id"),
    ("R09_RESERVATION_MESH_CANDIDATE", "RESERVATION", "/candidate_id", "MESH_BINDING", "/relationship/candidate_id"),
    ("R10_RESERVATION_MESH_RESERVATION", "RESERVATION", "/reservation_id", "MESH_BINDING", "/relationship/reservation_id"),
    ("R11_RESERVATION_MESH_ATTEMPT", "RESERVATION", "/attempt_id", "MESH_BINDING", "/relationship/attempt_id"),
    ("R12_RESERVATION_MESH_PACKAGE", "RESERVATION", "/package_id", "MESH_BINDING", "/relationship/package_id"),
    ("R13_JOB_MESH_SELECTION", "JOB_SNAPSHOT", "/selection/selection_sha256", "MESH_BINDING", "/relationship/selection_manifest_sha256"),
    ("R14_RESERVATION_MESH_BINDING_SHA", "RESERVATION", "/mesh_binding_sha256", "MESH_BINDING", "/digests/mesh_binding_sha256"),
    ("R15_RESERVATION_ATTEMPT_RESERVATION", "RESERVATION", "/reservation_id", "ATTEMPT", "/reservation_id"),
    ("R16_RESERVATION_ATTEMPT_ID", "RESERVATION", "/attempt_id", "ATTEMPT", "/attempt_id"),
    ("R17_RESERVATION_ATTEMPT_PACKAGE", "RESERVATION", "/package_id", "ATTEMPT", "/package_id"),
    ("R18_RESERVATION_ATTEMPT_MESH", "RESERVATION", "/mesh_binding_sha256", "ATTEMPT", "/mesh_binding_sha256"),
    ("R19_ATTEMPT_REQUEST_RESERVATION", "ATTEMPT", "/reservation_id", "PACKAGE_REQUEST", "/reservation_id"),
    ("R20_ATTEMPT_REQUEST_ATTEMPT", "ATTEMPT", "/attempt_id", "PACKAGE_REQUEST", "/attempt_id"),
    ("R21_ATTEMPT_REQUEST_PACKAGE", "ATTEMPT", "/package_id", "PACKAGE_REQUEST", "/package_id"),
    ("R22_ATTEMPT_REQUEST_MESH", "ATTEMPT", "/mesh_binding_sha256", "PACKAGE_REQUEST", "/mesh_binding_sha256"),
    ("R23_REQUEST_RESULT_RESERVATION", "PACKAGE_REQUEST", "/reservation_id", "PACKAGE_RESULT", "/reservation_id"),
    ("R24_REQUEST_RESULT_ATTEMPT", "PACKAGE_REQUEST", "/attempt_id", "PACKAGE_RESULT", "/attempt_id"),
    ("R25_REQUEST_RESULT_PACKAGE", "PACKAGE_REQUEST", "/package_id", "PACKAGE_RESULT", "/package_id"),
    ("R26_REQUEST_RESULT_MESH", "PACKAGE_REQUEST", "/mesh_binding_sha256", "PACKAGE_RESULT", "/mesh_binding_sha256"),
    ("R27_RESERVATION_MANIFEST_JOB", "RESERVATION", "/job_id", "OPERATIONAL_MANIFEST", "/run/job_id"),
    ("R28_RESERVATION_MANIFEST_UPLOAD", "RESERVATION", "/upload_id", "OPERATIONAL_MANIFEST", "/run/upload_id"),
    ("R29_RESERVATION_MANIFEST_RESERVATION", "RESERVATION", "/reservation_id", "OPERATIONAL_MANIFEST", "/run/reservation_id"),
    ("R30_RESERVATION_MANIFEST_ATTEMPT", "RESERVATION", "/attempt_id", "OPERATIONAL_MANIFEST", "/run/attempt_id"),
    ("R31_RESERVATION_MANIFEST_PACKAGE", "RESERVATION", "/package_id", "OPERATIONAL_MANIFEST", "/run/package_id"),
    ("R32_RESERVATION_MANIFEST_CANDIDATE", "RESERVATION", "/candidate_id", "OPERATIONAL_MANIFEST", "/candidate/candidate_id"),
    ("R33_MESH_MANIFEST_BINDING", "MESH_BINDING", "/digests/mesh_binding_sha256", "OPERATIONAL_MANIFEST", "/mesh/mesh_binding_sha256"),
    ("R34_RESULT_MANIFEST_CONTENT", "PACKAGE_RESULT", "/result_content_sha256", "OPERATIONAL_MANIFEST", "/package/result_content_sha256"),
    ("R35_RESULT_MANIFEST_RELATIONSHIP_STATUS", "PACKAGE_RESULT", "/relationship_integrity_status", "OPERATIONAL_MANIFEST", "/package/relationship_integrity_status"),
)


def _literal_tolerance_registry() -> dict[str, Any]:
    return {
        "schema_version": "latticegpt_reproduction_tolerances_v1",
        "numeric_groups": [
            {
                "group_order": order,
                "group_id": group_id,
                "source_json_pointer": pointer,
                "units": units,
                "atol": atol,
                "rtol": rtol,
                "shape_policy": shape,
                "component_id_policy": component,
                "required": True,
            }
            for order, group_id, pointer, units, atol, rtol, shape, component in TOLERANCE_ROWS
        ],
    }


def _closed(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) != set(keys):
        raise ValueError(f"{label} must contain exactly {list(keys)}")
    return dict(value)


def _uint32(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4294967295:
        raise ValueError(f"{label} must be uint32")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2147483647:
        raise ValueError(f"{label} must be positive_int")
    return value


def _finite(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite_number")
    return value


def _nonempty(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{label} must be nonempty_string")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_UPPER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256_upper")
    return value


def validate_reproduction_profile_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = _closed(
        value,
        (
            "schema_version", "random_seed", "rng", "device", "numeric",
            "threads", "versions", "identities", "llm", "tolerance",
            "volatile_field_policy", "profile_sha256",
        ),
        "ReproductionProfileV1",
    )
    if profile["schema_version"] != PROFILE_SCHEMA:
        raise ValueError("invalid reproduction profile schema_version")
    seed = _uint32(profile["random_seed"], "random_seed")
    rng = _closed(profile["rng"], ("python", "numpy", "torch_cpu", "algorithm_contract"), "rng")
    for name in ("python", "numpy", "torch_cpu"):
        item = _closed(rng[name], ("seed",), f"rng.{name}")
        if _uint32(item["seed"], f"rng.{name}.seed") != seed:
            raise ValueError("all four seed fields must be equal")
    if rng["algorithm_contract"] != "PYTHON_NUMPY_TORCH_CPU_EXPLICIT_GENERATOR_V1":
        raise ValueError("invalid RNG algorithm contract")
    device = _closed(profile["device"], ("execution_profile", "device_type", "device_name", "cuda_allowed"), "device")
    if device["execution_profile"] != "CPU_ONLY" or device["device_type"] != "cpu":
        raise ValueError("R3 execution profile must be CPU_ONLY/cpu")
    _nonempty(device["device_name"], "device.device_name")
    if device["cuda_allowed"] is not False:
        raise ValueError("CUDA is forbidden in the R3 reference profile")
    numeric = _closed(profile["numeric"], ("dtype", "deterministic_algorithms", "warn_only"), "numeric")
    if numeric != {"dtype": "float32", "deterministic_algorithms": True, "warn_only": False}:
        raise ValueError("invalid deterministic numeric profile")
    threads = _closed(profile["threads"], ("omp", "mkl", "openblas", "numexpr", "torch_intraop", "torch_interop"), "threads")
    for key, item in threads.items():
        _positive_int(item, f"threads.{key}")
    versions = _closed(profile["versions"], ("windows_build", "architecture", "python_implementation", "python", "numpy", "torch"), "versions")
    for key, item in versions.items():
        _nonempty(item, f"versions.{key}")
    identities = _closed(profile["identities"], (
        "config_sha256", "release_generation_contract_sha256",
        "active_release_binding_sha256", "source_manifest_sha256",
        "artifact_manifest_sha256", "environment_manifest_sha256",
        "formal_bundle_binding_sha256",
    ), "identities")
    for key, item in identities.items():
        _sha(item, f"identities.{key}")
    llm = _closed(profile["llm"], ("provider", "model", "interface_version", "prompt_template_version", "temperature", "top_p", "provider_seed", "online_text_determinism", "replay_start"), "llm")
    for key in ("provider", "model", "interface_version", "prompt_template_version"):
        _nonempty(llm[key], f"llm.{key}")
    temperature = _finite(llm["temperature"], "llm.temperature")
    if not 0 <= temperature <= 2:
        raise ValueError("llm.temperature must be in [0,2]")
    if llm["top_p"] is not None:
        top_p = _finite(llm["top_p"], "llm.top_p")
        if not 0 < top_p <= 1:
            raise ValueError("llm.top_p must be in (0,1]")
    if llm["provider_seed"] is not None:
        _uint32(llm["provider_seed"], "llm.provider_seed")
    if llm["online_text_determinism"] != "NOT_CLAIMED" or llm["replay_start"] != "FROZEN_QUERY_PLAN_OR_TARGET":
        raise ValueError("invalid LLM determinism boundary")
    tolerance = _closed(profile["tolerance"], ("schema_path", "schema_sha256"), "tolerance")
    if tolerance["schema_path"] != "config/reproduction_tolerances_v1.json":
        raise ValueError("invalid tolerance schema_path")
    _sha(tolerance["schema_sha256"], "tolerance.schema_sha256")
    if profile["volatile_field_policy"] != "DETERMINISTIC_RESULT_PROJECTION_V1":
        raise ValueError("invalid volatile field policy")
    expected = hashlib.sha256(
        canonical_json_object_v1(profile, self_hash_pointer="/profile_sha256")
    ).hexdigest().upper()
    if _sha(profile["profile_sha256"], "profile_sha256") != expected:
        raise ValueError("profile_sha256 identity mismatch")
    return copy.deepcopy(profile)


def build_reproduction_profile_v1(
    *,
    random_seed: int,
    versions: Mapping[str, Any],
    identities: Mapping[str, Any],
    llm: Mapping[str, Any],
    tolerance_schema_sha256: str,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA,
        "random_seed": random_seed,
        "rng": {
            "python": {"seed": random_seed},
            "numpy": {"seed": random_seed},
            "torch_cpu": {"seed": random_seed},
            "algorithm_contract": "PYTHON_NUMPY_TORCH_CPU_EXPLICIT_GENERATOR_V1",
        },
        "device": {"execution_profile": "CPU_ONLY", "device_type": "cpu", "device_name": "CPU_REFERENCE", "cuda_allowed": False},
        "numeric": {"dtype": "float32", "deterministic_algorithms": True, "warn_only": False},
        "threads": {"omp": 1, "mkl": 1, "openblas": 1, "numexpr": 1, "torch_intraop": 1, "torch_interop": 1},
        "versions": dict(versions),
        "identities": dict(identities),
        "llm": dict(llm),
        "tolerance": {"schema_path": "config/reproduction_tolerances_v1.json", "schema_sha256": tolerance_schema_sha256},
        "volatile_field_policy": "DETERMINISTIC_RESULT_PROJECTION_V1",
        "profile_sha256": "0" * 64,
    }
    profile["profile_sha256"] = hashlib.sha256(
        canonical_json_object_v1(profile, self_hash_pointer="/profile_sha256")
    ).hexdigest().upper()
    return validate_reproduction_profile_v1(profile)


def reproduction_child_environment(
    profile: Mapping[str, Any],
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    value = validate_reproduction_profile_v1(profile)
    env = dict(os.environ if base_environment is None else base_environment)
    env.update(
        {
            "PYTHONHASHSEED": str(value["random_seed"]),
            "OMP_NUM_THREADS": str(value["threads"]["omp"]),
            "MKL_NUM_THREADS": str(value["threads"]["mkl"]),
            "OPENBLAS_NUM_THREADS": str(value["threads"]["openblas"]),
            "NUMEXPR_NUM_THREADS": str(value["threads"]["numexpr"]),
            "PYTHONIOENCODING": "utf-8",
            "LATTICEGPT_SCALER_POLICY": "require_existing",
            "LATTICEGPT_REPRODUCTION_PROFILE_JSON": canonical_json_object_v1(value).decode("utf-8"),
        }
    )
    return env


COMPARISON_CONTRACT_KEYS = (
    "schema_version",
    "volatile_field_policy",
    "volatile_json_pointers",
    "stable_projection_schema_version",
    "projection_construction",
    "relationship_integrity_policy",
    "required_layers",
    "visual_acceptance",
    "tolerance_schema_path",
    "tolerance_schema_sha256",
    "aggregation_precedence",
)


def _build_comparison_contract(
    *,
    tolerance_schema_path: str,
    tolerance_schema_sha256: str,
    stable_projection_schema_version: str,
    projection_construction: str,
) -> dict[str, Any]:
    value = {
        "schema_version": "latticegpt_comparison_contract_v1",
        "volatile_field_policy": "DETERMINISTIC_RESULT_PROJECTION_V1",
        "volatile_json_pointers": list(VOLATILE_JSON_POINTERS),
        "stable_projection_schema_version": stable_projection_schema_version,
        "projection_construction": projection_construction,
        "relationship_integrity_policy": "PER_RUN_REQUIRED_NOT_CROSS_RUN_EQUALITY_V1",
        "required_layers": ["byte", "numeric", "semantic", "relationship_integrity"],
        "visual_acceptance": ["PASS", "NOT_APPLICABLE_BY_R3_SCOPE"],
        "tolerance_schema_path": tolerance_schema_path,
        "tolerance_schema_sha256": tolerance_schema_sha256,
        "aggregation_precedence": ["FAIL", "HOLD", "PASS"],
    }
    return _validate_comparison_contract(
        value,
        tolerance_schema_path=tolerance_schema_path,
        tolerance_schema_sha256=tolerance_schema_sha256,
        stable_projection_schema_version=stable_projection_schema_version,
        projection_construction=projection_construction,
    )


def _validate_comparison_contract(
    value: Mapping[str, Any],
    *,
    tolerance_schema_path: str,
    tolerance_schema_sha256: str,
    stable_projection_schema_version: str,
    projection_construction: str,
) -> dict[str, Any]:
    contract = _closed(value, COMPARISON_CONTRACT_KEYS, "comparison contract")
    expected = {
        "schema_version": "latticegpt_comparison_contract_v1",
        "volatile_field_policy": "DETERMINISTIC_RESULT_PROJECTION_V1",
        "volatile_json_pointers": list(VOLATILE_JSON_POINTERS),
        "stable_projection_schema_version": stable_projection_schema_version,
        "projection_construction": projection_construction,
        "relationship_integrity_policy": "PER_RUN_REQUIRED_NOT_CROSS_RUN_EQUALITY_V1",
        "required_layers": ["byte", "numeric", "semantic", "relationship_integrity"],
        "visual_acceptance": ["PASS", "NOT_APPLICABLE_BY_R3_SCOPE"],
        "tolerance_schema_path": "config/reproduction_tolerances_v1.json",
        "tolerance_schema_sha256": _sha(tolerance_schema_sha256, "tolerance_schema_sha256"),
        "aggregation_precedence": ["FAIL", "HOLD", "PASS"],
    }
    if tolerance_schema_path != expected["tolerance_schema_path"]:
        raise ValueError("invalid comparison tolerance schema path")
    if contract != expected:
        raise ValueError("comparison contract differs from its frozen parent contract")
    return copy.deepcopy(contract)


def build_comparison_contract_v1(
    *, tolerance_schema_path: str, tolerance_schema_sha256: str
) -> dict[str, Any]:
    """Build the historical design-job-v2 comparison contract."""
    return _build_comparison_contract(
        tolerance_schema_path=tolerance_schema_path,
        tolerance_schema_sha256=tolerance_schema_sha256,
        stable_projection_schema_version="latticegpt_deterministic_result_projection_v1",
        projection_construction="POSITIVE_CLOSED_SCHEMA_V1",
    )


def validate_comparison_contract_v1(
    value: Mapping[str, Any],
    *,
    tolerance_schema_path: str,
    tolerance_schema_sha256: str,
) -> dict[str, Any]:
    """Validate only the historical design-job-v2 comparison contract."""
    return _validate_comparison_contract(
        value,
        tolerance_schema_path=tolerance_schema_path,
        tolerance_schema_sha256=tolerance_schema_sha256,
        stable_projection_schema_version="latticegpt_deterministic_result_projection_v1",
        projection_construction="POSITIVE_CLOSED_SCHEMA_V1",
    )


def build_execution_context_comparison_contract_v1(
    *, tolerance_schema_path: str, tolerance_schema_sha256: str
) -> dict[str, Any]:
    """Build the v3 execution-context interpretation of the v1 schema name."""
    return _build_comparison_contract(
        tolerance_schema_path=tolerance_schema_path,
        tolerance_schema_sha256=tolerance_schema_sha256,
        stable_projection_schema_version="latticegpt_deterministic_result_projection_v2",
        projection_construction="POSITIVE_CLOSED_SCHEMA_V2",
    )


def validate_execution_context_comparison_contract_v1(
    value: Mapping[str, Any],
    *,
    tolerance_schema_path: str,
    tolerance_schema_sha256: str,
) -> dict[str, Any]:
    return _validate_comparison_contract(
        value,
        tolerance_schema_path=tolerance_schema_path,
        tolerance_schema_sha256=tolerance_schema_sha256,
        stable_projection_schema_version="latticegpt_deterministic_result_projection_v2",
        projection_construction="POSITIVE_CLOSED_SCHEMA_V2",
    )


def comparison_contract_sha256_v1(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("comparison contract must be an object")
    return hashlib.sha256(canonical_json_object_v1(dict(value))).hexdigest().upper()


def validate_tolerance_registry_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None
    if candidate != _literal_tolerance_registry():
        raise ValueError("tolerance registry differs from the sole ten-row literal contract")
    return candidate


def _component_sort_key(component_id: str) -> tuple[Any, ...]:
    match = re.fullmatch(r"node:(\d+):(x|y|z)", component_id)
    if match:
        return int(match.group(1)), {"x": 0, "y": 1, "z": 2}[match.group(2)]
    return (component_id,)


def _validate_component_ids(row: Mapping[str, Any], component_ids: Any) -> list[str]:
    if not isinstance(component_ids, list) or not component_ids:
        raise ValueError("component_ids must be a nonempty array")
    if any(not isinstance(item, str) or not item for item in component_ids):
        raise ValueError("component_ids must contain nonempty strings")
    if len(set(component_ids)) != len(component_ids):
        raise ValueError("component_ids must be unique")
    policy = row["component_id_policy"]
    if "NODE_AXIS_ID_V1:" in policy:
        if any(re.fullmatch(r"node:[1-9]\d*:(x|y|z)", item) is None for item in component_ids):
            raise ValueError("node component ID grammar mismatch")
        if component_ids != sorted(component_ids, key=_component_sort_key):
            raise ValueError("node component IDs are not canonically ordered")
    else:
        expected = policy.split(":", 1)[1].split(",")
        if component_ids != expected:
            raise ValueError("fixed component ID set/order mismatch")
    return list(component_ids)


def validate_numeric_comparison_input_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _closed(
        value,
        ("schema_version", "source_result_schema_version", "candidate_id", "groups"),
        "numeric comparison input",
    )
    if result["schema_version"] != "latticegpt_numeric_comparison_input_v1":
        raise ValueError("invalid numeric comparison input schema")
    _nonempty(result["source_result_schema_version"], "source_result_schema_version")
    _nonempty(result["candidate_id"], "candidate_id")
    if not isinstance(result["groups"], list) or len(result["groups"]) != 10:
        raise ValueError("numeric input must contain exactly ten groups")
    registry = _literal_tolerance_registry()["numeric_groups"]
    normalized: list[dict[str, Any]] = []
    for index, (raw_group, row) in enumerate(zip(result["groups"], registry, strict=True)):
        group = _closed(
            raw_group,
            ("group_order", "group_id", "source_json_pointer", "units", "shape", "component_ids", "values"),
            f"numeric group {index}",
        )
        for key in ("group_order", "group_id", "source_json_pointer", "units"):
            if group[key] != row[key]:
                raise ValueError(f"numeric group registry mismatch: {key}")
        ids = _validate_component_ids(row, group["component_ids"])
        shape = group["shape"]
        if not isinstance(shape, list) or not shape or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in shape):
            raise ValueError("numeric group shape must be positive integers")
        count = math.prod(shape)
        if count != len(ids):
            raise ValueError("numeric group shape/component count mismatch")
        if not isinstance(group["values"], list) or len(group["values"]) != count:
            raise ValueError("numeric group value count mismatch")
        values = [_finite(item, "numeric value") for item in group["values"]]
        normalized.append({**group, "component_ids": ids, "values": values})
    result["groups"] = normalized
    return copy.deepcopy(result)


def compare_numeric_inputs_v1(run_a: Mapping[str, Any], run_b: Mapping[str, Any]) -> list[dict[str, Any]]:
    left = validate_numeric_comparison_input_v1(run_a)
    right = validate_numeric_comparison_input_v1(run_b)
    if left["candidate_id"] != right["candidate_id"] or left["source_result_schema_version"] != right["source_result_schema_version"]:
        raise ValueError("numeric input identity mismatch")
    rows = _literal_tolerance_registry()["numeric_groups"]
    comparisons: list[dict[str, Any]] = []
    for row, group_a, group_b in zip(rows, left["groups"], right["groups"], strict=True):
        if group_a["component_ids"] != group_b["component_ids"] or group_a["shape"] != group_b["shape"]:
            raise ValueError("numeric input component identity mismatch")
        for index, (component_id, value_a, value_b) in enumerate(
            zip(group_a["component_ids"], group_a["values"], group_b["values"], strict=True)
        ):
            difference = abs(float(value_a) - float(value_b))
            status = "PASS" if difference <= row["atol"] + row["rtol"] * abs(float(value_b)) else "FAIL"
            comparisons.append({
                "group_id": row["group_id"],
                "source_json_pointer": row["source_json_pointer"],
                "component_id": component_id,
                "component_index": index,
                "field_class": "TOLERANCE_COMPARE_ONLY",
                "units": row["units"],
                "run_a_value": value_a,
                "run_b_value": value_b,
                "absolute_difference": difference,
                "atol": row["atol"],
                "rtol": row["rtol"],
                "status": status,
            })
    return comparisons


def _sorted_nonclaims(value: Any) -> list[str]:
    if not isinstance(value, list) or value != sorted(set(value)) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("non_claims must be sorted unique nonempty strings")
    return list(value)


def _pointer_value(document: Any, pointer: str) -> Any:
    current = document
    if pointer == "":
        return current
    for raw_token in pointer.removeprefix("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ValueError(f"volatile pointer does not resolve: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isascii() or not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError(f"noncanonical array index in pointer: {pointer}")
            index = int(token)
            if index >= len(current):
                raise ValueError(f"volatile pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise ValueError(f"volatile pointer traverses a scalar: {pointer}")
    return current


def build_volatile_audit_records_v2(operational_evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Hash every concrete volatile value without merging wildcard matches."""
    if not isinstance(operational_evidence, Mapping):
        raise ValueError("operational evidence must be an object")
    events = operational_evidence.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be an array")
    records: list[dict[str, Any]] = []
    for pattern in VOLATILE_JSON_POINTERS:
        if "*" in pattern:
            for index in range(len(events)):
                concrete = pattern.replace("*", str(index))
                value = _pointer_value(operational_evidence, concrete)
                wrapper = {
                    "schema_version": "latticegpt_volatile_pointer_value_evidence_v2",
                    "pattern_json_pointer": pattern,
                    "concrete_json_pointer": concrete,
                    "match_index": index,
                    "value": copy.deepcopy(value),
                }
                records.append({
                    "pattern_json_pointer": pattern,
                    "concrete_json_pointer": concrete,
                    "match_index": index,
                    "value_sha256": hashlib.sha256(canonical_json_object_v1(wrapper)).hexdigest().upper(),
                })
        else:
            value = _pointer_value(operational_evidence, pattern)
            wrapper = {
                "schema_version": "latticegpt_volatile_pointer_value_evidence_v2",
                "pattern_json_pointer": pattern,
                "concrete_json_pointer": pattern,
                "match_index": 0,
                "value": copy.deepcopy(value),
            }
            records.append({
                "pattern_json_pointer": pattern,
                "concrete_json_pointer": pattern,
                "match_index": 0,
                "value_sha256": hashlib.sha256(canonical_json_object_v1(wrapper)).hexdigest().upper(),
            })
    records.sort(key=lambda item: item["concrete_json_pointer"].encode("utf-8"))
    if len({item["concrete_json_pointer"] for item in records}) != len(records):
        raise ValueError("volatile concrete pointers must be unique")
    return records


def relationship_check_registry_v1() -> dict[str, Any]:
    return {
        "schema_version": "latticegpt_relationship_check_registry_v1",
        "checks": [
            {
                "check_id": check_id,
                "expected_object_kind": expected_kind,
                "expected_json_pointer": expected_pointer,
                "actual_object_kind": actual_kind,
                "actual_json_pointer": actual_pointer,
                "required": True,
            }
            for check_id, expected_kind, expected_pointer, actual_kind, actual_pointer
            in RELATIONSHIP_CHECK_REGISTRY
        ],
    }


def validate_success_relationship_checks_v1(checks: Any) -> list[dict[str, Any]]:
    if not isinstance(checks, list) or len(checks) != len(RELATIONSHIP_CHECK_REGISTRY):
        raise ValueError("success relationship checks must contain all 35 registry rows")
    normalized: list[dict[str, Any]] = []
    keys = (
        "check_id", "expected_object_kind", "expected_json_pointer",
        "actual_object_kind", "actual_json_pointer", "required",
        "expected_value_sha256", "actual_value_sha256", "status",
    )
    for index, (raw, registry_row) in enumerate(zip(checks, RELATIONSHIP_CHECK_REGISTRY, strict=True)):
        item = _closed(raw, keys, f"relationship check {index}")
        expected_prefix = registry_row[:5]
        actual_prefix = tuple(item[key] for key in keys[:5])
        if actual_prefix != expected_prefix or item["required"] is not True:
            raise ValueError("relationship check differs from mandatory registry")
        if item["status"] not in {"PASS", "HOLD", "FAIL"}:
            raise ValueError("invalid relationship check status")
        for sha_key in ("expected_value_sha256", "actual_value_sha256"):
            if item[sha_key] is not None:
                _sha(item[sha_key], sha_key)
        if item["status"] in {"PASS", "FAIL"} and (
            item["expected_value_sha256"] is None or item["actual_value_sha256"] is None
        ):
            raise ValueError("PASS/FAIL relationship checks require both value digests")
        if item["status"] == "PASS" and item["expected_value_sha256"] != item["actual_value_sha256"]:
            raise ValueError("PASS relationship check digests must match")
        if item["status"] == "FAIL" and item["expected_value_sha256"] == item["actual_value_sha256"]:
            raise ValueError("FAIL relationship check digests must differ")
        normalized.append(item)
    return copy.deepcopy(normalized)


def _rfc3339(value: Any, label: str) -> str:
    _nonempty(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be RFC3339 with offset") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an offset")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def validate_operational_result_manifest_v1(
    value: Mapping[str, Any],
    *,
    numeric_comparison_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = _closed(value, (
        "schema_version", "generation", "controlled_identities", "run", "frozen_input",
        "candidate", "mesh", "package", "numeric_comparison_input_sha256", "runtime",
        "paths", "events", "non_claims", "operational_manifest_sha256",
    ), "operational result manifest")
    if manifest["schema_version"] != "latticegpt_operational_result_manifest_v1":
        raise ValueError("invalid operational result manifest schema")
    generation = _closed(manifest["generation"], ("schema_version", "stage", "contract"), "generation")
    if generation != {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"}:
        raise ValueError("invalid R3 generation discriminator")
    controlled = _closed(manifest["controlled_identities"], CONTROLLED_IDENTITY_KEYS, "controlled identities")
    for key, item in controlled.items():
        if key == "tolerance_schema_path":
            if item != "config/reproduction_tolerances_v1.json":
                raise ValueError("invalid tolerance schema path")
        else:
            _sha(item, key)
    run = _closed(manifest["run"], (
        "job_id", "upload_id", "reservation_id", "attempt_id", "package_id",
        "created_at", "updated_at", "recorded_at", "elapsed_ms",
    ), "run")
    for key in ("job_id", "upload_id", "reservation_id", "attempt_id", "package_id"):
        _nonempty(run[key], f"run.{key}")
    for key in ("created_at", "updated_at", "recorded_at"):
        _rfc3339(run[key], f"run.{key}")
    _nonnegative_int(run["elapsed_ms"], "run.elapsed_ms")
    frozen = _closed(manifest["frozen_input"], (
        "replay_start", "query_plan_sha256", "target_sha256", "random_seed", "execution_profile", "dtype",
    ), "frozen input")
    if frozen["replay_start"] != "FROZEN_QUERY_PLAN_OR_TARGET":
        raise ValueError("invalid replay_start")
    if frozen["query_plan_sha256"] is not None:
        _sha(frozen["query_plan_sha256"], "query_plan_sha256")
    _sha(frozen["target_sha256"], "target_sha256")
    _uint32(frozen["random_seed"], "random_seed")
    if frozen["execution_profile"] != "CPU_ONLY" or frozen["dtype"] != "float32":
        raise ValueError("invalid frozen execution profile")
    candidate = _closed(manifest["candidate"], (
        "candidate_id", "candidate_order", "eligibility", "score", "rank",
        "candidate_topology_sha256", "structure_sha256", "numeric_values_sha256",
        "cad_selection", "cad_selection_key",
    ), "candidate")
    _nonempty(candidate["candidate_id"], "candidate_id")
    _nonnegative_int(candidate["candidate_order"], "candidate_order")
    if candidate["eligibility"] != "ELIGIBLE":
        raise ValueError("candidate must be ELIGIBLE")
    _finite(candidate["score"], "candidate.score")
    _positive_int(candidate["rank"], "candidate.rank")
    for key in ("candidate_topology_sha256", "structure_sha256", "numeric_values_sha256"):
        _sha(candidate[key], key)
    selection = _closed(candidate["cad_selection"], ("policy", "selected_candidate_id"), "cad selection")
    selection_key = _closed(candidate["cad_selection_key"], ("diameter_float64_hex", "candidate_id"), "cad selection key")
    if selection["policy"] != "DIAMETER_DESC_CANDIDATE_ID_ASC" or selection["selected_candidate_id"] != candidate["candidate_id"] or selection_key["candidate_id"] != candidate["candidate_id"]:
        raise ValueError("candidate CAD identity mismatch")
    diameter = selection_key["diameter_float64_hex"]
    try:
        diameter_value = float.fromhex(diameter) if isinstance(diameter, str) else math.nan
    except ValueError as exc:
        raise ValueError("diameter_float64_hex must be canonical lowercase binary64 hex") from exc
    if not math.isfinite(diameter_value) or diameter != float(diameter_value).hex():
        raise ValueError("diameter_float64_hex must be canonical lowercase binary64 hex")
    mesh = _closed(manifest["mesh"], (
        "raw_size_bytes", "raw_sha256", "precheck_evidence_sha256", "mesh_semantic_sha256",
        "mesh_binding_sha256", "design_domain_semantic_sha256", "canonical_b31_edges_sha256",
    ), "mesh")
    _positive_int(mesh["raw_size_bytes"], "mesh.raw_size_bytes")
    for key in ("raw_sha256", "precheck_evidence_sha256", "mesh_semantic_sha256", "mesh_binding_sha256", "design_domain_semantic_sha256"):
        _sha(mesh[key], f"mesh.{key}")
    if mesh["canonical_b31_edges_sha256"] is not None:
        _sha(mesh["canonical_b31_edges_sha256"], "mesh.canonical_b31_edges_sha256")
    package = _closed(manifest["package"], (
        "package_schema_version", "result_schema_version", "result_content_sha256", "relationship_integrity_status",
    ), "package")
    _nonempty(package["package_schema_version"], "package_schema_version")
    _nonempty(package["result_schema_version"], "result_schema_version")
    _sha(package["result_content_sha256"], "result_content_sha256")
    if package["relationship_integrity_status"] != "PASS":
        raise ValueError("success manifest relationship integrity must PASS")
    numeric_sha = _sha(manifest["numeric_comparison_input_sha256"], "numeric_comparison_input_sha256")
    if numeric_comparison_input is not None:
        numeric = validate_numeric_comparison_input_v1(numeric_comparison_input)
        if numeric["candidate_id"] != candidate["candidate_id"] or numeric["source_result_schema_version"] != package["result_schema_version"]:
            raise ValueError("numeric input identity mismatch")
        if hashlib.sha256(canonical_json_object_v1(numeric)).hexdigest().upper() != numeric_sha:
            raise ValueError("numeric comparison input SHA mismatch")
    runtime = _closed(manifest["runtime"], ("pid", "ports", "log_locations", "process_exit_code"), "runtime")
    for key in ("pid", "process_exit_code"):
        if runtime[key] is not None and (isinstance(runtime[key], bool) or not isinstance(runtime[key], int)):
            raise ValueError(f"runtime.{key} must be integer or null")
    ports = runtime["ports"]
    if ports is not None and (not isinstance(ports, list) or ports != sorted(set(ports)) or any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65535 for item in ports)):
        raise ValueError("runtime.ports must be sorted unique ports or null")
    logs = runtime["log_locations"]
    if logs is not None and (not isinstance(logs, list) or logs != sorted(set(logs)) or any(not isinstance(item, str) or not Path(item).is_absolute() for item in logs)):
        raise ValueError("runtime.log_locations must be sorted unique absolute paths or null")
    paths = _closed(manifest["paths"], (
        "temporary_root", "work_root_absolute", "output_root_absolute", "mesh_source_absolute",
        "source_job_dir_absolute", "work_dir_absolute",
    ), "paths")
    for key, item in paths.items():
        if item is not None and (not isinstance(item, str) or not Path(item).is_absolute()):
            raise ValueError(f"paths.{key} must be an absolute path or null")
    if not isinstance(manifest["events"], list):
        raise ValueError("events must be an array")
    for index, raw_event in enumerate(manifest["events"]):
        event = _closed(raw_event, ("event_id", "event_type", "recorded_at", "evidence_sha256"), f"event {index}")
        _nonempty(event["event_id"], "event_id"); _nonempty(event["event_type"], "event_type")
        _rfc3339(event["recorded_at"], "event.recorded_at"); _sha(event["evidence_sha256"], "event.evidence_sha256")
    _sorted_nonclaims(manifest["non_claims"])
    expected = hashlib.sha256(canonical_json_object_v1(manifest, self_hash_pointer="/operational_manifest_sha256")).hexdigest().upper()
    if _sha(manifest["operational_manifest_sha256"], "operational_manifest_sha256") != expected:
        raise ValueError("operational manifest identity mismatch")
    return copy.deepcopy(manifest)


def seal_operational_result_manifest_v1(
    value_without_sha: Mapping[str, Any],
    *,
    numeric_comparison_input: Mapping[str, Any],
) -> dict[str, Any]:
    if "operational_manifest_sha256" in value_without_sha:
        raise ValueError("unsealed operational manifest must not contain its self hash")
    result = {**copy.deepcopy(dict(value_without_sha)), "operational_manifest_sha256": "0" * 64}
    result["operational_manifest_sha256"] = hashlib.sha256(
        canonical_json_object_v1(result, self_hash_pointer="/operational_manifest_sha256")
    ).hexdigest().upper()
    return validate_operational_result_manifest_v1(result, numeric_comparison_input=numeric_comparison_input)


def _legacy_controlled_from_context(context: Mapping[str, Any]) -> dict[str, Any]:
    controlled = context["controlled_identity"]
    return {
        "source_manifest_sha256": controlled["source_manifest_sha256"],
        "artifact_manifest_sha256": controlled["artifact_manifest_sha256"],
        "environment_manifest_sha256": controlled["environment_manifest_sha256"],
        "formal_bundle_binding_sha256": controlled["formal_bundle_binding_sha256"],
        "reproduction_profile_sha256": controlled["reproduction_profile_sha256"],
        "comparison_contract_sha256": controlled["comparison_contract_sha256"],
        "tolerance_schema_path": context["reproduction"]["tolerance_schema_path"],
        "tolerance_schema_sha256": controlled["tolerance_schema_sha256"],
    }


def seal_operational_result_manifest_v2(
    value_without_sha: Mapping[str, Any],
    *,
    execution_context: Mapping[str, Any],
    numeric_comparison_input: Mapping[str, Any],
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    if "operational_manifest_sha256" in value_without_sha:
        raise ValueError("unsealed operational manifest must not contain its self hash")
    expected_input_keys = {
        "schema_version", "generation", "controlled_identities", "run", "frozen_input",
        "candidate", "mesh", "package", "numeric_comparison_input_sha256", "runtime",
        "paths", "events", "non_claims",
    }
    if not isinstance(value_without_sha, Mapping) or set(value_without_sha) != expected_input_keys:
        raise ValueError("unsealed operational manifest v2 input fields are invalid")
    proxy = copy.deepcopy(dict(value_without_sha))
    proxy["schema_version"] = "latticegpt_operational_result_manifest_v1"
    proxy["generation"] = {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"}
    proxy["controlled_identities"] = _legacy_controlled_from_context(context)
    sealed_v1 = seal_operational_result_manifest_v1(proxy, numeric_comparison_input=numeric_comparison_input)
    result = {key: copy.deepcopy(value) for key, value in sealed_v1.items() if key != "operational_manifest_sha256"}
    result["schema_version"] = "latticegpt_operational_result_manifest_v2"
    result["generation"] = {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1"}
    result["execution_context_sha256"] = context["execution_context_sha256"]
    result["controlled_identity"] = context["controlled_identity"]
    result["controlled_identity_sha256"] = context["controlled_identity_sha256"]
    result["operational_manifest_sha256"] = "0" * 64
    result["operational_manifest_sha256"] = hashlib.sha256(
        canonical_json_object_v1(result, self_hash_pointer="/operational_manifest_sha256")
    ).hexdigest().upper()
    return validate_operational_result_manifest_v2(
        result,
        execution_context=context,
        numeric_comparison_input=numeric_comparison_input,
    )


def validate_operational_result_manifest_v2(
    value: Mapping[str, Any],
    *,
    execution_context: Mapping[str, Any],
    numeric_comparison_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    v1_keys = {
        "schema_version", "generation", "controlled_identities", "run", "frozen_input",
        "candidate", "mesh", "package", "numeric_comparison_input_sha256", "runtime",
        "paths", "events", "non_claims", "operational_manifest_sha256",
    }
    manifest = _closed(
        value,
        tuple(v1_keys | {"execution_context_sha256", "controlled_identity", "controlled_identity_sha256"}),
        "operational result manifest v2",
    )
    if manifest["schema_version"] != "latticegpt_operational_result_manifest_v2" or manifest["generation"] != {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1"}:
        raise ValueError("invalid operational result manifest v2 identity")
    if manifest["execution_context_sha256"] != context["execution_context_sha256"] or manifest["controlled_identity"] != context["controlled_identity"] or manifest["controlled_identity_sha256"] != context["controlled_identity_sha256"]:
        raise ValueError("operational result manifest context identity mismatch")
    if manifest["controlled_identities"] != _legacy_controlled_from_context(context):
        raise ValueError("operational result manifest controlled identity mismatch")
    expected_sha = hashlib.sha256(
        canonical_json_object_v1(manifest, self_hash_pointer="/operational_manifest_sha256")
    ).hexdigest().upper()
    if _sha(manifest["operational_manifest_sha256"], "operational manifest SHA") != expected_sha:
        raise ValueError("operational manifest v2 self hash mismatch")
    proxy = {
        key: copy.deepcopy(item)
        for key, item in manifest.items()
        if key not in {"execution_context_sha256", "controlled_identity", "controlled_identity_sha256"}
    }
    proxy["schema_version"] = "latticegpt_operational_result_manifest_v1"
    proxy["generation"] = {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"}
    proxy["operational_manifest_sha256"] = hashlib.sha256(
        canonical_json_object_v1(proxy, self_hash_pointer="/operational_manifest_sha256")
    ).hexdigest().upper()
    validate_operational_result_manifest_v1(proxy, numeric_comparison_input=numeric_comparison_input)
    return manifest


def build_deterministic_projection_v1(
    *,
    controlled_identities: Mapping[str, Any],
    frozen_input: Mapping[str, Any],
    candidate: Mapping[str, Any],
    mesh: Mapping[str, Any],
    package: Mapping[str, Any],
    non_claims: list[str],
) -> dict[str, Any]:
    controlled = _closed(controlled_identities, CONTROLLED_IDENTITY_KEYS, "controlled identities")
    for key, item in controlled.items():
        if key == "tolerance_schema_path":
            if item != "config/reproduction_tolerances_v1.json":
                raise ValueError("invalid tolerance schema path")
        else:
            _sha(item, key)
    frozen = _closed(frozen_input, ("replay_start", "query_plan_sha256", "target_sha256", "random_seed", "execution_profile", "dtype"), "frozen input")
    _sha(frozen["query_plan_sha256"], "query_plan_sha256"); _sha(frozen["target_sha256"], "target_sha256"); _uint32(frozen["random_seed"], "random_seed")
    candidate_value = _closed(candidate, ("candidate_id", "candidate_order", "eligibility", "rank", "candidate_topology_sha256", "cad_selection"), "candidate")
    cad = _closed(candidate_value["cad_selection"], ("policy", "selected_candidate_id"), "cad selection")
    if cad["policy"] != "DIAMETER_DESC_CANDIDATE_ID_ASC" or cad["selected_candidate_id"] != candidate_value["candidate_id"]:
        raise ValueError("candidate CAD selection mismatch")
    mesh_value = _closed(mesh, ("raw_size_bytes", "raw_sha256", "mesh_semantic_sha256", "design_domain_semantic_sha256", "canonical_b31_edges_sha256"), "mesh")
    for key in ("raw_sha256", "mesh_semantic_sha256", "design_domain_semantic_sha256"):
        _sha(mesh_value[key], key)
    if mesh_value["canonical_b31_edges_sha256"] is not None:
        _sha(mesh_value["canonical_b31_edges_sha256"], "canonical_b31_edges_sha256")
    package_value = _closed(package, ("package_schema_version", "result_schema_version"), "package")
    claims = _sorted_nonclaims(non_claims)
    stable = {
        "schema_version": "latticegpt_stable_exact_result_content_v1",
        "controlled_identities": copy.deepcopy(controlled),
        "candidate_exact": {
            "candidate_id": candidate_value["candidate_id"], "candidate_order": candidate_value["candidate_order"],
            "eligibility": candidate_value["eligibility"], "rank": candidate_value["rank"],
            "candidate_topology_sha256": candidate_value["candidate_topology_sha256"],
            "cad_selection_policy": cad["policy"],
        },
        "mesh_exact": {key: mesh_value[key] for key in ("raw_sha256", "mesh_semantic_sha256", "design_domain_semantic_sha256", "canonical_b31_edges_sha256")},
        "schema_statuses": copy.deepcopy(package_value),
        "non_claims": claims,
    }
    projection = {
        "schema_version": "latticegpt_deterministic_result_projection_v1",
        "controlled_identities": copy.deepcopy(controlled), "frozen_input": copy.deepcopy(frozen),
        "candidate": copy.deepcopy(candidate_value), "mesh": copy.deepcopy(mesh_value),
        "package": {**copy.deepcopy(package_value), "stable_result_content_sha256": hashlib.sha256(canonical_json_object_v1(stable)).hexdigest().upper()},
        "stable_result_content": stable, "non_claims": claims, "projection_sha256": "0" * 64,
    }
    projection["projection_sha256"] = hashlib.sha256(canonical_json_object_v1(projection, self_hash_pointer="/projection_sha256")).hexdigest().upper()
    return projection


def build_deterministic_projection_v2(
    *,
    execution_context: Mapping[str, Any],
    frozen_input: Mapping[str, Any],
    candidate: Mapping[str, Any],
    mesh: Mapping[str, Any],
    package: Mapping[str, Any],
    non_claims: list[str],
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    projection_v1 = build_deterministic_projection_v1(
        controlled_identities=_legacy_controlled_from_context(context),
        frozen_input=frozen_input,
        candidate=candidate,
        mesh=mesh,
        package=package,
        non_claims=non_claims,
    )
    result = {key: copy.deepcopy(value) for key, value in projection_v1.items() if key != "projection_sha256"}
    result["schema_version"] = "latticegpt_deterministic_result_projection_v2"
    result["execution_context_sha256"] = context["execution_context_sha256"]
    result["controlled_identity"] = context["controlled_identity"]
    result["controlled_identity_sha256"] = context["controlled_identity_sha256"]
    result["projection_sha256"] = "0" * 64
    result["projection_sha256"] = hashlib.sha256(
        canonical_json_object_v1(result, self_hash_pointer="/projection_sha256")
    ).hexdigest().upper()
    return validate_deterministic_projection_v2(result, execution_context=context)


def validate_deterministic_projection_v2(
    value: Mapping[str, Any], *, execution_context: Mapping[str, Any]
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    keys = {
        "schema_version", "controlled_identities", "frozen_input", "candidate", "mesh",
        "package", "stable_result_content", "non_claims", "projection_sha256",
        "execution_context_sha256", "controlled_identity", "controlled_identity_sha256",
    }
    projection = _closed(value, tuple(keys), "deterministic projection v2")
    if projection["schema_version"] != "latticegpt_deterministic_result_projection_v2":
        raise ValueError("invalid deterministic projection v2 schema")
    if projection["execution_context_sha256"] != context["execution_context_sha256"] or projection["controlled_identity"] != context["controlled_identity"] or projection["controlled_identity_sha256"] != context["controlled_identity_sha256"]:
        raise ValueError("deterministic projection context identity mismatch")
    if projection["controlled_identities"] != _legacy_controlled_from_context(context):
        raise ValueError("deterministic projection controlled identity mismatch")
    expected_sha = hashlib.sha256(
        canonical_json_object_v1(projection, self_hash_pointer="/projection_sha256")
    ).hexdigest().upper()
    if _sha(projection["projection_sha256"], "projection SHA") != expected_sha:
        raise ValueError("deterministic projection v2 self hash mismatch")
    return projection


def _run_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    run = _closed(value, ("operational_evidence_kind", "operational_evidence_schema_version", "operational_evidence_sha256", "deterministic_projection_sha256", "numeric_comparison_input_sha256"), "run reference")
    if run["operational_evidence_kind"] != "SUCCESS_MANIFEST" or run["operational_evidence_schema_version"] != "latticegpt_operational_result_manifest_v1":
        raise ValueError("layered PASS/HOLD report requires success manifest references")
    for key in ("operational_evidence_sha256", "deterministic_projection_sha256", "numeric_comparison_input_sha256"):
        _sha(run[key], key)
    return run


def build_visual_layer_evidence_v1(
    *, run_a: Mapping[str, Any], run_b: Mapping[str, Any], visual_status: str
) -> dict[str, Any]:
    """Build the one canonical external visual-layer evidence object."""
    if visual_status not in {"PASS", "HOLD", "FAIL", "NOT_APPLICABLE"}:
        raise ValueError("invalid visual layer status")
    left = copy.deepcopy(dict(run_a))
    right = copy.deepcopy(dict(run_b))
    return {
        "schema_version": "latticegpt_visual_layer_evidence_v1",
        "status": visual_status,
        "reason_code": "NOT_APPLICABLE_BY_R3_SCOPE" if visual_status == "NOT_APPLICABLE" else None,
        "case_identity_sha256": left["operational_evidence_sha256"],
        "result_identity_sha256": right["operational_evidence_sha256"],
        "media_evidence_sha256": None if visual_status == "NOT_APPLICABLE" else hashlib.sha256(
            canonical_json_object_v1({"runs": [left, right]})
        ).hexdigest().upper(),
    }


def build_layered_comparison_report_v1(
    *, comparison_contract_sha256: str, run_a: Mapping[str, Any], run_b: Mapping[str, Any],
    byte_status: str, numeric_status: str, semantic_status: str,
    run_a_relationship_status: str, run_b_relationship_status: str,
    visual_status: str, non_claims: list[str],
    numeric_comparisons: list[Mapping[str, Any]],
    run_a_relationship_evidence: Mapping[str, Any],
    run_b_relationship_evidence: Mapping[str, Any],
    run_a_volatile_records: list[Mapping[str, Any]],
    run_b_volatile_records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    _sha(comparison_contract_sha256, "comparison_contract_sha256")
    left = _run_reference(run_a); right = _run_reference(run_b)
    if left["operational_evidence_sha256"] == right["operational_evidence_sha256"]:
        raise ValueError("SELF_COMPARISON_FORBIDDEN")
    statuses = (byte_status, numeric_status, semantic_status, run_a_relationship_status, run_b_relationship_status)
    if any(item not in {"PASS", "HOLD", "FAIL"} for item in statuses) or visual_status not in {"PASS", "HOLD", "FAIL", "NOT_APPLICABLE"}:
        raise ValueError("invalid layered status")
    mandatory = (*statuses, "PASS" if visual_status == "NOT_APPLICABLE" else visual_status)
    overall = "FAIL" if "FAIL" in mandatory else "HOLD" if "HOLD" in mandatory else "PASS"
    def digest(obj): return hashlib.sha256(canonical_json_object_v1(obj)).hexdigest().upper()
    def relationship_evidence(value: Mapping[str, Any], expected_run: Mapping[str, Any], expected_status: str) -> dict[str, Any]:
        evidence = _closed(value, (
            "schema_version", "operational_evidence_sha256", "reservation_sha256",
            "mesh_binding_sha256", "result_content_sha256", "status", "checks",
        ), "relationship integrity evidence")
        if evidence["schema_version"] != "latticegpt_relationship_integrity_evidence_v1" or evidence["status"] != expected_status:
            raise ValueError("relationship evidence schema/status mismatch")
        if evidence["operational_evidence_sha256"] != expected_run["operational_evidence_sha256"]:
            raise ValueError("relationship evidence run identity mismatch")
        for key in ("operational_evidence_sha256", "reservation_sha256", "mesh_binding_sha256", "result_content_sha256"):
            _sha(evidence[key], key)
        checks = validate_success_relationship_checks_v1(evidence["checks"])
        if expected_status == "PASS" and any(item["status"] != "PASS" for item in checks):
            raise ValueError("PASS relationship evidence requires all 35 checks PASS")
        if expected_status == "FAIL" and not any(item["status"] == "FAIL" for item in checks):
            raise ValueError("FAIL relationship evidence requires a failed check")
        return {**evidence, "checks": checks}
    left_relationship = relationship_evidence(run_a_relationship_evidence, left, run_a_relationship_status)
    right_relationship = relationship_evidence(run_b_relationship_evidence, right, run_b_relationship_status)
    comparisons = [copy.deepcopy(dict(item)) for item in numeric_comparisons]
    if numeric_status == "PASS" and any(item.get("status") != "PASS" for item in comparisons):
        raise ValueError("numeric PASS requires every comparison PASS")
    byte_evidence = {"schema_version": "latticegpt_byte_layer_evidence_v1", "run_a_projection_sha256": left["deterministic_projection_sha256"], "run_b_projection_sha256": right["deterministic_projection_sha256"], "status": byte_status, "compared_json_pointers": ["/"], "mismatch_json_pointers": [] if byte_status == "PASS" else ["/"]}
    numeric_evidence = {"schema_version": "latticegpt_numeric_layer_evidence_v1", "run_a_numeric_comparison_input_sha256": left["numeric_comparison_input_sha256"], "run_b_numeric_comparison_input_sha256": right["numeric_comparison_input_sha256"], "status": numeric_status, "tolerance_schema_sha256": comparison_contract_sha256, "comparisons": comparisons}
    semantic_evidence = {"schema_version": "latticegpt_semantic_layer_evidence_v1", "run_a_projection_sha256": left["deterministic_projection_sha256"], "run_b_projection_sha256": right["deterministic_projection_sha256"], "status": semantic_status, "stable_semantic_equality": semantic_status == "PASS"}
    visual_evidence = build_visual_layer_evidence_v1(run_a=left, run_b=right, visual_status=visual_status)
    report = {
        "schema_version": "latticegpt_layered_comparison_report_v1", "comparison_contract_sha256": comparison_contract_sha256,
        "run_a": left, "run_b": right,
        "byte": {"status": byte_status, "compared_json_pointers": byte_evidence["compared_json_pointers"], "mismatch_json_pointers": byte_evidence["mismatch_json_pointers"], "evidence_sha256": digest(byte_evidence)},
        "numeric": {"status": numeric_status, "tolerance_schema_sha256": comparison_contract_sha256, "comparisons": comparisons, "evidence_sha256": digest(numeric_evidence)},
        "semantic": {"status": semantic_status, "stable_semantic_equality": semantic_status == "PASS", "evidence_sha256": digest(semantic_evidence)},
        "relationship_integrity": {"run_a": {"status": run_a_relationship_status, "evidence_sha256": digest(left_relationship)}, "run_b": {"status": run_b_relationship_status, "evidence_sha256": digest(right_relationship)}},
        "visual": {"status": visual_status, "reason_code": visual_evidence["reason_code"], "evidence_sha256": digest(visual_evidence)},
        "volatile_audit": {"run_a_removed_value_hashes": [copy.deepcopy(dict(item)) for item in run_a_volatile_records], "run_b_removed_value_hashes": [copy.deepcopy(dict(item)) for item in run_b_volatile_records]},
        "overall_status": overall, "non_claims": _sorted_nonclaims(non_claims), "comparison_report_sha256": "0" * 64,
    }
    report["comparison_report_sha256"] = hashlib.sha256(canonical_json_object_v1(report, self_hash_pointer="/comparison_report_sha256")).hexdigest().upper()
    return report


def _run_reference_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    run = _closed(
        value,
        (
            "operational_evidence_kind",
            "operational_evidence_schema_version",
            "operational_evidence_sha256",
            "deterministic_projection_sha256",
            "numeric_comparison_input_sha256",
        ),
        "run reference v2",
    )
    if (
        run["operational_evidence_kind"] != "SUCCESS_MANIFEST"
        or run["operational_evidence_schema_version"]
        != "latticegpt_operational_result_manifest_v2"
    ):
        raise ValueError("layered comparison v2 requires success manifest v2 references")
    for key in (
        "operational_evidence_sha256",
        "deterministic_projection_sha256",
        "numeric_comparison_input_sha256",
    ):
        _sha(run[key], key)
    return run


def build_layered_comparison_report_v2(
    *,
    execution_context: Mapping[str, Any],
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
    byte_status: str,
    numeric_status: str,
    semantic_status: str,
    run_a_relationship_status: str,
    run_b_relationship_status: str,
    visual_status: str,
    non_claims: list[str],
    numeric_comparisons: list[Mapping[str, Any]],
    run_a_relationship_evidence: Mapping[str, Any],
    run_b_relationship_evidence: Mapping[str, Any],
    run_a_volatile_records: list[Mapping[str, Any]],
    run_b_volatile_records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    left = _run_reference_v2(run_a)
    right = _run_reference_v2(run_b)
    legacy_left = {
        **left,
        "operational_evidence_schema_version": "latticegpt_operational_result_manifest_v1",
    }
    legacy_right = {
        **right,
        "operational_evidence_schema_version": "latticegpt_operational_result_manifest_v1",
    }
    report = build_layered_comparison_report_v1(
        comparison_contract_sha256=context["reproduction"]["comparison_contract_sha256"],
        run_a=legacy_left,
        run_b=legacy_right,
        byte_status=byte_status,
        numeric_status=numeric_status,
        semantic_status=semantic_status,
        run_a_relationship_status=run_a_relationship_status,
        run_b_relationship_status=run_b_relationship_status,
        visual_status=visual_status,
        non_claims=non_claims,
        numeric_comparisons=numeric_comparisons,
        run_a_relationship_evidence=run_a_relationship_evidence,
        run_b_relationship_evidence=run_b_relationship_evidence,
        run_a_volatile_records=run_a_volatile_records,
        run_b_volatile_records=run_b_volatile_records,
    )
    result = {
        **{key: copy.deepcopy(value) for key, value in report.items() if key != "comparison_report_sha256"},
        "schema_version": "latticegpt_layered_comparison_report_v2",
        "execution_context_sha256": context["execution_context_sha256"],
        "controlled_identity": copy.deepcopy(context["controlled_identity"]),
        "controlled_identity_sha256": context["controlled_identity_sha256"],
        "tolerance_schema_sha256": context["reproduction"]["tolerance_schema_sha256"],
        "run_a": left,
        "run_b": right,
        "comparison_report_sha256": "0" * 64,
    }
    result["numeric"]["tolerance_schema_sha256"] = context["reproduction"]["tolerance_schema_sha256"]
    numeric_evidence = {
        "schema_version": "latticegpt_numeric_layer_evidence_v2",
        "execution_context_sha256": context["execution_context_sha256"],
        "comparison_contract_sha256": context["reproduction"]["comparison_contract_sha256"],
        "tolerance_schema_sha256": context["reproduction"]["tolerance_schema_sha256"],
        "run_a_numeric_comparison_input_sha256": left["numeric_comparison_input_sha256"],
        "run_b_numeric_comparison_input_sha256": right["numeric_comparison_input_sha256"],
        "status": numeric_status,
        "comparisons": copy.deepcopy(result["numeric"]["comparisons"]),
    }
    result["numeric"]["evidence_sha256"] = hashlib.sha256(
        canonical_json_object_v1(numeric_evidence)
    ).hexdigest().upper()
    visual_evidence = build_visual_layer_evidence_v1(
        run_a=left, run_b=right, visual_status=visual_status
    )
    result["visual"]["evidence_sha256"] = hashlib.sha256(
        canonical_json_object_v1(visual_evidence)
    ).hexdigest().upper()
    result["comparison_report_sha256"] = hashlib.sha256(
        canonical_json_object_v1(result, self_hash_pointer="/comparison_report_sha256")
    ).hexdigest().upper()
    return validate_layered_comparison_report_v2(result, execution_context=context)


def validate_layered_comparison_report_v2(
    value: Mapping[str, Any], *, execution_context: Mapping[str, Any]
) -> dict[str, Any]:
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(execution_context)
    report = _closed(
        value,
        (
            "schema_version",
            "comparison_contract_sha256",
            "tolerance_schema_sha256",
            "execution_context_sha256",
            "controlled_identity",
            "controlled_identity_sha256",
            "run_a",
            "run_b",
            "byte",
            "numeric",
            "semantic",
            "relationship_integrity",
            "visual",
            "volatile_audit",
            "overall_status",
            "non_claims",
            "comparison_report_sha256",
        ),
        "layered comparison report v2",
    )
    if report["schema_version"] != "latticegpt_layered_comparison_report_v2":
        raise ValueError("invalid layered comparison report v2 schema")
    if (
        report["execution_context_sha256"] != context["execution_context_sha256"]
        or report["controlled_identity"] != context["controlled_identity"]
        or report["controlled_identity_sha256"] != context["controlled_identity_sha256"]
    ):
        raise ValueError("layered comparison execution context mismatch")
    if report["comparison_contract_sha256"] != context["reproduction"]["comparison_contract_sha256"]:
        raise ValueError("layered comparison contract identity mismatch")
    if report["tolerance_schema_sha256"] != context["reproduction"]["tolerance_schema_sha256"]:
        raise ValueError("layered tolerance identity mismatch")
    if report["comparison_contract_sha256"] == report["tolerance_schema_sha256"]:
        raise ValueError("comparison contract and tolerance identities must be distinct")
    left = _run_reference_v2(report["run_a"])
    right = _run_reference_v2(report["run_b"])
    if left["operational_evidence_sha256"] == right["operational_evidence_sha256"]:
        raise ValueError("SELF_COMPARISON_FORBIDDEN")

    byte_layer = _closed(
        report["byte"],
        ("status", "compared_json_pointers", "mismatch_json_pointers", "evidence_sha256"),
        "byte layer",
    )
    numeric_layer = _closed(
        report["numeric"],
        ("status", "tolerance_schema_sha256", "comparisons", "evidence_sha256"),
        "numeric layer",
    )
    semantic_layer = _closed(
        report["semantic"],
        ("status", "stable_semantic_equality", "evidence_sha256"),
        "semantic layer",
    )
    relationship = _closed(report["relationship_integrity"], ("run_a", "run_b"), "relationship layer")
    rel_a = _closed(relationship["run_a"], ("status", "evidence_sha256"), "run_a relationship")
    rel_b = _closed(relationship["run_b"], ("status", "evidence_sha256"), "run_b relationship")
    visual = _closed(report["visual"], ("status", "reason_code", "evidence_sha256"), "visual layer")
    volatile = _closed(
        report["volatile_audit"],
        ("run_a_removed_value_hashes", "run_b_removed_value_hashes"),
        "volatile audit",
    )
    mandatory_statuses = [byte_layer["status"], numeric_layer["status"], semantic_layer["status"], rel_a["status"], rel_b["status"]]
    if any(item not in {"PASS", "HOLD", "FAIL"} for item in mandatory_statuses):
        raise ValueError("invalid mandatory layer status")
    if visual["status"] not in {"PASS", "HOLD", "FAIL", "NOT_APPLICABLE"}:
        raise ValueError("invalid visual layer status")
    aggregate = mandatory_statuses + (["PASS"] if visual["status"] == "NOT_APPLICABLE" else [visual["status"]])
    expected_overall = "FAIL" if "FAIL" in aggregate else "HOLD" if "HOLD" in aggregate else "PASS"
    if report["overall_status"] != expected_overall:
        raise ValueError("layered comparison aggregate mismatch")
    if semantic_layer["stable_semantic_equality"] is not (semantic_layer["status"] == "PASS"):
        raise ValueError("semantic equality/status mismatch")
    if numeric_layer["tolerance_schema_sha256"] != report["tolerance_schema_sha256"]:
        raise ValueError("numeric layer tolerance identity mismatch")
    if not isinstance(numeric_layer["comparisons"], list):
        raise ValueError("numeric comparisons must be an array")
    if numeric_layer["status"] == "PASS" and any(item.get("status") != "PASS" for item in numeric_layer["comparisons"]):
        raise ValueError("numeric PASS requires every comparison PASS")
    for rows_name, rows in volatile.items():
        if not isinstance(rows, list):
            raise ValueError(f"{rows_name} must be an array")
        pointers = [item.get("concrete_json_pointer") for item in rows if isinstance(item, Mapping)]
        if len(pointers) != len(rows) or pointers != sorted(set(pointers), key=lambda item: item.encode("utf-8")):
            raise ValueError("volatile records must have sorted unique concrete pointers")
        for row in rows:
            _closed(row, ("pattern_json_pointer", "concrete_json_pointer", "match_index", "value_sha256"), "volatile row")
            if isinstance(row["match_index"], bool) or not isinstance(row["match_index"], int) or row["match_index"] < 0:
                raise ValueError("volatile match_index must be nonnegative integer")
            _sha(row["value_sha256"], "volatile value SHA")
    for layer in (byte_layer, numeric_layer, semantic_layer, rel_a, rel_b, visual):
        _sha(layer["evidence_sha256"], "layer evidence SHA")
    _sorted_nonclaims(report["non_claims"])
    expected_sha = hashlib.sha256(
        canonical_json_object_v1(report, self_hash_pointer="/comparison_report_sha256")
    ).hexdigest().upper()
    if _sha(report["comparison_report_sha256"], "comparison report SHA") != expected_sha:
        raise ValueError("comparison report v2 self hash mismatch")
    return report


_LAYER_EVIDENCE_FILENAMES_V2 = (
    "byte_layer_evidence_v1.json",
    "numeric_layer_evidence_v1.json",
    "semantic_layer_evidence_v1.json",
    "run_a_relationship_integrity_evidence_v1.json",
    "run_b_relationship_integrity_evidence_v1.json",
    "visual_layer_evidence_v1.json",
)


def _parse_canonical_layer_evidence(raw: bytes, schema_version: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("layer evidence must be UTF-8 bytes without BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("layer evidence must use strict UTF-8") from exc
    if not text.endswith("\n") or text.endswith("\n\n") or "\r" in text:
        raise ValueError("layer evidence must have exactly one final LF")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {token}")

    try:
        value = json.loads(text[:-1], object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("layer evidence is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != schema_version:
        raise ValueError("layer evidence schema identity mismatch")
    if canonical_json_object_v1(value) != raw:
        raise ValueError("layer evidence is not canonical_json_object_v1 bytes")
    return value


def validate_layered_comparison_external_evidence_v2(
    report_value: Mapping[str, Any], *, external_evidence: Mapping[str, bytes],
    execution_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind every v2 report layer to its exact canonical external file bytes."""
    report = validate_layered_comparison_report_v2(report_value, execution_context=execution_context)
    if tuple(external_evidence.keys()) != _LAYER_EVIDENCE_FILENAMES_V2:
        raise ValueError("external layer evidence file set/order mismatch")
    schemas = {
        "byte_layer_evidence_v1.json": "latticegpt_byte_layer_evidence_v1",
        "numeric_layer_evidence_v1.json": "latticegpt_numeric_layer_evidence_v2",
        "semantic_layer_evidence_v1.json": "latticegpt_semantic_layer_evidence_v1",
        "run_a_relationship_integrity_evidence_v1.json": "latticegpt_relationship_integrity_evidence_v1",
        "run_b_relationship_integrity_evidence_v1.json": "latticegpt_relationship_integrity_evidence_v1",
        "visual_layer_evidence_v1.json": "latticegpt_visual_layer_evidence_v1",
    }
    parsed = {name: _parse_canonical_layer_evidence(external_evidence[name], schemas[name]) for name in _LAYER_EVIDENCE_FILENAMES_V2}
    references = {
        "byte_layer_evidence_v1.json": report["byte"]["evidence_sha256"],
        "numeric_layer_evidence_v1.json": report["numeric"]["evidence_sha256"],
        "semantic_layer_evidence_v1.json": report["semantic"]["evidence_sha256"],
        "run_a_relationship_integrity_evidence_v1.json": report["relationship_integrity"]["run_a"]["evidence_sha256"],
        "run_b_relationship_integrity_evidence_v1.json": report["relationship_integrity"]["run_b"]["evidence_sha256"],
        "visual_layer_evidence_v1.json": report["visual"]["evidence_sha256"],
    }
    for name, expected_sha in references.items():
        if hashlib.sha256(external_evidence[name]).hexdigest().upper() != expected_sha:
            raise ValueError(f"external layer evidence SHA mismatch: {name}")
    if parsed["byte_layer_evidence_v1.json"] != {
        "schema_version": "latticegpt_byte_layer_evidence_v1",
        "run_a_projection_sha256": report["run_a"]["deterministic_projection_sha256"],
        "run_b_projection_sha256": report["run_b"]["deterministic_projection_sha256"],
        "status": report["byte"]["status"],
        "compared_json_pointers": report["byte"]["compared_json_pointers"],
        "mismatch_json_pointers": report["byte"]["mismatch_json_pointers"],
    }:
        raise ValueError("byte external evidence content mismatch")
    if parsed["numeric_layer_evidence_v1.json"] != {
        "schema_version": "latticegpt_numeric_layer_evidence_v2",
        "execution_context_sha256": report["execution_context_sha256"],
        "comparison_contract_sha256": report["comparison_contract_sha256"],
        "tolerance_schema_sha256": report["tolerance_schema_sha256"],
        "run_a_numeric_comparison_input_sha256": report["run_a"]["numeric_comparison_input_sha256"],
        "run_b_numeric_comparison_input_sha256": report["run_b"]["numeric_comparison_input_sha256"],
        "status": report["numeric"]["status"],
        "comparisons": report["numeric"]["comparisons"],
    }:
        raise ValueError("numeric external evidence content mismatch")
    if parsed["semantic_layer_evidence_v1.json"] != {
        "schema_version": "latticegpt_semantic_layer_evidence_v1",
        "run_a_projection_sha256": report["run_a"]["deterministic_projection_sha256"],
        "run_b_projection_sha256": report["run_b"]["deterministic_projection_sha256"],
        "status": report["semantic"]["status"],
        "stable_semantic_equality": report["semantic"]["stable_semantic_equality"],
    }:
        raise ValueError("semantic external evidence content mismatch")
    for name, run_key in (("run_a_relationship_integrity_evidence_v1.json", "run_a"), ("run_b_relationship_integrity_evidence_v1.json", "run_b")):
        evidence = parsed[name]
        if evidence.get("status") != report["relationship_integrity"][run_key]["status"] or evidence.get("operational_evidence_sha256") != report[run_key]["operational_evidence_sha256"]:
            raise ValueError(f"relationship external evidence content mismatch: {run_key}")
    expected_visual = build_visual_layer_evidence_v1(run_a=report["run_a"], run_b=report["run_b"], visual_status=report["visual"]["status"])
    if parsed["visual_layer_evidence_v1.json"] != expected_visual:
        raise ValueError("visual external evidence content mismatch")
    return report


RELATIONSHIP_KEYS = (
    "reservation_id", "job_id", "upload_id", "attempt_id", "package_id",
    "candidate_id", "selection_sha256",
)


def _validate_relationship(value: Any) -> dict[str, str]:
    relationship = _closed(value, RELATIONSHIP_KEYS, "result.relationship")
    for key, item in relationship.items():
        if key == "selection_sha256":
            _sha(item, f"relationship.{key}")
        else:
            _nonempty(item, f"relationship.{key}")
    return relationship


def build_operational_result_content_v1(
    *,
    relationship: Mapping[str, Any],
    candidate_identity: Mapping[str, Any],
    mesh_binding_sha256: str,
    artifacts: list[Mapping[str, Any]],
    producer_status: str,
    non_claims: list[str],
) -> dict[str, Any]:
    payload = {
        "schema_version": "latticegpt_operational_result_content_v1",
        "relationship": _validate_relationship(relationship),
        "candidate_identity": copy.deepcopy(dict(candidate_identity)),
        "mesh_binding_sha256": _sha(mesh_binding_sha256, "mesh_binding_sha256"),
        "artifacts": copy.deepcopy(artifacts),
        "producer_status": producer_status,
        "non_claims": sorted(set(non_claims)),
    }
    result = dict(payload)
    result["result_content_sha256"] = hashlib.sha256(
        canonical_json_object_v1(payload)
    ).hexdigest().upper()
    return validate_operational_result_content_v1(
        result,
        expected_relationship=relationship,
    )


def validate_operational_result_content_v1(
    value: Mapping[str, Any],
    *,
    expected_relationship: Mapping[str, Any],
) -> dict[str, Any]:
    result = _closed(
        value,
        (
            "schema_version", "relationship", "candidate_identity",
            "mesh_binding_sha256", "artifacts", "producer_status", "non_claims",
            "result_content_sha256",
        ),
        "operational result content",
    )
    if result["schema_version"] != "latticegpt_operational_result_content_v1":
        raise ValueError("invalid operational result schema")
    relationship = _validate_relationship(result["relationship"])
    expected = _validate_relationship(expected_relationship)
    if relationship != expected:
        raise ValueError("RELATIONSHIP_MISMATCH")
    if not isinstance(result["candidate_identity"], Mapping):
        raise ValueError("candidate_identity must be an object")
    _sha(result["mesh_binding_sha256"], "mesh_binding_sha256")
    if not isinstance(result["artifacts"], list):
        raise ValueError("artifacts must be an array")
    _nonempty(result["producer_status"], "producer_status")
    if not isinstance(result["non_claims"], list) or result["non_claims"] != sorted(set(result["non_claims"])):
        raise ValueError("non_claims must be sorted unique")
    payload = {key: copy.deepcopy(item) for key, item in result.items() if key != "result_content_sha256"}
    expected_sha = hashlib.sha256(canonical_json_object_v1(payload)).hexdigest().upper()
    if _sha(result["result_content_sha256"], "result_content_sha256") != expected_sha:
        raise ValueError("result_content_sha256 identity mismatch")
    return copy.deepcopy(result)


def _directory_identity(path: str | Path) -> tuple[int, int]:
    normalized = Path(os.path.abspath(os.fspath(path)))
    if not normalized.is_dir():
        raise ValueError(f"output root is not an existing directory: {normalized}")
    stat = normalized.stat()
    return int(stat.st_dev), int(stat.st_ino)


def assert_distinct_success_runs(
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
) -> None:
    for run in (run_a, run_b):
        if run.get("operational_evidence_kind") != "SUCCESS_MANIFEST":
            raise ValueError("Gate 7 requires SUCCESS_MANIFEST")
        _sha(run.get("operational_evidence_sha256"), "operational_evidence_sha256")
        identity = run.get("run_identity")
        if not isinstance(identity, list) or len(identity) != 5 or any(not isinstance(item, str) or not item for item in identity):
            raise ValueError("run_identity must contain five nonempty IDs")
    if run_a["operational_evidence_sha256"] == run_b["operational_evidence_sha256"]:
        raise ValueError("SELF_COMPARISON_FORBIDDEN: equal operational evidence")
    if run_a["run_identity"] == run_b["run_identity"]:
        raise ValueError("SELF_COMPARISON_FORBIDDEN: duplicate run identity")
    if _directory_identity(run_a.get("output_root_absolute")) == _directory_identity(run_b.get("output_root_absolute")):
        raise ValueError("SELF_COMPARISON_FORBIDDEN: aliased output root")


CONTROLLED_IDENTITY_KEYS = (
    "source_manifest_sha256", "artifact_manifest_sha256",
    "environment_manifest_sha256", "formal_bundle_binding_sha256",
    "reproduction_profile_sha256", "comparison_contract_sha256",
    "tolerance_schema_path", "tolerance_schema_sha256",
)

FAILURE_RUN_KEYS = ("job_id", "upload_id", "reservation_id", "attempt_id", "package_id")
FAILURE_STAGES_V1 = {
    "RAW_JSON_INGRESS", "PROFILE_VALIDATION", "JOB_CREATION", "RESERVATION",
    "MESH_BINDING", "GATE3_PREFLIGHT", "GATE3_CREATE", "GATE4_PREFLIGHT",
    "GATE4_CREATE", "GATE5_PREFLIGHT", "GATE5_CREATE", "CONTROLLED_RUN",
    "COMPARISON",
}
FAILURE_ERROR_CODES_V1 = {
    "INVALID_UTF8", "BOM_FORBIDDEN", "INVALID_JSON_ROOT", "DUPLICATE_JSON_KEY",
    "TRAILING_JSON_DATA", "SCHEMA_REJECTED", "IDENTITY_MISMATCH",
    "RELATIONSHIP_MISMATCH", "RESERVATION_COLLISION", "OUTPUT_TARGET_EXISTS",
    "OUTPUT_PATH_UNSAFE", "OUTPUT_PARENT_IDENTITY_CHANGED", "EXCLUSIVE_CREATE_RACE",
    "PARTIAL_OUTPUT_QUARANTINED", "SELF_COMPARISON_FORBIDDEN",
    "CONTROLLED_RUN_FAILED", "REQUIRED_EVIDENCE_MISSING", "COMPARISON_MISMATCH",
}


def _relative_evidence_path(value: Any, label: str) -> str:
    text = _nonempty(value, label)
    if "\\" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{label} must be a POSIX relative path")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} contains an invalid path component")
    return text


def _validate_failure_run_v1(run: Any, stage: str) -> dict[str, Any]:
    result = _closed(run, FAILURE_RUN_KEYS, "failure run")
    for key, item in result.items():
        if item is not None:
            _nonempty(item, f"run.{key}")
    if stage == "COMPARISON":
        if any(item is not None for item in result.values()):
            raise ValueError("COMPARISON run leaves must all be null")
    elif stage == "JOB_CREATION":
        if result["job_id"] is None or any(result[key] is not None for key in FAILURE_RUN_KEYS[1:]):
            raise ValueError("JOB_CREATION requires only the proposed job_id")
    elif stage == "PROFILE_VALIDATION":
        if any(item is not None for item in result.values()):
            raise ValueError("PROFILE_VALIDATION run leaves must all be null")
    elif stage in {"RESERVATION", "MESH_BINDING", "CONTROLLED_RUN"}:
        if any(item is None for item in result.values()):
            raise ValueError(f"{stage} requires the complete five-ID tuple")
    return result


def _validate_produced_objects_v1(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("produced_objects must be an array")
    result: list[dict[str, Any]] = []
    allowed_kinds = {
        "SOURCE_MANIFEST", "SOURCE_DETACHED_SHA", "SOURCE_ALLOWLIST",
        "LEGACY_EXCLUSION", "IMPORT_CLOSURE_REPORT", "HARDCODED_PATH_REPORT",
        "ARTIFACT_MANIFEST", "ARTIFACT_DETACHED_SHA", "ENVIRONMENT_MANIFEST",
        "ENVIRONMENT_DETACHED_SHA", "JOB_SNAPSHOT", "JOB_EVENT", "RESERVATION",
        "MESH_BINDING", "PACKAGE_METADATA", "RESULT_METADATA", "RUNTIME_OUTPUT",
        "COMPARISON_REPORT",
    }
    allowed_producers = {
        "SOURCE_RELEASE_BUILDER", "ARTIFACT_REBIND_BUILDER",
        "ENVIRONMENT_REBIND_BUILDER", "JOB_STORE", "MESH_BINDING_BUILDER",
        "ADAPTER", "CONFORMAL_RUNTIME", "COMPARISON_ENGINE",
        "UNKNOWN_EXTERNAL_RACING_WRITER",
    }
    for index, item in enumerate(value):
        row = _closed(
            item,
            ("object_kind", "relative_path", "object_type", "size_bytes", "sha256",
             "producer", "ownership", "quarantine_status"),
            f"produced_objects[{index}]",
        )
        if row["object_kind"] not in allowed_kinds:
            raise ValueError("unknown produced object kind")
        _relative_evidence_path(row["relative_path"], "produced object path")
        if row["object_type"] not in {"REGULAR_FILE", "DIRECTORY", "SYMLINK", "REPARSE_POINT", "UNKNOWN"}:
            raise ValueError("unknown produced object type")
        if row["object_type"] == "REGULAR_FILE":
            _nonnegative_int(row["size_bytes"], "produced object size")
            _sha(row["sha256"], "produced object SHA")
        elif row["size_bytes"] is not None or row["sha256"] is not None:
            raise ValueError("non-file produced object size and SHA must be null")
        if row["producer"] not in allowed_producers:
            raise ValueError("unknown produced object producer")
        if row["ownership"] not in {"PREEXISTING", "CREATED_BY_CURRENT_INVOCATION", "CREATED_BY_RACING_WRITER", "EXTERNAL_CONTROLLED_INPUT"}:
            raise ValueError("unknown produced object ownership")
        if row["quarantine_status"] not in {"NOT_APPLICABLE", "QUARANTINED_NO_AUTOMATIC_CLEANUP"}:
            raise ValueError("unknown quarantine status")
        result.append(row)
    ordered = sorted(result, key=lambda row: (row["relative_path"].encode("utf-8"), row["object_kind"].encode("utf-8")))
    if result != ordered or len({(row["relative_path"], row["object_kind"]) for row in result}) != len(result):
        raise ValueError("produced_objects must be sorted and unique")
    return result


def _validate_partial_state_v1(value: Any) -> dict[str, Any]:
    partial = _closed(
        value,
        ("status", "expected_targets", "preexisting_targets", "builder_created_targets",
         "race_created_targets", "cleanup_authorized"),
        "partial_state",
    )
    if partial["status"] not in {"NONE", "PARTIAL_QUARANTINED"} or partial["cleanup_authorized"] is not False:
        raise ValueError("invalid partial state status or cleanup contract")
    for key in ("expected_targets", "preexisting_targets", "builder_created_targets", "race_created_targets"):
        values = partial[key]
        if not isinstance(values, list):
            raise ValueError(f"partial_state.{key} must be an array")
        for item in values:
            _relative_evidence_path(item, f"partial_state.{key}")
        if values != sorted(set(values), key=lambda item: item.encode("utf-8")):
            raise ValueError(f"partial_state.{key} must be sorted unique")
    builder = set(partial["builder_created_targets"])
    race = set(partial["race_created_targets"])
    if builder & race:
        raise ValueError("builder and race targets must be disjoint")
    if partial["status"] == "NONE" and (builder or race):
        raise ValueError("NONE partial state cannot contain created targets")
    if partial["status"] == "PARTIAL_QUARANTINED" and not (builder or race):
        raise ValueError("PARTIAL_QUARANTINED requires created targets")
    return partial


def build_operational_failure_evidence_v1(
    *,
    controlled_identities: Mapping[str, Any],
    run: Mapping[str, Any],
    status: str,
    stage: str,
    error_code: str,
    message: str,
    occurred_at: str,
    produced_objects: list[Mapping[str, Any]],
    partial_state: Mapping[str, Any],
    relationship_integrity_status: str,
    non_claims: list[str],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": "latticegpt_operational_failure_evidence_v1",
        "generation": {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"},
        "controlled_identities": dict(controlled_identities),
        "run": dict(run),
        "failure": {
            "status": status,
            "stage": stage,
            "error_code": error_code,
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest().upper(),
            "occurred_at": occurred_at,
        },
        "produced_objects": [dict(item) for item in produced_objects],
        "partial_state": dict(partial_state),
        "relationship_integrity_status": relationship_integrity_status,
        "non_claims": sorted(set(non_claims)),
        "failure_evidence_sha256": "0" * 64,
    }
    evidence["failure_evidence_sha256"] = hashlib.sha256(
        canonical_json_object_v1(evidence, self_hash_pointer="/failure_evidence_sha256")
    ).hexdigest().upper()
    return validate_operational_failure_evidence_v1(evidence)


def build_comparison_failure_evidence_v1(
    *,
    controlled_identities: Mapping[str, Any],
    status: str,
    error_code: str,
    message: str,
    occurred_at: str,
    non_claims: list[str],
) -> dict[str, Any]:
    controlled = _closed(controlled_identities, CONTROLLED_IDENTITY_KEYS, "controlled_identities")
    evidence: dict[str, Any] = {
        "schema_version": "latticegpt_operational_failure_evidence_v1",
        "generation": {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"},
        "controlled_identities": controlled,
        "run": {"job_id": None, "upload_id": None, "reservation_id": None, "attempt_id": None, "package_id": None},
        "failure": {
            "status": status,
            "stage": "COMPARISON",
            "error_code": error_code,
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest().upper(),
            "occurred_at": occurred_at,
        },
        "produced_objects": [],
        "partial_state": {
            "status": "NONE", "expected_targets": [], "preexisting_targets": [],
            "builder_created_targets": [], "race_created_targets": [],
            "cleanup_authorized": False,
        },
        "relationship_integrity_status": "NOT_REACHED",
        "non_claims": sorted(set(non_claims)),
        "failure_evidence_sha256": "0" * 64,
    }
    evidence["failure_evidence_sha256"] = hashlib.sha256(
        canonical_json_object_v1(evidence, self_hash_pointer="/failure_evidence_sha256")
    ).hexdigest().upper()
    return validate_operational_failure_evidence_v1(evidence)


def validate_operational_failure_evidence_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _closed(
        value,
        (
            "schema_version", "generation", "controlled_identities", "run", "failure",
            "produced_objects", "partial_state", "relationship_integrity_status",
            "non_claims", "failure_evidence_sha256",
        ),
        "operational failure evidence",
    )
    if evidence["schema_version"] != "latticegpt_operational_failure_evidence_v1":
        raise ValueError("invalid failure evidence schema")
    generation = _closed(evidence["generation"], ("schema_version", "stage", "contract"), "generation")
    if generation != {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_DETERMINISM_MESH_BINDING_V1"}:
        raise ValueError("invalid R3 generation discriminator")
    controlled = _closed(evidence["controlled_identities"], CONTROLLED_IDENTITY_KEYS, "controlled_identities")
    for key, item in controlled.items():
        if key == "tolerance_schema_path":
            if item != "config/reproduction_tolerances_v1.json":
                raise ValueError("invalid tolerance schema path")
        else:
            _sha(item, key)
    failure = _closed(evidence["failure"], ("status", "stage", "error_code", "message_sha256", "occurred_at"), "failure")
    if failure["status"] not in {"HOLD", "FAIL"}:
        raise ValueError("invalid failure status")
    if failure["stage"] not in FAILURE_STAGES_V1:
        raise ValueError("invalid failure stage")
    _validate_failure_run_v1(evidence["run"], failure["stage"])
    if failure["error_code"] not in FAILURE_ERROR_CODES_V1:
        raise ValueError("invalid failure error code")
    _sha(failure["message_sha256"], "message_sha256")
    try:
        datetime.fromisoformat(failure["occurred_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("occurred_at must be RFC3339 with offset") from exc
    if datetime.fromisoformat(failure["occurred_at"]).utcoffset() is None:
        raise ValueError("occurred_at must include an offset")
    _validate_produced_objects_v1(evidence["produced_objects"])
    _validate_partial_state_v1(evidence["partial_state"])
    if evidence["relationship_integrity_status"] not in {"NOT_REACHED", "PASS", "HOLD", "FAIL"}:
        raise ValueError("invalid failure relationship status")
    if failure["stage"] == "COMPARISON" and evidence["relationship_integrity_status"] != "NOT_REACHED":
        raise ValueError("comparison failure relationship status must be NOT_REACHED")
    if (
        not isinstance(evidence["non_claims"], list)
        or any(not isinstance(item, str) or not item for item in evidence["non_claims"])
        or evidence["non_claims"] != sorted(set(evidence["non_claims"]))
    ):
        raise ValueError("non_claims must be sorted unique")
    expected_sha = hashlib.sha256(
        canonical_json_object_v1(evidence, self_hash_pointer="/failure_evidence_sha256")
    ).hexdigest().upper()
    if _sha(evidence["failure_evidence_sha256"], "failure_evidence_sha256") != expected_sha:
        raise ValueError("failure_evidence_sha256 identity mismatch")
    return copy.deepcopy(evidence)


FAILURE_STAGES_V2 = {
    "RAW_JSON_INGRESS", "PROFILE_VALIDATION", "JOB_CREATION", "RESERVATION",
    "MESH_BINDING", "CONTROLLED_RUN", "COMPARISON",
}
FAILURE_PRODUCED_KINDS_V2 = {
    "OUTPUT_ROOT", "RUN_CONTRACT", "EXECUTION_CONTEXT", "JOB_SNAPSHOT",
    "JOB_EVENT", "RESERVATION", "MESH_BINDING", "PACKAGE_METADATA",
    "RESULT_METADATA", "RUNTIME_OUTPUT", "SUCCESS_MANIFEST",
    "FAILURE_EVIDENCE", "DETERMINISTIC_PROJECTION", "NUMERIC_INPUT",
    "RELATIONSHIP_EVIDENCE", "LAYER_EVIDENCE", "COMPARISON_REPORT",
}
FAILURE_PRODUCERS_V2 = {
    "GATE7_RUNNER", "JOB_STORE", "MESH_BINDING_BUILDER", "ADAPTER",
    "CONFORMAL_RUNTIME", "COMPARISON_ENGINE", "UNKNOWN_EXTERNAL_RACING_WRITER",
}


def _validate_produced_objects_v2(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("produced_objects must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        row = _closed(
            item,
            ("object_kind", "relative_path", "object_type", "size_bytes", "sha256",
             "producer", "ownership", "quarantine_status"),
            f"produced_objects[{index}]",
        )
        if row["object_kind"] not in FAILURE_PRODUCED_KINDS_V2:
            raise ValueError("unknown v2 produced object kind")
        _relative_evidence_path(row["relative_path"], "produced object path")
        if row["object_type"] not in {"REGULAR_FILE", "DIRECTORY", "SYMLINK", "REPARSE_POINT", "UNKNOWN"}:
            raise ValueError("unknown produced object type")
        if row["object_type"] == "REGULAR_FILE":
            _nonnegative_int(row["size_bytes"], "produced object size")
            _sha(row["sha256"], "produced object SHA")
        elif row["size_bytes"] is not None or row["sha256"] is not None:
            raise ValueError("directory/non-file size and SHA must be null")
        if row["producer"] not in FAILURE_PRODUCERS_V2:
            raise ValueError("unknown v2 produced object producer")
        if row["ownership"] not in {"PREEXISTING", "CREATED_BY_CURRENT_INVOCATION", "CREATED_BY_RACING_WRITER", "EXTERNAL_CONTROLLED_INPUT"}:
            raise ValueError("unknown produced object ownership")
        if row["quarantine_status"] not in {"NOT_APPLICABLE", "QUARANTINED_NO_AUTOMATIC_CLEANUP"}:
            raise ValueError("unknown quarantine status")
        result.append(row)
    ordered = sorted(result, key=lambda row: (row["relative_path"].encode("utf-8"), row["object_kind"].encode("utf-8")))
    if result != ordered or len({(row["relative_path"], row["object_kind"]) for row in result}) != len(result):
        raise ValueError("produced_objects must be sorted and unique")
    return result


def _validate_failure_run_v2(value: Any, stage: str) -> dict[str, Any]:
    run = _closed(value, FAILURE_RUN_KEYS, "failure run")
    for key, item in run.items():
        if item is not None:
            _nonempty(item, f"run.{key}")
    if stage in {"RAW_JSON_INGRESS", "PROFILE_VALIDATION", "COMPARISON"}:
        if any(item is not None for item in run.values()):
            raise ValueError(f"{stage} run leaves must all be null")
    elif stage == "JOB_CREATION":
        if run["job_id"] is None or any(run[key] is not None for key in FAILURE_RUN_KEYS[1:]):
            raise ValueError("JOB_CREATION requires only proposed job_id")
    else:
        present = [run[key] is not None for key in FAILURE_RUN_KEYS]
        if not present[0] or any(present[index] and not present[index - 1] for index in range(1, len(present))):
            raise ValueError(f"{stage} requires a contiguous run-identity prefix")
        if any(present[index] != present[2] for index in (2, 3, 4)):
            raise ValueError(f"{stage} reservation/attempt/package leaves are inseparable")
    return run


def build_operational_failure_evidence_v2(
    *,
    execution_context: Mapping[str, Any] | None,
    run: Mapping[str, Any],
    status: str,
    stage: str,
    error_code: str,
    message: str,
    occurred_at: str,
    produced_objects: list[Mapping[str, Any]],
    partial_state: Mapping[str, Any],
    relationship_integrity_status: str,
    persistence: Mapping[str, Any],
    non_claims: list[str],
) -> dict[str, Any]:
    context = None
    if execution_context is not None:
        from .r3_execution_context import validate_r3_execution_context_v1

        context = validate_r3_execution_context_v1(execution_context)
    evidence: dict[str, Any] = {
        "schema_version": "latticegpt_operational_failure_evidence_v2",
        "generation": {
            "schema_version": "latticegpt_job_generation_v1",
            "stage": "R3",
            "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1",
        },
        "execution_context_sha256": None if context is None else context["execution_context_sha256"],
        "controlled_identity": None if context is None else context["controlled_identity"],
        "controlled_identity_sha256": None if context is None else context["controlled_identity_sha256"],
        "run": dict(run),
        "failure": {
            "status": status,
            "stage": stage,
            "error_code": error_code,
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest().upper(),
            "occurred_at": occurred_at,
        },
        "produced_objects": [dict(item) for item in produced_objects],
        "partial_state": dict(partial_state),
        "relationship_integrity_status": relationship_integrity_status,
        "persistence": dict(persistence),
        "non_claims": sorted(set(non_claims)),
        "failure_evidence_sha256": "0" * 64,
    }
    evidence["failure_evidence_sha256"] = hashlib.sha256(
        canonical_json_object_v1(evidence, self_hash_pointer="/failure_evidence_sha256")
    ).hexdigest().upper()
    return validate_operational_failure_evidence_v2(evidence)


def validate_operational_failure_evidence_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _closed(
        value,
        (
            "schema_version", "generation", "execution_context_sha256",
            "controlled_identity", "controlled_identity_sha256", "run", "failure",
            "produced_objects", "partial_state", "relationship_integrity_status",
            "persistence", "non_claims", "failure_evidence_sha256",
        ),
        "operational failure evidence v2",
    )
    if evidence["schema_version"] != "latticegpt_operational_failure_evidence_v2":
        raise ValueError("invalid failure evidence v2 schema")
    generation = _closed(evidence["generation"], ("schema_version", "stage", "contract"), "generation")
    if generation != {"schema_version": "latticegpt_job_generation_v1", "stage": "R3", "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1"}:
        raise ValueError("invalid R3 v3 generation discriminator")
    failure = _closed(evidence["failure"], ("status", "stage", "error_code", "message_sha256", "occurred_at"), "failure")
    if failure["status"] not in {"HOLD", "FAIL"} or failure["stage"] not in FAILURE_STAGES_V2 or failure["error_code"] not in FAILURE_ERROR_CODES_V1:
        raise ValueError("invalid failure status, stage, or error code")
    _validate_failure_run_v2(evidence["run"], failure["stage"])
    _sha(failure["message_sha256"], "message_sha256")
    try:
        timestamp = datetime.fromisoformat(failure["occurred_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("occurred_at must be RFC3339") from exc
    if timestamp.utcoffset() is None:
        raise ValueError("occurred_at must include an offset")
    before_context = failure["stage"] in {"RAW_JSON_INGRESS", "PROFILE_VALIDATION"}
    if before_context:
        if any(evidence[key] is not None for key in ("execution_context_sha256", "controlled_identity", "controlled_identity_sha256")):
            raise ValueError("pre-context failure identities must be null")
    else:
        from .r3_execution_context import validate_r3_execution_context_v1

        controlled = evidence["controlled_identity"]
        if not isinstance(controlled, Mapping):
            raise ValueError("post-context failure requires controlled identity")
        _sha(evidence["execution_context_sha256"], "execution context SHA")
        expected_controlled_sha = hashlib.sha256(canonical_json_object_v1(dict(controlled))).hexdigest().upper()
        if _sha(evidence["controlled_identity_sha256"], "controlled identity SHA") != expected_controlled_sha:
            raise ValueError("controlled identity SHA mismatch")
    _validate_produced_objects_v2(evidence["produced_objects"])
    _validate_partial_state_v1(evidence["partial_state"])
    if evidence["relationship_integrity_status"] not in {"NOT_REACHED", "PASS", "HOLD", "FAIL"}:
        raise ValueError("invalid relationship status")
    persistence = _closed(evidence["persistence"], ("status", "relative_path", "error_code"), "persistence")
    if persistence["status"] == "IN_MEMORY_ONLY":
        if persistence["relative_path"] is not None or persistence["error_code"] is not None:
            raise ValueError("IN_MEMORY_ONLY persistence leaves must be null")
    elif persistence["status"] == "PERSISTED_EXCLUSIVE":
        if persistence != {"status": "PERSISTED_EXCLUSIVE", "relative_path": "evidence/operational_failure_evidence_v2.json", "error_code": None}:
            raise ValueError("invalid persisted failure evidence identity")
    elif persistence["status"] == "PERSISTENCE_FAILED":
        if persistence["relative_path"] != "evidence/operational_failure_evidence_v2.json" or persistence["error_code"] not in {"OUTPUT_PATH_UNSAFE", "OUTPUT_TARGET_EXISTS", "OUTPUT_PARENT_IDENTITY_CHANGED", "EXCLUSIVE_CREATE_RACE", "PARTIAL_OUTPUT_QUARANTINED"}:
            raise ValueError("invalid failure evidence persistence failure")
    else:
        raise ValueError("unknown failure evidence persistence status")
    if not isinstance(evidence["non_claims"], list) or any(not isinstance(item, str) or not item for item in evidence["non_claims"]) or evidence["non_claims"] != sorted(set(evidence["non_claims"])):
        raise ValueError("non_claims must be sorted unique")
    expected_sha = hashlib.sha256(
        canonical_json_object_v1(evidence, self_hash_pointer="/failure_evidence_sha256")
    ).hexdigest().upper()
    if _sha(evidence["failure_evidence_sha256"], "failure evidence SHA") != expected_sha:
        raise ValueError("failure evidence SHA mismatch")
    return copy.deepcopy(evidence)


def validate_closed_r3_schema(schema_version: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if schema_version == PROFILE_SCHEMA:
        return validate_reproduction_profile_v1(value)
    if schema_version == "latticegpt_reproduction_tolerances_v1":
        return validate_tolerance_registry_v1(value)
    if schema_version == "latticegpt_numeric_comparison_input_v1":
        return validate_numeric_comparison_input_v1(value)
    if schema_version == "latticegpt_operational_result_manifest_v1":
        return validate_operational_result_manifest_v1(value)
    if schema_version == "latticegpt_operational_failure_evidence_v1":
        return validate_operational_failure_evidence_v1(value)
    if schema_version == "latticegpt_operational_failure_evidence_v2":
        return validate_operational_failure_evidence_v2(value)
    if schema_version == "latticegpt_design_job_v2":
        from .job_state import validate_r3_job_snapshot_v2

        return validate_r3_job_snapshot_v2(value)
    if schema_version == "latticegpt_design_job_v3":
        from .job_state import validate_r3_job_snapshot_v3

        return validate_r3_job_snapshot_v3(value)
    if schema_version in {
        "latticegpt_release_generation_contract_v1",
        "latticegpt_release_generation_contract_v2",
    }:
        from .release_generation import validate_generation_contract_v1

        return validate_generation_contract_v1(value)
    if schema_version == "latticegpt_active_release_binding_v1":
        from .release_generation import validate_active_release_binding_v1

        return validate_active_release_binding_v1(value)
    raise ValueError(f"unsupported closed R3 schema: {schema_version}")


__all__ = [
    "RELATIONSHIP_CHECK_REGISTRY",
    "TOLERANCE_ROWS",
    "VOLATILE_JSON_POINTERS",
    "build_reproduction_profile_v1",
    "build_comparison_contract_v1",
    "build_execution_context_comparison_contract_v1",
    "comparison_contract_sha256_v1",
    "assert_distinct_success_runs",
    "build_comparison_failure_evidence_v1",
    "build_operational_failure_evidence_v1",
    "build_operational_failure_evidence_v2",
    "build_operational_result_content_v1",
    "seal_operational_result_manifest_v1",
    "seal_operational_result_manifest_v2",
    "build_deterministic_projection_v1",
    "build_deterministic_projection_v2",
    "build_layered_comparison_report_v1",
    "build_layered_comparison_report_v2",
    "build_visual_layer_evidence_v1",
    "build_volatile_audit_records_v2",
    "compare_numeric_inputs_v1",
    "relationship_check_registry_v1",
    "reproduction_child_environment",
    "validate_operational_failure_evidence_v1",
    "validate_operational_failure_evidence_v2",
    "validate_comparison_contract_v1",
    "validate_execution_context_comparison_contract_v1",
    "validate_operational_result_content_v1",
    "validate_operational_result_manifest_v1",
    "validate_operational_result_manifest_v2",
    "validate_deterministic_projection_v2",
    "validate_layered_comparison_report_v2",
    "validate_layered_comparison_external_evidence_v2",
    "validate_closed_r3_schema",
    "validate_reproduction_profile_v1",
    "validate_numeric_comparison_input_v1",
    "validate_success_relationship_checks_v1",
    "validate_tolerance_registry_v1",
]
