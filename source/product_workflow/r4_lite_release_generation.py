"""Independent R4-Lite generation, Config v2, and active binding contracts."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .manifest_io import canonical_json_object_v1, sha256_bytes


GENERATION = "r4_lite_single_reference_workstation_v50"
INVERSE_CANDIDATE_GENERATION = "r4_lite_single_reference_workstation_v51"
INVERSE_CLOSEOUT_GENERATION = "r4_lite_single_reference_workstation_v52"
CONTROLLED_REPLY_GENERATION = "r4_lite_single_reference_workstation_v53"
# Selection stays in each source tree's registry. Merely supporting a candidate
# must not select it in the existing workstation tree.
_PROFILES = {
    "r4_lite_single_reference_workstation_v62": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v63.json", 108, 275, (8000, 8001, 5173)),
    "r4_lite_single_reference_workstation_v61": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v62.json", 108, 275, (8000, 8001, 5173)),
    "r4_lite_single_reference_workstation_v57": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v58.json", 107, 274, (18280, 18281, 18283)),
    "r4_lite_single_reference_workstation_v58": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v59.json", 107, 274, (8000, 8001, 5173)),
    "r4_lite_single_reference_workstation_v56": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v57.json", 85, 252, (8000, 8001, 5173)),
    "r4_lite_single_reference_workstation_v55": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v56.json", 83, 250, (8000, 8001, 5173)),
    "r4_lite_single_reference_workstation_v54": ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v55.json", 83, 250, (18000, 18001, 15173)),
    GENERATION: ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v51.json", 76, 243, (8000, 8001, 5173)),
    INVERSE_CANDIDATE_GENERATION: ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v52.json", 81, 248, (18000, 18001, 15173)),
    INVERSE_CLOSEOUT_GENERATION: ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v53.json", 81, 248, (18000, 18001, 15173)),
    CONTROLLED_REPLY_GENERATION: ("latticegpt_v1.paths.r4_lite_single_reference_workstation_v54.json", 81, 248, (18000, 18001, 15173)),
}
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config/r4_lite_release_generation_registry_v1.json"
REGISTRY_SCHEMA = "latticegpt.r4_lite_release_generation_registry.v1"
CONTRACT_SCHEMA = "latticegpt.r4_lite_release_generation_contract.v1"
ACTIVE_BINDING_SCHEMA = "latticegpt.r4_lite_active_release_binding.v1"
CONFIG_SCHEMA = "latticegpt_paths_config_v2"
_SHA = re.compile(r"^[0-9A-F]{64}$")


def _closed(value: Mapping[str, Any], keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} must contain the exact closed key set")
    return deepcopy(dict(value))


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} must be uppercase SHA256")
    return value


def _relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


_REGISTRY_KEYS = (
    "schema_version", "generation", "config_name", "source_release_prefix",
    "metadata_prefix", "generation_contract_schema_version",
    "generation_contract_relative", "active_binding_schema_version",
    "active_binding_relative", "active_status", "final_active_identity_relative",
    "r3_generation", "r3_registry_snapshot_sha256", "r3_source_manifest_relative",
    "r3_source_manifest_sha256", "r3_source_count", "r4_delta_count", "source_count",
)


def load_generation_registry_v1(path: str | Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(Path(path if path is not None else REGISTRY_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("unable to load R4-Lite generation registry") from exc
    registry = _closed(value, _REGISTRY_KEYS, "R4-Lite generation registry")
    if registry["schema_version"] != REGISTRY_SCHEMA or registry["generation"] not in _PROFILES:
        raise ValueError("R4-Lite generation registry literal mismatch")
    selected = registry["generation"]
    config_name, additions, count, _ = _PROFILES[selected]
    if registry["generation_contract_schema_version"] != CONTRACT_SCHEMA or registry["active_binding_schema_version"] != ACTIVE_BINDING_SCHEMA:
        raise ValueError("R4-Lite registry schema binding mismatch")
    for key in ("config_name", "source_release_prefix", "metadata_prefix", "generation_contract_relative", "active_binding_relative", "final_active_identity_relative", "r3_source_manifest_relative"):
        _relative(registry[key], key)
    metadata_prefix = f"release_metadata/{selected}"
    identity_paths = {
        "config_name": config_name,
        "source_release_prefix": f"release/v1/{selected}",
        "metadata_prefix": metadata_prefix,
        "generation_contract_relative": f"{metadata_prefix}/release_generation_contract_v1.json",
        "active_binding_relative": f"{metadata_prefix}/active_release_binding_v1.json",
        "final_active_identity_relative": f"{metadata_prefix}/final_active_identity.json",
    }
    for key, expected in identity_paths.items():
        if registry[key] != expected:
            raise ValueError(f"R4-Lite registry identity path mismatch: {key}")
    _sha(registry["r3_registry_snapshot_sha256"], "R3 registry snapshot SHA")
    _sha(registry["r3_source_manifest_sha256"], "R3 source manifest SHA")
    if (registry["r3_source_count"], registry["r4_delta_count"], registry["source_count"]) != (167, additions, count):
        raise ValueError("R4-Lite source cardinality mismatch")
    return registry


def generation_registry_sha256_v1(path: str | Path | None = None) -> str:
    return sha256_bytes(canonical_json_object_v1(load_generation_registry_v1(path)))


_CONTRACT_KEYS = (*_REGISTRY_KEYS[1:], "registry_sha256", "generation_contract_content_sha256", "schema_version")


def build_generation_contract_v1() -> dict[str, Any]:
    registry = load_generation_registry_v1()
    contract = {key: deepcopy(value) for key, value in registry.items() if key != "schema_version"}
    contract["registry_sha256"] = generation_registry_sha256_v1()
    contract["generation_contract_content_sha256"] = "0" * 64
    contract["schema_version"] = CONTRACT_SCHEMA
    contract["generation_contract_content_sha256"] = sha256_bytes(
        canonical_json_object_v1(contract, self_hash_pointer="/generation_contract_content_sha256")
    )
    return validate_generation_contract_v1(contract)


def validate_generation_contract_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    contract = _closed(value, _CONTRACT_KEYS, "R4-Lite generation contract")
    if contract["schema_version"] != CONTRACT_SCHEMA:
        raise ValueError("R4-Lite generation contract schema mismatch")
    registry = load_generation_registry_v1()
    for key, expected in registry.items():
        if key != "schema_version" and contract[key] != expected:
            raise ValueError(f"R4-Lite generation contract registry mismatch: {key}")
    if contract["registry_sha256"] != generation_registry_sha256_v1():
        raise ValueError("R4-Lite registry snapshot drift")
    expected = sha256_bytes(canonical_json_object_v1(contract, self_hash_pointer="/generation_contract_content_sha256"))
    if contract["generation_contract_content_sha256"] != expected:
        raise ValueError("R4-Lite generation contract self hash mismatch")
    return contract


_CONFIG_KEYS = (
    "schema_version", "active_generation", "source_release_prefix", "metadata_prefix",
    "artifact_root", "environment_control_root", "runtime_root", "generation_contract",
    "generation_contract_sha256", "active_release_binding", "services",
)


def build_config_v2(*, generation_contract_sha256: str) -> dict[str, Any]:
    r = load_generation_registry_v1()
    chainlit_port, viewer_port, frontend_port = _PROFILES[r["generation"]][3]
    return {
        "schema_version": CONFIG_SCHEMA, "active_generation": r["generation"],
        "source_release_prefix": r["source_release_prefix"], "metadata_prefix": r["metadata_prefix"],
        "artifact_root": "artifacts_v1", "environment_control_root": "environment",
        "runtime_root": "runtime", "generation_contract": r["generation_contract_relative"],
        "generation_contract_sha256": _sha(generation_contract_sha256, "generation contract SHA"),
        "active_release_binding": r["active_binding_relative"],
        "services": {"chainlit_host": "127.0.0.1", "chainlit_port": chainlit_port, "viewer_host": "127.0.0.1", "viewer_port": viewer_port, "frontend_host": "127.0.0.1", "frontend_port": frontend_port},
    }


def validate_config_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    config = _closed(value, _CONFIG_KEYS, "R4-Lite Config v2")
    expected = build_config_v2(generation_contract_sha256=_sha(config["generation_contract_sha256"], "generation contract SHA"))
    if config != expected:
        raise ValueError("R4-Lite Config v2 literal mismatch")
    return config


_ACTIVE_KEYS = (
    "schema_version", "generation", "status", "config_sha256",
    "generation_contract_sha256", "source_manifest_sha256", "artifact_manifest_sha256",
    "environment_manifest_sha256", "formal_bundle_binding_sha256",
    "tolerance_schema_sha256", "final_active_identity_relative",
    "comparison_contract_sha256",
)


def build_active_release_binding_v1(**identities: str) -> dict[str, Any]:
    registry = load_generation_registry_v1()
    required = set(_ACTIVE_KEYS) - {"schema_version", "generation", "status", "final_active_identity_relative"}
    if set(identities) != required:
        raise ValueError("R4-Lite active binding identities are incomplete")
    return validate_active_release_binding_v1({
        "schema_version": ACTIVE_BINDING_SCHEMA, "generation": registry["generation"],
        "status": registry["active_status"],
        **{key: _sha(value, key) for key, value in identities.items()},
        "final_active_identity_relative": registry["final_active_identity_relative"],
    })


def validate_active_release_binding_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    binding = _closed(value, _ACTIVE_KEYS, "R4-Lite active binding")
    registry = load_generation_registry_v1()
    if binding["schema_version"] != ACTIVE_BINDING_SCHEMA or binding["generation"] != registry["generation"] or binding["status"] != registry["active_status"]:
        raise ValueError("R4-Lite active binding literal mismatch")
    if binding["final_active_identity_relative"] != registry["final_active_identity_relative"]:
        raise ValueError("R4-Lite final identity relative path mismatch")
    for key in _ACTIVE_KEYS:
        if key.endswith("_sha256"):
            _sha(binding[key], key)
    return binding


__all__ = [
    "ACTIVE_BINDING_SCHEMA", "CONFIG_SCHEMA", "CONTRACT_SCHEMA", "GENERATION",
    "build_active_release_binding_v1", "build_config_v2", "build_generation_contract_v1",
    "generation_registry_sha256_v1", "load_generation_registry_v1",
    "validate_active_release_binding_v1", "validate_config_v2", "validate_generation_contract_v1",
]
