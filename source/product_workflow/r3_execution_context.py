from __future__ import annotations

import copy
import hashlib
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .contracts import require_model_runtime_profile
from .manifest_io import canonical_json_object_v1
from .reproducibility import (
    comparison_contract_sha256_v1,
    validate_execution_context_comparison_contract_v1,
    validate_reproduction_profile_v1,
    validate_tolerance_registry_v1,
)
from .release_generation import generation_spec_v1


SCHEMA_VERSION = "latticegpt_r3_execution_context_v1"
CONTROLLED_SCHEMA_VERSION = "latticegpt_r3_controlled_identity_v1"
SHA256_UPPER = re.compile(r"^[0-9A-F]{64}$")

TOP_KEYS = (
    "schema_version", "release", "reproduction", "inputs", "execution",
    "outputs", "controlled_identity", "controlled_identity_sha256",
    "execution_context_sha256",
)
RELEASE_KEYS = (
    "generation", "config_path", "config_sha256", "generation_contract_path",
    "generation_contract_schema_version", "generation_contract_sha256",
    "active_release_binding_path", "active_release_binding_sha256",
    "source_manifest_sha256", "artifact_manifest_sha256",
    "environment_manifest_sha256", "formal_bundle_binding_sha256",
)
REPRODUCTION_KEYS = (
    "profile", "profile_sha256", "model_runtime_profile",
    "model_runtime_profile_sha256", "comparison_contract",
    "comparison_contract_sha256", "tolerance_schema_path",
    "tolerance_schema_sha256", "tolerance_registry",
)
TARGET_KEYS = (
    "source_path", "source_file_sha256", "identity_json_pointer",
    "target_sha256", "values_json_pointer", "units_json_pointer", "units",
    "physical_target_9d", "diagnostic_values_sha256",
    "reference_classification", "non_claim",
)
TARGET_VALUE_KEYS = ("C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66")
MESH_KEYS = (
    "path", "original_filename", "raw_size_bytes", "raw_sha256",
    "element_types", "units", "coordinate_system", "design_domain",
)
CASE_KEYS = ("path", "size_bytes", "sha256", "schema_version", "case_id")
EXECUTION_KEYS = (
    "seed", "execution_profile", "device_type", "cuda_allowed", "dtype",
    "deterministic_algorithms", "warn_only", "overwrite", "no_retry",
)
OUTPUT_KEYS = ("run_a_output_root", "run_b_output_root", "comparison_output_root")
CONTROLLED_KEYS = (
    "schema_version", "config_sha256", "generation_contract_sha256",
    "active_release_binding_sha256", "source_manifest_sha256",
    "artifact_manifest_sha256", "environment_manifest_sha256",
    "formal_bundle_binding_sha256", "reproduction_profile_sha256",
    "model_runtime_profile_sha256", "comparison_contract_sha256",
    "tolerance_schema_sha256", "target_sha256", "target_source_file_sha256",
    "mesh_raw_sha256", "case_manifest_sha256", "seed", "execution_profile",
    "dtype",
)


def _closed(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} must contain exactly {keys}")
    return copy.deepcopy(dict(value))


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_UPPER.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256_upper")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _absolute_path(value: Any, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute normalized path")
    resolved = str(path.resolve())
    if os.path.normcase(os.path.normpath(text)) != os.path.normcase(os.path.normpath(resolved)):
        raise ValueError(f"{label} must already be normalized")
    return resolved


def _context_sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_object_v1(dict(value), self_hash_pointer="/execution_context_sha256")
    ).hexdigest().upper()


def _validate_release(value: Any) -> dict[str, Any]:
    release = _closed(value, RELEASE_KEYS, "release")
    generation = _string(release["generation"], "release.generation")
    _absolute_path(release["config_path"], "release.config_path")
    _absolute_path(release["generation_contract_path"], "release.generation_contract_path")
    _absolute_path(release["active_release_binding_path"], "release.active_release_binding_path")
    schema_version = _string(
        release["generation_contract_schema_version"],
        "release.generation_contract_schema_version",
    )
    from .r4_lite_release_generation import CONTRACT_SCHEMA, GENERATION

    if generation == GENERATION:
        spec = {"generation_contract_schema_version": CONTRACT_SCHEMA}
    else:
        spec = generation_spec_v1(generation)
    if schema_version != spec["generation_contract_schema_version"]:
        raise ValueError(
            "generation contract schema does not match the unique generation registry entry"
        )
    for key in RELEASE_KEYS:
        if key.endswith("_sha256"):
            _sha(release[key], f"release.{key}")
    return release


def _validate_reproduction(value: Any, release: Mapping[str, Any]) -> dict[str, Any]:
    reproduction = _closed(value, REPRODUCTION_KEYS, "reproduction")
    profile = validate_reproduction_profile_v1(reproduction["profile"])
    if reproduction["profile_sha256"] != profile["profile_sha256"]:
        raise ValueError("reproduction profile SHA mismatch")
    model = require_model_runtime_profile(reproduction["model_runtime_profile"])
    if reproduction["model_runtime_profile_sha256"] != model["profile_sha256"]:
        raise ValueError("model runtime profile SHA mismatch")
    if model.get("reproduction_profile_sha256") != profile["profile_sha256"]:
        raise ValueError("model runtime profile does not bind the reproduction profile")
    tolerance = validate_tolerance_registry_v1(reproduction["tolerance_registry"])
    if reproduction["tolerance_schema_path"] != "config/reproduction_tolerances_v1.json":
        raise ValueError("invalid tolerance schema path")
    tolerance_sha = _sha(reproduction["tolerance_schema_sha256"], "tolerance schema SHA")
    if profile["tolerance"] != {
        "schema_path": reproduction["tolerance_schema_path"],
        "schema_sha256": tolerance_sha,
    }:
        raise ValueError("profile tolerance identity mismatch")
    contract = validate_execution_context_comparison_contract_v1(
        reproduction["comparison_contract"],
        tolerance_schema_path=reproduction["tolerance_schema_path"],
        tolerance_schema_sha256=tolerance_sha,
    )
    comparison_sha = comparison_contract_sha256_v1(contract)
    if reproduction["comparison_contract_sha256"] != comparison_sha:
        raise ValueError("comparison contract SHA mismatch")
    if comparison_sha == tolerance_sha:
        raise ValueError("comparison contract SHA may not alias tolerance SHA")
    identity_map = {
        "config_sha256": "config_sha256",
        "release_generation_contract_sha256": "generation_contract_sha256",
        "active_release_binding_sha256": "active_release_binding_sha256",
        "source_manifest_sha256": "source_manifest_sha256",
        "artifact_manifest_sha256": "artifact_manifest_sha256",
        "environment_manifest_sha256": "environment_manifest_sha256",
        "formal_bundle_binding_sha256": "formal_bundle_binding_sha256",
    }
    for profile_key, release_key in identity_map.items():
        if profile["identities"][profile_key] != release[release_key]:
            raise ValueError(f"profile identity mismatch: {profile_key}")
    return reproduction


def _validate_inputs(value: Any) -> dict[str, Any]:
    inputs = _closed(value, ("target", "mesh", "case"), "inputs")
    target = _closed(inputs["target"], TARGET_KEYS, "inputs.target")
    _absolute_path(target["source_path"], "target.source_path")
    _sha(target["source_file_sha256"], "target source file SHA")
    if target["identity_json_pointer"] != "/source_context/target_sha256":
        raise ValueError("invalid target identity pointer")
    if target["target_sha256"] != "D08EB2BDFFF79760C60C886BE8CF2B4B3F5313E143354ED4CD2FC1E16871F777":
        raise ValueError("invalid frozen target identity")
    if target["values_json_pointer"] != "/source_context/physical_target_9d/values" or target["units_json_pointer"] != "/source_context/physical_target_9d/units":
        raise ValueError("invalid frozen target pointer")
    if target["units"] != "GPa" or target["reference_classification"] != "DETERMINISM_TECHNICAL_REFERENCE_TARGET" or target["non_claim"] != "NOT_FORMAL_150W_PROVENANCE":
        raise ValueError("invalid target classification or units")
    values = _closed(target["physical_target_9d"], TARGET_VALUE_KEYS, "physical_target_9d")
    for key, item in values.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError(f"physical_target_9d.{key} must be finite")
    diagnostic = hashlib.sha256(canonical_json_object_v1(values)).hexdigest().upper()
    if target["diagnostic_values_sha256"] != diagnostic:
        raise ValueError("diagnostic target-values SHA mismatch")

    mesh = _closed(inputs["mesh"], MESH_KEYS, "inputs.mesh")
    _absolute_path(mesh["path"], "mesh.path")
    _string(mesh["original_filename"], "mesh.original_filename")
    _nonnegative_int(mesh["raw_size_bytes"], "mesh.raw_size_bytes")
    _sha(mesh["raw_sha256"], "mesh.raw_sha256")
    if (
        not isinstance(mesh["element_types"], list)
        or not mesh["element_types"]
        or mesh["element_types"] != sorted(set(mesh["element_types"]), key=lambda item: item.encode("utf-8"))
        or any(item not in {"C3D8", "C3D8R"} for item in mesh["element_types"])
    ):
        raise ValueError("mesh element types must be the sorted unique C3D8/C3D8R subset")
    for key in ("units", "coordinate_system", "design_domain"):
        _string(mesh[key], f"mesh.{key}")

    case = _closed(inputs["case"], CASE_KEYS, "inputs.case")
    _absolute_path(case["path"], "case.path")
    _nonnegative_int(case["size_bytes"], "case.size_bytes")
    _sha(case["sha256"], "case.sha256")
    _string(case["schema_version"], "case.schema_version")
    _string(case["case_id"], "case.case_id")
    return inputs


def _validate_execution(value: Any) -> dict[str, Any]:
    execution = _closed(value, EXECUTION_KEYS, "execution")
    expected = {
        "seed": 20260811,
        "execution_profile": "CPU_ONLY",
        "device_type": "cpu",
        "cuda_allowed": False,
        "dtype": "float32",
        "deterministic_algorithms": True,
        "warn_only": False,
        "overwrite": False,
        "no_retry": True,
    }
    if execution != expected or isinstance(execution["seed"], bool):
        raise ValueError("invalid frozen execution contract")
    return execution


def _validate_outputs(value: Any) -> dict[str, Any]:
    outputs = _closed(value, OUTPUT_KEYS, "outputs")
    normalized = [_absolute_path(outputs[key], f"outputs.{key}") for key in OUTPUT_KEYS]
    if len({os.path.normcase(item) for item in normalized}) != 3:
        raise ValueError("SELF_COMPARISON_FORBIDDEN: output roots must be distinct")
    return outputs


def _expected_controlled(
    release: Mapping[str, Any], reproduction: Mapping[str, Any],
    inputs: Mapping[str, Any], execution: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CONTROLLED_SCHEMA_VERSION,
        "config_sha256": release["config_sha256"],
        "generation_contract_sha256": release["generation_contract_sha256"],
        "active_release_binding_sha256": release["active_release_binding_sha256"],
        "source_manifest_sha256": release["source_manifest_sha256"],
        "artifact_manifest_sha256": release["artifact_manifest_sha256"],
        "environment_manifest_sha256": release["environment_manifest_sha256"],
        "formal_bundle_binding_sha256": release["formal_bundle_binding_sha256"],
        "reproduction_profile_sha256": reproduction["profile_sha256"],
        "model_runtime_profile_sha256": reproduction["model_runtime_profile_sha256"],
        "comparison_contract_sha256": reproduction["comparison_contract_sha256"],
        "tolerance_schema_sha256": reproduction["tolerance_schema_sha256"],
        "target_sha256": inputs["target"]["target_sha256"],
        "target_source_file_sha256": inputs["target"]["source_file_sha256"],
        "mesh_raw_sha256": inputs["mesh"]["raw_sha256"],
        "case_manifest_sha256": inputs["case"]["sha256"],
        "seed": execution["seed"],
        "execution_profile": execution["execution_profile"],
        "dtype": execution["dtype"],
    }


def build_r3_execution_context_v1(
    *,
    release: Mapping[str, Any],
    reproduction_profile: Mapping[str, Any],
    model_runtime_profile: Mapping[str, Any],
    comparison_contract: Mapping[str, Any],
    tolerance_registry: Mapping[str, Any],
    inputs: Mapping[str, Any],
    execution: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> dict[str, Any]:
    profile = validate_reproduction_profile_v1(reproduction_profile)
    model = require_model_runtime_profile(model_runtime_profile)
    contract = validate_execution_context_comparison_contract_v1(
        comparison_contract,
        tolerance_schema_path=profile["tolerance"]["schema_path"],
        tolerance_schema_sha256=profile["tolerance"]["schema_sha256"],
    )
    context: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release": copy.deepcopy(dict(release)),
        "reproduction": {
            "profile": profile,
            "profile_sha256": profile["profile_sha256"],
            "model_runtime_profile": model,
            "model_runtime_profile_sha256": model["profile_sha256"],
            "comparison_contract": contract,
            "comparison_contract_sha256": comparison_contract_sha256_v1(contract),
            "tolerance_schema_path": profile["tolerance"]["schema_path"],
            "tolerance_schema_sha256": profile["tolerance"]["schema_sha256"],
            "tolerance_registry": copy.deepcopy(dict(tolerance_registry)),
        },
        "inputs": copy.deepcopy(dict(inputs)),
        "execution": copy.deepcopy(dict(execution)),
        "outputs": copy.deepcopy(dict(outputs)),
        "controlled_identity": {},
        "controlled_identity_sha256": "0" * 64,
        "execution_context_sha256": "0" * 64,
    }
    release_value = _validate_release(context["release"])
    reproduction = _validate_reproduction(context["reproduction"], release_value)
    input_value = _validate_inputs(context["inputs"])
    execution_value = _validate_execution(context["execution"])
    _validate_outputs(context["outputs"])
    context["controlled_identity"] = _expected_controlled(
        release_value, reproduction, input_value, execution_value
    )
    context["controlled_identity_sha256"] = hashlib.sha256(
        canonical_json_object_v1(context["controlled_identity"])
    ).hexdigest().upper()
    context["execution_context_sha256"] = _context_sha(context)
    return validate_r3_execution_context_v1(context)


def validate_r3_execution_context_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    context = _closed(value, TOP_KEYS, "R3ExecutionContextV1")
    if context["schema_version"] != SCHEMA_VERSION:
        raise ValueError("invalid R3 execution-context schema")
    release = _validate_release(context["release"])
    reproduction = _validate_reproduction(context["reproduction"], release)
    inputs = _validate_inputs(context["inputs"])
    execution = _validate_execution(context["execution"])
    _validate_outputs(context["outputs"])
    controlled = _closed(context["controlled_identity"], CONTROLLED_KEYS, "controlled_identity")
    expected_controlled = _expected_controlled(release, reproduction, inputs, execution)
    if controlled != expected_controlled:
        raise ValueError("controlled identity does not match the execution context")
    expected_controlled_sha = hashlib.sha256(canonical_json_object_v1(controlled)).hexdigest().upper()
    if _sha(context["controlled_identity_sha256"], "controlled identity SHA") != expected_controlled_sha:
        raise ValueError("controlled identity SHA mismatch")
    if _sha(context["execution_context_sha256"], "execution context SHA") != _context_sha(context):
        raise ValueError("execution context SHA mismatch")
    return context


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(path.stat().st_file_attributes & 0x400)
    except AttributeError:
        return path.is_symlink()


def _safe_existing_chain(path: Path) -> None:
    current = path.parent
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    while True:
        if not current.is_dir() or _is_reparse(current):
            raise ValueError("OUTPUT_PATH_UNSAFE: parent is not an ordinary directory")
        if current.parent == current:
            break
        current = current.parent


def preflight_execution_output_roots_v1(
    context: Mapping[str, Any], *, phase: str
) -> dict[str, Any]:
    value = validate_r3_execution_context_v1(context)
    roots = {key: Path(value["outputs"][key]) for key in OUTPUT_KEYS}
    for root in roots.values():
        _safe_existing_chain(root)
    if phase == "PAIR":
        occupied = [key for key, root in roots.items() if root.exists()]
        if occupied:
            raise FileExistsError(f"OUTPUT_TARGET_EXISTS: {occupied}")
        return {"status": "ABSENT_3_OF_3", "roots": {key: str(root) for key, root in roots.items()}}
    if phase == "COMPARE":
        if not roots["run_a_output_root"].is_dir() or not roots["run_b_output_root"].is_dir():
            raise FileNotFoundError("completed Run A and Run B roots are required")
        if roots["comparison_output_root"].exists():
            raise FileExistsError("OUTPUT_TARGET_EXISTS: comparison root")
        return {"status": "RUN_ROOTS_PRESENT_COMPARISON_ABSENT", "roots": {key: str(root) for key, root in roots.items()}}
    raise ValueError("phase must be PAIR or COMPARE")


__all__ = [
    "SCHEMA_VERSION",
    "build_r3_execution_context_v1",
    "preflight_execution_output_roots_v1",
    "validate_r3_execution_context_v1",
]
