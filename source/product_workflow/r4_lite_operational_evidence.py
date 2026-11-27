"""Read-only R4-Lite derivation of R3 comparison layers from real Job bytes."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .job_evidence import verify_job
from .manifest_io import canonical_json_object_v1, sha256_bytes, sha256_file
from .reproducibility import build_deterministic_projection_v1
from .store import JobStore


def _selected(job: Mapping[str, Any]) -> Mapping[str, Any]:
    selected_id = job["selection"]["selected_candidate_id"]
    return next(row for row in job["candidate_set"]["candidates"] if row["candidate_id"] == selected_id)


def derive_verified_r3_layers_v1(
    runtime_root: str | Path,
    job_id: str,
    expected_active_identity: Mapping[str, object],
) -> dict[str, Any]:
    """Derive four R3 layers without accepting caller-supplied evidence fields."""
    root = Path(runtime_root).resolve(strict=True)
    verification = verify_job(root, job_id, expected_active_identity=expected_active_identity)
    if verification.get("status") != "PASS" or verification.get("terminal_evidence") != "SUCCESS":
        raise ValueError("R3 layers require a verified successful terminal Job")
    store = JobStore(root)
    job = store.get_consistent(job_id)
    if job.get("schema_version") not in {"latticegpt_design_job_v2", "latticegpt_design_job_v3"}:
        raise ValueError("R4-Lite layered evidence requires an unchanged R3 v2 or v3 Job")
    events_path = store.job_directory(job_id) / "events.jsonl"
    job_path = store.job_directory(job_id) / "job.json"
    upload_path = Path(job["mesh_uploads"][-1]["stored_path"]).resolve(strict=True)
    try:
        upload_path.relative_to(store.job_directory(job_id))
    except ValueError as exc:
        raise ValueError("layer derivation mesh bytes must remain inside the Job directory") from exc
    attempt = job["attempts"][0]
    package_dir = Path(attempt.get("conformal_result", {}).get("package_dir", attempt["conformal_output_dir"])).resolve(strict=True)
    expected_package_dir = root / "conformal" / job_id / attempt["package_id"]
    if package_dir != expected_package_dir.resolve(strict=True):
        raise ValueError("layer derivation package path mismatch")
    package_metadata_path = package_dir / "package_metadata.json"
    package_metadata_bytes = package_metadata_path.read_bytes()

    from .gate7 import _numeric_input, _relationship_evidence

    mesh_bytes = upload_path.read_bytes()
    numeric = _numeric_input(job, mesh_bytes)
    selected = _selected(job)
    profile = job["reproduction"]["profile"]
    identities = profile["identities"]
    controlled = {
        "source_manifest_sha256": identities["source_manifest_sha256"],
        "artifact_manifest_sha256": identities["artifact_manifest_sha256"],
        "environment_manifest_sha256": identities["environment_manifest_sha256"],
        "formal_bundle_binding_sha256": identities["formal_bundle_binding_sha256"],
        "reproduction_profile_sha256": job["reproduction"]["profile_sha256"],
        "comparison_contract_sha256": job["reproduction"]["comparison_contract_sha256"],
        "tolerance_schema_path": profile["tolerance"]["schema_path"],
        "tolerance_schema_sha256": profile["tolerance"]["schema_sha256"],
    }
    package_inventory_sha = sha256_bytes(canonical_json_object_v1({"files": verification["package_inventory"]}))
    result_content_sha = sha256_bytes(package_metadata_bytes)
    reservation = job["conformal_reservation"]
    operational = {
        "schema_version": "latticegpt.r4_lite_derived_operational_evidence.v1",
        "run": {
            "job_id": job_id,
            "upload_id": reservation["upload_id"],
            "reservation_id": reservation["reservation_id"],
            "attempt_id": reservation["attempt_id"],
            "package_id": reservation["package_id"],
        },
        "candidate": {"candidate_id": selected["candidate_id"]},
        "mesh": {"mesh_binding_sha256": job["mesh_binding"]["digests"]["mesh_binding_sha256"]},
        "package": {"result_content_sha256": result_content_sha, "relationship_integrity_status": "PASS"},
        "byte_bindings": {
            "job_snapshot_sha256": sha256_file(job_path),
            "events_sha256": sha256_file(events_path),
            "mesh_sha256": sha256_bytes(mesh_bytes),
            "package_metadata_sha256": sha256_bytes(package_metadata_bytes),
            "package_inventory_sha256": package_inventory_sha,
        },
    }
    operational_sha = sha256_bytes(canonical_json_object_v1(operational))
    relationship = _relationship_evidence(job=job, manifest={**operational, "operational_manifest_sha256": operational_sha}, result_content_sha256=result_content_sha)
    target_sha = str(job["target"].get("target_sha256") or job["target"].get("proposal_sha256"))
    binding = job["mesh_binding"]
    canonical_b31_sha = next(row["sha256"] for row in verification["package_inventory"] if row["relative_path"] == "core/conformal_b31.inp")
    projection = build_deterministic_projection_v1(
        controlled_identities=controlled,
        frozen_input={
            "replay_start": profile["llm"]["replay_start"],
            "query_plan_sha256": target_sha,
            "target_sha256": target_sha,
            "random_seed": profile["random_seed"],
            "execution_profile": profile["device"]["execution_profile"],
            "dtype": profile["numeric"]["dtype"],
        },
        candidate={
            "candidate_id": selected["candidate_id"],
            "candidate_order": int(selected.get("candidate_order", selected.get("rank", 1) - 1)),
            "eligibility": "ELIGIBLE",
            "rank": int(selected["rank"]),
            "candidate_topology_sha256": sha256_bytes(canonical_json_object_v1({"nodes": selected["nodes"], "connections": selected["connections"]})),
            "cad_selection": {"policy": "DIAMETER_DESC_CANDIDATE_ID_ASC", "selected_candidate_id": selected["candidate_id"]},
        },
        mesh={
            "raw_size_bytes": len(mesh_bytes),
            "raw_sha256": sha256_bytes(mesh_bytes),
            "mesh_semantic_sha256": binding["digests"]["mesh_semantic_sha256"],
            "design_domain_semantic_sha256": binding["design_domain"]["semantic_sha256"],
            "canonical_b31_edges_sha256": canonical_b31_sha,
        },
        package={
            "package_schema_version": "latticegpt_9x_package_metadata_v1",
            "result_schema_version": "latticegpt_operational_result_content_v1",
        },
        non_claims=["NOT_FORMAL_150W_PROVENANCE", "TECHNICAL_REFERENCE_ONLY"],
    )
    layers = {
        "schema_version": "latticegpt.r4_lite_derived_r3_layers.v1",
        "comparison_contract_sha256": job["reproduction"]["comparison_contract_sha256"],
        "operational_evidence_sha256": operational_sha,
        "projection": projection,
        "numeric": numeric,
        "relationship": relationship,
        "derivation_bindings": deepcopy(operational["byte_bindings"]),
    }
    return {**verification, "active_identity": deepcopy(dict(expected_active_identity)), "r3_layers": layers}


__all__ = ["derive_verified_r3_layers_v1"]
