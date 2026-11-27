from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any, Mapping

from .manifest_io import canonical_json_object_v1, sha256_bytes


TARGET_SCHEMA_VERSION = "latticegpt_target_proposal_v1"
MODEL_RUNTIME_PROFILE_SCHEMA_VERSION = "latticegpt_model_runtime_profile_v1"
FROZEN_REPLAY_PROVIDER = "replay"
FROZEN_REPLAY_MODEL = "frozen-target"
FROZEN_REPLAY_SELECTION_SOURCE = "gate7_frozen_target"
FROZEN_REPLAY_INTERFACE_VERSION = "gate7-frozen-target-v1"
FROZEN_REPLAY_PROMPT_TEMPLATE_VERSION = "not-used-frozen-target-v1"
FROZEN_REPLAY_START = "FROZEN_QUERY_PLAN_OR_TARGET"
FROZEN_REPLAY_OFFLINE_POLICY = {
    "dotenv_allowed": False,
    "environment_lookup_allowed": False,
    "llm_call_allowed": False,
    "model_catalog_lookup_allowed": False,
    "network_allowed": False,
}
FORMAL_BUNDLE_SCHEMA_VERSION = "latticegpt_formal_bundle_binding_v1"
QUERY_PLANNER_EVIDENCE_SCHEMA_VERSION = "latticegpt_query_planner_evidence_v1"
ADAPTER_RUN_RESULT_SCHEMA_VERSION = "latticegpt_adapter_run_result_v1"
FORMAL_BUNDLE_ARTIFACT_NAMES = (
    "formal_csv",
    "inverse_model",
    "forward_model",
    "scaler_x",
    "scaler_y",
)
STIFFNESS_NAMES = ("C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66")
BASE_MATERIAL_MODULUS_GPA = 110.0
BASE_MATERIAL_NAME = "TC4"
PRODUCT_INP_MAX_SIZE_MB = 50
PRODUCT_INP_MAX_SIZE_BYTES = PRODUCT_INP_MAX_SIZE_MB * 1024 * 1024
PRODUCT_MESH_CONTRACT: dict[str, str] = {
    "units": "mm",
    "frame": "source_native_cartesian_frame",
    "design_domain": "whole_file_implant_domain",
}


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 64 or any(ch not in "0123456789ABCDEF" for ch in normalized):
        raise ValueError(f"{label} must be a 64-character SHA256")
    return normalized


def require_nine(value: Mapping[str, Any], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(STIFFNESS_NAMES):
        raise ValueError(f"{label} must contain exactly the supported 9D fields")
    result: dict[str, float] = {}
    for name in STIFFNESS_NAMES:
        item = value[name]
        if isinstance(item, bool):
            raise ValueError(f"{label}.{name} must be a finite positive number")
        number = float(item)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{label}.{name} must be a finite positive number")
        result[name] = number
    return result


def require_model_runtime_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("ModelRuntimeProfileV1 is required")
    profile = dict(value)
    if profile.get("schema_version") != MODEL_RUNTIME_PROFILE_SCHEMA_VERSION:
        raise ValueError("ModelRuntimeProfileV1 is required")
    expected = require_sha256(profile.pop("profile_sha256", None), "profile_sha256")
    if canonical_sha256(profile) != expected:
        raise ValueError("ModelRuntimeProfileV1 identity mismatch")
    if profile.get("provider") == FROZEN_REPLAY_PROVIDER or profile.get(
        "selection_source"
    ) == FROZEN_REPLAY_SELECTION_SOURCE:
        replay_keys = {
            "schema_version", "provider", "model", "base_url", "provider_detail",
            "selection_source", "session_bound", "interface_version",
            "prompt_template_version", "temperature", "top_p", "provider_seed",
            "provider_seed_supported", "online_text_determinism", "replay_start",
            "reproduction_profile_sha256", "offline_policy",
        }
        if set(profile) != replay_keys:
            raise ValueError("frozen replay ModelRuntimeProfileV1 fields are invalid")
        expected_literals = {
            "schema_version": MODEL_RUNTIME_PROFILE_SCHEMA_VERSION,
            "provider": FROZEN_REPLAY_PROVIDER,
            "model": FROZEN_REPLAY_MODEL,
            "base_url": None,
            "provider_detail": "offline_frozen_target_replay",
            "selection_source": FROZEN_REPLAY_SELECTION_SOURCE,
            "session_bound": False,
            "interface_version": FROZEN_REPLAY_INTERFACE_VERSION,
            "prompt_template_version": FROZEN_REPLAY_PROMPT_TEMPLATE_VERSION,
            "temperature": 0.0,
            "top_p": None,
            "provider_seed": None,
            "provider_seed_supported": False,
            "online_text_determinism": "NOT_CLAIMED",
            "replay_start": FROZEN_REPLAY_START,
            "offline_policy": FROZEN_REPLAY_OFFLINE_POLICY,
        }
        if any(profile[key] != literal for key, literal in expected_literals.items()):
            raise ValueError("frozen replay ModelRuntimeProfileV1 contract mismatch")
        require_sha256(
            profile["reproduction_profile_sha256"],
            "reproduction_profile_sha256",
        )
    profile["profile_sha256"] = expected
    return profile


def build_frozen_replay_model_runtime_profile_v1(
    reproduction_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the pure offline Gate 7 model identity from ReproductionProfileV1."""

    if not isinstance(reproduction_profile, Mapping):
        raise ValueError("ReproductionProfileV1 is required for frozen replay")
    reproduction = deepcopy(dict(reproduction_profile))
    if reproduction.get("schema_version") != "latticegpt_reproduction_profile_v1":
        raise ValueError("ReproductionProfileV1 is required for frozen replay")
    reproduction_sha = require_sha256(
        reproduction.get("profile_sha256"), "reproduction_profile.profile_sha256"
    )
    expected_reproduction_sha = sha256_bytes(
        canonical_json_object_v1(
            reproduction, self_hash_pointer="/profile_sha256"
        )
    )
    if reproduction_sha != expected_reproduction_sha:
        raise ValueError("ReproductionProfileV1 identity mismatch for frozen replay")
    llm = reproduction.get("llm")
    llm_keys = {
        "provider", "model", "interface_version", "prompt_template_version",
        "temperature", "top_p", "provider_seed", "online_text_determinism",
        "replay_start",
    }
    if not isinstance(llm, Mapping) or set(llm) != llm_keys:
        raise ValueError("ReproductionProfileV1 frozen replay LLM fields are invalid")
    expected_llm = {
        "provider": FROZEN_REPLAY_PROVIDER,
        "model": FROZEN_REPLAY_MODEL,
        "interface_version": FROZEN_REPLAY_INTERFACE_VERSION,
        "prompt_template_version": FROZEN_REPLAY_PROMPT_TEMPLATE_VERSION,
        "temperature": 0.0,
        "top_p": None,
        "provider_seed": None,
        "online_text_determinism": "NOT_CLAIMED",
        "replay_start": FROZEN_REPLAY_START,
    }
    if dict(llm) != expected_llm:
        raise ValueError("ReproductionProfileV1 frozen replay identity mismatch")
    profile: dict[str, Any] = {
        "schema_version": MODEL_RUNTIME_PROFILE_SCHEMA_VERSION,
        "provider": FROZEN_REPLAY_PROVIDER,
        "model": FROZEN_REPLAY_MODEL,
        "base_url": None,
        "provider_detail": "offline_frozen_target_replay",
        "selection_source": FROZEN_REPLAY_SELECTION_SOURCE,
        "session_bound": False,
        "interface_version": llm["interface_version"],
        "prompt_template_version": llm["prompt_template_version"],
        "temperature": llm["temperature"],
        "top_p": llm["top_p"],
        "provider_seed": llm["provider_seed"],
        "provider_seed_supported": False,
        "online_text_determinism": llm["online_text_determinism"],
        "replay_start": llm["replay_start"],
        "reproduction_profile_sha256": reproduction_sha,
        "offline_policy": deepcopy(FROZEN_REPLAY_OFFLINE_POLICY),
    }
    profile["profile_sha256"] = canonical_sha256(profile)
    return require_model_runtime_profile(profile)


def formal_bundle_binding_from_verified_artifacts(
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(artifacts, Mapping) or tuple(artifacts) != FORMAL_BUNDLE_ARTIFACT_NAMES:
        raise ValueError("formal bundle must contain the five ordered authoritative artifacts")
    normalized: dict[str, dict[str, Any]] = {}
    for name in FORMAL_BUNDLE_ARTIFACT_NAMES:
        artifact = dict(artifacts[name])
        if artifact.get("status") != "PASS":
            raise ValueError(f"{name} is not verified")
        size_bytes = artifact.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(f"{name} size_bytes is invalid")
        normalized[name] = {
            "path": str(artifact.get("path") or ""),
            "size_bytes": size_bytes,
            "sha256": require_sha256(artifact.get("sha256"), f"{name}.sha256"),
            "status": "PASS",
        }
        if not normalized[name]["path"]:
            raise ValueError(f"{name} path is required")
    binding = {
        "schema_version": FORMAL_BUNDLE_SCHEMA_VERSION,
        "status": "PASS_5_OF_5",
        "artifacts": normalized,
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return binding


def require_formal_bundle_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("FormalBundleBindingV1 is required")
    binding = dict(value)
    if binding.get("schema_version") != FORMAL_BUNDLE_SCHEMA_VERSION:
        raise ValueError("FormalBundleBindingV1 is required")
    expected = require_sha256(binding.pop("binding_sha256", None), "binding_sha256")
    rebuilt = formal_bundle_binding_from_verified_artifacts(binding.get("artifacts") or {})
    if rebuilt["binding_sha256"] != expected or binding.get("status") != "PASS_5_OF_5":
        raise ValueError("FormalBundleBindingV1 identity mismatch")
    return rebuilt


def build_adapter_run_result(
    *,
    status: str,
    producer: str,
    producer_status: str,
    producer_result_sha256: str,
    artifacts: Mapping[str, Mapping[str, Any] | None],
    candidate_id: str,
    selection_manifest_sha256: str,
    mesh_binding_sha256: str,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    normalized_status = str(status).strip().upper()
    if normalized_status not in {"SUCCESS", "FAILED"}:
        raise ValueError("AdapterRunResultV1 status must be SUCCESS or FAILED")
    normalized_producer = str(producer or "").strip()
    normalized_producer_status = str(producer_status or "").strip()
    normalized_candidate_id = str(candidate_id or "").strip()
    if not normalized_producer:
        raise ValueError("AdapterRunResultV1 producer is required")
    if not normalized_producer_status:
        raise ValueError("AdapterRunResultV1 producer_status is required")
    if not normalized_candidate_id:
        raise ValueError("AdapterRunResultV1 candidate_id is required")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "custom_cell",
        "adapter_metadata",
    }:
        raise ValueError("AdapterRunResultV1 artifacts must contain both output keys")
    normalized_artifacts = {
        name: _normalize_adapter_artifact(artifacts[name], name)
        for name in ("custom_cell", "adapter_metadata")
    }
    normalized_errors = [str(item) for item in errors]
    normalized_warnings = [str(item) for item in warnings]
    if normalized_status == "SUCCESS":
        if any(value is None for value in normalized_artifacts.values()):
            raise ValueError("successful AdapterRunResultV1 requires both artifacts")
        if normalized_errors:
            raise ValueError("successful AdapterRunResultV1 cannot contain errors")
    elif not normalized_errors:
        raise ValueError("failed AdapterRunResultV1 requires at least one error")
    value = {
        "schema_version": ADAPTER_RUN_RESULT_SCHEMA_VERSION,
        "status": normalized_status,
        "producer": normalized_producer,
        "producer_status": normalized_producer_status,
        "producer_result_sha256": require_sha256(
            producer_result_sha256,
            "producer_result_sha256",
        ),
        "artifacts": normalized_artifacts,
        "identity": {
            "candidate_id": normalized_candidate_id,
            "selection_manifest_sha256": require_sha256(
                selection_manifest_sha256,
                "selection_manifest_sha256",
            ),
            "mesh_binding_sha256": require_sha256(
                mesh_binding_sha256,
                "mesh_binding_sha256",
            ),
        },
        "errors": normalized_errors,
        "warnings": normalized_warnings,
    }
    value["result_sha256"] = canonical_sha256(value)
    return value


def require_adapter_run_result(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("AdapterRunResultV1 is required")
    required_keys = {
        "schema_version",
        "status",
        "producer",
        "producer_status",
        "producer_result_sha256",
        "artifacts",
        "identity",
        "errors",
        "warnings",
        "result_sha256",
    }
    if set(value) != required_keys or value.get("schema_version") != ADAPTER_RUN_RESULT_SCHEMA_VERSION:
        raise ValueError("AdapterRunResultV1 is required")
    identity = value.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != {
        "candidate_id",
        "selection_manifest_sha256",
        "mesh_binding_sha256",
    }:
        raise ValueError("AdapterRunResultV1 identity is invalid")
    expected = require_sha256(value.get("result_sha256"), "result_sha256")
    rebuilt = build_adapter_run_result(
        status=value.get("status"),
        producer=value.get("producer"),
        producer_status=value.get("producer_status"),
        producer_result_sha256=value.get("producer_result_sha256"),
        artifacts=value.get("artifacts"),
        candidate_id=identity.get("candidate_id"),
        selection_manifest_sha256=identity.get("selection_manifest_sha256"),
        mesh_binding_sha256=identity.get("mesh_binding_sha256"),
        errors=value.get("errors") or [],
        warnings=value.get("warnings") or [],
    )
    if rebuilt["result_sha256"] != expected:
        raise ValueError("AdapterRunResultV1 identity mismatch")
    return rebuilt


def _normalize_adapter_artifact(
    value: Mapping[str, Any] | None,
    label: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"path", "size_bytes", "sha256"}:
        raise ValueError(f"AdapterRunResultV1 {label} artifact is invalid")
    path = str(value.get("path") or "").strip()
    size_bytes = value.get("size_bytes")
    if not path:
        raise ValueError(f"AdapterRunResultV1 {label} path is required")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError(f"AdapterRunResultV1 {label} size_bytes is invalid")
    return {
        "path": path,
        "size_bytes": size_bytes,
        "sha256": require_sha256(value.get("sha256"), f"{label}.sha256"),
    }


def build_query_planner_evidence(
    *,
    request_sha256: str,
    status: str,
    raw_response_sha256: str | None = None,
    raw_response_excerpt: str | None = None,
    parsed_response: Mapping[str, Any] | None = None,
    model_runtime_profile_sha256: str | None = None,
    normalizations: list[str] | None = None,
    failure_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in {"query", "needs_clarification", "invalid_response"}:
        raise ValueError("unsupported query planner evidence status")
    value: dict[str, Any] = {
        "schema_version": QUERY_PLANNER_EVIDENCE_SCHEMA_VERSION,
        "request_sha256": require_sha256(request_sha256, "request_sha256"),
        "model_runtime_profile_sha256": (
            require_sha256(model_runtime_profile_sha256, "model_runtime_profile_sha256")
            if model_runtime_profile_sha256
            else None
        ),
        "raw_response_sha256": (
            require_sha256(raw_response_sha256, "raw_response_sha256")
            if raw_response_sha256
            else None
        ),
        "raw_response_excerpt": (
            str(raw_response_excerpt)[:1000] if raw_response_excerpt is not None else None
        ),
        "parsed_response_sha256": (
            canonical_sha256(parsed_response) if isinstance(parsed_response, Mapping) else None
        ),
        "status": status,
        "normalizations": list(normalizations or []),
        "failure_code": str(failure_code) if failure_code else None,
        "error": str(error)[:1000] if error else None,
    }
    value["evidence_sha256"] = canonical_sha256(value)
    return value


def _proposal(
    *,
    source: Mapping[str, Any],
    normalized_9d: Mapping[str, Any],
    physical_9d_gpa: Mapping[str, Any],
) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "schema_version": TARGET_SCHEMA_VERSION,
        "status": "TARGET_PROPOSED",
        "confirmed": False,
        "source": dict(source),
        "base_material": {
            "name": BASE_MATERIAL_NAME,
            "elastic_modulus_gpa": BASE_MATERIAL_MODULUS_GPA,
        },
        "normalized_9d": require_nine(normalized_9d, "normalized_9d"),
        "physical_9d_gpa": require_nine(physical_9d_gpa, "physical_9d_gpa"),
        "range_status": "UNCHECKED",
        "non_claims": [
            "NOT_MECHANICAL_PASS",
            "NOT_OOD_PASS",
            "NOT_FEA_VALIDATION",
        ],
    }
    proposal["proposal_sha256"] = canonical_sha256(proposal)
    return proposal


def target_from_database_record(
    record: Mapping[str, Any],
    *,
    source_sha256: str,
    query_identity: str,
) -> dict[str, Any]:
    normalized = require_nine({name: record.get(name) for name in STIFFNESS_NAMES}, "database_9d")
    physical = {name: normalized[name] * BASE_MATERIAL_MODULUS_GPA for name in STIFFNESS_NAMES}
    return _proposal(
        source={
            "kind": "formal_150w_database",
            "sha256": require_sha256(source_sha256, "source_sha256"),
            "query_identity": str(query_identity),
            "sample_id": record.get("sample_id"),
            "units": "dimensionless_C_over_Eb",
        },
        normalized_9d=normalized,
        physical_9d_gpa=physical,
    )


def target_from_physical_9d(
    values: Mapping[str, Any],
    *,
    source_kind: str,
    source_sha256: str,
    source_name: str,
    source_record_index: int | None = None,
    source_record_count: int | None = None,
) -> dict[str, Any]:
    physical = require_nine(values, "physical_9d_gpa")
    normalized = {name: physical[name] / BASE_MATERIAL_MODULUS_GPA for name in STIFFNESS_NAMES}
    source = {
        "kind": str(source_kind),
        "sha256": require_sha256(source_sha256, "source_sha256"),
        "name": str(source_name),
        "units": "GPa",
    }
    if source_record_index is not None or source_record_count is not None:
        if (type(source_record_index) is not int or type(source_record_count) is not int
                or not 1 <= source_record_index <= source_record_count):
            raise ValueError("source record index/count must identify an existing CSV data record")
        source.update(record_index=source_record_index, record_count=source_record_count)
    return _proposal(
        source=source,
        normalized_9d=normalized,
        physical_9d_gpa=physical,
    )


_RESERVATION_KEYS = (
    "schema_version", "reservation_id", "job_id", "upload_id", "candidate_id",
    "attempt_id", "package_id", "mesh_binding_sha256", "status", "reserved_at",
    "consumed_at", "cancelled_at", "terminal_at", "error_code", "reservation_sha256",
)


def _reservation_hash(value: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_object_v1(
            dict(value),
            self_hash_pointer="/reservation_sha256",
        )
    )


def _timestamp(value: Any, label: str) -> str:
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an explicit offset")
    return value


def validate_conformal_reservation_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(_RESERVATION_KEYS):
        raise ValueError("conformal reservation must use the exact closed schema")
    result = dict(value)
    if result["schema_version"] != "latticegpt_conformal_reservation_v1":
        raise ValueError("invalid reservation schema_version")
    for key in ("job_id", "upload_id", "candidate_id"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"{key} must be nonempty")
    expected_reservation = f"{result['job_id']}-conformal-reservation-0001"
    expected_attempt = f"{result['job_id']}-attempt-0001"
    if result["reservation_id"] != expected_reservation:
        raise ValueError("reservation_id was replaced or regenerated")
    if result["attempt_id"] != expected_attempt or result["package_id"] != expected_attempt:
        raise ValueError("attempt/package identity was replaced or regenerated")
    _timestamp(result["reserved_at"], "reserved_at")
    status = result["status"]
    allowed = {
        "RESERVED_UNBOUND", "BOUND_AWAITING_AUTHORIZATION", "CANCELLED_BEFORE_START",
        "CONSUMED_RUNNING", "SUCCEEDED", "FAILED", "HOLD_INTERRUPTED",
    }
    if status not in allowed:
        raise ValueError("invalid reservation status")
    for key in ("consumed_at", "cancelled_at", "terminal_at"):
        if result[key] is not None:
            _timestamp(result[key], key)
    if status == "RESERVED_UNBOUND":
        if any(result[key] is not None for key in ("mesh_binding_sha256", "consumed_at", "cancelled_at", "terminal_at", "error_code")):
            raise ValueError("RESERVED_UNBOUND nullability mismatch")
    else:
        if not isinstance(result["mesh_binding_sha256"], str) or not re.fullmatch(r"[0-9A-F]{64}", result["mesh_binding_sha256"]):
            raise ValueError("bound reservation requires mesh_binding_sha256")
    if status == "BOUND_AWAITING_AUTHORIZATION" and any(result[key] is not None for key in ("consumed_at", "cancelled_at", "terminal_at", "error_code")):
        raise ValueError("BOUND_AWAITING_AUTHORIZATION nullability mismatch")
    if status == "CONSUMED_RUNNING" and (result["consumed_at"] is None or any(result[key] is not None for key in ("cancelled_at", "terminal_at", "error_code"))):
        raise ValueError("CONSUMED_RUNNING nullability mismatch")
    if status == "SUCCEEDED" and (result["consumed_at"] is None or result["terminal_at"] is None or result["error_code"] is not None):
        raise ValueError("SUCCEEDED nullability mismatch")
    if status in {"FAILED", "HOLD_INTERRUPTED"} and (result["consumed_at"] is None or result["terminal_at"] is None or not isinstance(result["error_code"], str)):
        raise ValueError("failed reservation terminal fields are incomplete")
    if not isinstance(result["reservation_sha256"], str) or result["reservation_sha256"] != _reservation_hash(result):
        raise ValueError("reservation_sha256 identity mismatch")
    return deepcopy(result)


def build_conformal_reservation_v1(
    *,
    job_id: str,
    upload_id: str,
    candidate_id: str,
    reserved_at: str,
) -> dict[str, Any]:
    value = {
        "schema_version": "latticegpt_conformal_reservation_v1",
        "reservation_id": f"{job_id}-conformal-reservation-0001",
        "job_id": job_id,
        "upload_id": upload_id,
        "candidate_id": candidate_id,
        "attempt_id": f"{job_id}-attempt-0001",
        "package_id": f"{job_id}-attempt-0001",
        "mesh_binding_sha256": None,
        "status": "RESERVED_UNBOUND",
        "reserved_at": reserved_at,
        "consumed_at": None,
        "cancelled_at": None,
        "terminal_at": None,
        "error_code": None,
        "reservation_sha256": "0" * 64,
    }
    value["reservation_sha256"] = _reservation_hash(value)
    return validate_conformal_reservation_v1(value)


def _reservation_transition(value: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    result = validate_conformal_reservation_v1(value)
    result.update(changes)
    result["reservation_sha256"] = _reservation_hash(result)
    return validate_conformal_reservation_v1(result)


def bind_conformal_reservation_v1(
    value: Mapping[str, Any],
    *,
    mesh_binding_sha256: str,
) -> dict[str, Any]:
    current = validate_conformal_reservation_v1(value)
    if current["status"] != "RESERVED_UNBOUND":
        raise ValueError("reservation may be bound exactly once")
    if not re.fullmatch(r"[0-9A-F]{64}", mesh_binding_sha256):
        raise ValueError("mesh_binding_sha256 must be sha256_upper")
    return _reservation_transition(
        current,
        mesh_binding_sha256=mesh_binding_sha256,
        status="BOUND_AWAITING_AUTHORIZATION",
    )


def consume_conformal_reservation_v1(
    value: Mapping[str, Any],
    *,
    consumed_at: str,
) -> dict[str, Any]:
    current = validate_conformal_reservation_v1(value)
    if current["status"] != "BOUND_AWAITING_AUTHORIZATION":
        raise ValueError("only a bound reservation may be consumed")
    return _reservation_transition(current, status="CONSUMED_RUNNING", consumed_at=consumed_at)


def terminal_conformal_reservation_v1(
    value: Mapping[str, Any],
    *,
    status: str,
    terminal_at: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    current = validate_conformal_reservation_v1(value)
    if current["status"] != "CONSUMED_RUNNING" or status not in {"SUCCEEDED", "FAILED", "HOLD_INTERRUPTED"}:
        raise ValueError("invalid reservation terminal transition")
    if status == "SUCCEEDED" and error_code is not None:
        raise ValueError("SUCCEEDED reservation cannot contain an error")
    if status != "SUCCEEDED" and not error_code:
        raise ValueError("failed reservation requires error_code")
    return _reservation_transition(current, status=status, terminal_at=terminal_at, error_code=error_code)


CANDIDATE_VERSIONS = {
    'latticegpt_candidate_set_v1': ('latticegpt_candidate_selection_v1', 'product_candidate_selection_v1', 'default_rank1'),
    'latticegpt_candidate_set_v2': ('latticegpt_candidate_selection_v2', 'product_candidate_selection_v2', 'recommended_candidate'),
}


def candidate_contract(candidate_set):
    return CANDIDATE_VERSIONS[candidate_set['schema_version']]


def candidate_ranking_view(candidate_set):
    """Describe the saved ranking, never rerank a historical Job on read."""
    nmse = candidate_set.get('schema_version') == 'latticegpt_candidate_set_v2'
    return {'metric': 'nmse' if nmse else 'score', 'label': 'NMSE' if nmse else 'P-10',
            'contract': candidate_set.get('ranking_contract') if nmse else candidate_set.get('p10_contract'),
            'lower_is_better': True, 'tie_break': 'source_order' if nmse else 'legacy_p10',
            'geometry_contract': candidate_set.get('geometry_contract')}


def validate_nmse_candidate_state(job):
    """Validate a saved v2 contract without migrating or rewriting any Job."""
    collection = job.get('candidate_set') or {}
    if collection.get('schema_version') != 'latticegpt_candidate_set_v2':
        return
    if (collection.get('ranking_contract') != 'truss_owner_nmse_v1'
            or collection.get('geometry_contract') != 'truss_20261110_unit_cell_v1'
            or 'p10_contract' in collection
            or collection.get('target_sha256') != job['target']['proposal_sha256']
            or collection.get('candidate_set_sha256') != canonical_sha256({k:v for k,v in collection.items() if k!='candidate_set_sha256'})):
        raise ValueError('NMSE candidate set contract or identity is invalid')
    candidates = collection.get('candidates')
    if not isinstance(candidates,list) or len(candidates)!=3:
        raise ValueError('NMSE candidate set requires three candidates')
    ids = [c.get('candidate_id') for c in candidates]
    if any(not isinstance(identity,str) or not identity for identity in ids) or len(set(ids))!=3:
        raise ValueError('NMSE candidate identities are invalid')
    for order,candidate in enumerate(candidates,1):
        if (type(candidate.get('source_order')) is not int or candidate['source_order']!=order
                or type(candidate.get('nmse')) not in (int,float) or not math.isfinite(candidate['nmse']) or candidate['nmse']<0
                or type(candidate.get('eligible')) is not bool or 'score' in candidate):
            raise ValueError('NMSE candidate metric, source order or eligibility is invalid')
    ordered = sorted(candidates,key=lambda c:(c['nmse'],c['source_order']))
    if any(type(c.get('rank')) is not int or c['rank']!=rank for rank,c in enumerate(ordered,1)):
        raise ValueError('NMSE candidate ranks do not match the saved metric and source order')
    recommended = next((c['candidate_id'] for c in ordered if c['eligible']), None)
    if collection.get('recommended_candidate_id') != recommended or collection.get('status') != ('RANKED' if recommended else 'HOLD_NO_ELIGIBLE_CANDIDATE'):
        raise ValueError('NMSE candidate recommendation is invalid')
    selection = job.get('selection')
    if recommended is None:
        if selection is not None: raise ValueError('No eligible candidate may have a selection')
        return
    if not isinstance(selection,Mapping) or selection.get('schema_version')!=candidate_contract(collection)[0]:
        raise ValueError('NMSE candidate selection version is invalid')
    selected = next((c for c in candidates if c['candidate_id']==selection.get('selected_candidate_id')),None)
    if (selected is None or not selected['eligible'] or selection.get('selected_rank')!=selected['rank']
            or selection.get('recommended_candidate_id')!=recommended
            or selection.get('selection_source')!=('recommended_candidate' if selected['candidate_id']==recommended else 'user_override')
            or selection.get('candidate_set_sha256')!=collection['candidate_set_sha256']
            or selection.get('selected_candidate_payload_sha256')!=canonical_sha256(selected)
            or selection.get('selection_sha256')!=canonical_sha256({k:v for k,v in selection.items() if k!='selection_sha256'})):
        raise ValueError('NMSE candidate selection identity is invalid')
