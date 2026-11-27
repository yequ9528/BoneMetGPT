from __future__ import annotations

import json
from urllib.parse import quote
from pathlib import Path
from typing import Callable

import conformal_mesh_payloads
import viewer_payloads
import product_viewer_display as display
from product_workflow.artifacts import (
    LEGACY_FILE_KEYS,
    PRODUCT_FILE_MAP,
    product_package_file_url,
    resolve_product_package,
    resolve_product_package_file,
)
from product_workflow.job_state import build_product_job_projection, build_readonly_product_job_projection, candidate_confirmation_status
from product_workflow.service import normalize_job_mesh_binding
from product_workflow.store import JobStore
from product_workflow.contracts import candidate_ranking_view
from product_workflow.ui_previews import STANDARD_CELL_RENDERER


PRODUCT_VIEWER_SCHEMA_VERSION = "latticegpt_product_viewer_state_v1"
RUNTIME_STATUSES = {
    "TARGET_PROPOSED",
    "TARGET_CONFIRMED",
    "INVERSE_RUNNING",
    "ADAPTER_RUNNING",
    "CONFORMAL_RUNNING",
    "PACKAGE_VERIFYING",
}
KNOWN_STATUSES = {
    *RUNTIME_STATUSES,
    "CANDIDATES_READY",
    "MESH_READY",
    "AWAITING_CONFORMAL_AUTHORIZATION",
    "VIEWER_READY",
    "FAILED",
    "HOLD_RUNTIME_INTERRUPTED",
}
PUBLIC_EVENTS = frozenset("""
    job_created target_confirmed formal_bundle_preflight_failed target_range_hold inverse_started inverse_failed
    candidates_ready candidate_selected mesh_domain_proposed mesh_upload_rejected mesh_ready mesh_domain_cancelled
    conformal_authorization_requested mesh_domain_quality_error mesh_domain_quality_rejected adapter_started conformal_started
    package_verifying viewer_ready conformal_workflow_failed conformal_identity_reserved runtime_process_interrupted
""".split())
FIXED_NON_CLAIMS = (
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_FEA_VALIDATION",
    "NOT_MANUFACTURING_VALIDATION",
    "NOT_CLINICAL_VALIDATION",
    "NOT_FULL_DUAL_SPACE_FUSION",
)


def build_product_job_viewer_payload(
    job_id: str,
    *,
    store: JobStore | None = None,
    conformal_root: str | Path | None = None,
    source_mesh_builder: Callable = conformal_mesh_payloads.build_source_mesh_visualization,
    conformal_payload_builder: Callable | None = None,
    include_original_mesh: bool = False,
) -> dict:
    if store is None:
        raise ValueError("product Job store must be provided explicitly")
    selected_store = store
    job = selected_store.get_consistent_for_viewer(job_id)
    projection = build_readonly_product_job_projection(
        job, compatibility_entry=selected_store.viewer_compatibility.get("jobs", {}).get(job_id),
    )
    view_state = projection["view_state"]
    if view_state == "target":
        raise ValueError("product job is not yet available to the Viewer")
    candidates = _build_candidates(job)
    selection = _build_selection(job)
    mesh = _build_mesh(job, source_mesh_builder, selected_store,
                       include_original_mesh=include_original_mesh)
    progress = _build_progress(job, selected_store)
    result = None
    if view_state == "result":
        if conformal_root is None:
            raise ValueError("current conformal root must be provided for result view")
        result = _build_result(
            job,
            selected_store,
            conformal_root,
            conformal_payload_builder,
            mesh,
        )

    non_claims = _build_non_claims(job)
    if projection["generation_classification"]["classification"] == "HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED":
        non_claims.append("REPRODUCIBILITY_NOT_VERIFIED")
    return {
        "schema_version": PRODUCT_VIEWER_SCHEMA_VERSION,
        "job_id": job_id,
        "revision": projection["revision"],
        "updated_at": display.timestamp(projection["updated_at"]),
        "workflow_disposition": projection["workflow_disposition"],
        "capabilities": projection["capabilities"],
        "generation_classification": display.project(projection["generation_classification"], {
            "classification": display.enum("HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED", "HOLD_UNKNOWN_JOB_GENERATION",
                                           "VALID_R3_JOB", "VALID_R3_JOB_V3", "INVALID_R3_JOB"),
            "runtime_permitted": lambda x: x is True, "migration_performed": lambda x: x is True,
        }),
        "view_state": view_state,
        "target": _build_target(job),
        "candidates": candidates,
        "ranking": _ranking(job.get("candidate_set") or {}),
        "candidate_set_sha256": display.digest((job.get("candidate_set") or {}).get("candidate_set_sha256")),
        "selection": selection,
        "mesh": mesh,
        "progress": progress,
        "result": result,
        "dual_space_status": _build_dual_space_status(
            job,
            verified_result=result is not None,
        ),
        "non_claims": non_claims,
    }


def _resolve_view_state(job: dict) -> str:
    return build_product_job_projection(job)["view_state"]


def _target_source(source):
    if not isinstance(source, dict):
        return {}
    safe = display.project(source, {
        "kind": display.enum("csv_upload", "uploaded_csv", "manual", "formal_150w_database", "formal_150w_query",
                             "determinism_technical_reference_target", "frozen_technical_reference"),
        "sample_id": display.count, "record_index": display.count, "record_count": display.count,
        "planner_evidence_sha256": display.digest,
    })
    for key, alias, reader in (("source_name", "name", display.filename), ("source_sha256", "sha256", display.digest)):
        value, alternate = source.get(key), source.get(alias)
        if value is not None and alternate is not None and value != alternate:
            raise ValueError("product target source aliases conflict")
        value = value if value is not None else alternate
        if value is not None:
            projected = reader(value)
            if projected is None:
                raise ValueError("product target source identity is invalid")
            safe[key] = projected
    return safe


def _build_target(job: dict) -> dict:
    target = job.get("target")
    if not isinstance(target, dict):
        raise ValueError("product job target is invalid")
    material = target.get("base_material")
    if material is not None:
        if not isinstance(material, dict):
            raise ValueError("product target base material is invalid")
        name, modulus = material.get("name"), material.get("elastic_modulus_gpa")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("product target material name is invalid")
        if modulus is not None and (display.number(modulus) is None or modulus <= 0):
            raise ValueError("product target material modulus is invalid")
        material = {"name": display.choice(name, ("TC4", "Ti-6Al-4V")), "elastic_modulus_gpa": modulus}
    return {
        "source": _target_source(target.get("source")), "base_material": material,
        "physical_9d_gpa": display.nine(target.get("physical_9d_gpa")),
        "normalized_9d": display.nine(target.get("normalized_9d")),
        "target_sha256": display.digest(target.get("proposal_sha256")),
        "range_status": display.choice(target.get("range_status"), ("UNCHECKED", "PASS", "HOLD", "FAIL")),
    }


def _ranking(candidate_set):
    value = candidate_ranking_view(candidate_set)
    value["contract"] = display.choice(value["contract"], ("truss_owner_nmse_v1", "scaler_range_weighted_RMSE_v1"))
    value["geometry_contract"] = display.choice(value["geometry_contract"], (display.GEOMETRY_CONTRACT,))
    return value


def _build_candidates(job: dict) -> list[dict]:
    candidate_set = job.get("candidate_set")
    if candidate_set is None:
        return []
    if not isinstance(candidate_set, dict):
        raise ValueError("product candidate set is invalid")
    source_candidates = candidate_set.get("candidates")
    if not isinstance(source_candidates, list) or len(source_candidates) != 3:
        raise ValueError("product candidate set must contain exactly three candidates")

    selection = job.get("selection") or {}
    recommended_id = candidate_set.get("recommended_candidate_id")
    selected_id = selection.get("selected_candidate_id")
    result = [
        _build_candidate(
            candidate,
            recommended_id=recommended_id,
            selected_id=selected_id,
        )
        for candidate in source_candidates
    ]

    set_sha = display.digest(candidate_set.get("candidate_set_sha256"))
    confirmed = candidate_confirmation_status(job) == "CONFIRMED"
    for candidate in result:
        candidate["ranking"] = _ranking(candidate_set)
        if candidate["ranking"]["metric"] == "nmse":
            candidate.pop("score", None)
        candidate["preview"] = None
        if set_sha is not None:
            candidate_id = quote(candidate["candidate_id"], safe="")
            candidate["preview"] = {
                "renderer_version": STANDARD_CELL_RENDERER,
                "candidate_set_sha256": set_sha,
                "url": (f"/api/viewer/product/jobs/{job['job_id']}/candidates/{candidate_id}/preview.png"
                        f"?candidate_set_sha256={set_sha}&renderer_version={STANDARD_CELL_RENDERER}"),
            }
        candidate["confirmed"] = candidate["selected"] and confirmed

    return result


def _build_candidate(
    candidate: dict,
    *,
    recommended_id: str | None,
    selected_id: str | None,
) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("product candidate is invalid")
    candidate_id = display.identifier(candidate.get("candidate_id"))
    if not candidate_id:
        raise ValueError("product candidate_id is missing")
    nodes = _sanitize_nodes(candidate.get("nodes"))
    connections = _sanitize_connections(candidate.get("connections"), len(nodes))
    diameter = _finite_float(
        candidate.get("normalized_diameter", candidate.get("diameter")),
        "candidate diameter",
    )
    if diameter <= 0:
        raise ValueError("candidate diameter must be positive")

    rank = candidate.get("rank")
    if rank is not None and (type(rank) is not int or rank <= 0):
        raise ValueError("candidate rank is invalid")
    row = {
        "candidate_id": candidate_id, "rank": rank, "eligible": candidate.get("eligible") is True,
        "recommended": candidate_id == recommended_id, "selected": candidate_id == selected_id,
        "nodes": nodes, "connections": connections, "diameter": diameter,
        "nmse": display.number(candidate.get("nmse")), "score": display.number(candidate.get("score")),
        "source_order": display.count(candidate.get("source_order")),
        "geometry_contract": display.choice(candidate.get("geometry_contract"), (display.GEOMETRY_CONTRACT,)),
        "exclusion_reasons": display.codes(candidate.get("exclusion_reasons"), (
            "CAD_READY_REQUIRED", "B14_PASS_REQUIRED", "TARGET_RANGE_PASS_REQUIRED", "COMPLETE_FINITE_9D_REQUIRED")),
    }
    predictions = candidate.get("predicted_stiffness")
    if isinstance(predictions, dict):
        row["predicted_stiffness"] = {key: _finite_float(predictions[key], "candidate prediction")
                                      for key in display.STIFFNESS_NAMES if key in predictions}
    if candidate.get("relative_density") is not None:
        row["relative_density"] = _finite_float(candidate["relative_density"], "candidate relative density")
    return row


def _sanitize_nodes(value) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise ValueError("candidate nodes are missing")
    nodes = []
    for node in value:
        if not isinstance(node, (list, tuple)) or len(node) != 3:
            raise ValueError("candidate node must contain three coordinates")
        nodes.append([
            _finite_float(coordinate, "candidate node coordinate")
            for coordinate in node
        ])
    return nodes


def _sanitize_connections(value, node_count: int) -> list[list[int]]:
    if not isinstance(value, list) or not value:
        raise ValueError("candidate connections are missing")
    connections = []
    for connection in value:
        if not isinstance(connection, (list, tuple)) or len(connection) != 2:
            raise ValueError("candidate connection must contain two node indexes")
        start, end = connection
        if type(start) is not int or type(end) is not int:
            raise ValueError("candidate connection indexes must be integers")
        if start < 0 or end < 0 or start >= node_count or end >= node_count:
            raise ValueError("candidate connection index is out of range")
        connections.append([start, end])
    return connections


def _finite_float(value, label: str) -> float:
    if display.number(value) is None:
        raise ValueError(f"{label} must be a finite number")
    return value


def _build_selection(job: dict) -> dict:
    selection = job.get("selection")
    if selection is None:
        return {}
    if not isinstance(selection, dict):
        raise ValueError("product selection is invalid")
    value = display.project(selection, {
        "recommended_candidate_id": display.identifier, "selected_candidate_id": display.identifier,
        "selected_rank": display.count, "selection_source": display.enum(*display.SELECTION_SOURCES),
        "selection_sha256": display.digest,
    })
    status = candidate_confirmation_status(job)
    value["confirmation_status"] = ("LEGACY_NOT_RECORDED" if status == "UNCONFIRMED" and job.get("status") == "VIEWER_READY"
                                    else status)
    value["confirmation"] = None
    if status == "CONFIRMED":
        value["confirmation"] = display.project(selection["confirmation"], {
            "schema_version": display.enum("latticegpt_candidate_confirmation_v1"),
            "method": display.enum("explicit_user_selection"), "job_id": display.identifier,
            "candidate_set_schema_version": display.enum("latticegpt_candidate_set_v1", "latticegpt_candidate_set_v2"),
            "selected_candidate_id": display.identifier, "confirmed_at": display.timestamp,
            **dict.fromkeys(("candidate_set_sha256", "selected_candidate_payload_sha256", "confirmation_sha256"), display.digest),
            "action_revision": display.count, "confirmed_revision": display.count,
        })
    return value


def _build_mesh(job: dict, source_mesh_builder: Callable, store: JobStore,
                *, include_original_mesh: bool = False) -> dict | None:
    binding = job.get("mesh_binding")
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValueError("product mesh binding is invalid")
    mesh = normalize_job_mesh_binding(job, verify_stored_file=True)
    from product_workflow.config import require_within
    stored_path = str(require_within(mesh["stored_path"], store.job_directory(job["job_id"]), label="Viewer source mesh"))
    visualization = source_mesh_builder(stored_path)
    if not isinstance(visualization, dict):
        raise ValueError("source mesh visualization payload is invalid")
    visualization = display.mesh_visualization(visualization)
    value = display.project(mesh, {"original_filename": display.filename, "element_type_original": display.enum(*display.ELEMENT_TYPES),
        "element_type_effective": display.enum(*display.ELEMENT_TYPES), "node_count": display.count, "element_count": display.count,
        "units": display.enum("mm"), "frame": display.enum("source_native_cartesian_frame"),
        "precheck_status": display.enum("PASS", "passed", "FAIL", "failed", "HOLD")})
    value.update(status=visualization["status"], sha256=mesh["raw_sha256"], mesh_binding_sha256=mesh["binding_sha256"],
                 visualization=visualization, design_domain=display.design_domain(mesh.get("design_domain")))
    original = mesh.get("original_upload")
    value["original_upload"] = (display.project(original, {"original_filename": display.filename,
        "raw_sha256": display.digest, "raw_size_bytes": display.count,
        "raw_bytes_preserved": lambda x: x if type(x) is bool else None}) if isinstance(original, dict) else None)
    upload = next((u for u in job.get("mesh_uploads", []) if u.get("upload_id") == mesh["upload_id"]), None)
    report = (upload or {}).get("domain_precheck") or (upload or {}).get("precheck") or binding.get("precheck") or {}
    value["quality_summary"] = display.quality_summary(report)


    if include_original_mesh:
        import hashlib
        from product_workflow.viewer_reference import build_original_reference, ReferenceDomainMismatch
        from product_workflow.artifacts import ProductFileIdentityError
        from product_workflow.contracts import PRODUCT_INP_MAX_SIZE_BYTES
        original = mesh.get('original_upload') or {}
        path = require_within((upload or {}).get('stored_path', stored_path), store.job_directory(job['job_id']), label='Viewer original upload')
        raw = path.read_bytes()
        expected = original.get('raw_sha256', mesh['raw_sha256'])
        if len(raw) > PRODUCT_INP_MAX_SIZE_BYTES or hashlib.sha256(raw).hexdigest().upper() != expected:
            raise ProductFileIdentityError('Original upload identity mismatch')
        try:
            reference = build_original_reference(raw, selected_ids=(mesh.get('design_domain') or {}).get('selected_element_ids'))
        except ReferenceDomainMismatch as exc:
            raise ProductFileIdentityError('Original upload and selected domain mismatch') from exc
        except ValueError:
            reference = {'status': 'unavailable', 'message': 'The input mesh cannot be displayed completely. Generated results remain available.'}
        value['original_visualization'] = {**display.mesh_visualization(reference), 'source_sha256': expected,
            'original_filename': display.filename(original.get('original_filename', mesh['original_filename'])),
            'job_id': job['job_id'], 'revision': job['revision'], 'binding_sha256': mesh['binding_sha256']}
    return value


def _build_progress(job: dict, store: JobStore) -> dict:
    attempts = job.get("attempts") or []
    if not isinstance(attempts, list):
        raise ValueError("product attempts are invalid")
    current_stage = str(job.get("status") or "")
    if attempts:
        current_stage = display.choice((attempts[-1] or {}).get("status"), KNOWN_STATUSES) or current_stage
    failure = job.get("failure")
    safe_failure = None
    if failure:
        safe_failure = {
            "stage": display.choice((failure or {}).get("stage"), KNOWN_STATUSES | {"FORMAL_BUNDLE_PREFLIGHT", "MESH_PRECHECK"}),
            "message": "The recorded workflow step failed.",
        }
    return {
        "job_status": job.get("status"),
        "attempt_count": len(attempts),
        "current_stage": current_stage,
        "steps": _build_progress_steps(job, attempts),
        "events": _load_events(store, job["job_id"]),
        "failure": safe_failure,
    }


def _build_progress_steps(job: dict, attempts: list) -> list[dict]:
    status = str(job.get("status") or "")
    current_attempt = attempts[-1] if attempts else {}
    attempt_status = str((current_attempt or {}).get("status") or "")
    failure_stage = str((job.get("failure") or {}).get("stage") or "")

    def step_status(stage: str, complete: bool, active: bool) -> str:
        if failure_stage == stage:
            return "failed"
        if complete:
            return "complete"
        if active:
            return "active"
        return "pending"

    return [
        {
            "key": "precheck",
            "label": "Precheck",
            "status": step_status(
                "MESH_PRECHECK",
                bool(job.get("mesh_binding")),
                status == "MESH_READY",
            ),
        },
        {
            "key": "adapter",
            "label": "Adapter",
            "status": step_status(
                "ADAPTER_RUNNING",
                bool((current_attempt or {}).get("adapter_result")),
                attempt_status == "ADAPTER_RUNNING",
            ),
        },
        {
            "key": "conformal",
            "label": "Conformal generation",
            "status": step_status(
                "CONFORMAL_RUNNING",
                bool((current_attempt or {}).get("conformal_result")),
                attempt_status == "CONFORMAL_RUNNING",
            ),
        },
        {
            "key": "package",
            "label": "Package verification",
            "status": step_status(
                "PACKAGE_VERIFYING",
                _verification_passed((current_attempt or {}).get("package_verification")),
                attempt_status == "PACKAGE_VERIFYING",
            ),
        },
    ]


def _load_events(store: JobStore, job_id: str) -> list[dict]:
    path = store.job_directory(job_id) / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("product job event log is invalid") from exc
        if event.get("job_id") != job_id:
            raise ValueError("product job event identity mismatch")
        events.append({
            "event": display.choice(event.get("event"), PUBLIC_EVENTS),
            "revision": event.get("revision"),
            "status": display.choice(event.get("status"), KNOWN_STATUSES),
            "recorded_at": display.timestamp(event.get("recorded_at")),
        })
    return events


def _build_result(
    job: dict,
    store: JobStore,
    conformal_root: str | Path,
    conformal_payload_builder: Callable | None,
    mesh: dict | None,
) -> dict:
    attempts = job.get("attempts") or []
    if len(attempts) != 1 or not isinstance(attempts[0], dict):
        raise ValueError("VIEWER_READY product job must contain one unique attempt")
    attempt = attempts[0]
    package_id = str(attempt.get("package_id") or "")
    verification = attempt.get("package_verification")
    if (
        not package_id
        or attempt.get("status") != "VIEWER_READY"
        or not _verification_passed(verification)
        or (verification or {}).get("package_id") != package_id
    ):
        raise ValueError("product package identity verification mismatch")
    package = resolve_product_package(store, job["job_id"], conformal_root)
    if package.package_id != package_id:
        raise ValueError("product package resolver identity mismatch")
    from product_viewer_exports import safe_warnings
    from product_viewer_exports import FILES, SCENES, delivery_catalog, endpoint
    metadata = package.metadata
    provided = {}
    if conformal_payload_builder is not None:
        provided = conformal_payload_builder(package)
        if not isinstance(provided, dict) or provided.get("package_id") != package_id:
            raise ValueError("conformal viewer package identity mismatch")
    downloads, deliveries = delivery_catalog(job, package)
    unavailable = [role for role in FILES if role not in downloads]
    scene = {role: endpoint(job, package, "scene", role) for role in SCENES}
    selection = display.mapping(job.get("selection"))
    candidates = display.mapping(job.get("candidate_set")).get("candidates") or []
    selected = next((row for row in candidates if row.get("candidate_id") == selection.get("selected_candidate_id")), {})
    trace = _trace(metadata.get("traceability"), job, selected)
    product = _product_identity(package, job, selected, mesh)
    evidence = _p13(package)
    params = display.project(metadata, {
        "cell_type": display.enum("BCC", "FCC", "Octet", "custom", "CUSTOM"), "diameter": display.number,
        "diameter_unit": display.enum("mm", "m", "µm")})
    source_labels = {display.PHYSICAL_SOURCE: "Normalized unit-cell diameter scaled by the mesh reference length",
                     "user_provided": "User-provided diameter",
                     "bbox_max_dim * 0.03": "3% of the maximum bounding-box dimension"}
    recorded_source = display.choice(metadata.get("diameter_source"), source_labels)
    if recorded_source:
        params["diameter_source"] = source_labels[recorded_source]
    if "diameter_unit" not in params and params.get("diameter") is not None:
        for scale, amount_key, unit_key in ((trace or {}, "physical_diameter", "physical_diameter_units"),
                                            (evidence.get("diameter", {}), "physical_value", "physical_units")):
            if scale.get(amount_key) == params["diameter"] and scale.get(unit_key) == "mm":
                params["diameter_unit"] = "mm"
                break
    source_mesh = display.project(viewer_payloads._build_conformal_input_mesh(metadata), {
        "element_type": display.enum(*display.ELEMENT_TYPES), "node_count": display.count, "element_count": display.count})
    if mesh is None:
        path = resolve_product_package_file(package, "source_mesh")
        wireframe = display.mesh_visualization(conformal_mesh_payloads.build_source_mesh_visualization(path))
    else:
        wireframe = mesh["visualization"]
        for key in ("node_count", "element_count"):
            if key not in source_mesh and key in mesh:
                source_mesh[key] = mesh[key]
        if "element_type" not in source_mesh and "element_type_original" in mesh:
            source_mesh["element_type"] = mesh["element_type_original"]
    core = display.project(viewer_payloads._build_conformal_core_output(metadata, {"b31": downloads.get("conformal_inp")}), {
        "node_count": display.count, "beam_count": display.count})
    for key in ("node_count", "beam_count"):
        fallback = display.count(display.mapping(provided.get("core_output")).get(key))
        if key not in core and fallback is not None:
            core[key] = fallback
    core.update(type="B31_INP", role="conformal design engineering output", download_url=downloads.get("conformal_inp"))
    visualization = {"role": "derived visualization output"}
    mechanical = {"candidate_adapter_status": (trace or {}).get("adapter_status", "not_validated"),
                  "candidate_id": (trace or {}).get("candidate_id"), "candidate_graph": None,
                  "candidate_stl_url": None, "candidate_png_url": None,
                  "evidence_status": "traceability carried from adapter metadata" if trace else "future / not connected"}
    return {
        "status": "success", "mode": "conformal", "viewer_contract_version": "dual_space_v1",
        "package_id": package_id, "space_type": "geometric_space",
        "input_mesh": source_mesh, "core_output": core, "visualization": visualization, "downloads": downloads,
        "parameters": params, "traceability": trace, "product_identity": product, "mechanical_space": mechanical,
        "geometric_space": {"source_mesh": source_mesh, "source_mesh_visualization": wireframe,
                            "b31_visualization": {"status": "future", "representation": "beam_graph"},
                            "stl_png": visualization},
        "fusion_workflow": viewer_payloads._build_fusion_workflow(), "p13_evidence": evidence,
        "warnings": safe_warnings(metadata, package.package_metadata),
        "compatibility": display.compatibility(package.compatibility), "unavailable_downloads": unavailable,
        "scene": scene, "download_identities": deliveries,
    }


def _build_file_identities(package, deliveries):
    """Keep the six-file shape only when the resolver verified a sealed six-file receipt."""
    records = package.file_identities
    if records is None:
        return None
    if not isinstance(records, dict) or set(records) != set(LEGACY_FILE_KEYS):
        raise ValueError("product display file roles are invalid")
    result = {}
    for role in LEGACY_FILE_KEYS:
        record = records[role]
        if (not isinstance(record, dict) or record.get("path") != PRODUCT_FILE_MAP[role].as_posix()
                or display.count(record.get("size_bytes")) is None or not display.digest(record.get("sha256"))):
            raise ValueError("product display file identity is invalid")
        if role in deliveries:
            item = deliveries[role]
            result[role] = {"filename": item["filename"], "size_bytes": item["size_bytes"], "sha256": item["sha256"]}
    return result


def _p13(package):
    if package.package_metadata is None:
        return display.p13_evidence({}, package)
    # Retain the original identity, row-count and disposition checks before projection.
    paths = {key: resolve_product_package_file(package, key)
             for key in ("package_metadata", "geometry_ledger", "source_element_map")}
    value = viewer_payloads._build_p13_evidence(package_id=package.package_id, p13_paths=paths,
                                                b31_path=resolve_product_package_file(package, "b31"))
    return display.p13_evidence(value, package)


def _trace(value, job, selected):
    if not isinstance(value, dict):
        return None
    selection = display.mapping(job.get("selection"))
    expected = {"job_id": job["job_id"], "candidate_id": selected.get("candidate_id"),
                "selected_candidate_id": selection.get("selected_candidate_id"),
                "source_candidate_id": selected.get("source_candidate_id", selected.get("candidate_id")),
                "recommended_candidate_id": selection.get("recommended_candidate_id")}
    public = {key: identity for key, identity in expected.items()
              if display.identifier(identity) and value.get(key) == identity}
    public.update(display.project(value, {
        **dict.fromkeys(("target_sha256", "selection_manifest_sha256", "case_manifest_sha256", "candidate_manifest_sha256",
                         "run_manifest_sha256", "adapter_metadata_sha256", "custom_cell_sha256"), display.digest),
        "selected_rank": display.count, "selection_source": display.enum(*display.SELECTION_SOURCES),
        "adapter_status": display.enum("static_validated", "not_validated"),
        "mechanical_match_status": display.enum("NOT_VALIDATED"),
        **dict.fromkeys(("normalized_diameter", "reference_length", "physical_diameter"), display.number),
        "physical_diameter_units": display.enum("mm"), "non_claims": lambda x: display.codes(x, display.KNOWN_CLAIMS),
    }))
    return public


def _product_identity(package, job, selected, mesh):
    metadata = package.metadata
    context = metadata.get("product_context")
    if not isinstance(context, dict):
        return None
    binding = package.mesh_view
    if binding is None:
        binding = display.mapping(metadata.get("product_mesh_binding"))
        binding_sha = binding.get("mesh_binding_sha256")
    else:
        binding_sha = binding.get("binding_sha256")
    trace = display.mapping(metadata.get("traceability"))
    target = display.mapping(context.get("target"))
    # Apply the original shared builder's identity checks to the full inputs before projecting them.
    if (context.get("schema_version") != "latticegpt_product_viewer_context_v1"
            or not binding or context.get("mesh_binding_sha256") != binding_sha
            or context.get("selection_sha256") != trace.get("selection_manifest_sha256")
            or target.get("target_sha256") != trace.get("target_sha256")):
        raise ValueError("product Viewer context identity mismatch")
    # Several legacy producer fields were optional. Only emit their recorded values if
    # they agree with the already validated Job and source, including the selected 9D vector.
    selection = display.mapping(job.get("selection"))
    public_mesh = display.mapping(mesh)
    confirmed = {
        "selected_candidate_id": display.identifier(selection.get("selected_candidate_id")),
        "recommended_candidate_id": display.identifier(selection.get("recommended_candidate_id")),
        "selected_rank": display.count(selection.get("selected_rank")),
        "selection_source": display.choice(selection.get("selection_source"), display.SELECTION_SOURCES),
        "mesh_original_filename": public_mesh.get("original_filename"),
        "candidate_prediction_9d_gpa": display.nine(selected.get("predicted_stiffness")),
    }
    projected = {key: expected for key, expected in confirmed.items()
                 if expected is not None and key in context and context[key] == expected}
    projected.update(display.project(context, dict.fromkeys(("selection_sha256", "mesh_binding_sha256", "mesh_sha256"), display.digest)))
    job_target = display.mapping(job.get("target"))
    public_target = display.project(target, {"target_sha256": display.digest})
    for key, reader in (("source", _target_source), ("physical_9d_gpa", display.nine), ("normalized_9d", display.nine)):
        if key in target and job_target.get(key) == target[key]:
            public_target[key] = reader(target[key])
    projected.update(schema_version="latticegpt_product_viewer_context_v1", job_id=package.job_id, package_id=package.package_id,
        target=public_target,
        mesh=display.project(mesh, {"original_filename": display.filename, "sha256": display.digest,
            "element_type_original": display.enum(*display.ELEMENT_TYPES), "element_type_effective": display.enum(*display.ELEMENT_TYPES)}))
    projected["mesh"]["binding_sha256"] = display.digest(binding_sha)
    return projected


def _verification_passed(verification) -> bool:
    return (
        isinstance(verification, dict)
        and verification.get("status") in {"passed", "success", "complete"}
    )


def _build_dual_space_status(job: dict, *, verified_result: bool) -> dict:
    mechanical_status = (
        "CANDIDATES_RECORDED"
        if isinstance(job.get("candidate_set"), dict)
        else "TARGET_RECORDED"
    )
    if verified_result:
        geometric_status = "GEOMETRIC_OUTPUT_RECORDED"
        fusion_status = "TRACEABLE_BRIDGE_RECORDED"
        output_status = "READONLY_RESULT_AVAILABLE"
    else:
        mesh_recorded = isinstance(job.get("mesh_binding"), dict)
        geometric_status = (
            "SOURCE_MESH_RECORDED" if mesh_recorded else "NOT_YET_RECORDED"
        )
        fusion_status = (
            "AWAITING_EXPLICIT_AUTHORIZATION"
            if mesh_recorded
            else "NOT_YET_RECORDED"
        )
        output_status = "READONLY_JOB_STATE_AVAILABLE"
    return {
        "schema_version": "latticegpt_v1_dual_space_status_v1",
        "mainline": "LLM-assisted dual-space generative design for bone implants",
        "mechanical_space": {"status": mechanical_status},
        "geometric_space": {"status": geometric_status},
        "fusion_bridge": {
            "status": fusion_status,
            "full_fusion_validation": "NOT_VALIDATED",
        },
        "output_and_visualization": {"status": output_status},
        "non_claims": ["NOT_FULL_DUAL_SPACE_FUSION"],
    }


def _build_non_claims(job: dict) -> list[str]:
    candidate_set = job.get("candidate_set") or {}
    values = display.codes(candidate_set.get("non_claims"), display.KNOWN_CLAIMS)

    domain_claims = (["NOT_FULL_BONE_COVERAGE", "NOT_EXCLUDED_ELEMENT_MAPPING", "NOT_SHELL_MECHANICAL_VALIDATION"]
                     if (job.get("mesh_binding") or {}).get("schema_version") == "latticegpt_mesh_binding_v3" else [])
    return list(dict.fromkeys([*values, *FIXED_NON_CLAIMS, *domain_claims]))
