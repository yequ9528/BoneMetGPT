"""Closed R4-Lite workstation and final-active identity contracts."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .manifest_io import canonical_json_object_v1, detached_sha256_bytes, sha256_bytes
from .reproducibility import (
    build_comparison_contract_v1,
    validate_comparison_contract_v1,
)


WORKSTATION_PROFILE_SCHEMA = "latticegpt.workstation_reproduction_profile.v1"
FINAL_ACTIVE_IDENTITY_SCHEMA = "latticegpt.r4_lite_active_identity.v1"
_SHA = re.compile(r"^[0-9A-F]{64}$")


def _closed(value: Mapping[str, Any], keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} must contain the exact closed key set")
    return deepcopy(dict(value))


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} must be uppercase SHA256")
    return value


def build_workstation_reproduction_profile_v1(
    *, computer_name: str, os_version: str, architecture: str,
    python_sha256: str, npm_sha256: str,
) -> dict[str, Any]:
    profile = {
        "schema_version": WORKSTATION_PROFILE_SCHEMA,
        "computer_name": str(computer_name),
        "os_version": str(os_version),
        "architecture": str(architecture),
        "python_sha256": _sha(python_sha256, "python_sha256"),
        "npm_sha256": _sha(npm_sha256, "npm_sha256"),
        "scope": "SINGLE_REFERENCE_WORKSTATION",
        "profile_sha256": "0" * 64,
    }
    profile["profile_sha256"] = sha256_bytes(
        canonical_json_object_v1(profile, self_hash_pointer="/profile_sha256")
    )
    return validate_workstation_reproduction_profile_v1(profile)


def validate_workstation_reproduction_profile_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = _closed(value, (
        "schema_version", "computer_name", "os_version", "architecture",
        "python_sha256", "npm_sha256", "scope", "profile_sha256",
    ), "workstation reproduction profile")
    if profile["schema_version"] != WORKSTATION_PROFILE_SCHEMA or profile["scope"] != "SINGLE_REFERENCE_WORKSTATION":
        raise ValueError("workstation reproduction profile literal mismatch")
    for key in ("computer_name", "os_version", "architecture"):
        if not isinstance(profile[key], str) or not profile[key].strip():
            raise ValueError(f"{key} must be non-empty")
    for key in ("python_sha256", "npm_sha256", "profile_sha256"):
        _sha(profile[key], key)
    expected = sha256_bytes(canonical_json_object_v1(profile, self_hash_pointer="/profile_sha256"))
    if profile["profile_sha256"] != expected:
        raise ValueError("workstation reproduction profile self hash mismatch")
    return profile


_FINAL_KEYS = (
    "schema_version", "generation", "config_sha256", "generation_contract_sha256",
    "active_release_binding_sha256", "source_manifest_sha256",
    "artifact_manifest_sha256", "environment_manifest_sha256",
    "formal_bundle_binding_sha256", "reproduction_profile",
    "reproduction_profile_sha256", "comparison_contract_sha256",
    "tolerance_schema_sha256", "scope", "non_claims", "identity_sha256",
)


def build_final_active_identity_v1(
    *, generation: str, config_sha256: str, generation_contract_sha256: str,
    active_release_binding_sha256: str, source_manifest_sha256: str,
    artifact_manifest_sha256: str, environment_manifest_sha256: str,
    formal_bundle_binding_sha256: str, reproduction_profile: Mapping[str, Any],
    comparison_contract_sha256: str, tolerance_schema_sha256: str,
) -> dict[str, Any]:
    profile = validate_workstation_reproduction_profile_v1(reproduction_profile)
    identity = {
        "schema_version": FINAL_ACTIVE_IDENTITY_SCHEMA,
        "generation": str(generation),
        "config_sha256": _sha(config_sha256, "config_sha256"),
        "generation_contract_sha256": _sha(generation_contract_sha256, "generation_contract_sha256"),
        "active_release_binding_sha256": _sha(active_release_binding_sha256, "active_release_binding_sha256"),
        "source_manifest_sha256": _sha(source_manifest_sha256, "source_manifest_sha256"),
        "artifact_manifest_sha256": _sha(artifact_manifest_sha256, "artifact_manifest_sha256"),
        "environment_manifest_sha256": _sha(environment_manifest_sha256, "environment_manifest_sha256"),
        "formal_bundle_binding_sha256": _sha(formal_bundle_binding_sha256, "formal_bundle_binding_sha256"),
        "reproduction_profile": profile,
        "reproduction_profile_sha256": profile["profile_sha256"],
        "comparison_contract_sha256": _sha(comparison_contract_sha256, "comparison_contract_sha256"),
        "tolerance_schema_sha256": _sha(tolerance_schema_sha256, "tolerance_schema_sha256"),
        "scope": "R4_LITE_SINGLE_REFERENCE_WORKSTATION",
        "non_claims": [
            "NOT_CROSS_MACHINE_REPRODUCIBILITY", "NOT_CROSS_PLATFORM_REPRODUCIBILITY",
            "NOT_NEW_ENVIRONMENT_REPRODUCIBILITY", "NOT_GENERAL_DEPLOYMENT",
            "NOT_MULTIUSER_OR_DISTRIBUTED", "NOT_CUDA_EQUIVALENCE",
            "NOT_ONLINE_LLM_DETERMINISM", "NOT_FORMAL_150W_PROVENANCE",
            "NOT_ABAQUS_VALIDATED", "NOT_MANUFACTURING_VALIDATED",
            "NOT_CLINICALLY_VALIDATED", "NOT_ENGINEERING_CERTIFIED",
        ],
        "identity_sha256": "0" * 64,
    }
    identity["identity_sha256"] = sha256_bytes(
        canonical_json_object_v1(identity, self_hash_pointer="/identity_sha256")
    )
    return validate_final_active_identity_v1(identity)


def validate_final_active_identity_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    identity = _closed(value, _FINAL_KEYS, "final active identity")
    if identity["schema_version"] != FINAL_ACTIVE_IDENTITY_SCHEMA or identity["scope"] != "R4_LITE_SINGLE_REFERENCE_WORKSTATION":
        raise ValueError("final active identity literal mismatch")
    if not isinstance(identity["generation"], str) or not identity["generation"]:
        raise ValueError("generation must be non-empty")
    for key in _FINAL_KEYS:
        if key.endswith("_sha256"):
            _sha(identity[key], key)
    profile = validate_workstation_reproduction_profile_v1(identity["reproduction_profile"])
    if identity["reproduction_profile_sha256"] != profile["profile_sha256"]:
        raise ValueError("final identity profile binding mismatch")
    if identity["comparison_contract_sha256"] == identity["tolerance_schema_sha256"]:
        raise ValueError("comparison contract must be distinct from tolerance registry")
    expected = sha256_bytes(canonical_json_object_v1(identity, self_hash_pointer="/identity_sha256"))
    if identity["identity_sha256"] != expected:
        raise ValueError("final active identity self hash mismatch")
    return identity


def final_active_identity_outputs_v1(value: Mapping[str, Any]) -> dict[str, bytes]:
    identity = validate_final_active_identity_v1(value)
    data = canonical_json_object_v1(identity)
    return {
        "final_active_identity.json": data,
        "final_active_identity.json.sha256": detached_sha256_bytes(data, "final_active_identity.json"),
    }


def workstation_reproduction_profile_outputs_v1(value: Mapping[str, Any]) -> dict[str, bytes]:
    profile = validate_workstation_reproduction_profile_v1(value)
    data = canonical_json_object_v1(profile)
    return {
        "workstation_reproduction_profile.json": data,
        "workstation_reproduction_profile.json.sha256": detached_sha256_bytes(data, "workstation_reproduction_profile.json"),
    }


def comparison_contract_outputs_v1(*, tolerance_schema_sha256: str) -> dict[str, bytes]:
    contract = build_comparison_contract_v1(
        tolerance_schema_path="config/reproduction_tolerances_v1.json",
        tolerance_schema_sha256=_sha(tolerance_schema_sha256, "tolerance_schema_sha256"),
    )
    validate_comparison_contract_v1(
        contract,
        tolerance_schema_path="config/reproduction_tolerances_v1.json",
        tolerance_schema_sha256=tolerance_schema_sha256,
    )
    data = canonical_json_object_v1(contract)
    return {
        "comparison_contract.json": data,
        "comparison_contract.json.sha256": detached_sha256_bytes(data, "comparison_contract.json"),
    }


__all__ = [
    "FINAL_ACTIVE_IDENTITY_SCHEMA", "WORKSTATION_PROFILE_SCHEMA",
    "build_final_active_identity_v1", "build_workstation_reproduction_profile_v1",
    "comparison_contract_outputs_v1", "final_active_identity_outputs_v1",
    "workstation_reproduction_profile_outputs_v1", "validate_final_active_identity_v1",
    "validate_workstation_reproduction_profile_v1",
]
