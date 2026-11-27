"""R4-Lite exclusive-create export, replay, comparison, and archive builders."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import argparse
import json
import platform
import re
from pathlib import Path
import sys
from typing import Any, Mapping

from .job_evidence import verify_job
from .manifest_io import (
    OutputSetHold,
    canonical_json_object_v1,
    detached_sha256_bytes,
    exclusive_create_set_v1,
    sha256_bytes,
    validate_relative_path,
)
from .release_identity import validate_final_active_identity_v1
from .reproducibility import (
    build_reproduction_profile_v1,
    build_deterministic_projection_v1,
    build_layered_comparison_report_v1,
    compare_numeric_inputs_v1,
    validate_numeric_comparison_input_v1,
)


R4_LITE_NON_CLAIMS = (
    "NOT_CROSS_MACHINE_REPRODUCIBILITY",
    "NOT_CROSS_PLATFORM_REPRODUCIBILITY",
    "NOT_NEW_ENVIRONMENT_REPRODUCIBILITY",
    "NOT_GENERAL_DEPLOYMENT",
    "NOT_MULTIUSER_OR_DISTRIBUTED",
    "NOT_CUDA_EQUIVALENCE",
    "NOT_ONLINE_LLM_DETERMINISM",
    "NOT_FORMAL_150W_PROVENANCE",
    "NOT_ABAQUS_VALIDATED",
    "NOT_MANUFACTURING_VALIDATED",
    "NOT_CLINICALLY_VALIDATED",
    "NOT_ENGINEERING_CERTIFIED",
)

R4_LITE_BASELINE_SEQUENCE = (
    "TARGET_PROPOSAL_FORMED",
    "CREATE_R3_V2_JOB_WITH_SYSTEM_GENERATED_COMPLETE_JOB_ID",
    "USER_CONFIRM_TARGET",
    "CANDIDATE",
    "MESH",
    "CASE_AND_RESERVATION",
    "SEPARATE_ATTEMPT_AUTHORIZATION",
    "VERIFY",
    "EXPORT",
)

R4_LITE_RUNTIME_SEED = 20260811


def _valid_id(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,79}", value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _canonical(value: object) -> bytes:
    return canonical_json_object_v1(value)


def build_runtime_reproduction_profile_v1(config, *, preflight=None) -> dict[str, Any]:
    """Build the reviewed normal-product computation profile from verified identity."""

    from .config import LatticeGPTConfig
    from .preflight import require_preflight

    if not isinstance(config, LatticeGPTConfig):
        raise TypeError("config must be LatticeGPTConfig")
    if not config.paths.active_generation.startswith("r4_lite_"):
        raise ValueError("R4-Lite Config v2 is required")
    active_preflight = preflight or require_preflight(config, profile="full_product")
    active = active_preflight.active_release_binding
    if not isinstance(active, Mapping):
        raise ValueError("R4-Lite ActiveReleaseBinding is required")
    if active.get("generation") != config.paths.active_generation:
        raise ValueError("Config and ActiveReleaseBinding generation mismatch")

    import numpy
    import torch

    return build_reproduction_profile_v1(
        random_seed=R4_LITE_RUNTIME_SEED,
        versions={
            "windows_build": platform.version(),
            "architecture": platform.machine(),
            "python_implementation": platform.python_implementation(),
            "python": platform.python_version(),
            "numpy": str(numpy.__version__),
            "torch": str(torch.__version__),
        },
        identities={
            "config_sha256": config.paths.config_sha256,
            "release_generation_contract_sha256": config.paths.generation_contract_sha256_value,
            "active_release_binding_sha256": active_preflight.active_release_binding_sha256,
            "source_manifest_sha256": active_preflight.source_manifest_sha256,
            "artifact_manifest_sha256": active_preflight.artifact_manifest_sha256,
            "environment_manifest_sha256": active_preflight.environment_manifest_sha256,
            "formal_bundle_binding_sha256": active.get("formal_bundle_binding_sha256"),
        },
        llm={
            "provider": "product-session",
            "model": "per-job-model-runtime-profile",
            "interface_version": "chat-interface-v1",
            "prompt_template_version": "query-plan-prompt-v1",
            "temperature": 0.0,
            "top_p": None,
            "provider_seed": None,
            "online_text_determinism": "NOT_CLAIMED",
            "replay_start": "FROZEN_QUERY_PLAN_OR_TARGET",
        },
        tolerance_schema_sha256=active_preflight.reproduction_tolerances_sha256,
    )


def _hold(exc: OutputSetHold) -> dict[str, object]:
    return {
        "status": "HOLD",
        "code": exc.code,
        "output_state": exc.status,
        "writes_performed": exc.writes_performed,
        "created": list(exc.created),
    }


def _sanitize_export_value(value: object, runtime_root: Path) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_export_value(item, runtime_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_export_value(item, runtime_root) for item in value]
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            try:
                relative = resolved.relative_to(runtime_root)
            except ValueError:
                identity = sha256_bytes(value.encode("utf-8"))
                return f"external-path-redacted-sha256://{identity}"
            return f"runtime://{relative.as_posix()}"
    return value


def _export_safe_source_bytes(
    relative_path: str, data: bytes, runtime_root: Path
) -> bytes:
    if relative_path.endswith(".json"):
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("export source JSON must contain an object")
        return _canonical(_sanitize_export_value(value, runtime_root))
    if relative_path.endswith(".jsonl"):
        records = []
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError("export source JSONL record must contain an object")
            records.append(_canonical(_sanitize_export_value(value, runtime_root)))
        return b"".join(records)
    return data


def export_job(
    runtime_root: str | Path,
    job_id: str,
    export_id: str,
    *,
    expected_active_identity: Mapping[str, object],
    final_active_identity_path: str | Path | None = None,
) -> dict[str, object]:
    """Export one verified terminal Job to an exclusive-create evidence set."""
    _valid_id(export_id, "export_id")
    root = Path(runtime_root).resolve(strict=True)
    verification = verify_job(
        root, job_id, expected_active_identity=expected_active_identity
    )
    if verification.get("status") != "PASS":
        return {
            "status": "HOLD",
            "code": "JOB_VERIFICATION_NOT_PASS",
            "verification": verification,
        }

    outputs: OrderedDict[str, bytes] = OrderedDict()
    identity_relative = "evidence/identity/final_active_identity.json"
    identity_sidecar_relative = "evidence/identity/final_active_identity.json.sha256"
    if final_active_identity_path is None:
        identity_bytes = _canonical(dict(expected_active_identity))
        identity_sidecar_bytes = detached_sha256_bytes(identity_bytes, "final_active_identity.json")
    else:
        identity_path = Path(final_active_identity_path).resolve(strict=True)
        identity_sidecar_path = Path(str(identity_path) + ".sha256").resolve(strict=True)
        identity_bytes = identity_path.read_bytes()
        parsed_identity = json.loads(identity_bytes.decode("utf-8"))
        validate_final_active_identity_v1(parsed_identity)
        if _canonical(parsed_identity) != identity_bytes:
            raise ValueError("final active identity must use canonical JSON bytes")
        identity_sidecar_bytes = identity_sidecar_path.read_bytes()
        if identity_sidecar_bytes != detached_sha256_bytes(identity_bytes, "final_active_identity.json"):
            raise ValueError("final active identity detached SHA mismatch")
        if parsed_identity != dict(expected_active_identity):
            raise ValueError("final active identity content mismatch")
    identity_json_sha = sha256_bytes(identity_bytes)
    identity_sidecar_sha = sha256_bytes(identity_sidecar_bytes)
    identity_sidecar_declared_sha = identity_sidecar_bytes.decode("ascii").split()[0]
    identity_binding = {
        "json_relative_path": identity_relative,
        "json_sha256": identity_json_sha,
        "sidecar_relative_path": identity_sidecar_relative,
        "sidecar_file_sha256": identity_sidecar_sha,
        "sidecar_declared_json_sha256": identity_sidecar_declared_sha,
    }
    outputs[identity_relative] = identity_bytes
    outputs[identity_sidecar_relative] = identity_sidecar_bytes
    job_dir = root / "jobs" / job_id
    for item in verification["job_inventory"]:
        relative = item["relative_path"]
        source_bytes = (job_dir / relative).read_bytes()
        outputs[f"job/{relative}"] = _export_safe_source_bytes(
            relative, source_bytes, root
        )
    if verification["terminal_evidence"] == "SUCCESS":
        package_dir = root / "conformal" / job_id / f"{job_id}-attempt-0001"
        for item in verification["package_inventory"]:
            relative = item["relative_path"]
            source_bytes = (package_dir / relative).read_bytes()
            outputs[f"package/{relative}"] = _export_safe_source_bytes(
                relative, source_bytes, root
            )

    evidence_index = {
        "schema_version": "latticegpt.r4_lite_export_evidence_index.v1",
        "job_id": job_id,
        "terminal_evidence": verification["terminal_evidence"],
        "job_inventory": deepcopy(verification["job_inventory"]),
        "package_inventory": deepcopy(verification["package_inventory"]),
        "identity": deepcopy(identity_binding),
    }
    non_claims = {
        "schema_version": "latticegpt.r4_lite_non_claims.v1",
        "non_claims": list(R4_LITE_NON_CLAIMS),
    }
    outputs["README.md"] = (
        "# LatticeGPT R4-Lite Job export\n\n"
        "Verified terminal evidence only. No publication, deployment, manufacturing, clinical, or engineering certification is granted.\n"
    ).encode("utf-8")
    outputs["evidence_index.json"] = _canonical(evidence_index)
    outputs["verify_job.json"] = _canonical(verification)
    outputs["baseline_job_binding.json"] = _canonical({
        "schema_version": "latticegpt.r4_lite_baseline_job_binding.v1",
        "job_id": job_id,
        "job_revision": verification["revision"],
        "identity": deepcopy(identity_binding),
    })
    outputs["non_claims.json"] = _canonical(non_claims)
    payload_inventory = [
        {
            "relative_path": relative,
            "size_bytes": len(data),
            "sha256": sha256_bytes(data),
        }
        for relative, data in outputs.items()
    ]
    manifest = {
        "schema_version": "latticegpt.r4_lite_export_manifest.v1",
        "export_id": export_id,
        "source_job_id": job_id,
        "source_job_revision": verification["revision"],
        "source_job_status": verification["job_status"],
        "terminal_evidence": verification["terminal_evidence"],
        "active_identity": deepcopy(identity_binding),
        "files": payload_inventory,
        "large_artifacts": "NOT_COPIED_PER_JOB",
        "path_policy": "RUNTIME_RELATIVE_OR_EXTERNAL_PATH_SHA256_REDACTION",
        "non_claims": list(R4_LITE_NON_CLAIMS),
    }
    manifest_bytes = _canonical(manifest)
    outputs["export_manifest.json"] = manifest_bytes
    outputs["export_manifest.json.sha256"] = detached_sha256_bytes(
        manifest_bytes, "export_manifest.json"
    )
    target = root / "exports" / export_id
    try:
        created = exclusive_create_set_v1(target, outputs)
    except OutputSetHold as exc:
        return _hold(exc)
    return {
        "status": "PASS",
        "export_id": export_id,
        "target": str(target),
        "created": created,
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }


def create_replay_contract(
    runtime_root: str | Path,
    replay_id: str,
    baseline_verification: Mapping[str, object],
    *,
    python_process_identity: Mapping[str, object],
) -> dict[str, object]:
    """Create the one-run replay contract; this function never executes a Job."""
    _valid_id(replay_id, "replay_id")
    if (
        not isinstance(baseline_verification, Mapping)
        or baseline_verification.get("status") != "PASS"
        or baseline_verification.get("terminal_evidence") != "SUCCESS"
    ):
        raise ValueError("baseline verification must be a successful PASS")
    required_process = {"pid", "start_time_utc", "python_executable_sha256"}
    if not isinstance(python_process_identity, Mapping) or not required_process.issubset(
        python_process_identity
    ):
        raise ValueError("new Python process identity is incomplete")
    digest = sha256_bytes(canonical_json_object_v1(dict(baseline_verification)))
    contract = {
        "schema_version": "latticegpt.r4_lite_replay_contract.v1",
        "replay_id": replay_id,
        "baseline_job_id": baseline_verification.get("job_id"),
        "baseline_verification_sha256": digest,
        "active_identity": deepcopy(baseline_verification.get("active_identity")),
        "python_process_identity": deepcopy(dict(python_process_identity)),
        "new_python_process_required": True,
        "replay_count": 1,
        "compute_checkpoint_resume": False,
        "output_policy": "EXCLUSIVE_CREATE_NO_RETRY",
        "non_claims": [*R4_LITE_NON_CLAIMS, "NOT_COMPUTE_CHECKPOINT_RESUME"],
    }
    contract_bytes = _canonical(contract)
    outputs = OrderedDict(
        (
            ("contract/replay_contract.json", contract_bytes),
            (
                "contract/replay_contract.json.sha256",
                detached_sha256_bytes(contract_bytes, "replay_contract.json"),
            ),
        )
    )
    root = Path(runtime_root).resolve(strict=True)
    target = root / "r4_lite" / "replay" / replay_id
    try:
        created = exclusive_create_set_v1(target, outputs)
    except OutputSetHold as exc:
        return _hold(exc)
    return {
        "status": "PASS",
        "replay_id": replay_id,
        "target": str(target),
        "created": created,
        "contract_sha256": sha256_bytes(contract_bytes),
    }


_STABLE_REPLAY_PACKAGE_PATHS = (
    "input/source_c3d8.inp",
    "core/conformal_b31.inp",
    "visualization/conformal_preview.stl",
    "visualization/conformal_preview.png",
    "geometry_ledger.json",
    "source_element_map.csv",
)


def _inventory_by_path(value: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    inventory = value.get("package_inventory")
    if not isinstance(inventory, list):
        raise ValueError("package inventory is missing")
    result: dict[str, Mapping[str, object]] = {}
    for item in inventory:
        if not isinstance(item, Mapping) or not isinstance(item.get("relative_path"), str):
            raise ValueError("package inventory item is invalid")
        relative = item["relative_path"]
        if relative in result:
            raise ValueError("package inventory path is duplicated")
        result[relative] = item
    return result


def _r3_layers(value: Mapping[str, object], label: str) -> dict[str, object]:
    layers = value.get("r3_layers")
    if not isinstance(layers, Mapping):
        raise ValueError(f"{label} R3 layered evidence is missing")
    required = {
        "schema_version",
        "comparison_contract_sha256",
        "operational_evidence_sha256",
        "projection",
        "numeric",
        "relationship",
        "derivation_bindings",
    }
    if set(layers) != required:
        raise ValueError(f"{label} R3 layered evidence schema is invalid")
    if layers["schema_version"] != "latticegpt.r4_lite_derived_r3_layers.v1":
        raise ValueError(f"{label} R3 layered evidence is not production-derived")
    projection = deepcopy(layers["projection"])
    numeric = deepcopy(layers["numeric"])
    relationship = deepcopy(layers["relationship"])
    bindings = deepcopy(layers["derivation_bindings"])
    if not all(isinstance(item, Mapping) for item in (projection, numeric, relationship, bindings)):
        raise ValueError(f"{label} R3 numeric/relationship evidence is invalid")
    if projection.get("schema_version") != "latticegpt_deterministic_result_projection_v1":
        raise ValueError(f"{label} R3 projection schema is invalid")
    expected_projection_sha = sha256_bytes(
        canonical_json_object_v1(dict(projection), self_hash_pointer="/projection_sha256")
    )
    if projection.get("projection_sha256") != expected_projection_sha:
        raise ValueError(f"{label} R3 projection self hash mismatch")
    numeric = validate_numeric_comparison_input_v1(numeric)
    operational_sha = str(layers["operational_evidence_sha256"])
    if relationship.get("operational_evidence_sha256") != operational_sha:
        raise ValueError(f"{label} R3 relationship evidence is not bound to the run")
    comparison_contract_sha256 = str(layers["comparison_contract_sha256"])
    if (
        projection.get("controlled_identities", {}).get("comparison_contract_sha256")
        != comparison_contract_sha256
    ):
        raise ValueError(f"{label} R3 projection comparison contract is not bound")
    return {
        "comparison_contract_sha256": comparison_contract_sha256,
        "operational_evidence_sha256": operational_sha,
        "projection": projection,
        "numeric": numeric,
        "relationship": relationship,
        "derivation_bindings": bindings,
    }


def compare_replay_jobs(
    baseline_runtime_root: str | Path,
    baseline_job_id: str,
    replay_runtime_root: str | Path,
    replay_job_id: str,
    *,
    expected_active_identity: Mapping[str, object],
) -> dict[str, object]:
    """Production comparison path: derive both inputs from verified on-disk Jobs."""
    from .r4_lite_operational_evidence import derive_verified_r3_layers_v1

    baseline = derive_verified_r3_layers_v1(
        baseline_runtime_root, baseline_job_id, expected_active_identity
    )
    replay = derive_verified_r3_layers_v1(
        replay_runtime_root, replay_job_id, expected_active_identity
    )
    return compare_replay_verifications(baseline, replay)


def compare_replay_verifications(
    baseline_verification: Mapping[str, object],
    replay_verification: Mapping[str, object],
) -> dict[str, object]:
    """Reuse the R3 layered comparison boundary on stable package inventory."""
    for label, value in (
        ("baseline", baseline_verification),
        ("replay", replay_verification),
    ):
        if (
            not isinstance(value, Mapping)
            or value.get("status") != "PASS"
            or value.get("terminal_evidence") != "SUCCESS"
        ):
            raise ValueError(f"{label} verification must be a successful PASS")
    if baseline_verification.get("job_id") == replay_verification.get("job_id"):
        raise ValueError("self-comparison is forbidden")
    active_match = (
        canonical_json_object_v1(dict(baseline_verification.get("active_identity") or {}))
        == canonical_json_object_v1(dict(replay_verification.get("active_identity") or {}))
    )
    left = _inventory_by_path(baseline_verification)
    right = _inventory_by_path(replay_verification)
    if set(left) != set(right):
        raise ValueError("baseline and replay package inventory paths differ")
    file_layers = []
    for relative in _STABLE_REPLAY_PACKAGE_PATHS:
        if relative not in left or relative not in right:
            raise ValueError(f"stable package inventory is missing: {relative}")
        status = (
            "PASS"
            if left[relative].get("size_bytes") == right[relative].get("size_bytes")
            and left[relative].get("sha256") == right[relative].get("sha256")
            else "FAIL"
        )
        file_layers.append({"relative_path": relative, "status": status})
    stable_inventory_status = "PASS" if all(
        item["status"] == "PASS" for item in file_layers
    ) else "FAIL"

    baseline_layers = _r3_layers(baseline_verification, "baseline")
    replay_layers = _r3_layers(replay_verification, "replay")
    if (
        baseline_layers["comparison_contract_sha256"]
        != replay_layers["comparison_contract_sha256"]
    ):
        raise ValueError("R3 comparison contract identity differs")
    active_contract = (baseline_verification.get("active_identity") or {}).get(
        "comparison_contract_sha256"
    )
    if active_contract != baseline_layers["comparison_contract_sha256"]:
        raise ValueError("R3 comparison contract is not bound to the active identity")
    numeric_comparisons = compare_numeric_inputs_v1(
        baseline_layers["numeric"], replay_layers["numeric"]
    )
    numeric_status = (
        "PASS"
        if all(item["status"] == "PASS" for item in numeric_comparisons)
        else "FAIL"
    )
    byte_status = (
        "PASS"
        if _canonical(baseline_layers["projection"])
        == _canonical(replay_layers["projection"])
        else "FAIL"
    )
    semantic_status = (
        "PASS"
        if baseline_layers["projection"]["stable_result_content"]
        == replay_layers["projection"]["stable_result_content"]
        else "FAIL"
    )
    visual_status = next(
        item["status"]
        for item in file_layers
        if item["relative_path"] == "visualization/conformal_preview.png"
    )
    baseline_ref = {
        "operational_evidence_kind": "SUCCESS_MANIFEST",
        "operational_evidence_schema_version": "latticegpt_operational_result_manifest_v1",
        "operational_evidence_sha256": baseline_layers["operational_evidence_sha256"],
        "deterministic_projection_sha256": baseline_layers["projection"]["projection_sha256"],
        "numeric_comparison_input_sha256": sha256_bytes(
            _canonical(baseline_layers["numeric"])
        ),
    }
    replay_ref = {
        "operational_evidence_kind": "SUCCESS_MANIFEST",
        "operational_evidence_schema_version": "latticegpt_operational_result_manifest_v1",
        "operational_evidence_sha256": replay_layers["operational_evidence_sha256"],
        "deterministic_projection_sha256": replay_layers["projection"]["projection_sha256"],
        "numeric_comparison_input_sha256": sha256_bytes(
            _canonical(replay_layers["numeric"])
        ),
    }
    layered_report = build_layered_comparison_report_v1(
        comparison_contract_sha256=baseline_layers["comparison_contract_sha256"],
        run_a=baseline_ref,
        run_b=replay_ref,
        byte_status=byte_status,
        numeric_status=numeric_status,
        semantic_status=semantic_status,
        run_a_relationship_status=baseline_layers["relationship"].get("status"),
        run_b_relationship_status=replay_layers["relationship"].get("status"),
        visual_status=visual_status,
        non_claims=sorted(set(R4_LITE_NON_CLAIMS)),
        numeric_comparisons=numeric_comparisons,
        run_a_relationship_evidence=baseline_layers["relationship"],
        run_b_relationship_evidence=replay_layers["relationship"],
        run_a_volatile_records=[],
        run_b_volatile_records=[],
    )
    overall = "PASS" if (
        active_match
        and stable_inventory_status == "PASS"
        and layered_report["overall_status"] == "PASS"
    ) else "FAIL"
    return {
        "schema_version": "latticegpt.r4_lite_replay_comparison.v1",
        "status": overall,
        "comparison_contract": "R3_LAYERED_PACKAGE_INVENTORY_REUSE_V1",
        "active_identity_status": "PASS" if active_match else "FAIL",
        "stable_package_inventory_status": stable_inventory_status,
        "package_inventory_layers": file_layers,
        "r3_layered_report": layered_report,
        "baseline_job_id": baseline_verification["job_id"],
        "replay_job_id": replay_verification["job_id"],
        "non_claims": list(R4_LITE_NON_CLAIMS),
    }


_ARCHIVE_CORE_ALLOWLIST = OrderedDict(
    (
        ("active_identity", "identity/final_active_identity.json"),
        ("baseline_job", "baseline/job.json"),
        ("baseline_verification", "baseline/verify_job.json"),
        ("replay_contract", "replay/replay_contract.json"),
        ("replay_verification", "replay/verify_job.json"),
        ("replay_comparison", "replay/comparison.json"),
        ("export_manifest", "export/export_manifest.json"),
        ("paper_evidence_index", "evidence/paper_evidence_index.json"),
        ("figure_evidence_index", "evidence/figure_evidence_index.json"),
        ("video_evidence_index", "evidence/video_evidence_index.json"),
    )
)


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def create_r4_lite_archive(
    runtime_root: str | Path,
    archive_id: str,
    *,
    core_files: Mapping[str, str | Path],
    companion_artifacts: list[Mapping[str, object]],
) -> dict[str, object]:
    """Create a core archive while binding, not duplicating, large companions."""
    _valid_id(archive_id, "archive_id")
    if not isinstance(core_files, Mapping) or set(core_files) != set(
        _ARCHIVE_CORE_ALLOWLIST
    ):
        raise ValueError("archive core_files must match the fixed logical allowlist")
    if not isinstance(companion_artifacts, list):
        raise ValueError("companion_artifacts must be a list")

    outputs: OrderedDict[str, bytes] = OrderedDict()
    core_inventory = []
    bound_objects: dict[str, dict[str, object]] = {}
    for logical_id, relative in _ARCHIVE_CORE_ALLOWLIST.items():
        raw_source = core_files[logical_id]
        relative = validate_relative_path(relative, label="archive core path")
        source = Path(raw_source).resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise ValueError("archive core source must be a regular file")
        data = source.read_bytes()
        target_relative = f"core/{relative}"
        outputs[target_relative] = data
        item = {
            "logical_id": logical_id,
            "relative_path": target_relative,
            "size_bytes": len(data),
            "sha256": sha256_bytes(data),
        }
        core_inventory.append(item)
        bound_objects[logical_id] = _json_object(source, logical_id)

    active = bound_objects["active_identity"]
    baseline_job_id = bound_objects["baseline_job"].get("job_id")
    baseline_verification = bound_objects["baseline_verification"]
    replay_contract = bound_objects["replay_contract"]
    replay_verification = bound_objects["replay_verification"]
    comparison = bound_objects["replay_comparison"]
    export_manifest = bound_objects["export_manifest"]
    if (
        not baseline_job_id
        or baseline_verification.get("status") != "PASS"
        or baseline_verification.get("job_id") != baseline_job_id
        or baseline_verification.get("active_identity") != active
    ):
        raise ValueError("baseline Job/verification/final identity binding is invalid")
    if (
        replay_contract.get("baseline_job_id") != baseline_job_id
        or replay_contract.get("active_identity") != active
        or replay_contract.get("replay_count") != 1
        or replay_verification.get("status") != "PASS"
        or replay_verification.get("active_identity") != active
    ):
        raise ValueError("single replay/final identity binding is invalid")
    if (
        comparison.get("status") != "PASS"
        or comparison.get("baseline_job_id") != baseline_job_id
        or comparison.get("replay_job_id") != replay_verification.get("job_id")
    ):
        raise ValueError("replay comparison binding is invalid")
    if (
        export_manifest.get("source_job_id") != baseline_job_id
        or export_manifest.get("active_identity") != active
    ):
        raise ValueError("export/final identity binding is invalid")
    for logical_id in (
        "paper_evidence_index",
        "figure_evidence_index",
        "video_evidence_index",
    ):
        if (
            not isinstance(bound_objects[logical_id].get("items"), list)
            or not bound_objects[logical_id]["items"]
        ):
            raise ValueError("paper/figure/video evidence index is incomplete")

    companions = []
    seen_companions = set()
    for item in companion_artifacts:
        if not isinstance(item, Mapping):
            raise ValueError("companion binding must be an object")
        companion_id = _valid_id(str(item.get("companion_id") or ""), "companion_id")
        if companion_id in seen_companions:
            raise ValueError("companion identity is duplicated")
        seen_companions.add(companion_id)
        path = Path(str(item.get("path") or "")).resolve(strict=True)
        if not path.is_file() or path.is_symlink():
            raise ValueError("companion source must be a regular file")
        size = path.stat().st_size
        digest = sha256_bytes(path.read_bytes())
        if item.get("size_bytes") != size or str(item.get("sha256") or "").upper() != digest:
            raise ValueError("companion size/SHA binding mismatch")
        companions.append(
            {
                "companion_id": companion_id,
                "source_path": str(path),
                "size_bytes": size,
                "sha256": digest,
                "binding_status": "BOUND_NOT_COPIED",
            }
        )

    non_claims_bytes = _canonical(
        {
            "schema_version": "latticegpt.r4_lite_non_claims.v1",
            "non_claims": list(R4_LITE_NON_CLAIMS),
        }
    )
    outputs["non_claims.json"] = non_claims_bytes
    archive_manifest = {
        "schema_version": "latticegpt.r4_lite_archive_manifest.v1",
        "archive_id": archive_id,
        "core_files": core_inventory,
        "bindings": {
            item["logical_id"]: {
                "relative_path": item["relative_path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            for item in core_inventory
        },
        "companions": companions,
        "non_claims": list(R4_LITE_NON_CLAIMS),
    }
    manifest_bytes = _canonical(archive_manifest)
    outputs["archive_manifest.json"] = manifest_bytes
    outputs["archive_manifest.json.sha256"] = detached_sha256_bytes(
        manifest_bytes, "archive_manifest.json"
    )
    root = Path(runtime_root).resolve(strict=True)
    target = root / "r4_lite" / "archive" / archive_id
    try:
        created = exclusive_create_set_v1(target, outputs)
    except OutputSetHold as exc:
        return _hold(exc)
    return {
        "status": "PASS",
        "archive_id": archive_id,
        "target": str(target),
        "created": created,
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }


__all__ = [
    "R4_LITE_NON_CLAIMS",
    "R4_LITE_BASELINE_SEQUENCE",
    "R4_LITE_RUNTIME_SEED",
    "build_runtime_reproduction_profile_v1",
    "compare_replay_jobs",
    "compare_replay_verifications",
    "create_r4_lite_archive",
    "create_replay_contract",
    "export_job",
]


_CLI_EXIT_CODES = {"PASS": 0, "FAIL": 1, "HOLD": 2}


def _load_cli_identity(path: str | Path) -> dict[str, object]:
    value = _json_object(Path(path).resolve(strict=True), "active identity")
    if not value:
        raise ValueError("active identity must be nonempty")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R4-Lite runtime identity, readonly verify, and bounded export")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "verify-layers", "export"):
        child = subparsers.add_parser(command)
        child.add_argument("--runtime-root", required=True)
        child.add_argument("--job-id", required=True)
        child.add_argument("--active-identity", required=True)
        if command == "export":
            child.add_argument("--export-id", required=True)
    runtime_profile = subparsers.add_parser("runtime-profile")
    runtime_profile.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "runtime-profile":
            from .config import load_latticegpt_config

            profile = build_runtime_reproduction_profile_v1(
                load_latticegpt_config(config_path=args.config)
            )
            sys.stdout.write(
                json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
            )
            return 0
        identity = _load_cli_identity(args.active_identity)
        if args.command == "verify":
            result = verify_job(
                args.runtime_root,
                args.job_id,
                expected_active_identity=identity,
            )
        elif args.command == "verify-layers":
            from .r4_lite_operational_evidence import derive_verified_r3_layers_v1

            result = derive_verified_r3_layers_v1(
                args.runtime_root,
                args.job_id,
                identity,
            )
        else:
            result = export_job(
                args.runtime_root,
                args.job_id,
                args.export_id,
                expected_active_identity=identity,
                final_active_identity_path=(
                    args.active_identity if "identity_sha256" in identity else None
                ),
            )
    except (OSError, TypeError, ValueError) as exc:
        result = {"status": "FAIL", "reasons": [str(exc)]}
    status = str(result.get("status") or "FAIL")
    if status not in _CLI_EXIT_CODES:
        result = {"status": "FAIL", "reasons": ["invalid command result status"]}
        status = "FAIL"
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return _CLI_EXIT_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
