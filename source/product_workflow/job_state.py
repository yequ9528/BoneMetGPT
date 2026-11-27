from __future__ import annotations

from copy import deepcopy
import re
import math
from typing import Any, Mapping, Sequence

from .store import JOB_ID_PATTERN, JOB_SCHEMA_VERSION
from .reproducibility import (
    comparison_contract_sha256_v1,
    validate_comparison_contract_v1,
    validate_reproduction_profile_v1,
)
from .manifest_io import canonical_json_object_v1, sha256_bytes
from .contracts import validate_conformal_reservation_v1, canonical_sha256, candidate_contract, validate_nmse_candidate_state
from datetime import datetime, timezone


def candidate_confirmation_status(job: Mapping[str, Any]) -> str:
    """Validate the new nested contract without inferring consent from old selections."""
    selection = job.get("selection")
    if not isinstance(selection, Mapping) or "confirmation" not in selection:
        return "UNCONFIRMED"
    try:
        confirmation = selection["confirmation"]
        fields = {"schema_version", "job_id", "candidate_set_schema_version", "candidate_set_sha256",
                  "selected_candidate_id", "selected_candidate_payload_sha256", "action_revision",
                  "confirmed_revision", "method", "confirmed_at", "confirmation_sha256"}
        if not isinstance(confirmation, Mapping) or set(confirmation) != fields:
            return "INVALID"
        candidates = job["candidate_set"]
        collection_sha = candidates["candidate_set_sha256"]
        matches = [c for c in candidates["candidates"] if c["candidate_id"] == selection["selected_candidate_id"]]
        if (len(matches) != 1 or matches[0].get("eligible") is not True
            or selection["schema_version"] != candidate_contract(candidates)[0]
            or confirmation["schema_version"] != "latticegpt_candidate_confirmation_v1"
            or confirmation["method"] != "explicit_user_selection"
            or confirmation["job_id"] != job["job_id"]
            or not JOB_ID_PATTERN.fullmatch(str(job["job_id"]))
            or confirmation["candidate_set_schema_version"] != candidates["schema_version"]
            or confirmation["candidate_set_sha256"] != collection_sha
            or selection["candidate_set_sha256"] != collection_sha
            or collection_sha != canonical_sha256({k: v for k, v in candidates.items() if k != "candidate_set_sha256"})
            or confirmation["selected_candidate_id"] != selection["selected_candidate_id"]
            or confirmation["selected_candidate_payload_sha256"] != canonical_sha256(matches[0])
            or confirmation["selected_candidate_payload_sha256"] != selection["selected_candidate_payload_sha256"]
            or selection["selected_rank"] != matches[0]["rank"]
            or selection["recommended_candidate_id"] != candidates["recommended_candidate_id"]
            or type(confirmation["action_revision"]) is not int or confirmation["action_revision"] < 0
            or type(confirmation["confirmed_revision"]) is not int
            or confirmation["confirmed_revision"] != confirmation["action_revision"] + 1
            or type(job["revision"]) is not int or confirmation["confirmed_revision"] > job["revision"]
            or confirmation["confirmation_sha256"] != canonical_sha256({k: v for k, v in confirmation.items() if k != "confirmation_sha256"})
            or selection["selection_sha256"] != canonical_sha256({k: v for k, v in selection.items() if k != "selection_sha256"})):
            return "INVALID"
        timestamp = datetime.fromisoformat(confirmation["confirmed_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            return "INVALID"
        return "CONFIRMED"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "INVALID"


INVERSE_INTERRUPTION_STAGES = frozenset({"TARGET_CONFIRMED", "INVERSE_RUNNING"})
RUNTIME_STATUSES = {
    "ADAPTER_RUNNING",
    "CONFORMAL_RUNNING",
    "PACKAGE_VERIFYING",
}
KNOWN_STATUSES = {
    "TARGET_PROPOSED",
    "TARGET_CONFIRMED",
    "INVERSE_RUNNING",
    "CANDIDATES_READY",
    "MESH_READY",
    "AWAITING_CONFORMAL_AUTHORIZATION",
    *RUNTIME_STATUSES,
    "VIEWER_READY",
    "FAILED",
    "HOLD_RUNTIME_INTERRUPTED",
}


def classify_job_generation(job: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(job)) if isinstance(job, Mapping) else {}
    schema_version = value.get("schema_version")
    generation = value.get("generation")
    reproduction = value.get("reproduction")
    r3_marker = any(
        (
            schema_version in {"latticegpt_design_job_v2", "latticegpt_design_job_v3"},
            value.get("source_status") == "PASS_R3_SOURCE_IDENTITY",
            str(value.get("source_prefix") or "").replace("\\", "/").startswith("release/v1/r3"),
            isinstance(generation, Mapping) and generation.get("stage") == "R3",
            isinstance(reproduction, Mapping) and reproduction.get("status") in {"FROZEN_R3", "FROZEN_R3_EXECUTION_CONTEXT_V1"},
        )
    )
    base = {"runtime_permitted": False, "migration_performed": False}
    expected_generation = {
        "schema_version": "latticegpt_job_generation_v1",
        "stage": "R3",
        "contract": "R3_DETERMINISM_MESH_BINDING_V1",
    }
    if schema_version == "latticegpt_design_job_v3":
        try:
            expected_v3 = {
                "schema_version": "latticegpt_job_generation_v1",
                "stage": "R3",
                "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1",
            }
            if dict(generation or {}) != expected_v3 or not isinstance(reproduction, Mapping):
                raise ValueError("incomplete R3 v3 discriminator")
            if set(reproduction) != {"status", "execution_context", "execution_context_sha256"} or reproduction["status"] != "FROZEN_R3_EXECUTION_CONTEXT_V1":
                raise ValueError("incomplete R3 v3 reproduction object")
            from .r3_execution_context import validate_r3_execution_context_v1

            context = validate_r3_execution_context_v1(reproduction["execution_context"])
            if reproduction["execution_context_sha256"] != context["execution_context_sha256"]:
                raise ValueError("R3 v3 execution-context identity mismatch")
        except (TypeError, ValueError):
            return {**base, "classification": "INVALID_R3_JOB"}
        return {**base, "classification": "VALID_R3_JOB_V3", "runtime_permitted": True}
    if schema_version == "latticegpt_design_job_v2":
        try:
            if dict(generation or {}) != expected_generation or not isinstance(reproduction, Mapping):
                raise ValueError("incomplete R3 discriminator")
            required = {"profile", "profile_sha256", "status", "comparison_contract", "comparison_contract_sha256"}
            if set(reproduction) != required or reproduction["status"] != "FROZEN_R3":
                raise ValueError("incomplete R3 reproduction object")
            profile = validate_reproduction_profile_v1(reproduction["profile"])
            if reproduction["profile_sha256"] != profile["profile_sha256"]:
                raise ValueError("profile identity mismatch")
            contract = validate_comparison_contract_v1(
                reproduction["comparison_contract"],
                tolerance_schema_path=profile["tolerance"]["schema_path"],
                tolerance_schema_sha256=profile["tolerance"]["schema_sha256"],
            )
            expected_contract_sha = comparison_contract_sha256_v1(contract)
            if reproduction["comparison_contract_sha256"] != expected_contract_sha:
                raise ValueError("comparison contract identity missing")
        except (TypeError, ValueError):
            return {**base, "classification": "INVALID_R3_JOB"}
        return {**base, "classification": "VALID_R3_JOB", "runtime_permitted": True}
    if r3_marker:
        return {**base, "classification": "INVALID_R3_JOB"}
    if (
        schema_version == "latticegpt_design_job_v1"
        and value.get("source_status") in {"PASS_R2_SOURCE_IDENTITY", "PASS_R2_7_SOURCE_IDENTITY"}
        and str(value.get("source_prefix") or "").replace("\\", "/").startswith("release/v1/r2")
    ):
        return {**base, "classification": "HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED"}
    return {**base, "classification": "HOLD_UNKNOWN_JOB_GENERATION"}


def validate_r3_job_snapshot_v2(job: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(job, Mapping):
        raise ValueError("R3 Job must be an object")
    required = {
        "schema_version", "generation", "job_id", "revision", "status",
        "workflow_disposition", "created_at", "updated_at", "target",
        "model_runtime_profile", "formal_bundle_binding", "runtime_authorizations",
        "candidate_set", "selection", "mesh_binding", "mesh_uploads",
        "conformal_reservation", "attempts", "failure", "reproduction",
    }
    if not required.issubset(job) or set(job) - required - {"inverse_result", "mesh_precheck"} or job.get("schema_version") != "latticegpt_design_job_v2":
        raise ValueError("R3 Job closed schema mismatch")
    value = deepcopy(dict(job))
    if classify_job_generation(value)["classification"] != "VALID_R3_JOB":
        raise ValueError("R3 Job generation/reproduction identity is invalid")
    if not JOB_ID_PATTERN.fullmatch(str(value.get("job_id") or "")):
        raise ValueError("R3 Job ID is invalid")
    if isinstance(value.get("revision"), bool) or not isinstance(value.get("revision"), int) or value["revision"] < 0:
        raise ValueError("R3 Job revision is invalid")
    if not isinstance(value.get("attempts"), list) or len(value["attempts"]) > 1:
        raise ValueError("R3 Job permits at most one attempt")
    if value.get("conformal_reservation") is not None:
        reservation = validate_conformal_reservation_v1(value["conformal_reservation"])
        if reservation["job_id"] != value["job_id"]:
            raise ValueError("R3 Job reservation relationship mismatch")
    _validate_product_job_state(value)
    _validate_product_job_extensions(value)
    if candidate_confirmation_status(value) == "INVALID":
        raise ValueError("candidate confirmation is invalid")
    return value


def validate_r3_job_snapshot_v3(job: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(job, Mapping):
        raise ValueError("R3 v3 Job must be an object")
    required = {
        "schema_version", "generation", "job_id", "revision", "status",
        "workflow_disposition", "created_at", "updated_at", "target",
        "model_runtime_profile", "formal_bundle_binding", "runtime_authorizations",
        "candidate_set", "selection", "mesh_binding", "mesh_uploads",
        "conformal_reservation", "attempts", "failure", "reproduction",
    }
    if set(job) != required or job.get("schema_version") != "latticegpt_design_job_v3":
        raise ValueError("R3 v3 Job closed schema mismatch")
    value = deepcopy(dict(job))
    if classify_job_generation(value)["classification"] != "VALID_R3_JOB_V3":
        raise ValueError("R3 v3 Job generation/context identity is invalid")
    if not JOB_ID_PATTERN.fullmatch(str(value.get("job_id") or "")):
        raise ValueError("R3 v3 Job ID is invalid")
    if isinstance(value.get("revision"), bool) or not isinstance(value.get("revision"), int) or value["revision"] < 0:
        raise ValueError("R3 v3 Job revision is invalid")
    if not isinstance(value.get("attempts"), list) or len(value["attempts"]) > 1:
        raise ValueError("R3 v3 Job permits at most one attempt")
    from .contracts import require_model_runtime_profile
    from .r3_execution_context import validate_r3_execution_context_v1

    context = validate_r3_execution_context_v1(value["reproduction"]["execution_context"])
    model = require_model_runtime_profile(value["model_runtime_profile"])
    if model != context["reproduction"]["model_runtime_profile"]:
        raise ValueError("R3 v3 Job model profile differs from execution context")
    binding = value["formal_bundle_binding"]
    if not isinstance(binding, Mapping) or binding.get("binding_sha256") != context["release"]["formal_bundle_binding_sha256"]:
        raise ValueError("R3 v3 Job formal-bundle identity differs from execution context")
    target = value["target"]
    source = target.get("source") if isinstance(target, Mapping) else None
    if not isinstance(source, Mapping) or source.get("sha256") != context["inputs"]["target"]["target_sha256"]:
        raise ValueError("R3 v3 Job target identity differs from execution context")
    if value.get("conformal_reservation") is not None:
        reservation = validate_conformal_reservation_v1(value["conformal_reservation"])
        if reservation["job_id"] != value["job_id"]:
            raise ValueError("R3 v3 Job reservation relationship mismatch")
    if candidate_confirmation_status(value) == "INVALID":
        raise ValueError("candidate confirmation is invalid")
    return value


def validate_product_job_snapshot(
    job: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(job, Mapping):
        raise ValueError("product job snapshot must be a mapping")
    value = deepcopy(dict(job))
    job_id = str(value.get("job_id") or "")
    if value.get("schema_version") != JOB_SCHEMA_VERSION or not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("product job snapshot identity is invalid")
    return _validate_product_job_state(value, events)


def _validate_product_job_state(
    value: dict[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    job_id = value["job_id"]
    validate_nmse_candidate_state(value)
    if candidate_confirmation_status(value) == "INVALID":
        raise ValueError("candidate confirmation is invalid")

    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("product job revision is invalid")
    status = str(value.get("status") or "")
    if status not in KNOWN_STATUSES:
        raise ValueError("product job status is invalid")
    disposition = str(value.get("workflow_disposition") or "")
    if disposition not in {"ACTIVE", "HOLD"}:
        raise ValueError("product job workflow disposition is invalid")
    if not isinstance(value.get("target"), Mapping):
        raise ValueError("product job target is missing")
    if not isinstance(value.get("model_runtime_profile"), Mapping):
        raise ValueError("product job model runtime profile is missing")
    if not isinstance(value.get("formal_bundle_binding"), Mapping):
        raise ValueError("product job formal bundle binding is missing")
    if not isinstance(value.get("created_at"), str) or not isinstance(value.get("updated_at"), str):
        raise ValueError("product job timestamps are invalid")

    attempts = value.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("product job attempts are invalid")
    if len(attempts) > 1:
        raise ValueError("V1 permits at most one runtime attempt per Job")
    if attempts and not isinstance(attempts[0], Mapping):
        raise ValueError("product job attempt is invalid")

    mesh_uploads = value.get("mesh_uploads", [])
    if not isinstance(mesh_uploads, list) or any(not isinstance(item, Mapping) for item in mesh_uploads):
        raise ValueError("product mesh upload evidence is invalid")
    value["mesh_uploads"] = deepcopy(mesh_uploads)

    candidate_required = status not in {
        "TARGET_PROPOSED",
        "TARGET_CONFIRMED",
        "INVERSE_RUNNING",
        "FAILED",
    }
    if candidate_required:
        candidate_set = value.get("candidate_set")
        selection = value.get("selection")
        if not isinstance(candidate_set, Mapping) or not isinstance(
            candidate_set.get("candidates"), list
        ):
            raise ValueError("product candidate set is missing")
        no_eligible = (
            status == 'CANDIDATES_READY' and disposition == 'HOLD'
            and candidate_set.get('status') == 'HOLD_NO_ELIGIBLE_CANDIDATE'
            and candidate_set.get('recommended_candidate_id') is None
            and len(candidate_set['candidates']) == 3
            and all(c.get('eligible') is False for c in candidate_set['candidates'])
            and selection is None and not value.get('mesh_binding') and not attempts)
        if not no_eligible and (not isinstance(selection, Mapping) or not selection.get("selected_candidate_id")):
            raise ValueError("product candidate selection is missing")

    mesh_required = status in {
        "MESH_READY",
        "AWAITING_CONFORMAL_AUTHORIZATION",
        *RUNTIME_STATUSES,
        "VIEWER_READY",
        "HOLD_RUNTIME_INTERRUPTED",
    }
    if mesh_required and not isinstance(value.get("mesh_binding"), Mapping):
        raise ValueError("product mesh binding is missing")

    if status == "AWAITING_CONFORMAL_AUTHORIZATION" and attempts:
        raise ValueError("authorization-wait Job must not contain a runtime attempt")
    if status in RUNTIME_STATUSES | {"VIEWER_READY", "HOLD_RUNTIME_INTERRUPTED"}:
        if len(attempts) != 1:
            raise ValueError("runtime Job must contain one attempt")
    if status == "VIEWER_READY":
        attempt = attempts[0]
        verification = attempt.get("package_verification")
        if attempt.get("status") != "VIEWER_READY" or not isinstance(verification, Mapping):
            raise ValueError("VIEWER_READY Job attempt is incomplete")
        if verification.get("status") not in {"passed", "success", "complete"}:
            raise ValueError("VIEWER_READY Job package verification did not pass")

    failure = value.get("failure")
    if status in {"FAILED", "HOLD_RUNTIME_INTERRUPTED"}:
        if disposition != "HOLD" or not isinstance(failure, Mapping):
            raise ValueError("terminal HOLD Job is missing failure evidence")
    if status == "HOLD_RUNTIME_INTERRUPTED":
        if failure.get("code") != "RUNTIME_PROCESS_INTERRUPTED":
            raise ValueError("interrupted runtime HOLD code is invalid")

    if events is not None:
        _validate_events(job_id, revision, status, events)
    return value


def validate_job_snapshot_by_schema(
    job: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a persisted v1/R3 v2/R3 v3 snapshot without migration."""
    if not isinstance(job, Mapping):
        raise ValueError("Job snapshot must be an object")
    schema_version = job.get("schema_version")
    if schema_version == JOB_SCHEMA_VERSION:
        return validate_product_job_snapshot(job, events)
    if schema_version == "latticegpt_design_job_v2":
        value = validate_r3_job_snapshot_v2(job)
    elif schema_version == "latticegpt_design_job_v3":
        value = validate_r3_job_snapshot_v3(job)
    else:
        raise ValueError("unsupported Job snapshot schema")
    if events is not None:
        _validate_events(value["job_id"], value["revision"], value["status"], events)
    return value


def build_product_job_projection(
    job: Mapping[str, Any],
    *,
    runtime_active: bool = False,
) -> dict[str, Any]:
    value = validate_job_snapshot_by_schema(job)
    return _build_product_job_projection(value, runtime_active=runtime_active)


def validate_readonly_product_job_snapshot(
    job: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
    *,
    compatibility_entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep formal schemas closed; permit only an exactly registered old snapshot."""
    if not isinstance(job, Mapping):
        raise ValueError("Viewer Job must be an object")
    original = deepcopy(dict(job))
    canonical = canonical_json_object_v1(original)
    extensions = {"inverse_result", "mesh_precheck"}
    if compatibility_entry is not None:
        identity = compatibility_entry.get("job")
        if (
            not isinstance(identity, Mapping)
            or identity.get("path") != f"jobs/{original.get('job_id')}/job.json"
            or identity.get("sha256") != sha256_bytes(canonical)
            or type(identity.get("size_bytes")) is not int
            or identity["size_bytes"] != len(canonical)
        ):
            raise ValueError("Viewer Job compatibility byte identity mismatch")
    validate_job_snapshot_by_schema(original)
    if compatibility_entry is not None:
        if compatibility_entry.get("allowed_job_extensions") != sorted(extensions.intersection(original)):
            raise ValueError("Viewer Job extension registration mismatch")
        attempts = original.get("attempts")
        package_id = f"{original['job_id']}-attempt-0001"
        if (original.get("status") != "VIEWER_READY"
            or compatibility_entry.get("package_format") != "LEGACY_SIX_FILES_V1"
            or compatibility_entry.get("package_id") != package_id
            or not isinstance(attempts, list) or len(attempts) != 1
            or attempts[0].get("attempt_id") != package_id
            or attempts[0].get("package_id") != package_id):
            raise ValueError("Viewer compatibility requires its unique ready package")
    _validate_product_job_state(deepcopy(original), events)
    return original


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_product_job_extensions(job: Mapping[str, Any]) -> None:
    if "inverse_result" in job:
        inverse = job["inverse_result"]
        required = {"schema_version", "status", "candidate_count", "cad_ready_count",
                    "warnings", "errors", "steps", "cleanup", "work_dir_retained",
                    "formal_bundle_binding_sha256"}
        invalid_type = isinstance(inverse, Mapping) and inverse.get("status") == "invalid_result_type"
        version2 = isinstance(inverse, Mapping) and inverse.get('schema_version') == 'latticegpt_inverse_result_summary_v2'
        if version2:
            required |= {'algorithm', 'diagnostics', 'elapsed_ms', 'failure'}
        if not invalid_type:
            required |= {"model_variant", "runtime_mode"}
        if not isinstance(inverse, Mapping) or set(inverse) != required:
            raise ValueError("inverse result closed shape mismatch")
        binding_sha = job["formal_bundle_binding"].get("binding_sha256")
        if (inverse["schema_version"] not in {"latticegpt_inverse_result_summary_v1", "latticegpt_inverse_result_summary_v2"}
            or not isinstance(inverse["status"], str) or not inverse["status"]
            or not isinstance(binding_sha, str) or not re.fullmatch(r"[0-9A-F]{64}", binding_sha)
            or inverse["formal_bundle_binding_sha256"] != binding_sha
            or type(inverse["candidate_count"]) is not int or inverse["candidate_count"] < 0
            or (inverse["cad_ready_count"] is not None and (type(inverse["cad_ready_count"]) is not int or inverse["cad_ready_count"] < 0))
            or not _string_list(inverse["warnings"]) or not _string_list(inverse["errors"])
            or (inverse["work_dir_retained"] is not None and type(inverse["work_dir_retained"]) is not bool)):
            raise ValueError("inverse result identity/count/diagnostics mismatch")
        if version2:
            if (inverse['algorithm'] != {'version': 'truss_latent_20261110', 'prediction_kind': 'forward_surrogate'}
                or type(inverse['elapsed_ms']) is not int or inverse['elapsed_ms'] < 0
                or not isinstance(inverse['diagnostics'], list)):
                raise ValueError('inverse v2 algorithm/timing mismatch')
            for index, diagnostic in enumerate(inverse['diagnostics'], 1):
                if (not isinstance(diagnostic, Mapping)
                    or set(diagnostic) != {'target_index', 'diversity_threshold', 'diversity_fallback'}
                    or type(diagnostic['target_index']) is not int or diagnostic['target_index'] != index
                    or type(diagnostic['diversity_threshold']) not in (int, float)
                    or diagnostic['diversity_threshold'] not in (0., .16, .11, .08, .055, .035, .02)
                    or type(diagnostic['diversity_fallback']) is not bool
                    or diagnostic['diversity_fallback'] != (diagnostic['diversity_threshold'] == 0.)):
                    raise ValueError('inverse v2 diversity diagnostic mismatch')
            failure = inverse['failure']
            if failure is not None and (not isinstance(failure, Mapping)
                or set(failure) != {'code', 'compute_stage', 'message', 'next_step'}
                or any(not isinstance(v, str) or not v for v in failure.values())):
                raise ValueError('inverse v2 failure mismatch')
        successful_candidates = isinstance(job.get("candidate_set"), Mapping)
        if successful_candidates:
            candidates = job["candidate_set"].get("candidates")
            if version2:
                if len(inverse['diagnostics']) != 1 or inverse['failure'] is not None:
                    raise ValueError('inverse v2 successful diagnostics mismatch')
                for candidate in candidates or []:
                    nmse = candidate.get('nmse')
                    if type(nmse) not in (int, float) or not math.isfinite(nmse) or nmse < 0:
                        raise ValueError('inverse v2 candidate NMSE mismatch')
            if (inverse["status"] != "success" or inverse.get("model_variant") != "150w"
                or inverse.get("runtime_mode") != "formal_product"
                or not isinstance(candidates, list) or not candidates
                or any(not isinstance(c, Mapping) or type(c.get("cad_ready")) is not bool for c in candidates)
                or inverse["candidate_count"] != len(candidates)
                or inverse["cad_ready_count"] != sum(c["cad_ready"] for c in candidates)
                or inverse["errors"] or inverse["work_dir_retained"] is not False
                or inverse["cleanup"] != {"status": "complete", "raw_files_available": False, "work_dir_removed": True}):
                raise ValueError("inverse result successful candidate relationship mismatch")
        elif job.get("status") != "FAILED" or job.get("workflow_disposition") != "HOLD" or not isinstance(job.get("failure"), Mapping):
            raise ValueError("inverse result without candidates requires FAILED evidence")
        if invalid_type:
            if inverse["candidate_count"] != 0 or inverse["cad_ready_count"] != 0 or not inverse["errors"] or inverse["steps"] != [] or inverse["cleanup"] is not None or inverse["work_dir_retained"] is not None:
                raise ValueError("invalid-type inverse summary mismatch")
        elif inverse["model_variant"] not in {None, "150w"} or inverse["runtime_mode"] not in {None, "formal_product"}:
            raise ValueError("inverse result model/runtime mismatch")
        cleanup = inverse["cleanup"]
        if cleanup is not None and (not isinstance(cleanup, Mapping) or set(cleanup) != {"status", "raw_files_available", "work_dir_removed"}
                                   or not isinstance(cleanup["status"], str) or any(type(cleanup[k]) is not bool for k in ("raw_files_available", "work_dir_removed"))):
            raise ValueError("inverse cleanup shape mismatch")
        if not isinstance(inverse["steps"], list):
            raise ValueError("inverse steps are invalid")
        for step in inverse["steps"]:
            if (not isinstance(step, Mapping) or set(step) != {"step", "status", "command", "cwd", "elapsed_ms", "returncode", "error", "stdout_tail", "stderr_tail"}
                or not isinstance(step["status"], str) or not step["status"]
                or type(step["elapsed_ms"]) is not int or step["elapsed_ms"] < 0
                or not _string_list(step["command"])
                or any(step[k] is not None and not isinstance(step[k], str) for k in ("step", "cwd", "error", "stdout_tail", "stderr_tail"))
                or (step["returncode"] is not None and type(step["returncode"]) is not int)
                or (step["status"] == "success" and (step["returncode"] != 0 or step["error"] is not None))
                or (successful_candidates and step["status"] != "success")):
                raise ValueError("inverse result step is invalid")
    if "mesh_precheck" not in job:
        return
    precheck = job["mesh_precheck"]
    compact = {"status", "errors", "warnings"}
    full = compact | {"schema_version", "source", "mesh", "design_domain", "geometry_quality", "non_claims", "writes_files", "runs_runtime"}
    if (not isinstance(precheck, Mapping) or set(precheck) not in (compact, full)
        or precheck["status"] != "failed" or not _string_list(precheck["errors"]) or not precheck["errors"]
        or not _string_list(precheck["warnings"])):
        raise ValueError("rejected mesh precheck shape/status mismatch")
    rejected = [u for u in job["mesh_uploads"] if u.get("status") == "REJECTED"]
    if not rejected or rejected[-1].get("precheck") != precheck:
        raise ValueError("mesh precheck must match the last REJECTED upload")
    if set(precheck) == full:
        upload = rejected[-1]
        if (precheck["schema_version"] != "latticegpt_9x_mesh_precheck_v1"
            or precheck["writes_files"] is not False or precheck["runs_runtime"] is not False
            or not _string_list(precheck["non_claims"])
            or any(not isinstance(precheck[k], Mapping) for k in ("source", "mesh", "design_domain", "geometry_quality"))
            or precheck["source"] != {"path": upload.get("stored_path"), "sha256": upload.get("sha256"), "size_bytes": upload.get("size_bytes")}):
            raise ValueError("rejected mesh precheck source/shape mismatch")


def build_readonly_product_job_projection(
    job: Mapping[str, Any], *, compatibility_entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = validate_readonly_product_job_snapshot(job, compatibility_entry=compatibility_entry)
    projection = _build_product_job_projection(value, runtime_active=False)
    projection["capabilities"] = {
        key: enabled if key in {"refresh", "view_in_viewer"} else False
        for key, enabled in projection["capabilities"].items()
    }
    projection["generation_classification"] = {**classify_job_generation(value), "runtime_permitted": False}
    projection["readonly"] = True
    return projection


def _build_product_job_projection(
    value: Mapping[str, Any], *, runtime_active: bool,
) -> dict[str, Any]:
    status = value["status"]
    disposition = value["workflow_disposition"]
    attempts = value["attempts"]
    active = disposition == "ACTIVE"
    confirmation_status = candidate_confirmation_status(value)

    if status in {"FAILED", "HOLD_RUNTIME_INTERRUPTED"} or value.get("failure"):
        view_state = "failed"
    elif status == "VIEWER_READY":
        view_state = "result"
    elif status in RUNTIME_STATUSES:
        view_state = "running"
    elif value.get("mesh_binding"):
        view_state = "mesh"
    elif status == "CANDIDATES_READY":
        view_state = "candidate"
    else:
        view_state = "target"

    capabilities = {
        "resume": True,
        "refresh": True,
        "select_candidate": active
        and status == "CANDIDATES_READY"
        and value.get("selection") is not None
        and value.get("mesh_binding") is None,
        "upload_mesh": active
        and status == "CANDIDATES_READY"
        and confirmation_status == "CONFIRMED"
        and value.get("mesh_binding") is None,
        "start_conformal": active
        and status == "AWAITING_CONFORMAL_AUTHORIZATION"
        and confirmation_status == "CONFIRMED"
        and value.get("mesh_binding") is not None
        and not attempts,
        "view_in_viewer": view_state in {"candidate", "mesh", "running", "result", "failed"}
        and value.get("candidate_set") is not None,
        "observe_active_runtime": status in RUNTIME_STATUSES and bool(runtime_active),
    }
    return {
        "schema_version": "latticegpt_product_job_projection_v1",
        "job_id": value["job_id"],
        "revision": value["revision"],
        "status": status,
        "workflow_disposition": disposition,
        "updated_at": value["updated_at"],
        "view_state": view_state,
        "runtime_active": status in RUNTIME_STATUSES and bool(runtime_active),
        "capabilities": capabilities,
        "candidate_confirmation": confirmation_status,
    }


def _validate_events(
    job_id: str,
    revision: int,
    status: str,
    events: Sequence[Mapping[str, Any]],
) -> None:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or not events:
        raise ValueError("product job event evidence is missing")
    expected_revisions = list(range(revision + 1))
    actual_revisions: list[int] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("job_id") != job_id:
            raise ValueError("product job event identity mismatch")
        if type(event.get("revision")) is not int:
            raise ValueError("product job event revision is invalid")
        actual_revisions.append(event.get("revision"))
    if actual_revisions != expected_revisions:
        raise ValueError("product job event revisions are not contiguous")
    last = events[-1]
    if last.get("revision") != revision or last.get("status") != status:
        raise ValueError("product job event revision does not match snapshot")
