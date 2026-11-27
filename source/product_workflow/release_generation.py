"""Closed R3 recovery generation, config, and active-release identities."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .manifest_io import (
    canonical_json_object_v1,
    detached_sha256_bytes,
    exclusive_create_set_v1,
    sha256_bytes,
)


GENERATION = "r3_recovery_v1"
CONFIG_SCHEMA = "latticegpt_paths_config_v2"
ACTIVE_BINDING_SCHEMA = "latticegpt_active_release_binding_v1"
GENERATION_REGISTRY_RELATIVE = "config/r3_release_generation_registry_v1.json"
GENERATION_REGISTRY_PATH = Path(__file__).resolve().parents[1] / GENERATION_REGISTRY_RELATIVE
REGISTRY_SCHEMA = "latticegpt_r3_release_generation_registry_v1"
REGISTRY_ENTRY_KEYS = (
    "generation", "config_name", "source_release_prefix", "metadata_prefix",
    "generation_contract_schema_version", "generation_contract_relative",
    "active_binding_schema_version", "active_binding_relative", "active_status",
    "source_outputs", "artifact_outputs", "environment_outputs",
    "active_binding_outputs",
)


def _registry_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a canonical relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any("*" in part or "?" in part for part in path.parts)
    ):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


def _registry_output_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result = tuple(_registry_relative(item, label) for item in value)
    if result != tuple(sorted(result, key=lambda item: item.encode("utf-8"))) or len(result) != len(set(result)):
        raise ValueError(f"{label} must be UTF-8 sorted and unique")
    return result


def load_generation_registry_v1(
    path: str | Path = GENERATION_REGISTRY_PATH,
) -> dict[str, Any]:
    selected = Path(path)
    raw = selected.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid release generation registry JSON") from exc
    if raw.startswith(b"\xef\xbb\xbf") or not raw.endswith(b"\n") or b"\r" in raw:
        raise ValueError("release generation registry must be UTF-8/no-BOM/LF")
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "entries"}:
        raise ValueError("release generation registry fields are invalid")
    if payload["schema_version"] != REGISTRY_SCHEMA or not isinstance(payload["entries"], list):
        raise ValueError("release generation registry identity is invalid")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(payload["entries"]):
        if not isinstance(item, Mapping) or set(item) != set(REGISTRY_ENTRY_KEYS):
            raise ValueError(f"release generation registry entry {index} fields are invalid")
        entry = copy.deepcopy(dict(item))
        if not isinstance(entry["generation"], str) or not re.fullmatch(r"r3_[a-z0-9_]+_v1", entry["generation"]):
            raise ValueError("release generation name is invalid")
        _registry_relative(entry["config_name"], "config_name")
        _registry_relative(entry["source_release_prefix"], "source_release_prefix")
        _registry_relative(entry["metadata_prefix"], "metadata_prefix")
        _registry_relative(entry["generation_contract_relative"], "generation_contract_relative")
        _registry_relative(entry["active_binding_relative"], "active_binding_relative")
        if entry["generation_contract_schema_version"] not in {
            "latticegpt_release_generation_contract_v1",
            "latticegpt_release_generation_contract_v2",
            "latticegpt_release_generation_contract_v3",
        }:
            raise ValueError("unknown generation contract schema")
        if entry["active_binding_schema_version"] != ACTIVE_BINDING_SCHEMA:
            raise ValueError("unknown active binding schema")
        if not isinstance(entry["active_status"], str) or not entry["active_status"].startswith("PASS_ACTIVE_R3_"):
            raise ValueError("unknown active generation status")
        for key in ("source_outputs", "artifact_outputs", "environment_outputs", "active_binding_outputs"):
            entry[key] = list(_registry_output_array(entry[key], key))
        if len(entry["source_outputs"]) != 6 or any(len(entry[key]) != 2 for key in ("artifact_outputs", "environment_outputs", "active_binding_outputs")):
            raise ValueError("release generation output membership is invalid")
        entries.append(entry)
    names = [entry["generation"] for entry in entries]
    if names != sorted(names, key=lambda item: item.encode("utf-8")) or len(names) != len(set(names)):
        raise ValueError("release generation entries must be UTF-8 sorted and unique")
    result = {"entries": entries, "schema_version": REGISTRY_SCHEMA}
    if canonical_json_object_v1(result) != raw:
        raise ValueError("release generation registry bytes are not canonical")
    return result


def generation_registry_sha256_v1(
    path: str | Path = GENERATION_REGISTRY_PATH,
) -> str:
    selected = Path(path)
    load_generation_registry_v1(selected)
    return hashlib.sha256(selected.read_bytes()).hexdigest().upper()


def generation_registry_entry_sha256_v1(entry: Mapping[str, Any]) -> str:
    """Return the canonical identity of one closed generation registry entry."""
    if not isinstance(entry, Mapping) or set(entry) != set(REGISTRY_ENTRY_KEYS):
        raise ValueError("generation registry entry fields are invalid")
    generation = entry.get("generation")
    current = _build_generation_spec(str(generation))
    closed = _registry_entry_for_generation(str(generation))
    del current
    if dict(entry) != closed:
        raise ValueError("generation registry entry literal mismatch")
    return hashlib.sha256(canonical_json_object_v1(closed)).hexdigest().upper()


def _registry_entry_for_generation(generation: str) -> dict[str, Any]:
    registry = load_generation_registry_v1()
    matches = [entry for entry in registry["entries"] if entry["generation"] == generation]
    if len(matches) != 1:
        raise ValueError("unsupported R3 release generation")
    return copy.deepcopy(matches[0])


def _build_generation_spec(generation: str) -> dict[str, Any]:
    entry = _registry_entry_for_generation(generation)
    return {
        "generation": entry["generation"],
        "config_name": entry["config_name"],
        "source_release_prefix": entry["source_release_prefix"],
        "metadata_prefix": entry["metadata_prefix"],
        "generation_contract_schema_version": entry["generation_contract_schema_version"],
        "contract_relative": entry["generation_contract_relative"],
        "active_binding_schema_version": entry["active_binding_schema_version"],
        "active_relative": entry["active_binding_relative"],
        "source_outputs": tuple(entry["source_outputs"]),
        "artifact_outputs": tuple(entry["artifact_outputs"]),
        "environment_outputs": tuple(entry["environment_outputs"]),
        "active_binding_outputs": tuple(entry["active_binding_outputs"]),
        "active_status": entry["active_status"],
    }


_DEFAULT_SPEC = _build_generation_spec(GENERATION)
CONFIG_NAME = _DEFAULT_SPEC["config_name"]
CONTRACT_RELATIVE = _DEFAULT_SPEC["contract_relative"]
ACTIVE_RELATIVE = _DEFAULT_SPEC["active_relative"]
SOURCE_OUTPUTS = _DEFAULT_SPEC["source_outputs"]
ARTIFACT_OUTPUTS = _DEFAULT_SPEC["artifact_outputs"]
ENVIRONMENT_OUTPUTS = _DEFAULT_SPEC["environment_outputs"]
ACTIVE_BINDING_OUTPUTS = _DEFAULT_SPEC["active_binding_outputs"]


def generation_spec_v1(generation: str = GENERATION) -> dict[str, Any]:
    """Return one closed, immutable-by-copy generation specification."""

    return copy.deepcopy(_build_generation_spec(generation))

_SHA = re.compile(r"^[0-9A-F]{64}$")
_DETACHED = re.compile(r"^([0-9A-F]{64})  ([A-Za-z0-9_.-]+)\n$")


def _closed(value: Mapping[str, Any], keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} fields are invalid")
    return dict(value)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256_upper")
    return value


def _validate_relative(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any("*" in part or "?" in part for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


def _validate_output_array(value: Any, expected: tuple[str, ...], label: str) -> list[str]:
    if not isinstance(value, list) or value != list(expected):
        raise ValueError(f"{label} does not match the frozen literal registry")
    for item in value:
        _validate_relative(item, label=label)
    if value != sorted(value, key=lambda item: item.encode("utf-8")) or len(value) != len(set(value)):
        raise ValueError(f"{label} must be UTF-8 sorted and unique")
    return list(value)


def build_generation_contract_v1(*, generation: str = GENERATION) -> dict[str, Any]:
    spec = _build_generation_spec(generation)
    value: dict[str, Any] = {
        "schema_version": spec["generation_contract_schema_version"],
        "generation": generation,
        "source_release_prefix": spec["source_release_prefix"],
        "metadata_prefix": spec["metadata_prefix"],
        "artifact_root": "artifacts_v1",
        "environment_control_root": "environment",
        "source_outputs": list(spec["source_outputs"]),
        "artifact_outputs": list(spec["artifact_outputs"]),
        "environment_outputs": list(spec["environment_outputs"]),
        "active_binding_outputs": list(spec["active_binding_outputs"]),
        "historical_identity_policy": "PRESERVE_R2_7_ORIGINAL_R3_ORIGINAL_METADATA_AND_CONTROLLED_BYTES",
        "generation_contract_content_sha256": "0" * 64,
    }
    if spec["generation_contract_schema_version"] == "latticegpt_release_generation_contract_v2":
        value["generation_registry_path"] = GENERATION_REGISTRY_RELATIVE
        value["generation_registry_sha256"] = generation_registry_sha256_v1()
    elif spec["generation_contract_schema_version"] == "latticegpt_release_generation_contract_v3":
        entry = _registry_entry_for_generation(generation)
        value["generation_registry_path"] = GENERATION_REGISTRY_RELATIVE
        value["generation_registry_snapshot_sha256"] = generation_registry_sha256_v1()
        value["generation_registry_entry"] = entry
        value["generation_registry_entry_sha256"] = hashlib.sha256(
            canonical_json_object_v1(entry)
        ).hexdigest().upper()
    value["generation_contract_content_sha256"] = hashlib.sha256(
        canonical_json_object_v1(
            value, self_hash_pointer="/generation_contract_content_sha256"
        )
    ).hexdigest().upper()
    return validate_generation_contract_v1(value, require_current_registry_snapshot=True)


def validate_generation_contract_v1(
    value: Mapping[str, Any], *, require_current_registry_snapshot: bool = False
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("generation"), str):
        raise ValueError("ReleaseGenerationContract must be an object with generation")
    spec = _build_generation_spec(value["generation"])
    keys = (
        "schema_version", "generation", "source_release_prefix", "metadata_prefix",
        "artifact_root", "environment_control_root", "source_outputs",
        "artifact_outputs", "environment_outputs", "active_binding_outputs",
        "historical_identity_policy", "generation_contract_content_sha256",
    )
    schema_version = value.get("schema_version")
    if schema_version == "latticegpt_release_generation_contract_v2":
        keys = keys + ("generation_registry_path", "generation_registry_sha256")
    elif schema_version == "latticegpt_release_generation_contract_v3":
        keys = keys + (
            "generation_registry_path", "generation_registry_snapshot_sha256",
            "generation_registry_entry", "generation_registry_entry_sha256",
        )
    result = _closed(value, keys, "ReleaseGenerationContract")
    if result["schema_version"] != spec["generation_contract_schema_version"]:
        raise ValueError("invalid release generation contract identity")
    literals = {
        "source_release_prefix": spec["source_release_prefix"],
        "metadata_prefix": spec["metadata_prefix"],
        "artifact_root": "artifacts_v1",
        "environment_control_root": "environment",
        "historical_identity_policy": "PRESERVE_R2_7_ORIGINAL_R3_ORIGINAL_METADATA_AND_CONTROLLED_BYTES",
    }
    if any(result[key] != expected for key, expected in literals.items()):
        raise ValueError("release generation contract literal mismatch")
    if result["schema_version"] == "latticegpt_release_generation_contract_v2":
        if result["generation_registry_path"] != GENERATION_REGISTRY_RELATIVE:
            raise ValueError("generation registry path mismatch")
        _sha(result["generation_registry_sha256"], "generation registry SHA")
    elif result["schema_version"] == "latticegpt_release_generation_contract_v3":
        if result["generation_registry_path"] != GENERATION_REGISTRY_RELATIVE:
            raise ValueError("generation registry path mismatch")
        snapshot_sha = _sha(
            result["generation_registry_snapshot_sha256"],
            "generation registry snapshot SHA",
        )
        current_entry = _registry_entry_for_generation(result["generation"])
        embedded_entry = result["generation_registry_entry"]
        if not isinstance(embedded_entry, Mapping) or set(embedded_entry) != set(REGISTRY_ENTRY_KEYS):
            raise ValueError("generation registry entry fields are invalid")
        if dict(embedded_entry) != current_entry:
            raise ValueError("generation registry entry literal mismatch")
        expected_entry_sha = hashlib.sha256(
            canonical_json_object_v1(dict(embedded_entry))
        ).hexdigest().upper()
        if _sha(result["generation_registry_entry_sha256"], "generation registry entry SHA") != expected_entry_sha:
            raise ValueError("generation registry entry SHA mismatch")
        if require_current_registry_snapshot and snapshot_sha != generation_registry_sha256_v1():
            raise ValueError("generation registry snapshot SHA mismatch")
    _validate_output_array(result["source_outputs"], spec["source_outputs"], "source_outputs")
    _validate_output_array(result["artifact_outputs"], spec["artifact_outputs"], "artifact_outputs")
    _validate_output_array(result["environment_outputs"], spec["environment_outputs"], "environment_outputs")
    _validate_output_array(result["active_binding_outputs"], spec["active_binding_outputs"], "active_binding_outputs")
    expected = hashlib.sha256(
        canonical_json_object_v1(
            result, self_hash_pointer="/generation_contract_content_sha256"
        )
    ).hexdigest().upper()
    if _sha(result["generation_contract_content_sha256"], "generation contract content SHA") != expected:
        raise ValueError("generation contract content SHA mismatch")
    return copy.deepcopy(result)


def build_config_v2(*, generation_contract_sha256: str, generation: str = GENERATION) -> dict[str, Any]:
    spec = _build_generation_spec(generation)
    return {
        "schema_version": CONFIG_SCHEMA,
        "active_generation": generation,
        "source_release_prefix": spec["source_release_prefix"],
        "metadata_prefix": spec["metadata_prefix"],
        "artifact_root": "artifacts_v1",
        "environment_control_root": "environment",
        "runtime_root": "runtime",
        "generation_contract": spec["contract_relative"],
        "generation_contract_sha256": _sha(generation_contract_sha256, "generation_contract_sha256"),
        "active_release_binding": spec["active_relative"],
        "services": {
            "chainlit_host": "127.0.0.1", "chainlit_port": 8000,
            "viewer_host": "127.0.0.1", "viewer_port": 8001,
            "frontend_host": "127.0.0.1", "frontend_port": 5173,
        },
    }


def validate_config_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _closed(
        value,
        (
            "schema_version", "active_generation", "source_release_prefix",
            "metadata_prefix", "artifact_root", "environment_control_root",
            "runtime_root", "generation_contract", "generation_contract_sha256",
            "active_release_binding", "services",
        ),
        "latticegpt_paths_config_v2",
    )
    spec = _build_generation_spec(result["active_generation"])
    expected = {
        "schema_version": CONFIG_SCHEMA,
        "active_generation": result["active_generation"],
        "source_release_prefix": spec["source_release_prefix"],
        "metadata_prefix": spec["metadata_prefix"],
        "artifact_root": "artifacts_v1",
        "environment_control_root": "environment",
        "runtime_root": "runtime",
        "generation_contract": spec["contract_relative"],
        "active_release_binding": spec["active_relative"],
    }
    if any(result[key] != literal for key, literal in expected.items()):
        raise ValueError("v2 config literal mismatch")
    _sha(result["generation_contract_sha256"], "generation_contract_sha256")
    services = _closed(
        result["services"],
        ("chainlit_host", "chainlit_port", "viewer_host", "viewer_port", "frontend_host", "frontend_port"),
        "services",
    )
    for key in ("generation_contract", "active_release_binding"):
        _validate_relative(result[key], label=key)
    for key in ("artifact_root", "environment_control_root", "runtime_root", "metadata_prefix", "source_release_prefix"):
        _validate_relative(result[key], label=key)
    return copy.deepcopy(result)


def build_configr_outputs_v1(*, project_root: str | Path, release_root: str | Path, generation: str = GENERATION) -> dict[str, bytes]:
    del project_root, release_root
    spec = _build_generation_spec(generation)
    contract = build_generation_contract_v1(generation=generation)
    contract_bytes = canonical_json_object_v1(contract)
    config = validate_config_v2(
        build_config_v2(generation_contract_sha256=sha256_bytes(contract_bytes), generation=generation)
    )
    config_bytes = canonical_json_object_v1(config)
    return {
        "generation_contract": contract_bytes,
        "generation_contract_sha": detached_sha256_bytes(contract_bytes, Path(spec["contract_relative"]).name),
        "config": config_bytes,
        "config_sha": detached_sha256_bytes(config_bytes, spec["config_name"]),
    }


def write_configr_v1(*, project_root: str | Path, release_root: str | Path, generation: str = GENERATION) -> dict[str, Any]:
    spec = _build_generation_spec(generation)
    outputs = build_configr_outputs_v1(
        project_root=project_root, release_root=release_root, generation=generation
    )
    return exclusive_create_set_v1(
        Path(release_root).resolve(),
        {
            spec["contract_relative"]: outputs["generation_contract"],
            spec["contract_relative"] + ".sha256": outputs["generation_contract_sha"],
            spec["config_name"]: outputs["config"],
            spec["config_name"] + ".sha256": outputs["config_sha"],
        },
    )


def build_active_release_binding_v1(
    *, config_sha256: str, generation_contract_sha256: str,
    source_manifest_sha256: str, artifact_manifest_sha256: str,
    environment_manifest_sha256: str, tolerance_schema_sha256: str,
    formal_bundle_binding_sha256: str, generation: str = GENERATION,
) -> dict[str, Any]:
    spec = _build_generation_spec(generation)
    value: dict[str, Any] = {
        "schema_version": ACTIVE_BINDING_SCHEMA,
        "generation": generation,
        "config_sha256": config_sha256,
        "generation_contract_sha256": generation_contract_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "environment_manifest_sha256": environment_manifest_sha256,
        "tolerance_schema_sha256": tolerance_schema_sha256,
        "formal_bundle_binding_sha256": formal_bundle_binding_sha256,
        "source_release_prefix": spec["source_release_prefix"],
        "metadata_prefix": spec["metadata_prefix"],
        "environment_control_root": "environment",
        "status": spec["active_status"],
        "active_release_binding_content_sha256": "0" * 64,
    }
    value["active_release_binding_content_sha256"] = hashlib.sha256(
        canonical_json_object_v1(
            value, self_hash_pointer="/active_release_binding_content_sha256"
        )
    ).hexdigest().upper()
    return validate_active_release_binding_v1(value)


def validate_active_release_binding_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _closed(
        value,
        (
            "schema_version", "generation", "config_sha256",
            "generation_contract_sha256", "source_manifest_sha256",
            "artifact_manifest_sha256", "environment_manifest_sha256",
            "tolerance_schema_sha256", "formal_bundle_binding_sha256",
            "source_release_prefix", "metadata_prefix", "environment_control_root",
            "status", "active_release_binding_content_sha256",
        ),
        "ActiveReleaseBindingV1",
    )
    if result["schema_version"] != ACTIVE_BINDING_SCHEMA:
        raise ValueError("active release binding identity is invalid")
    spec = _build_generation_spec(result["generation"])
    if result["source_release_prefix"] != spec["source_release_prefix"] or result["metadata_prefix"] != spec["metadata_prefix"] or result["environment_control_root"] != "environment" or result["status"] != spec["active_status"]:
        raise ValueError("active release binding literals are invalid")
    for key in (
        "config_sha256", "generation_contract_sha256", "source_manifest_sha256",
        "artifact_manifest_sha256", "environment_manifest_sha256",
        "tolerance_schema_sha256", "formal_bundle_binding_sha256",
    ):
        _sha(result[key], key)
    expected = hashlib.sha256(
        canonical_json_object_v1(
            result, self_hash_pointer="/active_release_binding_content_sha256"
        )
    ).hexdigest().upper()
    if _sha(result["active_release_binding_content_sha256"], "active content SHA") != expected:
        raise ValueError("active release binding content SHA mismatch")
    return copy.deepcopy(result)


def _is_reparse_or_symlink(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _assert_safe_existing_chain(root: Path, target: Path) -> None:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("RECOVERY_TARGET_PATH_ESCAPE") from exc
    probe = target.parent
    while not probe.exists() and probe != root:
        probe = probe.parent
    while True:
        if not probe.is_dir() or _is_reparse_or_symlink(probe):
            raise ValueError("RECOVERY_PARENT_NOT_REGULAR_DIRECTORY")
        if probe == root:
            break
        probe = probe.parent


def assert_recovery_targets_absent_v1(*, project_root: str | Path, release_root: str | Path, generation: str = GENERATION, include_configr: bool = False) -> dict[str, list[str]]:
    project = Path(project_root).resolve(strict=True)
    release = Path(release_root).resolve(strict=True)
    spec = _build_generation_spec(generation)
    registries = {
        "source_outputs": (project, spec["source_outputs"]),
        "artifact_outputs": (release, spec["artifact_outputs"]),
        "environment_outputs": (release, spec["environment_outputs"]),
        "active_binding_outputs": (release, spec["active_binding_outputs"]),
    }
    if include_configr:
        registries["config_outputs"] = (
            release, (spec["config_name"], spec["config_name"] + ".sha256")
        )
        registries["generation_contract_outputs"] = (
            release, (spec["contract_relative"], spec["contract_relative"] + ".sha256")
        )
    result: dict[str, list[str]] = {}
    for label, (root, values) in registries.items():
        result[label] = list(values)
        for relative in values:
            target = root.joinpath(*PurePosixPath(relative).parts)
            _assert_safe_existing_chain(root, target)
            if target.exists() or target.is_symlink():
                raise ValueError(f"RECOVERY_TARGET_NOT_ABSENT: {relative}")
    return result


def load_verified_canonical_object(path: str | Path, detached_path: str | Path, *, validator) -> tuple[dict[str, Any], str]:
    target = Path(path)
    raw = target.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("identity JSON must not contain BOM")
    pairs_seen: list[str] = []
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        pairs_seen.append("object")
        return value
    payload = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique)
    validated = validator(payload)
    if canonical_json_object_v1(validated) != raw:
        raise ValueError("identity JSON is not canonical_json_object_v1")
    detached = Path(detached_path).read_text(encoding="ascii", errors="strict")
    match = _DETACHED.fullmatch(detached)
    actual = sha256_bytes(raw)
    if match is None or match.group(1) != actual or match.group(2) != target.name:
        raise ValueError("detached SHA mismatch")
    return validated, actual


__all__ = [
    "ACTIVE_BINDING_OUTPUTS", "ARTIFACT_OUTPUTS", "ENVIRONMENT_OUTPUTS",
    "SOURCE_OUTPUTS",
    "assert_recovery_targets_absent_v1",
    "build_active_release_binding_v1", "build_configr_outputs_v1",
    "build_generation_contract_v1", "load_verified_canonical_object",
    "generation_registry_sha256_v1", "generation_spec_v1",
    "load_generation_registry_v1",
    "validate_active_release_binding_v1", "validate_config_v2",
    "validate_generation_contract_v1", "write_configr_v1",
]
