from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from latticegpt_9x_mesh_precheck import (
    WHOLE_FILE_TECHNICAL_DOMAIN,
    WHOLE_FILE_TECHNICAL_PURPOSE,
    WHOLE_FILE_SELECTOR_VALUE,
    _parse_inp,
    precheck_inp_mesh,
    precheck_technical_fixture_record,
)


PACKAGE_SCHEMA_VERSION = "latticegpt_9x_package_metadata_v1"
GEOMETRY_LEDGER_SCHEMA_VERSION = "latticegpt_9x_geometry_ledger_v1"
SOURCE_MAP_SCHEMA_VERSION = "latticegpt_9x_source_element_map_v1"
STABLE_NODE_SET = "NSET_IMPLANT_ALL"
STABLE_ELEMENT_SET = "ELSET_IMPLANT_B31_ALL"
CONDITIONAL_INTERFACE_SET = "NSET_IMPLANT_INTERFACE"
PHYSICAL_SCALE_FORMULA = "P11_AVERAGE_EQUIVALENT_SCALE_V1"
REQUIRED_NON_CLAIMS = [
    "TECHNICAL_VALIDATION_ONLY",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
    "NOT_REAL_BONE_DEFECT_VALIDATION",
    "NOT_MECHANICAL_PASS",
    "NOT_CURRENT_FORMAL_150W_PROVENANCE",
    "NOT_FULL_DUAL_SPACE_FUSION_COMPLETION",
    "NOT_FEA_VALIDATION",
]
FORMAL_CASE_NON_CLAIMS = [
    "NOT_REAL_PATIENT_OR_REAL_BONE_DEFECT_VALIDATION",
    "NOT_MECHANICAL_PASS",
    "NOT_FEA_VALIDATION",
    "NOT_MANUFACTURING_OR_PRINT_READY",
    "NOT_CLINICAL_VALIDATION",
    "NOT_FULL_DUAL_SPACE_FUSION_COMPLETION",
    "NOT_GATE_A5_PASS",
]
BOUNDED_CASE_NON_CLAIMS = [
    "NOT_REAL_PATIENT_VALIDATION",
    "NOT_MECHANICAL_PASS",
    "NOT_FEA_VALIDATION",
    "NOT_MANUFACTURING_OR_PRINT_READY",
    "NOT_CLINICAL_VALIDATION",
    "NOT_FULL_DUAL_SPACE_FUSION_COMPLETION",
    "NOT_GENERALIZATION_TO_OTHER_ANATOMIES_OR_MESHES",
    "NOT_GATE_B5_PASS",
]
P10_SELECTION_POLICY = "scaler_range_weighted_RMSE_v1"
P10_REQUIRED_TRACEABILITY_FIELDS = (
    "selection_policy",
    "selection_authority",
    "p10_ranking_manifest_sha256",
    "p10_schema_version",
    "p10_evidence_level",
    "p10_ranking_status",
    "p10_selected_status",
    "p09_status",
    "mechanical_match_status",
    "ood_status",
    "diameter_used_in_ranking",
    "non_claims",
)
PRODUCT_SELECTION_POLICY = "product_candidate_selection_v1"
PRODUCT_SELECTION_POLICIES = {PRODUCT_SELECTION_POLICY, "product_candidate_selection_v2"}
PRODUCT_REQUIRED_TRACEABILITY_FIELDS = (
    "selection_policy",
    "job_id",
    "selection_manifest_sha256",
    "recommended_candidate_id",
    "selected_candidate_id",
    "selected_rank",
    "selection_source",
    "p09_status",
    "mechanical_match_status",
    "diameter_used_in_ranking",
    "non_claims",
)
PRODUCT_REQUIRED_NON_CLAIMS = {
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_FEA_VALIDATION",
}
P10_REQUIRED_NON_CLAIMS = {
    "TECHNICAL_VALIDATION_ONLY",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_CURRENT_FORMAL_150W_PROVENANCE",
    "NOT_ORIGINAL_FULL_6X6",
}

_HEX8_FACE_INDEXES = (
    (0, 1, 2, 3),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7),
)


def precheck_latticegpt_9x_case_manifest(
    case_manifest_path: str | Path,
    *,
    source_mesh_path: str | Path | None = None,
    expected_case_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a formal 9.x-A or bounded 9.x-B case before conformal runtime."""

    path = Path(case_manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError("case manifest not found")
    manifest_sha256 = _sha256(path)
    if (
        expected_case_manifest_sha256 is not None
        and manifest_sha256 != str(expected_case_manifest_sha256).upper()
    ):
        raise ValueError("case manifest SHA256 does not match adapter binding")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("case manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("case manifest must be a JSON object")
    if manifest.get("schema_version") != "latticegpt_9x_case_manifest_v1":
        raise ValueError("unsupported formal case manifest schema")

    case = manifest.get("case")
    source = manifest.get("source")
    mesh = manifest.get("mesh_contract")
    domain = manifest.get("design_domain")
    applicability = manifest.get("applicability")
    for label, value in (
        ("case", case),
        ("source", source),
        ("mesh_contract", mesh),
        ("design_domain", domain),
        ("applicability", applicability),
    ):
        if not isinstance(value, dict):
            raise ValueError(f"case manifest {label} must be an object")
    gate_lane = None
    if case.get("case_status") == "approved" and case.get("gate_A1a") == "PASS":
        gate_lane = "9.x-A"
    elif (
        case.get("case_status")
        == "approved_for_9x_B_gates_under_separate_authorizations"
        and case.get("gate_B1a") == "PASS"
    ):
        gate_lane = "9.x-B"
    else:
        raise ValueError(
            "formal case identity must be approved at Gate A1a or Gate B1a"
        )
    if not case.get("case_id"):
        raise ValueError("formal case_id is required")

    manifest_source_path = Path(source.get("path", "")).resolve()
    if source_mesh_path is not None:
        requested_source_path = Path(source_mesh_path).resolve()
        if not requested_source_path.is_file():
            raise ValueError("runtime source mesh is not a regular file")
        if (
            _sha256(requested_source_path) != str(source.get("sha256") or "").upper()
            or requested_source_path.stat().st_size != source.get("size_bytes")
        ):
            raise ValueError("case manifest source identity does not match runtime source mesh")
    if mesh.get("units") != "mm":
        raise ValueError("formal case mesh units must be mm")
    if mesh.get("frame") != "source_native_cartesian_frame":
        raise ValueError("formal case mesh frame is not supported")
    if mesh.get("frame_transform") != "identity":
        raise ValueError("formal case frame transform must be identity")
    if mesh.get("rotation") != "none" or mesh.get("reflection") != "none":
        raise ValueError("formal case rotation and reflection must be none")
    element_types = mesh.get("element_types_original")
    if element_types not in (["C3D8"], ["C3D8R"]):
        raise ValueError(
            "formal conformal runtime requires exactly one C3D8 or C3D8R source type"
        )
    original_element_type = element_types[0]
    effective_element_type = (
        "C3D8"
        if original_element_type == "C3D8"
        else "C3D8_GEOMETRY_COMPATIBLE"
    )
    if mesh.get("quality_contract_version") != "P12_PROJECT_TOLERANCE_V1":
        raise ValueError("formal case quality contract is not supported")

    if domain.get("selector_type") != "whole_file_implant_domain":
        raise ValueError("formal case requires whole_file_implant_domain")
    if domain.get("semantic_role") != "implant_lattice_fill_domain":
        raise ValueError("formal case design-domain semantic role is invalid")
    if domain.get("fallback_to_all_elements") is not False:
        raise ValueError("formal case design domain must not use fallback selection")
    if domain.get("non_design_regions") != []:
        raise ValueError("formal whole-file case must contain no non-design regions")
    if domain.get("non_design_regions_statement") != "none_in_file":
        raise ValueError("formal case non-design-region statement is invalid")

    mesh_precheck = precheck_inp_mesh(
        manifest_source_path,
        expected_sha256=source.get("sha256"),
        expected_size_bytes=source.get("size_bytes"),
        expected_node_count=mesh.get("expected_node_count"),
        expected_element_count=mesh.get("expected_element_count"),
        expected_element_type_original=original_element_type,
        expected_element_type_effective=effective_element_type,
        selector_type=WHOLE_FILE_TECHNICAL_DOMAIN,
        selector_value=WHOLE_FILE_SELECTOR_VALUE,
        expected_selected_element_count=domain.get(
            "expected_selected_element_count"
        ),
        selector_explicit_declaration=True,
        selector_purpose=WHOLE_FILE_TECHNICAL_PURPOSE,
        expected_selected_element_ids_sha256=domain.get(
            "expected_selected_element_ids_sha256"
        ),
    )
    if mesh_precheck.get("status") != "passed":
        raise ValueError(
            "formal case mesh precheck failed: "
            + "; ".join(mesh_precheck.get("errors") or [])
        )
    selected = mesh_precheck["design_domain"]
    mesh_precheck["design_domain"] = {
        "semantic_role": domain["semantic_role"],
        "selector_type": domain["selector_type"],
        "selector_value": domain.get("selector_value"),
        "selected_element_count": selected["selected_element_count"],
        "selected_element_ids_sha256": selected[
            "selected_element_ids_sha256"
        ],
        "selection_is_explicit": True,
        "selection_source": "FORMAL_CASE_MANIFEST_WHOLE_FILE_DECLARATION",
        "fallback_to_all_elements": False,
        "non_design_regions": [],
    }
    mesh_precheck["case_manifest"] = {
        "path": str(path),
        "sha256": manifest_sha256,
        "case_id": case["case_id"],
        "case_class": case.get("case_class"),
        "permitted_use": applicability.get("allowed_9x_use"),
        "gate_lane": gate_lane,
    }
    mesh_precheck["formal_case"] = True
    return mesh_precheck


def finalize_latticegpt_9x_package(
    *,
    package_dir: str | Path,
    package_id: str,
    source_mesh_path: str | Path,
    traceability: dict[str, Any] | None,
    technical_fixture_record_path: str | Path | None = None,
    case_manifest_path: str | Path | None = None,
    source_mesh_contract: dict[str, str] | None = None,
    expected_case_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Add the approved P-13 handoff contract to a bounded 9.x package.

    This layer records source-element identity and selected-domain membership.
    It does not invent a per-beam source-element lineage that the current
    conformal core does not produce.
    """

    resolved_package_dir = Path(package_dir).resolve()
    if not resolved_package_dir.is_dir():
        raise FileNotFoundError("package directory not found")
    if not isinstance(package_id, str) or not package_id:
        raise ValueError("package_id is required")

    source_path = Path(source_mesh_path).resolve()
    if (technical_fixture_record_path is None) == (case_manifest_path is None):
        raise ValueError(
            "exactly one of technical_fixture_record_path or case_manifest_path is required"
        )
    fixture_record_path = (
        Path(technical_fixture_record_path).resolve()
        if technical_fixture_record_path is not None
        else None
    )
    formal_case_manifest_path = (
        Path(case_manifest_path).resolve() if case_manifest_path is not None else None
    )
    source_copy_path = resolved_package_dir / "input" / "source_c3d8.inp"
    b31_path = resolved_package_dir / "core" / "conformal_b31.inp"
    stl_path = resolved_package_dir / "visualization" / "conformal_preview.stl"
    png_path = resolved_package_dir / "visualization" / "conformal_preview.png"
    legacy_metadata_path = resolved_package_dir / "metadata.json"
    run_summary_path = resolved_package_dir / "run_summary.txt"
    required_paths = (
        source_path,
        source_copy_path,
        b31_path,
        stl_path,
        png_path,
        legacy_metadata_path,
        run_summary_path,
    )
    required_paths = required_paths + (
        formal_case_manifest_path
        if formal_case_manifest_path is not None
        else fixture_record_path,
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("required package input missing: " + ", ".join(missing))

    is_formal_case = formal_case_manifest_path is not None
    if is_formal_case:
        case_precheck = precheck_latticegpt_9x_case_manifest(
            formal_case_manifest_path,
            source_mesh_path=source_path,
            expected_case_manifest_sha256=(
                expected_case_manifest_sha256
                or (traceability or {}).get("case_manifest_sha256")
            ),
        )
    else:
        case_precheck = precheck_technical_fixture_record(fixture_record_path)
        if case_precheck.get("status") != "passed":
            raise ValueError("technical fixture precheck must pass")
    prechecked_source = Path(case_precheck["source"]["path"]).resolve()
    if not prechecked_source.is_file():
        raise ValueError("case record source path is not a regular file")
    source_sha256 = _sha256(source_path)
    if case_precheck["source"]["sha256"] != source_sha256:
        raise ValueError("case record source SHA256 does not match runtime source mesh")
    if _sha256(source_copy_path) != source_sha256:
        raise ValueError("package source mesh copy does not match runtime source mesh")

    geometry_quality = case_precheck.get("geometry_quality", {})
    if geometry_quality.get("status") != "PASS":
        raise ValueError("source geometry quality must PASS before package finalization")
    design_domain = case_precheck.get("design_domain", {})
    if not design_domain.get("selection_is_explicit"):
        raise ValueError("design domain must be explicitly selected")

    legacy_metadata = json.loads(legacy_metadata_path.read_text(encoding="utf-8"))
    if legacy_metadata.get("result_id") != package_id:
        raise ValueError("legacy metadata result_id does not match package_id")
    expected_original_type = case_precheck["mesh"]["element_type_original"]
    expected_effective_type = case_precheck["mesh"]["element_type_effective"]
    if source_mesh_contract is not None:
        expected_contract = {
            "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
            "element_type_original": expected_original_type,
            "element_type_effective": expected_effective_type,
        }
        if source_mesh_contract != expected_contract:
            raise ValueError(
                "runtime source mesh contract does not match package precheck"
            )
    legacy_type_identity = {
        "source_element_type": legacy_metadata.get("source_element_type"),
        "source_element_type_original": legacy_metadata.get(
            "source_element_type_original"
        ),
        "source_element_type_effective": legacy_metadata.get(
            "source_element_type_effective"
        ),
    }
    if (
        legacy_type_identity["source_element_type"] != expected_original_type
        or legacy_type_identity["source_element_type_original"]
        != expected_original_type
        or legacy_type_identity["source_element_type_effective"]
        != expected_effective_type
    ):
        raise ValueError(
            "legacy metadata source element type identity does not match "
            "the prechecked source mesh: "
            f"metadata={legacy_type_identity}, "
            f"precheck={expected_original_type}/{expected_effective_type}"
        )

    trace = dict(traceability or {})
    physical_scale = _validated_physical_scale(trace)
    mechanical_source, candidate_selection = _mechanical_source_from_traceability(
        trace
    )
    parsed_source = _parse_inp(source_copy_path)
    if parsed_source.get("errors"):
        raise ValueError("source mesh parse failed: " + "; ".join(parsed_source["errors"]))
    selected_element_ids = _selected_element_ids(parsed_source, design_domain)
    selected_digest = _digest_ids(selected_element_ids)
    if selected_digest != design_domain.get("selected_element_ids_sha256"):
        raise ValueError("selected source element identity does not match precheck")

    b31_text = b31_path.read_text(encoding="utf-8")
    b31_analysis = _analyze_b31(b31_text)
    required_zero_counts = b31_analysis["required_zero_counts"]
    nonzero_checks = {
        name: count for name, count in required_zero_counts.items() if count != 0
    }
    if nonzero_checks:
        raise ValueError(f"B31 required-zero checks failed: {nonzero_checks}")

    source_component_count = _source_component_count(
        parsed_source["elements"], selected_element_ids
    )
    output_component_count = b31_analysis["connected_component_count"]
    if output_component_count != source_component_count:
        raise ValueError(
            "output component count does not match source design-domain component count"
        )

    if _contains_named_set(b31_text, "NSET", STABLE_NODE_SET):
        raise ValueError(f"stable set already exists: {STABLE_NODE_SET}")
    if _contains_named_set(b31_text, "ELSET", STABLE_ELEMENT_SET):
        raise ValueError(f"stable set already exists: {STABLE_ELEMENT_SET}")

    b31_with_sets = _append_stable_sets(
        b31_text,
        sorted(b31_analysis["nodes"]),
        sorted(b31_analysis["elements"]),
    )
    source_map_text = _build_source_map_csv(
        parsed_source["elements"],
        selected_element_ids,
        case_precheck["mesh"]["element_type_effective"],
    )
    b31_sha256 = _sha256_bytes(b31_with_sets.encode("utf-8"))
    source_map_sha256 = _sha256_bytes(source_map_text.encode("utf-8"))

    if is_formal_case:
        formal_gate_lane = case_precheck["case_manifest"]["gate_lane"]
        evidence_level = "FORMAL_ENGINEERING_CASE"
        case_record = case_precheck["case_manifest"]
        if formal_gate_lane == "9.x-B":
            evidence_level = "BOUNDED_REAL_CASE"
            package_status = "PASS_GATE_B4B"
            actual_stage = "9.x-B-GATE-B4b-CONFORMAL-RUNTIME"
            actual_gate = "Gate_B4b"
            non_claims = list(BOUNDED_CASE_NON_CLAIMS)
            formal_gate_disposition = "PASS_GATE_B4B_GATE_B5_NOT_EVALUATED"
            formal_gate_effect = "Gate_B4b_RUNTIME_ONLY"
            allowed_claims = [
                "Gate B4b bounded conformal runtime completed for the bound anatomical case",
                "P-13 bounded-case package contract was generated",
                "source design-domain identity was recorded",
            ]
        else:
            package_status = "PASS_GATE_A4B"
            actual_stage = "9.x-A-GATE-A4b-CONFORMAL-RUNTIME"
            actual_gate = "Gate_A4b"
            non_claims = list(FORMAL_CASE_NON_CLAIMS)
            formal_gate_disposition = "PASS_GATE_A4B_GATE_A5_NOT_EVALUATED"
            formal_gate_effect = "Gate_A4b_RUNTIME_ONLY"
            allowed_claims = [
                "Gate A4b bounded conformal runtime completed for the bound engineering case",
                "P-13 formal engineering-case package contract was generated",
                "source design-domain identity was recorded",
            ]
        case_metadata = {
            "case_id": case_record["case_id"],
            "classification": case_record.get("case_class"),
            "case_manifest_path": case_record["path"],
            "case_manifest_sha256": case_record["sha256"],
            "permitted_use": case_record.get("permitted_use"),
            "formal_gate_effect": formal_gate_effect,
        }
    else:
        evidence_level = "TECHNICAL_FIXTURE"
        package_status = "PASS_TECHNICAL_VALIDATION_ONLY"
        actual_stage = "9.x-TECHNICAL-FIXTURE-PACKAGE"
        actual_gate = "TECHNICAL_FIXTURE_LANE_NOT_FORMAL_GATE"
        non_claims = list(REQUIRED_NON_CLAIMS)
        formal_gate_disposition = "NOT_EVALUATED"
        fixture_record = case_precheck.get("fixture_record", {})
        case_metadata = {
            "fixture_id": fixture_record.get("fixture_id"),
            "classification": fixture_record.get("classification"),
            "fixture_record_path": str(fixture_record_path),
            "fixture_record_sha256": _sha256(fixture_record_path),
            "permitted_use": fixture_record.get("permitted_use"),
            "formal_gate_effect": "NONE",
        }
        allowed_claims = [
            "P-13 technical-fixture package contract was generated",
            "P-12 geometry checks recorded in a technical ledger passed",
            "source design-domain identity was recorded",
        ]

    geometry_ledger = {
        "schema_version": GEOMETRY_LEDGER_SCHEMA_VERSION,
        "contract": "PROJECT_TOLERANCE_V1",
        "evidence_level": evidence_level,
        "technical_disposition": "PASS",
        "formal_gate_disposition": formal_gate_disposition,
        "source_geometry": {
            **geometry_quality,
            "design_domain_component_count": source_component_count,
        },
        "output_geometry": {
            "element_type": "B31",
            "node_count": b31_analysis["node_count"],
            "beam_count": b31_analysis["beam_count"],
            "bbox": b31_analysis["bbox"],
            "beam_length": b31_analysis["beam_length"],
            "connected_component_count": output_component_count,
        },
        "required_zero_counts": required_zero_counts,
        "component_rule": {
            "contract": "output_components_equal_source_design_domain_components",
            "source_count": source_component_count,
            "output_count": output_component_count,
            "status": "PASS",
        },
        "stable_sets": {
            STABLE_NODE_SET: len(b31_analysis["nodes"]),
            STABLE_ELEMENT_SET: len(b31_analysis["elements"]),
            CONDITIONAL_INTERFACE_SET: "NOT_PROVIDED_NOT_INFERRED",
        },
        "source_element_map": {
            "schema_version": SOURCE_MAP_SCHEMA_VERSION,
            "mapping_scope": "ORIGINAL_TO_SELECTED_DESIGN_DOMAIN_IDENTITY",
            "beam_origin_mapping_status": "NOT_RECORDED_BY_CURRENT_CORE",
            "sha256": source_map_sha256,
        },
        "non_claims": non_claims,
    }
    geometry_ledger_text = json.dumps(
        geometry_ledger, ensure_ascii=False, indent=2
    ) + "\n"
    geometry_ledger_sha256 = _sha256_bytes(geometry_ledger_text.encode("utf-8"))

    package_metadata = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package": {
            "package_id": package_id,
            "result_id": package_id,
            "actual_stage": actual_stage,
            "actual_gate": actual_gate,
            "evidence_level": evidence_level,
            "status": package_status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "case": case_metadata,
        "producer": {
            "entrypoint": "conformal_service.run_conformal_service",
            "contract_finalizer": (
                "latticegpt_9x_package_contract.finalize_latticegpt_9x_package"
            ),
            "contract_finalizer_sha256": _sha256(Path(__file__).resolve()),
        },
        "mechanical_source": mechanical_source,
        "candidate": {
            "task_id": trace.get("task_id"),
            "run_id": trace.get("run_id"),
            "candidate_id": trace.get("candidate_id"),
            "source_candidate_id": trace.get("source_candidate_id"),
            "candidate_manifest_sha256": trace.get("candidate_manifest_sha256"),
            "run_manifest_sha256": trace.get("run_manifest_sha256"),
            "mechanical_claim_status": "NOT_VALIDATED",
            **candidate_selection,
        },
        "adapter": {
            "adapter_metadata_sha256": trace.get("adapter_metadata_sha256"),
            "custom_cell_sha256": trace.get("custom_cell_sha256"),
            "assignment_scope": "global_uniform",
        },
        "source_mesh": {
            "original_path": str(source_path),
            "original_sha256": source_sha256,
            "package_copy_path": "input/source_c3d8.inp",
            "package_copy_sha256": _sha256(source_copy_path),
            "mesh_units": (
                "mm" if is_formal_case else fixture_record.get("mesh_units")
            ),
            "mesh_frame": (
                "source_native_cartesian_frame"
                if is_formal_case
                else fixture_record.get("mesh_frame")
            ),
            "element_type_original": case_precheck["mesh"][
                "element_type_original"
            ],
            "element_type_effective": case_precheck["mesh"][
                "element_type_effective"
            ],
            "node_count": case_precheck["mesh"]["node_count"],
            "element_count_original": case_precheck["mesh"]["element_count"],
        },
        "design_domain": {
            **design_domain,
            "source_element_map_path": "source_element_map.csv",
            "source_element_map_sha256": source_map_sha256,
            "source_element_map_scope": (
                "ORIGINAL_TO_SELECTED_DESIGN_DOMAIN_IDENTITY"
            ),
            "per_beam_source_lineage": "NOT_RECORDED_BY_CURRENT_CORE",
        },
        "cell_assignment": {
            "scope": "global_uniform",
            "unique_cell_identity_count": 1,
            "spatial_variation": False,
        },
        "diameter": physical_scale,
        "outputs": {
            "b31": {
                "path": "core/conformal_b31.inp",
                "sha256": b31_sha256,
                "node_count": b31_analysis["node_count"],
                "beam_count": b31_analysis["beam_count"],
            },
            "stl": {
                "path": "visualization/conformal_preview.stl",
                "sha256": _sha256(stl_path),
                "role": "derived_visualization_artifact",
            },
            "png": {
                "path": "visualization/conformal_preview.png",
                "sha256": _sha256(png_path),
                "role": "derived_visualization_artifact",
            },
            "legacy_metadata": {
                "path": "metadata.json",
                "sha256": _sha256(legacy_metadata_path),
            },
            "run_summary": {
                "path": "run_summary.txt",
                "sha256": _sha256(run_summary_path),
            },
            "geometry_ledger": {
                "path": "geometry_ledger.json",
                "sha256": geometry_ledger_sha256,
            },
            "source_element_map": {
                "path": "source_element_map.csv",
                "sha256": source_map_sha256,
            },
        },
        "stable_sets": {
            STABLE_NODE_SET: "core/conformal_b31.inp",
            STABLE_ELEMENT_SET: "core/conformal_b31.inp",
            CONDITIONAL_INTERFACE_SET: "NOT_PROVIDED_NOT_INFERRED",
        },
        "acceptance": {
            "contract_version": "P12_GEOMETRY_ACCEPTANCE_V1",
            "geometry_ledger_path": "geometry_ledger.json",
            "geometry_ledger_sha256": geometry_ledger_sha256,
            "technical_disposition": "PASS",
            "formal_gate_disposition": formal_gate_disposition,
            "failure_ids": [],
            "warnings": [
                "per-beam source-element lineage is not produced by the current core",
                "interface node set was not provided and was not inferred",
            ],
        },
        "claims": {
            "allowed_claims": allowed_claims,
            "non_claims": non_claims,
            "manufacturing_status": "not_validated",
            "post_conformal_mechanics_status": "not_validated",
            "FEA_status": "not_started",
            "clinical_status": "not_validated",
        },
        "integrity": {
            "artifact_hash_algorithm": "SHA256",
            "metadata_sha256_external_record": "REQUIRED",
            "identity_mismatch_behavior": "EVIDENCE_FAIL",
            "authoritative_files": [
                "package_metadata.json",
                "geometry_ledger.json",
                "source_element_map.csv",
                "core/conformal_b31.inp",
            ],
        },
    }
    package_metadata_text = json.dumps(
        package_metadata, ensure_ascii=False, indent=2
    ) + "\n"

    package_metadata_path = resolved_package_dir / "package_metadata.json"
    geometry_ledger_path = resolved_package_dir / "geometry_ledger.json"
    source_map_path = resolved_package_dir / "source_element_map.csv"
    for path in (package_metadata_path, geometry_ledger_path, source_map_path):
        if path.exists():
            raise ValueError(f"9.x package contract file already exists: {path.name}")

    b31_path.write_bytes(b31_with_sets.encode("utf-8"))
    source_map_path.write_bytes(source_map_text.encode("utf-8"))
    geometry_ledger_path.write_bytes(geometry_ledger_text.encode("utf-8"))
    package_metadata_path.write_bytes(package_metadata_text.encode("utf-8"))

    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "status": package_status,
        "package_metadata_path": str(package_metadata_path),
        "package_metadata_sha256": _sha256(package_metadata_path),
        "geometry_ledger_path": str(geometry_ledger_path),
        "geometry_ledger_sha256": _sha256(geometry_ledger_path),
        "source_element_map_path": str(source_map_path),
        "source_element_map_sha256": _sha256(source_map_path),
        "core_b31_path": str(b31_path),
        "core_b31_sha256": _sha256(b31_path),
        "stable_sets": {
            STABLE_NODE_SET: len(b31_analysis["nodes"]),
            STABLE_ELEMENT_SET: len(b31_analysis["elements"]),
            CONDITIONAL_INTERFACE_SET: "NOT_PROVIDED_NOT_INFERRED",
        },
        "non_claims": non_claims,
    }


def _validated_physical_scale(traceability: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(traceability, dict):
        raise ValueError("candidate traceability is required for the 9.x package contract")
    normalized = _positive_float(traceability.get("normalized_diameter"), "normalized diameter")
    reference_length = _positive_float(traceability.get("reference_length"), "reference length")
    physical = _positive_float(traceability.get("physical_diameter"), "physical diameter")
    if traceability.get("physical_scale_formula") != PHYSICAL_SCALE_FORMULA:
        raise ValueError("physical scale formula is not supported")
    if traceability.get("physical_diameter_units") != "mm":
        raise ValueError("physical diameter units must be mm")
    if not math.isclose(
        physical,
        normalized * reference_length,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("physical diameter does not match the P-11 formula")
    return {
        "normalized_value": normalized,
        "reference_length": reference_length,
        "physical_value": physical,
        "physical_units": "mm",
        "transform_contract": PHYSICAL_SCALE_FORMULA,
        "transform_formula": "d_physical=d_normalized*L_ref",
        "physical_diameter_source": traceability.get("physical_diameter_source"),
        "unique_physical_diameter_count": 1,
        "assignment_scope": "global_uniform",
    }


def _mechanical_source_from_traceability(
    traceability: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    legacy_source = {
        "evidence_scope": "HISTORICAL_DOWNSTREAM_TECHNICAL_ONLY",
        "formal_150w_provenance": "FAIL",
        "mechanical_match_status": "NOT_VALIDATED",
        "OOD_status": "not_available",
    }
    if traceability.get("selection_policy") in PRODUCT_SELECTION_POLICIES:
        missing = [
            field
            for field in PRODUCT_REQUIRED_TRACEABILITY_FIELDS
            if field not in traceability
        ]
        if missing:
            raise ValueError(
                "product traceability missing required fields: " + ", ".join(missing)
            )
        selection_sha = traceability["selection_manifest_sha256"]
        if not isinstance(selection_sha, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", selection_sha
        ):
            raise ValueError("product selection manifest SHA256 is invalid")
        if traceability["selected_candidate_id"] != traceability.get("candidate_id"):
            raise ValueError("product selected candidate does not match candidate identity")
        selected_rank = traceability["selected_rank"]
        if isinstance(selected_rank, bool) or not isinstance(selected_rank, int) or not 1 <= selected_rank <= 3:
            raise ValueError("product selected rank must be between 1 and 3")
        if traceability["selection_source"] not in ({"recommended_candidate", "user_override"} if traceability.get("selection_policy") == "product_candidate_selection_v2" else {"default_rank1", "user_override"}):
            raise ValueError("product selection source is invalid")
        if traceability["p09_status"] != "PASS":
            raise ValueError("product P-09 status must be PASS")
        if traceability["mechanical_match_status"] != "NOT_VALIDATED":
            raise ValueError("product mechanical status must remain NOT_VALIDATED")
        if traceability["diameter_used_in_ranking"] is not False:
            raise ValueError("product diameter must not be used in ranking")
        non_claims = traceability["non_claims"]
        if (
            not isinstance(non_claims, list)
            or any(not isinstance(item, str) for item in non_claims)
            or not PRODUCT_REQUIRED_NON_CLAIMS.issubset(set(non_claims))
        ):
            raise ValueError("product non-claims are incomplete")
        return (
            {
                "evidence_scope": "R3_PRODUCT_CANDIDATE_SELECTION_ONLY",
                "formal_150w_provenance": "FAIL",
                "p09_status": "PASS",
                "mechanical_match_status": "NOT_VALIDATED",
                "OOD_status": "not_available",
                "diameter_used_in_ranking": False,
            },
            {
                field: traceability[field]
                for field in (
                    "selection_policy",
                    "job_id",
                    "selection_manifest_sha256",
                    "recommended_candidate_id",
                    "selected_candidate_id",
                    "selected_rank",
                    "selection_source",
                    "p09_status",
                    "mechanical_match_status",
                    "diameter_used_in_ranking",
                )
            },
        )

    p10_markers = set(P10_REQUIRED_TRACEABILITY_FIELDS) - {"selection_policy"}
    is_p10 = (
        traceability.get("selection_policy") == P10_SELECTION_POLICY
        or any(field in traceability for field in p10_markers)
    )
    if not is_p10:
        return legacy_source, {}

    missing = [
        field
        for field in P10_REQUIRED_TRACEABILITY_FIELDS
        if field not in traceability
    ]
    if missing:
        raise ValueError(
            "P-10 traceability missing required fields: " + ", ".join(missing)
        )
    if traceability["selection_policy"] != P10_SELECTION_POLICY:
        raise ValueError("P-10 selection_policy is invalid")
    if traceability["selection_authority"] != "p10_ranking_manifest":
        raise ValueError("P-10 selection_authority is invalid")
    p10_sha = traceability["p10_ranking_manifest_sha256"]
    if not isinstance(p10_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", p10_sha):
        raise ValueError("P-10 ranking manifest SHA256 is invalid")
    if traceability["p10_schema_version"] != "latticegpt_9x_p10_ranking_manifest_v1":
        raise ValueError("P-10 schema version is invalid")
    if traceability["p10_evidence_level"] != "TECHNICAL_FIXTURE":
        raise ValueError("P-10 evidence level must be TECHNICAL_FIXTURE")
    if traceability["p10_ranking_status"] != "RANKED":
        raise ValueError("P-10 p10_ranking_status must be RANKED")
    if (
        traceability["p10_selected_status"]
        != "SURROGATE_RANKED_GEOMETRY_CANDIDATE"
    ):
        raise ValueError("P-10 selected status is invalid")
    if traceability["p09_status"] != "PASS":
        raise ValueError("P-10 P-09 status must be PASS")
    if traceability["mechanical_match_status"] != "NOT_VALIDATED":
        raise ValueError("P-10 mechanical status must remain NOT_VALIDATED")
    if traceability["ood_status"] != "not_available":
        raise ValueError("P-10 OOD status must remain not_available")
    if traceability["diameter_used_in_ranking"] is not False:
        raise ValueError("P-10 diameter must not be used in ranking")
    non_claims = traceability["non_claims"]
    if (
        not isinstance(non_claims, list)
        or any(not isinstance(item, str) for item in non_claims)
        or not P10_REQUIRED_NON_CLAIMS.issubset(set(non_claims))
    ):
        raise ValueError("P-10 non-claims are incomplete")

    mechanical_source = {
        "evidence_scope": "P10_RANKED_TECHNICAL_FIXTURE_ONLY",
        "formal_150w_provenance": "FAIL",
        "selection_authority": traceability["selection_authority"],
        "p09_status": traceability["p09_status"],
        "p10_ranking_status": traceability["p10_ranking_status"],
        "p10_selected_status": traceability["p10_selected_status"],
        "mechanical_match_status": "NOT_VALIDATED",
        "OOD_status": "not_available",
        "diameter_used_in_ranking": False,
    }
    candidate_selection = {
        field: traceability[field]
        for field in (
            "selection_policy",
            "selection_authority",
            "p10_ranking_manifest_sha256",
            "p10_schema_version",
            "p10_evidence_level",
            "p09_status",
            "p10_ranking_status",
            "p10_selected_status",
            "mechanical_match_status",
            "ood_status",
            "diameter_used_in_ranking",
        )
    }
    return mechanical_source, candidate_selection


def _positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be positive and finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive and finite") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return result


def _selected_element_ids(
    parsed_source: dict[str, Any], design_domain: dict[str, Any]
) -> list[int]:
    selector_type = design_domain.get("selector_type")
    selector_value = design_domain.get("selector_value")
    if selector_type in {
        "WHOLE_FILE_TECHNICAL_DOMAIN",
        "whole_file_implant_domain",
    }:
        selected = sorted(parsed_source["elements"])
    elif selector_type == "ELSET":
        if selector_value not in parsed_source["elsets"]:
            raise ValueError("selected ELSET is missing from packaged source mesh")
        selected = sorted(set(parsed_source["elsets"][selector_value]))
    else:
        raise ValueError("unsupported design-domain selector")
    if not selected:
        raise ValueError("selected design domain is empty")
    unknown = sorted(set(selected) - set(parsed_source["elements"]))
    if unknown:
        raise ValueError(f"selected design domain references unknown elements: {unknown}")
    return selected


def _source_component_count(
    elements: dict[int, dict[str, Any]], selected_ids: list[int]
) -> int:
    adjacency = {element_id: set() for element_id in selected_ids}
    face_owners: dict[tuple[int, ...], list[int]] = {}
    for element_id in selected_ids:
        node_ids = elements[element_id]["node_ids"]
        if len(node_ids) != 8:
            raise ValueError("selected source element is not an eight-node hex")
        for indexes in _HEX8_FACE_INDEXES:
            face = tuple(sorted(node_ids[index] for index in indexes))
            face_owners.setdefault(face, []).append(element_id)
    for owners in face_owners.values():
        for owner in owners:
            adjacency[owner].update(other for other in owners if other != owner)
    return _component_count(adjacency)


def _analyze_b31(text: str) -> dict[str, Any]:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, int]] = {}
    section: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            compact = line.upper().replace(" ", "")
            if compact.startswith("*NODE"):
                section = "node"
            elif compact.startswith("*ELEMENT") and "TYPE=B31" in compact:
                section = "element"
            else:
                section = None
            continue
        values = [value.strip() for value in line.split(",") if value.strip()]
        try:
            if section == "node":
                if len(values) < 4:
                    raise ValueError(f"invalid B31 node row at line {line_number}")
                node_id = int(values[0])
                if node_id in nodes:
                    raise ValueError(f"duplicate B31 node id: {node_id}")
                nodes[node_id] = tuple(float(value) for value in values[1:4])
            elif section == "element":
                if len(values) < 3:
                    raise ValueError(f"invalid B31 element row at line {line_number}")
                element_id = int(values[0])
                if element_id in elements:
                    raise ValueError(f"duplicate B31 element id: {element_id}")
                elements[element_id] = (int(values[1]), int(values[2]))
        except ValueError as exc:
            if str(exc).startswith(("invalid B31", "duplicate B31")):
                raise
            raise ValueError(f"invalid B31 numeric data at line {line_number}") from exc
    if not nodes:
        raise ValueError("B31 contains no nodes")
    if not elements:
        raise ValueError("B31 contains no elements")

    invalid_references = sorted(
        {
            node_id
            for endpoints in elements.values()
            for node_id in endpoints
            if node_id not in nodes
        }
    )
    mins = [min(point[index] for point in nodes.values()) for index in range(3)]
    maxs = [max(point[index] for point in nodes.values()) for index in range(3)]
    size = [maxs[index] - mins[index] for index in range(3)]
    l_char = max(size)
    tolerance = max(1e-9 * l_char, 1e-12)
    lengths: list[float] = []
    zero_length_ids: list[int] = []
    undirected: dict[tuple[int, int], list[int]] = {}
    adjacency = {node_id: set() for node_id in nodes}
    for element_id, (node_a, node_b) in elements.items():
        key = tuple(sorted((node_a, node_b)))
        undirected.setdefault(key, []).append(element_id)
        if node_a not in nodes or node_b not in nodes:
            continue
        length = math.dist(nodes[node_a], nodes[node_b])
        lengths.append(length)
        if length <= tolerance:
            zero_length_ids.append(element_id)
        adjacency[node_a].add(node_b)
        adjacency[node_b].add(node_a)
    duplicate_edges = {
        key: ids for key, ids in undirected.items() if len(ids) > 1
    }
    isolated_nodes = sorted(node_id for node_id, links in adjacency.items() if not links)
    connected_component_count = _component_count(adjacency)
    return {
        "nodes": nodes,
        "elements": elements,
        "node_count": len(nodes),
        "beam_count": len(elements),
        "bbox": {"min": mins, "max": maxs, "size": size, "L_char": l_char},
        "beam_length": {
            "minimum": min(lengths) if lengths else None,
            "maximum": max(lengths) if lengths else None,
            "mean": sum(lengths) / len(lengths) if lengths else None,
            "zero_length_tolerance": tolerance,
        },
        "required_zero_counts": {
            "invalid_references": len(invalid_references),
            "zero_length_beams": len(zero_length_ids),
            "duplicate_undirected_beams": len(duplicate_edges),
            "isolated_nodes": len(isolated_nodes),
        },
        "connected_component_count": connected_component_count,
    }


def _component_count(adjacency: dict[int, set[int]]) -> int:
    seen: set[int] = set()
    count = 0
    for start in adjacency:
        if start in seen:
            continue
        count += 1
        stack = [start]
        seen.add(start)
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
    return count


def _contains_named_set(text: str, keyword: str, name: str) -> bool:
    compact_name = name.upper()
    for raw_line in text.splitlines():
        compact = raw_line.upper().replace(" ", "")
        if compact.startswith(f"*{keyword}") and f"{keyword}={compact_name}" in compact:
            return True
    return False


def _append_stable_sets(text: str, node_ids: list[int], element_ids: list[int]) -> str:
    sections = [
        text.rstrip(),
        "",
        "** LatticeGPT 9.x P-13 stable handoff sets",
        _format_id_set("NSET", STABLE_NODE_SET, node_ids).rstrip(),
        _format_id_set("ELSET", STABLE_ELEMENT_SET, element_ids).rstrip(),
    ]
    return "\n".join(sections) + "\n"


def _format_id_set(keyword: str, name: str, ids: list[int]) -> str:
    if not ids:
        raise ValueError(f"{name} cannot be empty")
    lines = [f"*{keyword}, {keyword}={name}"]
    for start in range(0, len(ids), 16):
        lines.append(", ".join(str(value) for value in ids[start : start + 16]))
    return "\n".join(lines) + "\n"


def _build_source_map_csv(
    elements: dict[int, dict[str, Any]],
    selected_ids: list[int],
    element_type_effective: str,
) -> str:
    selected_index = {
        element_id: index for index, element_id in enumerate(selected_ids, start=1)
    }
    fieldnames = [
        "source_element_id",
        "selected",
        "selected_index",
        "element_type_original",
        "element_type_effective",
        "node_ids",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for element_id in sorted(elements):
        element = elements[element_id]
        writer.writerow(
            {
                "source_element_id": element_id,
                "selected": "true" if element_id in selected_index else "false",
                "selected_index": selected_index.get(element_id, ""),
                "element_type_original": element["type"],
                "element_type_effective": element_type_effective,
                "node_ids": " ".join(str(value) for value in element["node_ids"]),
            }
        )
    return stream.getvalue()


def _digest_ids(ids: list[int]) -> str:
    return hashlib.sha256(
        ",".join(str(value) for value in ids).encode("ascii")
    ).hexdigest().upper()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()
