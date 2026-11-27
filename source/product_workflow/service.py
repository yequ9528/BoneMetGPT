from __future__ import annotations

from copy import deepcopy
from functools import wraps
import hashlib
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from latticegpt_9x_candidate_ranking import (
    HOLD_NO_ELIGIBLE_CANDIDATE,
    check_target_scaler_range,
    rank_candidates_p10,
    rank_candidates_nmse,
    target_validation_failure_kind,
    validate_target_p09,
    validate_restricted_9d_p09,
)
from latticegpt_9x_mesh_precheck import (
    WHOLE_FILE_SELECTOR_VALUE,
    WHOLE_FILE_TECHNICAL_DOMAIN,
    WHOLE_FILE_TECHNICAL_PURPOSE,
    WHOLE_FILE_PRODUCT_DOMAIN,
    WHOLE_FILE_PRODUCT_PURPOSE,
    precheck_inp_mesh,
)

from .contracts import (
    candidate_contract,
    PRODUCT_INP_MAX_SIZE_BYTES,
    PRODUCT_INP_MAX_SIZE_MB,
    STIFFNESS_NAMES,
    TARGET_SCHEMA_VERSION,
    canonical_sha256,
    require_adapter_run_result,
    require_formal_bundle_binding,
    require_model_runtime_profile,
    require_nine,
    build_conformal_reservation_v1,
    bind_conformal_reservation_v1,
    consume_conformal_reservation_v1,
)
from .mesh_identity import build_mesh_binding_v2
from .reproducibility import validate_reproduction_profile_v1
from .manifest_io import canonical_json_object_v1, sha256_bytes, sha256_file
from .store import JobStore, utc_now
from .config import require_within
from .job_state import candidate_confirmation_status


CANDIDATE_SET_SCHEMA_VERSION = "latticegpt_candidate_set_v1"
SELECTION_SCHEMA_VERSION = "latticegpt_candidate_selection_v1"
AUTHORIZATION_SCHEMA_VERSION = "latticegpt_runtime_authorization_v1"
MESH_BINDING_SCHEMA_VERSION = "latticegpt_mesh_binding_v1"


class CandidateConfirmationCommitError(RuntimeError):
    def __init__(self, outcome: Mapping[str, Any]):
        super().__init__("候选确认写入状态待核对，上传入口暂不可用。")
        self.outcome = dict(outcome)


def _runtime_reference_length(precheck: Any, *, schema: str) -> float:
    if not isinstance(precheck, Mapping):
        raise ValueError(f"{schema} upload precheck is invalid")
    geometry_quality = precheck.get("geometry_quality")
    if not isinstance(geometry_quality, Mapping):
        raise ValueError(f"{schema} upload precheck geometry_quality is invalid")
    value = geometry_quality.get("reference_length")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{schema} upload precheck reference_length is invalid")
    return float(value)


def normalize_job_mesh_binding(
    job: Mapping[str, Any],
    *,
    verify_stored_file: bool = False,
) -> dict[str, Any]:
    """Return one closed runtime/UI view for MeshBindingV1 or MeshBindingV2."""

    if not isinstance(job, Mapping):
        raise TypeError("job must be an object")
    binding = job.get("mesh_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("mesh binding must be an object")
    schema = binding.get("schema_version")
    if schema == MESH_BINDING_SCHEMA_VERSION:
        runtime_precheck = binding.get("precheck")
        required = (
            "mesh_binding_sha256", "stored_path", "original_filename", "sha256",
            "size_bytes", "node_count", "element_count", "element_type_original",
            "element_type_effective", "upload_id",
        )
        if any(key not in binding for key in required):
            raise ValueError("MeshBindingV1 runtime fields are incomplete")
        result = {
            "schema_version": schema,
            "binding_sha256": binding["mesh_binding_sha256"],
            "stored_path": binding["stored_path"],
            "original_filename": binding["original_filename"],
            "raw_sha256": binding["sha256"],
            "raw_size_bytes": binding["size_bytes"],
            "node_count": binding["node_count"],
            "element_count": binding["element_count"],
            "element_type_original": binding["element_type_original"],
            "element_type_effective": binding["element_type_effective"],
            "upload_id": binding["upload_id"],
            "reference_length_mm": _runtime_reference_length(
                binding.get("precheck"), schema=schema
            ),
        }
    elif schema == "latticegpt_mesh_binding_v3":
        source, semantics = binding["source"], binding["semantics"]
        upload_id = binding["relationship"]["upload_id"]
        matches = [u for u in job.get("mesh_uploads", []) if u.get("upload_id") == upload_id]
        if len(matches) != 1 or matches[0].get("status") != "DOMAIN_ACCEPTED":
            raise ValueError("MeshBindingV3 requires one explicitly accepted domain")
        upload = matches[0]
        derived = upload["derived_mesh"]
        runtime_precheck = upload["domain_precheck"]
        original = binding["original_upload"]
        if (original["raw_sha256"] != upload["sha256"] or original["raw_size_bytes"] != upload["size_bytes"]
            or original["original_filename"] != upload["original_filename"]
            or source["raw_sha256"] != derived["sha256"] or source["raw_size_bytes"] != derived["size_bytes"]
            or binding["confirmation"] != upload["domain_confirmation"]
            or binding["design_domain"] != upload["domain_proposal"]["domain"]
            or binding["confirmation"]["candidate_confirmation_sha256"] != job["selection"]["confirmation"]["confirmation_sha256"]
            or sha256_bytes(canonical_json_object_v1(runtime_precheck)) != binding["precheck"]["evidence_sha256"]):
            raise ValueError("MeshBindingV3 upload, domain or confirmation mismatch")
        parent = Path(upload["stored_path"]).parent
        if (parent.name != upload_id or parent.parent.name != "uploads" or parent.parent.parent.name != job["job_id"]
            or Path(derived["stored_path"]) != parent / "selected_hex.inp"):
            raise ValueError("MeshBindingV3 derivative escaped its original upload")
        result = {"schema_version": schema, "binding_sha256": binding["digests"]["mesh_binding_sha256"],
            "stored_path": derived["stored_path"], "original_filename": source["original_filename"],
            "raw_sha256": source["raw_sha256"], "raw_size_bytes": source["raw_size_bytes"],
            **{k: semantics[k] for k in ("node_count", "element_count", "element_type_original", "element_type_effective")},
            "upload_id": upload_id, "reference_length_mm": _runtime_reference_length(runtime_precheck, schema=schema),
            "original_upload": dict(original), "design_domain": dict(binding["design_domain"])}
    elif schema == "latticegpt_mesh_binding_v2":
        source = binding.get("source")
        semantics = binding.get("semantics")
        relationship = binding.get("relationship")
        digests = binding.get("digests")
        if not all(isinstance(value, Mapping) for value in (source, semantics, relationship, digests)):
            raise ValueError("MeshBindingV2 nested identity objects are incomplete")
        source_keys = (
            "original_filename", "raw_sha256", "raw_size_bytes",
        )
        semantic_keys = (
            "node_count", "element_count", "element_type_original",
            "element_type_effective",
        )
        if (
            any(key not in source for key in source_keys)
            or any(key not in semantics for key in semantic_keys)
            or "upload_id" not in relationship
            or "mesh_binding_sha256" not in digests
        ):
            raise ValueError("MeshBindingV2 runtime identity fields are incomplete")
        upload_id = relationship["upload_id"]
        uploads = job.get("mesh_uploads")
        if not isinstance(uploads, list):
            raise ValueError("MeshBindingV2 upload evidence must be a list")
        matches = [
            item for item in uploads
            if isinstance(item, Mapping) and item.get("upload_id") == upload_id
        ]
        if len(matches) != 1 or matches[0].get("status") != "ACCEPTED":
            raise ValueError("MeshBindingV2 upload evidence must have one ACCEPTED relationship match")
        upload = matches[0]
        runtime_precheck = upload.get("precheck")
        expected = {
            "original_filename": source["original_filename"],
            "size_bytes": source["raw_size_bytes"],
            "sha256": source["raw_sha256"],
        }
        if any(upload.get(key) != value for key, value in expected.items()):
            raise ValueError("MeshBindingV2 upload evidence identity mismatch")
        result = {
            "schema_version": schema,
            "binding_sha256": digests["mesh_binding_sha256"],
            "stored_path": upload.get("stored_path"),
            "original_filename": source["original_filename"],
            "raw_sha256": source["raw_sha256"],
            "raw_size_bytes": source["raw_size_bytes"],
            "node_count": semantics["node_count"],
            "element_count": semantics["element_count"],
            "element_type_original": semantics["element_type_original"],
            "element_type_effective": semantics["element_type_effective"],
            "upload_id": upload_id,
            "reference_length_mm": _runtime_reference_length(
                upload.get("precheck"), schema=schema
            ),
        }
    else:
        raise ValueError("unsupported mesh binding schema")

    source_contract = binding["source"] if schema in {"latticegpt_mesh_binding_v2", "latticegpt_mesh_binding_v3"} else binding
    result.update({
        "units": source_contract.get("units"),
        "frame": source_contract.get("frame"),
        "precheck_status": (binding.get("precheck") or {}).get("status"),
    })
    precheck_mesh = (
        runtime_precheck.get("mesh")
        if isinstance(runtime_precheck, Mapping)
        and runtime_precheck.get("status") == "passed"
        else None
    )
    if (
        not isinstance(precheck_mesh, Mapping)
        or precheck_mesh.get("element_type_original")
        != result["element_type_original"]
        or precheck_mesh.get("element_type_effective")
        != result["element_type_effective"]
    ):
        raise ValueError(f"{schema} runtime mesh precheck identity mismatch")

    if (
        not isinstance(result["stored_path"], str)
        or not result["stored_path"]
        or not isinstance(result["original_filename"], str)
        or Path(result["original_filename"]).name != result["original_filename"]
        or not isinstance(result["raw_size_bytes"], int)
        or isinstance(result["raw_size_bytes"], bool)
        or result["raw_size_bytes"] <= 0
    ):
        raise ValueError(f"{schema} runtime path or scalar identity is invalid")
    for key in ("binding_sha256", "raw_sha256"):
        value = result[key]
        if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789ABCDEF" for ch in value):
            raise ValueError(f"{schema} {key} is invalid")
    for key in ("node_count", "element_count"):
        value = result[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{schema} {key} is invalid")
    if verify_stored_file:
        stored = Path(result["stored_path"])
        if not stored.is_file():
            raise ValueError(f"{schema} stored mesh is missing")
        if stored.stat().st_size != result["raw_size_bytes"] or sha256_file(stored) != result["raw_sha256"]:
            raise ValueError(f"{schema} stored mesh identity mismatch")
        if result["units"] != "mm" or result["frame"] != "source_native_cartesian_frame":
            raise ValueError(f"{schema} stored mesh coordinate contract mismatch")
        if schema == "latticegpt_mesh_binding_v3":
            from .mesh_identity import validate_mesh_binding_v3
            original_path = Path(upload["stored_path"])
            attempts = job.get("attempts") or []
            attempt = attempts[0] if len(attempts) == 1 else {}
            reservation = job.get("conformal_reservation") or {}
            validate_mesh_binding_v3(binding, original_bytes=original_path.read_bytes(), derived_bytes=stored.read_bytes(),
                precheck=runtime_precheck, expected_relationship={"job_id": job["job_id"], "upload_id": upload_id,
                    "candidate_id": job["selection"]["selected_candidate_id"], "selection_manifest_sha256": job["selection"]["selection_sha256"],
                    "reservation_id": reservation.get("reservation_id"), "attempt_id": attempt.get("attempt_id", reservation.get("attempt_id")),
                    "package_id": attempt.get("package_id", reservation.get("package_id"))})
        elif schema == "latticegpt_mesh_binding_v2":
            from .mesh_identity import validate_mesh_binding_v2
            attempts = job.get("attempts") or []
            attempt = attempts[0] if len(attempts) == 1 else {}
            reservation = job.get("conformal_reservation") or {}
            validate_mesh_binding_v2(
                binding, raw_mesh_bytes=stored.read_bytes(),
                expected_relationship={
                    "job_id": job["job_id"], "upload_id": result["upload_id"],
                    "candidate_id": job["selection"]["selected_candidate_id"],
                    "selection_manifest_sha256": job["selection"]["selection_sha256"],
                    "reservation_id": reservation.get("reservation_id"),
                    "attempt_id": attempt.get("attempt_id", reservation.get("attempt_id")),
                    "package_id": attempt.get("package_id", reservation.get("package_id")),
                },
            )
        elif canonical_sha256({k: v for k, v in binding.items() if k != "mesh_binding_sha256"}) != result["binding_sha256"]:
            raise ValueError("MeshBindingV1 identity mismatch")
    return result


def _runtime_source_mesh_contract(mesh: Mapping[str, Any]) -> dict[str, str]:
    original = mesh.get("element_type_original")
    effective = mesh.get("element_type_effective")
    expected = {
        "C3D8": "C3D8",
        "C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
        "C3D8+C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
    }.get(original)
    if effective != expected:
        raise ValueError("runtime source mesh element type contract is invalid")
    return {
        "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
        "element_type_original": original,
        "element_type_effective": effective,
    }


def _serialized_job_command(method):
    @wraps(method)
    def wrapped(self, job_id, *args, **kwargs):
        with self.store.mutation(job_id):
            return method(self, job_id, *args, **kwargs)

    return wrapped


class ProductWorkflowService:
    def __init__(
        self,
        runtime_root: str | Path,
        *,
        inverse_runner: Callable[..., Mapping[str, Any]],
        scaler_y_min: Mapping[str, Any],
        scaler_y_max: Mapping[str, Any],
        formal_bundle_binding: Mapping[str, Any],
        formal_bundle_verifier: Callable[[], Mapping[str, Any]],
        adapter_runner: Callable[..., Mapping[str, Any]] | None = None,
        conformal_runner: Callable[..., Mapping[str, Any]] | None = None,
        package_verifier: Callable[..., Mapping[str, Any]] | None = None,
        adapter_output_root: str | Path | None = None,
        conformal_output_root: str | Path | None = None,
        reproduction_profile: Mapping[str, Any] | None = None,
        comparison_contract: Mapping[str, Any] | None = None,
        source_manifest_sha256: str | None = None,
        execution_context: Mapping[str, Any] | None = None,
        readonly_package_producer: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(inverse_runner):
            raise TypeError("inverse_runner must be callable")
        if not callable(formal_bundle_verifier):
            raise TypeError("formal_bundle_verifier must be callable")
        self.runtime_root = Path(runtime_root).resolve()
        self.store = JobStore(self.runtime_root)
        self.inverse_runner = inverse_runner
        self.formal_bundle_binding = require_formal_bundle_binding(formal_bundle_binding)
        self.formal_bundle_verifier = formal_bundle_verifier
        self.adapter_runner = adapter_runner
        self.conformal_runner = conformal_runner
        self.package_verifier = package_verifier
        self.readonly_package_producer = deepcopy(readonly_package_producer)
        self.execution_context = None
        if execution_context is not None:
            from .r3_execution_context import validate_r3_execution_context_v1

            if reproduction_profile is not None or comparison_contract is not None or source_manifest_sha256 is not None:
                raise ValueError("R3ExecutionContextV1 is the sole active R3 identity input")
            self.execution_context = validate_r3_execution_context_v1(execution_context)
            allowed_roots = {
                Path(self.execution_context["outputs"]["run_a_output_root"]),
                Path(self.execution_context["outputs"]["run_b_output_root"]),
            }
            if self.runtime_root not in allowed_roots:
                raise ValueError("service runtime root is not a context run root")
            reproduction_profile = self.execution_context["reproduction"]["profile"]
            comparison_contract = self.execution_context["reproduction"]["comparison_contract"]
            source_manifest_sha256 = self.execution_context["release"]["source_manifest_sha256"]
        self.reproduction_profile = (
            None
            if reproduction_profile is None
            else validate_reproduction_profile_v1(reproduction_profile)
        )
        self.comparison_contract = (
            None if comparison_contract is None else deepcopy(dict(comparison_contract))
        )
        if (self.reproduction_profile is None) != (self.comparison_contract is None):
            raise ValueError("R3 reproduction profile and comparison contract are inseparable")
        self.source_manifest_sha256 = source_manifest_sha256
        if self.reproduction_profile is not None and (
            not isinstance(source_manifest_sha256, str) or len(source_manifest_sha256) != 64
        ):
            raise ValueError("R3 source_manifest_sha256 is required")
        selected_adapter_root = (
            self.runtime_root / "adapter"
            if adapter_output_root is None
            else adapter_output_root
        )
        selected_conformal_root = (
            self.runtime_root / "conformal"
            if conformal_output_root is None
            else conformal_output_root
        )
        self.adapter_output_root = require_within(
            selected_adapter_root,
            self.runtime_root,
            label="adapter output root",
        )
        self.conformal_output_root = require_within(
            selected_conformal_root,
            self.runtime_root,
            label="conformal output root",
        )
        self.scaler_y_min = self._finite_nine(scaler_y_min, "scaler_y_min")
        self.scaler_y_max = self._finite_nine(scaler_y_max, "scaler_y_max")
        for name in STIFFNESS_NAMES:
            if self.scaler_y_max[name] <= self.scaler_y_min[name]:
                raise ValueError(f"scaler range must be positive for {name}")

    def create_job(
        self,
        target_proposal: Mapping[str, Any],
        *,
        model_runtime_profile: Mapping[str, Any],
        proposed_job_id: str | None = None,
    ) -> dict[str, Any]:
        target = deepcopy(dict(target_proposal))
        if target.get("schema_version") != TARGET_SCHEMA_VERSION:
            raise ValueError("TargetProposalV1 is required")
        if target.get("status") != "TARGET_PROPOSED" or target.get("confirmed") is not False:
            raise ValueError("target must be proposed and unconfirmed")
        profile = require_model_runtime_profile(model_runtime_profile)
        if self.reproduction_profile is None:
            return self.store.create(
                target,
                model_runtime_profile=profile,
                formal_bundle_binding=self.formal_bundle_binding,
            )
        comparison_sha = sha256_bytes(canonical_json_object_v1(self.comparison_contract))
        if self.execution_context is not None:
            return self.store.create_r3(
                target,
                model_runtime_profile=profile,
                formal_bundle_binding=self.formal_bundle_binding,
                execution_context=self.execution_context,
                proposed_job_id=proposed_job_id,
            )
        if proposed_job_id is not None:
            raise ValueError("proposed_job_id is supported only by R3 Job v3")
        return self.store.create_r3(
            target,
            model_runtime_profile=profile,
            formal_bundle_binding=self.formal_bundle_binding,
            reproduction_profile=self.reproduction_profile,
            comparison_contract=self.comparison_contract,
            comparison_contract_sha256=comparison_sha,
        )

    @_serialized_job_command
    def confirm_target_and_run_inverse(
        self,
        job_id: str,
        *,
        expected_revision: int,
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(authorization, Mapping) or authorization.get("authorized") is not True:
            raise ValueError("explicit 150w runtime authorization is required")
        job = self.store.get(job_id)
        self._expect(job, expected_revision, "TARGET_PROPOSED")

        try:
            current_binding = require_formal_bundle_binding(self.formal_bundle_verifier())
            frozen_sha = job["formal_bundle_binding"]["binding_sha256"]
            if current_binding["binding_sha256"] != frozen_sha:
                raise ValueError("formal bundle identity drifted after job creation")
        except Exception as exc:
            job["status"] = "FAILED"
            job["workflow_disposition"] = "HOLD"
            job["failure"] = {
                "stage": "FORMAL_BUNDLE_PREFLIGHT",
                "message": str(exc),
            }
            return self.store.replace(
                job,
                expected_revision=expected_revision,
                event="formal_bundle_preflight_failed",
            )

        target = deepcopy(job["target"])
        range_check, target_p09, failure_kind = self._target_scope_checks(target)
        target["confirmed"] = True
        target["status"] = "TARGET_CONFIRMED"
        target["confirmed_at"] = utc_now()
        target["range_status"] = "PASS" if failure_kind is None else "HOLD"
        target["scaler_range_status"] = range_check["status"]
        target["range_check"] = range_check
        target["validation_failure_kind"] = failure_kind
        target["p09_validation"] = target_p09
        target["proposal_sha256"] = canonical_sha256(
            {key: value for key, value in target.items() if key != "proposal_sha256"}
        )
        job["target"] = target
        job["status"] = "TARGET_CONFIRMED"
        job["runtime_authorizations"].append(
            self._authorization("inverse_150w", authorization)
        )
        job = self.store.replace(job, expected_revision=expected_revision, event="target_confirmed")

        if target["range_status"] != "PASS":
            job["status"] = "FAILED"
            job["workflow_disposition"] = "HOLD"
            job["failure"] = {
                "stage": "TARGET_CONFIRMED",
                "code": failure_kind,
                "message": {
                    "SCALER_RANGE": "effective float32 target is outside the supported scaler_Y interval; P-09 passed",
                    "P09": "P-09 target validation did not pass; scaler_Y interval check passed",
                    "BOTH": "scaler_Y interval check and P-09 target validation did not pass",
                    "INVALID_CONVERSION": "target conversion, scaler_Y boundary or P-09 numeric evaluation is invalid; inverse was not started",
                }[failure_kind],
                "details": {
                    "scaler_range_status": range_check["status"],
                    "p09_status": target_p09["status"],
                    "fields": [item["field"] for item in range_check["fields"] if item["status"] != "PASS"],
                    "range_check": range_check,
                    "p09_validation": target_p09,
                },
            }
            return self.store.replace(
                job, expected_revision=job["revision"], event="target_range_hold"
            )

        job["status"] = "INVERSE_RUNNING"
        job = self.store.replace(job, expected_revision=job["revision"], event="inverse_started")
        result = None
        try:
            inverse_kwargs = {
                "num_candidates": 3,
                "run_cad": False,
                "keep_work_dir": False,
                "runtime_mode": "formal_product",
                "formal_bundle_binding": current_binding,
            }
            if self.reproduction_profile is not None:
                inverse_kwargs["reproduction_profile"] = self.reproduction_profile
            result = self.inverse_runner(
                target["physical_9d_gpa"],
                **inverse_kwargs,
            )
            job["inverse_result"] = self._inverse_result_summary(
                result,
                formal_bundle_binding_sha256=current_binding["binding_sha256"],
            )
            candidate_set, selection = self._build_candidate_set(target, result)
            if job["schema_version"] == "latticegpt_design_job_v2":
                from .job_state import _validate_product_job_extensions
                proposed = {**job, "candidate_set": candidate_set, "selection": selection}
                _validate_product_job_extensions(proposed)
        except Exception as exc:
            job["status"] = "FAILED"
            job["workflow_disposition"] = "HOLD"
            job["failure"] = {"stage": "INVERSE_RUNNING", "message": str(exc)}
            compute_failure = (job.get('inverse_result') or {}).get('failure')
            if isinstance(compute_failure, dict):
                job['failure'].update(deepcopy(compute_failure))
            elif isinstance(result, Mapping):
                steps = result.get('steps')
                errors = result.get('errors')
                preflight_failed = result.get('status') == 'failed' and steps == []
                job['failure'].update(
                    code='INVERSE_PREFLIGHT_FAILED' if preflight_failed else 'INVERSE_OUTPUT_INVALID',
                    compute_stage='preflight' if preflight_failed else 'result_conversion',
                    message='; '.join(errors) if isinstance(errors, list) and errors and all(isinstance(e, str) for e in errors) else str(exc),
                    next_step='Inspect the saved diagnostics before a new manually confirmed run.')
            if job["schema_version"] == "latticegpt_design_job_v2" and "inverse_result" in job:
                from .job_state import _validate_product_job_extensions
                try:
                    _validate_product_job_extensions(job)
                except (ValueError, TypeError, KeyError):
                    job["inverse_result"] = self._inverse_result_summary(
                        {"status": "invalid_summary", "errors": ["inverse summary contract rejected"]},
                        formal_bundle_binding_sha256=current_binding["binding_sha256"])
            return self.store.replace(
                job, expected_revision=job["revision"], event="inverse_failed"
            )

        job["candidate_set"] = candidate_set
        job["selection"] = selection
        job["status"] = "CANDIDATES_READY"
        job["workflow_disposition"] = (
            "HOLD" if candidate_set["status"] == HOLD_NO_ELIGIBLE_CANDIDATE else "ACTIVE"
        )
        return self.store.replace(
            job, expected_revision=job["revision"], event="candidates_ready"
        )

    @_serialized_job_command
    def select_candidate(
        self,
        job_id: str,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_candidate_set_sha256: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        job = self.store.get_consistent(job_id)
        self._expect(job, expected_revision, "CANDIDATES_READY")
        collection = job["candidate_set"]
        if (type(expected_revision) is not int
            or expected_candidate_set_sha256 != collection["candidate_set_sha256"]
            or collection["candidate_set_sha256"] != canonical_sha256({k: v for k, v in collection.items() if k != "candidate_set_sha256"})):
            raise ValueError("candidate set version or identity mismatch")
        candidates = job["candidate_set"]["candidates"]
        matches = [item for item in candidates if item["candidate_id"] == candidate_id]
        if len(matches) != 1:
            raise ValueError("candidate does not exist in CandidateSetV1")
        if matches[0].get("eligible") is not True:
            raise ValueError("only an eligible candidate may be selected")
        recommended = job["candidate_set"]["recommended_candidate_id"]
        source = candidate_contract(collection)[2] if candidate_id == recommended else "user_override"
        job["selection"] = self._selection(
            recommended,
            matches[0],
            source=source,
            reason=reason,
            candidate_set_sha256=job["candidate_set"]["candidate_set_sha256"],
            schema_version=candidate_contract(collection)[0],
        )
        confirmation = {
            "schema_version": "latticegpt_candidate_confirmation_v1", "job_id": job_id,
            "candidate_set_schema_version": collection["schema_version"],
            "candidate_set_sha256": expected_candidate_set_sha256,
            "selected_candidate_id": candidate_id,
            "selected_candidate_payload_sha256": job["selection"]["selected_candidate_payload_sha256"],
            "action_revision": expected_revision, "confirmed_revision": expected_revision + 1,
            "method": "explicit_user_selection", "confirmed_at": utc_now(),
        }
        confirmation["confirmation_sha256"] = canonical_sha256(confirmation)
        job["selection"]["confirmation"] = confirmation
        job["selection"]["selection_sha256"] = canonical_sha256({k: v for k, v in job["selection"].items() if k != "selection_sha256"})
        try:
            return self.store.replace(job, expected_revision=expected_revision, event="candidate_selected")
        except Exception as exc:
            raise CandidateConfirmationCommitError(self.confirmation_commit_outcome(job_id, confirmation)) from exc

    @_serialized_job_command
    def confirmation_commit_outcome(self, job_id: str, action: Mapping[str, Any]) -> dict[str, Any]:
        """Read snapshot and events under the same short lock. Never repair or retry."""
        result = {"status": "READ_UNAVAILABLE", "job_id": job_id, "action": dict(action), "job": None}
        try:
            job = self.store.get_consistent(job_id)
            result["job"] = job
            confirmation = (job.get("selection") or {}).get("confirmation")
            if job["revision"] == action["action_revision"] and confirmation != action:
                result["status"] = "NOT_COMMITTED"
            elif (candidate_confirmation_status(job) == "CONFIRMED" and confirmation == action
                  and any(e.get("event") == "candidate_selected" and e.get("revision") == action["confirmed_revision"] and e.get("job_id") == job_id for e in self.store.read_events(job_id))):
                result["status"] = "COMMITTED"
            else:
                result["status"] = "INCONSISTENT"
        except ValueError:
            result["status"] = "INCONSISTENT"
        except (OSError, KeyError, TypeError):
            pass
        return result

    @_serialized_job_command
    def get_consistent_job(self, job_id: str) -> dict[str, Any]:
        return self.store.get_consistent(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    @_serialized_job_command
    def bind_mesh(
        self,
        job_id: str,
        *,
        original_filename: str,
        content: bytes,
        expected_revision: int,
        confirmed_contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        job = self.store.get_consistent(job_id)
        self._expect(job, expected_revision, "CANDIDATES_READY")
        if candidate_confirmation_status(job) != "CONFIRMED":
            raise ValueError("explicit candidate confirmation is required before mesh upload")
        if Path(original_filename).name != original_filename or not original_filename.lower().endswith(".inp"):
            raise ValueError("original filename must be a basename ending in .inp")
        if not isinstance(content, bytes) or not content:
            raise ValueError("INP content must be non-empty bytes")
        if len(content) > PRODUCT_INP_MAX_SIZE_BYTES:
            raise ValueError(
                f"INP content must not exceed {PRODUCT_INP_MAX_SIZE_MB} MB"
            )
        if not isinstance(confirmed_contract, Mapping):
            raise ValueError("mesh contract confirmation is required")
        if confirmed_contract.get("units") != "mm":
            raise ValueError("mesh units must be millimetres (mm)")
        if confirmed_contract.get("frame") != "source_native_cartesian_frame":
            raise ValueError("mesh must use its source-native coordinate frame")
        if confirmed_contract.get("design_domain") not in {"whole_file_implant_domain", "selected_hex_domain"}:
            raise ValueError("mesh design domain must be the whole-file implant domain")

        mesh_uploads = job.setdefault("mesh_uploads", [])
        if not isinstance(mesh_uploads, list):
            raise ValueError("product mesh upload evidence is invalid")
        upload_id = f"upload-{len(mesh_uploads) + 1:04d}"
        upload_dir = self.store.job_directory(job_id) / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=False)
        stored_path = upload_dir / "source.inp"
        with stored_path.open("xb") as stream:
            stream.write(content)
            stream.flush()
        source_sha = hashlib.sha256(content).hexdigest().upper()
        if confirmed_contract.get("design_domain") == "selected_hex_domain":
            from .mesh_domain import extract_hex_domain
            upload = {"schema_version": "latticegpt_mesh_upload_evidence_v2", "upload_id": upload_id,
                "original_filename": original_filename, "stored_path": str(stored_path), "size_bytes": len(content),
                "sha256": source_sha, "status": "DOMAIN_PENDING", "recorded_at": utc_now()}
            mesh_uploads.append(upload)
            try:
                _, domain = extract_hex_domain(content)
                proposal = {"schema_version": "latticegpt_mesh_domain_proposal_v1", "domain": domain,
                    "candidate_confirmation_sha256": job["selection"]["confirmation"]["confirmation_sha256"],
                    "selection_sha256": job["selection"]["selection_sha256"], "proposed_revision": expected_revision + 1}
                proposal["proposal_sha256"] = canonical_sha256(proposal)
                upload["domain_proposal"] = proposal
            except (ValueError, UnicodeError) as exc:
                upload.update(status="REJECTED", precheck={"status": "failed", "errors": [str(exc)], "warnings": []})
                job["mesh_precheck"] = upload["precheck"]
            return self.store.replace(job, expected_revision=expected_revision,
                event="mesh_domain_proposed" if upload["status"] == "DOMAIN_PENDING" else "mesh_upload_rejected")
        try:
            precheck = precheck_inp_mesh(
                stored_path,
                expected_sha256=source_sha,
                expected_size_bytes=len(content),
                selector_type=WHOLE_FILE_PRODUCT_DOMAIN,
                selector_value=WHOLE_FILE_SELECTOR_VALUE,
                selector_explicit_declaration=True,
                selector_purpose=WHOLE_FILE_PRODUCT_PURPOSE,
            )
        except Exception as exc:
            precheck = {
                "status": "failed",
                "errors": [str(exc)],
                "warnings": [],
            }
        upload_evidence = {
            "schema_version": "latticegpt_mesh_upload_evidence_v1",
            "upload_id": upload_id,
            "original_filename": original_filename,
            "stored_path": str(stored_path),
            "size_bytes": len(content),
            "sha256": source_sha,
            "status": "ACCEPTED" if precheck.get("status") == "passed" else "REJECTED",
            "precheck": precheck,
            "recorded_at": utc_now(),
        }
        mesh_uploads.append(upload_evidence)
        if precheck.get("status") != "passed":
            job["status"] = "CANDIDATES_READY"
            job["workflow_disposition"] = "ACTIVE"
            job["failure"] = None
            job["mesh_precheck"] = precheck
            return self.store.replace(
                job, expected_revision=expected_revision, event="mesh_upload_rejected"
            )

        mesh = precheck["mesh"]
        domain = precheck["design_domain"]
        if job.get("schema_version") in {
            "latticegpt_design_job_v2",
            "latticegpt_design_job_v3",
        }:
            selected_candidate_id = job["selection"]["selected_candidate_id"]
            reservation = build_conformal_reservation_v1(
                job_id=job_id,
                upload_id=upload_id,
                candidate_id=selected_candidate_id,
                reserved_at=utc_now(),
            )
            relationship = {
                "job_id": job_id,
                "upload_id": upload_id,
                "reservation_id": reservation["reservation_id"],
                "attempt_id": reservation["attempt_id"],
                "package_id": reservation["package_id"],
                "candidate_id": selected_candidate_id,
                "selection_manifest_sha256": job["selection"]["selection_sha256"],
            }
            binding = build_mesh_binding_v2(
                content,
                original_filename=original_filename,
                parser_schema_version="latticegpt_mesh_parser_v1",
                parser_algorithm_version="R3_EXPLICIT_ABAQUS_SUBSET_V1",
                source_manifest_sha256=self.source_manifest_sha256,
                relationship=relationship,
            )
            job["conformal_reservation"] = bind_conformal_reservation_v1(
                reservation,
                mesh_binding_sha256=binding["digests"]["mesh_binding_sha256"],
            )
        else:
            binding = {
            "schema_version": MESH_BINDING_SCHEMA_VERSION,
            "upload_id": upload_id,
            "original_filename": original_filename,
            "stored_path": str(stored_path),
            "size_bytes": len(content),
            "sha256": source_sha,
            "units": "mm",
            "frame": "source_native_cartesian_frame",
            "transform": "identity",
            "rotation": "none",
            "reflection": "none",
            "element_type_original": mesh["element_type_original"],
            "element_type_effective": mesh["element_type_effective"],
            "node_count": mesh["node_count"],
            "element_count": mesh["element_count"],
            "design_domain": "whole_file_implant_domain",
            "selected_element_count": domain["selected_element_count"],
            "selected_element_ids_sha256": domain["selected_element_ids_sha256"],
            "precheck": precheck,
        }
            binding["selection_sha256"] = job["selection"]["selection_sha256"]
            binding["mesh_binding_sha256"] = canonical_sha256(binding)
        job["mesh_binding"] = binding
        job["status"] = "MESH_READY"
        job = self.store.replace(job, expected_revision=expected_revision, event="mesh_ready")
        job["status"] = "AWAITING_CONFORMAL_AUTHORIZATION"
        return self.store.replace(
            job, expected_revision=job["revision"], event="conformal_authorization_requested"
        )

    @_serialized_job_command
    def confirm_mesh_domain(self, job_id: str, *, expected_revision: int, upload_id: str,
                            proposal_sha256: str, session_generation: str, confirmed: bool) -> dict[str, Any]:
        """Confirm or cancel one current proposal; never invoke a producer."""
        from .mesh_domain import extract_hex_domain
        from .mesh_identity import build_mesh_binding_v3
        job = self.store.get_consistent(job_id)
        self._expect(job, expected_revision, "CANDIDATES_READY")
        if candidate_confirmation_status(job) != "CONFIRMED" or job.get("attempts") or job.get("mesh_binding"):
            raise ValueError("a current explicit candidate and unused mesh slot are required")
        uploads = job.get("mesh_uploads") or []
        upload = uploads[-1] if uploads else {}
        proposal = upload.get("domain_proposal") or {}
        if (upload.get("upload_id") != upload_id or upload.get("status") != "DOMAIN_PENDING"
            or proposal.get("proposal_sha256") != proposal_sha256
            or canonical_sha256({k:v for k,v in proposal.items() if k != "proposal_sha256"}) != proposal_sha256
            or proposal.get("proposed_revision") != expected_revision
            or proposal.get("selection_sha256") != job["selection"]["selection_sha256"]
            or proposal.get("candidate_confirmation_sha256") != job["selection"]["confirmation"]["confirmation_sha256"]
            or not isinstance(session_generation, str) or not session_generation or type(confirmed) is not bool):
            raise ValueError("stale or invalid domain confirmation")
        if not confirmed:
            upload["status"] = "DOMAIN_CANCELLED"
            return self.store.replace(job, expected_revision=expected_revision, event="mesh_domain_cancelled")
        original = Path(upload["stored_path"])
        expected_original = self.store.job_directory(job_id) / "uploads" / upload_id / "source.inp"
        if original.resolve() != expected_original.resolve():
            raise ValueError("original mesh path does not match this Job upload")
        content = original.read_bytes()
        if len(content) != upload["size_bytes"] or hashlib.sha256(content).hexdigest().upper() != upload["sha256"]:
            raise ValueError("original uploaded mesh changed")
        derived, domain = extract_hex_domain(content)
        if domain != proposal["domain"]:
            raise ValueError("selected domain changed since preview")
        target = original.parent / "selected_hex.inp"
        with target.open("xb") as out:
            out.write(derived)
        upload["derived_mesh"] = {"stored_path": str(target), "sha256": domain["derived_sha256"], "size_bytes": len(derived)}
        try:
            quality = precheck_inp_mesh(target, expected_sha256=domain["derived_sha256"], expected_size_bytes=len(derived),
                selector_type=WHOLE_FILE_PRODUCT_DOMAIN, selector_value=WHOLE_FILE_SELECTOR_VALUE,
                selector_explicit_declaration=True, selector_purpose=WHOLE_FILE_PRODUCT_PURPOSE, allow_mixed_hex=True)
        except Exception as exc:
            upload["status"] = "DOMAIN_REJECTED"
            upload["domain_precheck"] = {"status": "error", "errors": [f"{type(exc).__name__}: {exc}"],
                "source": {"path": str(target), "sha256": domain["derived_sha256"], "size_bytes": len(derived)}}
            return self.store.replace(job, expected_revision=expected_revision, event="mesh_domain_quality_error")
        non_finite = []
        def json_quality(value, path=""):
            if isinstance(value, float) and not math.isfinite(value):
                non_finite.append({"path": path, "value": repr(value)})
                return repr(value)
            if isinstance(value, dict):
                return {k: json_quality(v, path+"/"+k) for k, v in value.items()}
            if isinstance(value, list):
                return [json_quality(v, path+"/"+str(i)) for i, v in enumerate(value)]
            return value
        quality = json_quality(quality)
        if non_finite:
            # Preserve exact exceptional values without emitting invalid JSON or relaxing quality.
            quality["non_finite_metrics"] = non_finite
            quality["status"] = "failed"
            quality["errors"].append("非有限质量指标已原样记录；请修正退化网格后重新上传。")
        upload["domain_precheck"] = quality
        confirmation = {"method": "explicit_user_domain_selection", "domain_selection_sha256": domain["selection_sha256"],
            "candidate_confirmation_sha256": proposal["candidate_confirmation_sha256"], "confirmed_revision": expected_revision + 1,
            "confirmed_at": utc_now(), "session_generation": session_generation}
        upload["domain_confirmation"] = confirmation
        if quality.get("status") != "passed":
            upload["status"] = "DOMAIN_REJECTED"
            return self.store.replace(job, expected_revision=expected_revision, event="mesh_domain_quality_rejected")
        reservation = build_conformal_reservation_v1(job_id=job_id, upload_id=upload_id,
            candidate_id=job["selection"]["selected_candidate_id"], reserved_at=utc_now())
        relationship = {"job_id": job_id, "upload_id": upload_id, "reservation_id": reservation["reservation_id"],
            "attempt_id": reservation["attempt_id"], "package_id": reservation["package_id"],
            "candidate_id": job["selection"]["selected_candidate_id"], "selection_manifest_sha256": job["selection"]["selection_sha256"]}
        binding = build_mesh_binding_v3(content, derived, original_filename=upload["original_filename"],
            source_manifest_sha256=self.source_manifest_sha256, relationship=relationship, confirmation=confirmation, precheck=quality)
        upload["status"] = "DOMAIN_ACCEPTED"
        job["conformal_reservation"] = bind_conformal_reservation_v1(reservation, mesh_binding_sha256=binding["digests"]["mesh_binding_sha256"])
        job["mesh_binding"] = binding
        job["status"] = "MESH_READY"
        job = self.store.replace(job, expected_revision=expected_revision, event="mesh_ready")
        job["status"] = "AWAITING_CONFORMAL_AUTHORIZATION"
        return self.store.replace(job, expected_revision=job["revision"], event="conformal_authorization_requested")

    @_serialized_job_command
    def start_conformal(
        self,
        job_id: str,
        *,
        expected_revision: int,
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(authorization, Mapping) or authorization.get("authorized") is not True:
            raise ValueError("explicit Adapter and conformal runtime authorization is required")
        job = self.store.get_consistent(job_id)
        if candidate_confirmation_status(job) != "CONFIRMED":
            raise ValueError("explicit candidate confirmation is required before conformal start")
        if not callable(self.adapter_runner) or not callable(self.conformal_runner):
            raise RuntimeError("Adapter and conformal runners are not configured")
        if job["revision"] != expected_revision:
            raise ValueError(
                f"stale revision: expected {expected_revision}, current {job['revision']}"
            )
        if job.get("attempts"):
            raise ValueError("V1 permits only one runtime attempt per Job")
        if job["status"] != "AWAITING_CONFORMAL_AUTHORIZATION":
            raise ValueError(
                "invalid state transition; conformal start requires authorization wait"
            )
        if not job.get("selection") or not job.get("mesh_binding"):
            raise ValueError("candidate selection and mesh binding must be frozen")

        mesh = normalize_job_mesh_binding(job, verify_stored_file=True)
        if (job["mesh_binding"].get("schema_version") == MESH_BINDING_SCHEMA_VERSION
            and job["mesh_binding"].get("selection_sha256") != job["selection"]["selection_sha256"]):
            raise ValueError("mesh frozen selection differs from confirmed candidate")
        source_mesh_contract = _runtime_source_mesh_contract(mesh)
        case_identity = (
            self.execution_context["inputs"]["case"]
            if self.execution_context is not None
            else None
        )

        reservation = job.get("conformal_reservation")
        attempt_id = (
            reservation["attempt_id"]
            if isinstance(reservation, Mapping)
            else f"{job_id}-attempt-0001"
        )
        adapter_dir = self.adapter_output_root / job_id / attempt_id
        conformal_dir = self.conformal_output_root / job_id / attempt_id
        if adapter_dir.exists() or conformal_dir.exists():
            raise FileExistsError("attempt output already exists; overwrite is forbidden")
        selected = next(
            item for item in job["candidate_set"]["candidates"]
            if item["candidate_id"] == job["selection"]["selected_candidate_id"]
        )
        frozen = {
            "target_sha256": job["target"]["proposal_sha256"],
            "candidate_set_sha256": job["candidate_set"]["candidate_set_sha256"],
            "selection_sha256": job["selection"]["selection_sha256"],
            "mesh_binding_sha256": mesh["binding_sha256"],
            "job_revision": expected_revision,
        }
        attempt = {
            "attempt_id": attempt_id,
            "status": "ADAPTER_RUNNING",
            "frozen_inputs": frozen,
            "adapter_output_dir": str(adapter_dir),
            "conformal_output_dir": str(conformal_dir),
            "package_id": (
                reservation["package_id"]
                if isinstance(reservation, Mapping)
                else attempt_id
            ),
            "adapter_result": None,
            "conformal_result": None,
            "package_verification": None,
        }
        job["runtime_authorizations"].extend([
            self._authorization("adapter_controlled_write", authorization),
            self._authorization("conformal_runtime", authorization),
        ])
        job["attempts"].append(attempt)
        if isinstance(reservation, Mapping):
            job["conformal_reservation"] = consume_conformal_reservation_v1(
                reservation,
                consumed_at=utc_now(),
            )
        job["status"] = "ADAPTER_RUNNING"
        job = self.store.replace(job, expected_revision=expected_revision, event="adapter_started")

        try:
            traceability = self._product_traceability(job, selected)
            adapter_result = self.adapter_runner(
                candidate=selected,
                traceability=traceability,
                mesh_binding=job["mesh_binding"],
                reference_length_mm=mesh["reference_length_mm"],
                output_dir=str(adapter_dir),
                allowed_output_root=str(self.adapter_output_root),
                overwrite=False,
            )
            adapter_result = require_adapter_run_result(adapter_result)
            job["attempts"][-1]["adapter_result"] = adapter_result
            if adapter_result["status"] != "SUCCESS":
                raise RuntimeError(
                    "Adapter controlled write failed: "
                    + "; ".join(adapter_result["errors"])
                )
            job["attempts"][-1]["status"] = "CONFORMAL_RUNNING"
            job["status"] = "CONFORMAL_RUNNING"
            job = self.store.replace(job, expected_revision=job["revision"], event="conformal_started")

            conformal_result = self.conformal_runner(
                source_mesh_path=mesh["stored_path"],
                custom_cell_path=adapter_result["artifacts"]["custom_cell"]["path"],
                adapter_metadata_path=adapter_result["artifacts"]["adapter_metadata"]["path"],
                output_dir=str(conformal_dir),
                allowed_output_root=str(self.conformal_output_root),
                package_id=attempt["package_id"],
                normalized_diameter=selected["normalized_diameter"],
                reference_length=mesh["reference_length_mm"],
                mesh_binding=job["mesh_binding"],
                source_mesh_contract=source_mesh_contract,
                case_manifest_path=(
                    case_identity["path"] if case_identity is not None else None
                ),
                case_manifest_sha256=(
                    case_identity["sha256"] if case_identity is not None else None
                ),
                reproduction_profile=(
                    self.reproduction_profile
                    if self.reproduction_profile is not None
                    else None
                ),
                product_context={
                    "schema_version": "latticegpt_product_viewer_context_v1",
                    "job_id": job_id,
                    "target": {
                        "source": job["target"]["source"],
                        "normalized_9d": job["target"]["normalized_9d"],
                        "physical_9d_gpa": job["target"]["physical_9d_gpa"],
                        "target_sha256": job["target"]["proposal_sha256"],
                    },
                    "recommended_candidate_id": job["selection"]["recommended_candidate_id"],
                    "selected_candidate_id": job["selection"]["selected_candidate_id"],
                    "selected_rank": job["selection"]["selected_rank"],
                    "selection_source": job["selection"]["selection_source"],
                    "selection_sha256": job["selection"]["selection_sha256"],
                    "candidate_prediction_9d_gpa": selected["predicted_stiffness"],
                    "mesh_binding_sha256": mesh["binding_sha256"],
                    "mesh_original_filename": mesh["original_filename"],
                    "mesh_sha256": mesh["raw_sha256"],
                },
                overwrite=False,
            )
            if conformal_result.get("status") not in {"success", "complete", "passed"}:
                detail = str(
                    conformal_result.get("message")
                    or conformal_result.get("error")
                    or conformal_result.get("status")
                )
                raise RuntimeError(f"conformal runtime failed: {detail}")
            job["attempts"][-1]["conformal_result"] = dict(conformal_result)
            job["attempts"][-1]["status"] = "PACKAGE_VERIFYING"
            job["status"] = "PACKAGE_VERIFYING"
            job = self.store.replace(job, expected_revision=job["revision"], event="package_verifying")

            verification_inputs = dict(package_id=attempt["package_id"],
                package_dir=conformal_result.get("package_dir", str(conformal_dir)), frozen_inputs=frozen)
            if self.readonly_package_producer is not None:
                if not callable(self.package_verifier):
                    raise ValueError("readonly receipt requires package verifier")
                job = self.store.get_consistent(job_id)
                verification_inputs.update(job_snapshot=deepcopy(job), normalized_mesh=mesh,
                    producer=deepcopy(self.readonly_package_producer))
            verification = self.package_verifier(**verification_inputs) if callable(self.package_verifier) else {"status": "passed"}
            if verification.get("status") not in {"passed", "success", "complete"}:
                raise RuntimeError("package identity verification failed")
            job["attempts"][-1]["package_verification"] = dict(verification)
            job["attempts"][-1]["status"] = "VIEWER_READY"
            job["status"] = "VIEWER_READY"
            if self.readonly_package_producer is not None:
                from .job_state import validate_job_snapshot_by_schema
                from .artifacts import validate_product_six_file_package
                receipt = verification.get("readonly_receipt")
                if not isinstance(receipt, dict) or receipt != validate_product_six_file_package(
                    job_snapshot=job, package_dir=conformal_dir, normalized_mesh=mesh,
                    producer=self.readonly_package_producer, expected_file_identities=receipt.get("files")):
                    raise ValueError("ready package receipt is invalid")
                validate_job_snapshot_by_schema(job)
            return self.store.replace(
                job, expected_revision=job["revision"], event="viewer_ready"
            )
        except Exception as exc:
            failed_stage = job["status"]
            job["attempts"][-1]["status"] = "FAILED"
            job["attempts"][-1]["error"] = str(exc)
            job["status"] = "FAILED"
            job["workflow_disposition"] = "HOLD"
            job["failure"] = {"stage": failed_stage, "message": str(exc)}
            return self.store.replace(
                job, expected_revision=job["revision"], event="conformal_workflow_failed"
            )

    @staticmethod
    def _product_traceability(job: Mapping[str, Any], selected: Mapping[str, Any]) -> dict[str, Any]:
        selection = job["selection"]
        return {
            "job_id": job["job_id"],
            "candidate_id": selected["candidate_id"],
            "source_candidate_id": selected["source_candidate_id"],
            "target_sha256": job["target"]["proposal_sha256"],
            "evidence_level": "real_150w_candidate",
            "selection_policy": candidate_contract(job["candidate_set"])[1],
            "candidate_manifest_sha256": selection["selected_candidate_payload_sha256"],
            "run_manifest_sha256": job["candidate_set"]["candidate_set_sha256"],
            "selection_manifest_sha256": selection["selection_sha256"],
            "recommended_candidate_id": selection["recommended_candidate_id"],
            "selected_candidate_id": selection["selected_candidate_id"],
            "selected_rank": selection["selected_rank"],
            "selection_source": selection["selection_source"],
            "p09_status": selected["p09_validation"]["status"],
            "mechanical_match_status": "NOT_VALIDATED",
            "diameter_used_in_ranking": False,
            "non_claims": job["candidate_set"]["non_claims"],
            "formal_bundle_binding_sha256": job["formal_bundle_binding"]["binding_sha256"],
            "model_runtime_profile_sha256": job["model_runtime_profile"]["profile_sha256"],
        }

    def _target_scope_checks(
        self, target: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        range_check = check_target_scaler_range(
            target.get("physical_9d_gpa"),
            scaler_y_min=self.scaler_y_min,
            scaler_y_max=self.scaler_y_max,
            raw_normalized_9d=target.get("normalized_9d"),
            scaler_y_identity=self.formal_bundle_binding["artifacts"]["scaler_y"],
        )
        p09 = validate_target_p09(target.get("physical_9d_gpa"), range_check)
        return range_check, p09, target_validation_failure_kind(range_check, p09)

    def _build_candidate_set(
        self,
        target: Mapping[str, Any],
        service_result: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not isinstance(service_result, Mapping):
            raise RuntimeError("150w inverse service returned an invalid result")
        service_status = str(service_result.get("status") or "missing")
        if service_status != "success":
            raise RuntimeError(f"150w inverse service status={service_status}")
        source_candidates = service_result.get("candidates")
        if not isinstance(source_candidates, list) or len(source_candidates) != 3:
            raise ValueError("inverse service must return exactly three candidates")

        range_check, target_p09, failure_kind = self._target_scope_checks(target)
        target_range_status = "PASS" if failure_kind is None else "HOLD"

        owner = (service_result.get("algorithm") or {}).get("version") == "truss_latent_20261110"
        ranking_inputs = []
        normalized_candidates = []
        seen = set()
        for source in source_candidates:
            candidate = deepcopy(dict(source))
            candidate_id = str(candidate.get("candidate_id") or "")
            if not candidate_id or candidate_id in seen:
                raise ValueError("candidate identities must be non-empty and unique")
            seen.add(candidate_id)
            if owner:
                from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
                from services.truss_inverse_service import POINT_NAMES
                geometry = build_unit_cell([candidate.get('relative_density'),
                    *[candidate.get('compressed_parameters',{}).get(k) for k in POINT_NAMES]])
                if any(candidate.get(k) != geometry[k] for k in ('nodes','connections','diameter','geometry_contract')):
                    raise ValueError('delivered candidate geometry does not match owner parameters')
                if candidate.get('target_stiffness', target['physical_9d_gpa']) != target['physical_9d_gpa']:
                    raise ValueError('delivered candidate target does not match current Job')
                for key in ('score','normalized_errors','max_absolute_component_error'):
                    candidate.pop(key, None)
            prediction = require_nine(candidate.get("predicted_stiffness"), "predicted_stiffness")
            p09 = validate_restricted_9d_p09(prediction)
            ranking_inputs.append({
                "source_candidate_id": candidate_id,
                "cad_ready": candidate.get("cad_ready") is True,
                "b14_status": p09["status"],
                "predicted_stiffness": prediction,
                "diameter": candidate.get("diameter"),
                "nmse": candidate.get("nmse"),
            })
            candidate["source_candidate_id"] = candidate_id
            candidate["target_sha256"] = target["proposal_sha256"]
            candidate["predicted_stiffness"] = prediction
            candidate["normalized_diameter"] = candidate.get("diameter")
            candidate["p09_validation"] = p09
            normalized_candidates.append(candidate)

        if owner:
            ranking = rank_candidates_nmse(ranking_inputs,
                target_stiffness_gpa=target['physical_9d_gpa'], target_range_status=target_range_status)
        else:
            ranking = rank_candidates_p10(
                ranking_inputs,
                target_stiffness_gpa=target["physical_9d_gpa"],
                scaler_y_min=self.scaler_y_min,
                scaler_y_max=self.scaler_y_max,
                target_range_status=target_range_status,
            )
        ranking_by_id = {item["source_candidate_id"]: item for item in ranking["candidates"]}
        for candidate in normalized_candidates:
            candidate.update(ranking_by_id[candidate["candidate_id"]])

        candidate_set = {
            "schema_version": "latticegpt_candidate_set_v2" if owner else CANDIDATE_SET_SCHEMA_VERSION,
            "candidate_count": 3,
            "target_sha256": target["proposal_sha256"],
            "formal_bundle_binding_sha256": self.formal_bundle_binding["binding_sha256"],
            "target_validation": {
                "range_check": range_check,
                "p09_validation": target_p09,
                "failure_kind": failure_kind,
            },
            "p10_contract": ranking["contract"],
            "status": ranking["status"],
            "recommended_candidate_id": ranking["selected_source_candidate_id"],
            "mechanical_match_status": "NOT_VALIDATED",
            "diameter_used_in_ranking": False,
            "candidates": normalized_candidates,
            "non_claims": ["NOT_MECHANICAL_PASS", "NOT_OOD_PASS", "NOT_FEA_VALIDATION"],
        }
        if owner:
            candidate_set.pop('p10_contract')
            candidate_set['ranking_contract'] = ranking['contract']
            candidate_set['geometry_contract'] = 'truss_20261110_unit_cell_v1'
        candidate_set["candidate_set_sha256"] = canonical_sha256(candidate_set)
        recommended = candidate_set["recommended_candidate_id"]
        if recommended is None:
            return candidate_set, None
        selected = next(item for item in normalized_candidates if item["candidate_id"] == recommended)
        selection = self._selection(
            recommended,
            selected,
            source=candidate_contract(candidate_set)[2],
            reason=None,
            candidate_set_sha256=candidate_set["candidate_set_sha256"],
            schema_version=candidate_contract(candidate_set)[0],
        )
        return candidate_set, selection

    @staticmethod
    def _inverse_result_summary(
        service_result: Any,
        *,
        formal_bundle_binding_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(service_result, Mapping):
            return {
                "schema_version": "latticegpt_inverse_result_summary_v1",
                "status": "invalid_result_type",
                "candidate_count": 0,
                "cad_ready_count": 0,
                "warnings": [],
                "errors": ["inverse service result is not a mapping"],
                "steps": [],
                "cleanup": None,
                "work_dir_retained": None,
                "formal_bundle_binding_sha256": formal_bundle_binding_sha256,
            }
        source_candidates = service_result.get("candidates")
        steps = service_result.get("steps")
        summary = {
            "schema_version": "latticegpt_inverse_result_summary_v1",
            "status": str(service_result.get("status") or "missing"),
            "model_variant": service_result.get("model_variant"),
            "candidate_count": len(source_candidates) if isinstance(source_candidates, list) else 0,
            "cad_ready_count": service_result.get("cad_ready_count"),
            "warnings": list(service_result.get("warnings") or []),
            "errors": list(service_result.get("errors") or []),
            "steps": deepcopy(steps) if isinstance(steps, list) else [],
            "cleanup": deepcopy(service_result.get("cleanup")),
            "work_dir_retained": service_result.get("work_dir_retained"),
            "runtime_mode": service_result.get("runtime_mode") or "formal_product",
            "formal_bundle_binding_sha256": formal_bundle_binding_sha256,
        }
        if 'algorithm' in service_result:
            summary.update(schema_version='latticegpt_inverse_result_summary_v2',
                           algorithm=deepcopy(service_result.get('algorithm')),
                           diagnostics=deepcopy(service_result.get('diagnostics', [])),
                           elapsed_ms=service_result.get('elapsed_ms'),
                           failure=deepcopy(service_result.get('failure')))
        return summary

    @staticmethod
    def _selection(
        recommended_candidate_id: str,
        selected: Mapping[str, Any],
        *,
        source: str,
        reason: str | None,
        candidate_set_sha256: str,
        schema_version: str = SELECTION_SCHEMA_VERSION,
    ) -> dict[str, Any]:
        value = {
            "schema_version": schema_version,
            "recommended_candidate_id": recommended_candidate_id,
            "selected_candidate_id": selected["candidate_id"],
            "selected_rank": selected["rank"],
            "selection_source": source,
            "reason": reason,
            "candidate_set_sha256": candidate_set_sha256,
            "selected_candidate_payload_sha256": canonical_sha256(dict(selected)),
            "selected_at": utc_now(),
        }
        value["selection_sha256"] = canonical_sha256(value)
        return value

    @staticmethod
    def _authorization(action: str, source: Mapping[str, Any]) -> dict[str, Any]:
        value = {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "action": action,
            "authorized": True,
            "authorized_at": utc_now(),
            "authorized_by": str(source.get("authorized_by") or "interactive_user"),
        }
        value["authorization_sha256"] = canonical_sha256(value)
        return value

    @staticmethod
    def _expect(job: Mapping[str, Any], revision: int, status: str) -> None:
        if job["revision"] != revision:
            raise ValueError(
                f"stale revision: expected {revision}, current {job['revision']}"
            )
        if job["status"] != status:
            raise ValueError(f"invalid state transition from {job['status']}; expected {status}")

    @staticmethod
    def _finite_nine(value: Mapping[str, Any], label: str) -> dict[str, float]:
        if not isinstance(value, Mapping) or set(value) != set(STIFFNESS_NAMES):
            raise ValueError(f"{label} must contain exactly the supported 9D fields")
        result = {}
        for name in STIFFNESS_NAMES:
            number = float(value[name])
            if not math.isfinite(number):
                raise ValueError(f"{label}.{name} must be finite")
            result[name] = number
        return result
