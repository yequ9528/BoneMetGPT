"""Convert a ranked candidate into a validated custom cell.

This module never runs inverse design or conformal runtime. File writing is
limited to the controlled 6.13e custom-cell static validation output root.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from numbers import Real
from pathlib import Path
from typing import Any


ADAPTER_SCHEMA_VERSION = "candidate_cell_adapter_preview_v1"
ADAPTER_METADATA_SCHEMA_VERSION = "candidate_cell_adapter_metadata_v1"
SAFE_DIR_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
LEGACY_SELECTION_POLICY = "cad_ready_max_diameter_then_source_id_v1"
P10_SELECTION_POLICY = "scaler_range_weighted_RMSE_v1"
PRODUCT_SELECTION_POLICY = "product_candidate_selection_v1"
PRODUCT_SELECTION_POLICIES = {PRODUCT_SELECTION_POLICY, "product_candidate_selection_v2"}
FORMAL_CASE_BINDING_SCHEMA = "latticegpt_9x_adapter_case_binding_v1"
PHYSICAL_SCALE_CONTRACT = "average_equivalent_selected_hex_scale_v1"
GLOBAL_ASSIGNMENT_SCOPE = "global_uniform"
P10_REQUIRED_NON_CLAIMS = {
    "TECHNICAL_VALIDATION_ONLY",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
}
PRODUCT_REQUIRED_NON_CLAIMS = {
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_FEA_VALIDATION",
}


def build_custom_cell_from_candidate(
    candidate: Any,
    output_path: Any = None,
    *,
    overwrite: bool = False,
    dry_run: bool = True,
    traceability: Any = None,
    formal_case_binding: Any = None,
    allowed_output_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a mechanical candidate and optionally write custom-cell files.

    ``dry_run=True`` never resolves, creates, checks, or writes filesystem paths.
    ``dry_run=False`` requires and writes only inside ``allowed_output_root``.
    """

    if dry_run is not True and allowed_output_root is None:
        return {
            "status": "blocked",
            "adapter_status": "not_validated",
            "message": "custom_cell.yml write requires an explicit allowed root",
            "dry_run": False,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": None,
            "errors": ["allowed_output_root is required when dry_run=False"],
            "warnings": [],
        }

    errors, warnings, normalized = _validate_candidate(candidate)
    if errors:
        return {
            "status": "invalid",
            "adapter_status": "not_validated",
            "message": "candidate failed custom-cell adapter validation",
            "dry_run": True,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": None,
            "errors": errors,
            "warnings": warnings,
        }

    traceability_errors, normalized_traceability = _validate_traceability(
        traceability,
        normalized,
    )
    if traceability_errors:
        return {
            "status": "invalid",
            "adapter_status": "not_validated",
            "message": "candidate traceability failed custom-cell adapter validation",
            "dry_run": True,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": None,
            "errors": traceability_errors,
            "warnings": warnings,
        }

    case_binding_errors, normalized_case_binding = _validate_formal_case_binding(
        formal_case_binding,
        normalized,
    )
    if case_binding_errors:
        return {
            "status": "invalid",
            "adapter_status": "not_validated",
            "message": "formal case binding failed custom-cell adapter validation",
            "dry_run": True,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": None,
            "errors": case_binding_errors,
            "warnings": warnings,
        }

    custom_cell = _build_custom_cell_payload(normalized)
    metadata = _build_adapter_metadata(
        normalized,
        custom_cell,
        warnings,
        traceability=normalized_traceability,
        formal_case_binding=normalized_case_binding,
    )

    if dry_run is not True:
        return _write_static_custom_cell(
            custom_cell=custom_cell,
            metadata=metadata,
            output_path=output_path,
            overwrite=overwrite,
            warnings=warnings,
            allowed_output_root=allowed_output_root,
        )

    return {
        "status": "preview",
        "adapter_status": "not_validated",
        "message": "custom_cell preview generated; no file was written",
        "dry_run": True,
        "requested_output_path": output_path,
        "overwrite_requested": overwrite,
        "candidate_id": normalized["candidate_id"],
        "custom_cell_preview": {"format": "cell_library_yml_preview", **custom_cell},
        "adapter_metadata": metadata,
        "errors": [],
        "warnings": warnings
        + [
            "6.13 dry-run preview only",
            "no custom_cell.yml generated",
            "candidate adapter not validated by conformal runtime",
            "not dual-space fusion completion",
        ],
    }


def _validate_candidate(candidate: Any) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Any] = {}

    if not isinstance(candidate, Mapping):
        return ["candidate must be a dict-like mapping"], warnings, normalized

    required_fields = ["nodes", "connections", "diameter", "relative_density", "cad_ready"]
    for field in required_fields:
        if field not in candidate:
            errors.append(f"missing required field: {field}")

    nodes = _normalize_nodes(candidate.get("nodes"), errors)
    edges = _normalize_edges(candidate.get("connections"), len(nodes), errors, warnings)
    diameter = _positive_float(candidate.get("diameter"), "diameter", errors)
    relative_density = _positive_float(
        candidate.get("relative_density"), "relative_density", errors
    )
    cad_ready = candidate.get("cad_ready")
    if cad_ready is not True:
        errors.append("cad_ready must be True")

    normalized.update(
        {
            "candidate_id": candidate.get("candidate_id"),
            "source_candidate_id": candidate.get("source_candidate_id"),
            "target_sha256": candidate.get("target_sha256"),
            "source_schema_version": candidate.get("schema_version"),
            "geometry_contract": candidate.get("geometry_contract"),
            "nodes": nodes,
            "edges": edges,
            "diameter": diameter,
            "relative_density": relative_density,
            "cad_ready": cad_ready,
        }
    )
    return errors, warnings, normalized


def _build_custom_cell_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    nodes = normalized["nodes"]
    edges = normalized["edges"]
    return {
        "nodes": {
            index + 1: _to_natural_coordinates(node, full_precision=normalized.get("geometry_contract") == "truss_20261110_unit_cell_v1")
            for index, node in enumerate(nodes)
        },
        "edges": [[a + 1, b + 1] for a, b in edges],
    }


def _build_adapter_metadata(
    normalized: Mapping[str, Any],
    custom_cell: Mapping[str, Any],
    warnings: Sequence[str],
    *,
    traceability: Mapping[str, Any] | None = None,
    formal_case_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "source_schema_version": normalized["source_schema_version"],
        "candidate_id": normalized["candidate_id"],
        "source": "candidate_cell_adapter",
        "node_count": len(custom_cell["nodes"]),
        "edge_count": len(custom_cell["edges"]),
        "diameter": normalized["diameter"],
        "relative_density": normalized["relative_density"],
        "cad_ready": normalized["cad_ready"],
        "coordinate_mapping": "[0,1] -> [-1,1]",
        "edge_mapping": "0-based candidate connections -> 1-based cell edges",
        "diameter_handling": "recorded in metadata only; cell yml has no diameter field",
        "warnings": list(warnings),
    }
    if normalized.get("geometry_contract") == "truss_20261110_unit_cell_v1":
        metadata["geometry_contract"] = normalized["geometry_contract"]
    if traceability is not None:
        metadata["traceability"] = dict(traceability)
    if formal_case_binding is not None:
        metadata["case_manifest_sha256"] = formal_case_binding[
            "case_manifest_sha256"
        ]
        metadata["physical_scale"] = {
            "contract": formal_case_binding["physical_scale_contract"],
            "normalized_diameter": normalized["diameter"],
            "reference_length_mm": formal_case_binding["reference_length_mm"],
            "physical_diameter_mm": (
                normalized["diameter"]
                * formal_case_binding["reference_length_mm"]
            ),
            "units": "mm",
            "assignment_scope": formal_case_binding["assignment_scope"],
        }
    return metadata


def _validate_formal_case_binding(
    formal_case_binding: Any,
    normalized: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any] | None]:
    if formal_case_binding is None:
        return [], None
    if not isinstance(formal_case_binding, Mapping):
        return ["formal_case_binding must be a dict-like mapping"], None

    errors: list[str] = []
    required_fields = [
        "schema_version",
        "case_manifest_sha256",
        "physical_scale_contract",
        "reference_length_mm",
        "assignment_scope",
    ]
    for field in required_fields:
        if field not in formal_case_binding:
            errors.append(f"formal_case_binding missing required field: {field}")

    if formal_case_binding.get("schema_version") != FORMAL_CASE_BINDING_SCHEMA:
        errors.append(
            f"formal_case_binding.schema_version must be {FORMAL_CASE_BINDING_SCHEMA}"
        )
    if not _is_sha256_hex(formal_case_binding.get("case_manifest_sha256")):
        errors.append(
            "formal_case_binding.case_manifest_sha256 must be a 64-character SHA256 hex string"
        )
    if formal_case_binding.get("physical_scale_contract") != PHYSICAL_SCALE_CONTRACT:
        errors.append(
            f"formal_case_binding.physical_scale_contract must be {PHYSICAL_SCALE_CONTRACT}"
        )

    reference_length_mm = formal_case_binding.get("reference_length_mm")
    if not _is_finite_number(reference_length_mm) or float(reference_length_mm) <= 0:
        errors.append(
            "formal_case_binding.reference_length_mm must be a finite positive number"
        )
    if formal_case_binding.get("assignment_scope") != GLOBAL_ASSIGNMENT_SCOPE:
        errors.append(
            f"formal_case_binding.assignment_scope must be {GLOBAL_ASSIGNMENT_SCOPE}"
        )
    if not _is_finite_number(normalized.get("diameter")):
        errors.append("candidate normalized diameter must be finite for physical scaling")

    if errors:
        return errors, None
    return [], {
        "schema_version": FORMAL_CASE_BINDING_SCHEMA,
        "case_manifest_sha256": str(formal_case_binding["case_manifest_sha256"]),
        "physical_scale_contract": PHYSICAL_SCALE_CONTRACT,
        "reference_length_mm": float(reference_length_mm),
        "assignment_scope": GLOBAL_ASSIGNMENT_SCOPE,
    }


def _validate_traceability(
    traceability: Any,
    normalized: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any] | None]:
    if traceability is None:
        return [], None

    errors: list[str] = []
    if not isinstance(traceability, Mapping):
        return ["traceability must be a dict-like mapping"], None

    common_fields = [
        "candidate_id",
        "source_candidate_id",
        "target_sha256",
        "evidence_level",
        "selection_policy",
        "candidate_manifest_sha256",
        "run_manifest_sha256",
    ]
    selection_policy = traceability.get("selection_policy")
    if selection_policy == LEGACY_SELECTION_POLICY:
        common_fields = ["task_id", "run_id", *common_fields]
        policy_fields = [
            "gate1_review",
            "gate1_review_basis",
            "gate1_review_artifact",
        ]
    elif selection_policy == P10_SELECTION_POLICY:
        common_fields = ["task_id", "run_id", *common_fields]
        policy_fields = [
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
        ]
    elif selection_policy in PRODUCT_SELECTION_POLICIES:
        common_fields = ["job_id", *common_fields]
        policy_fields = [
            "selection_manifest_sha256",
            "recommended_candidate_id",
            "selected_candidate_id",
            "selected_rank",
            "selection_source",
            "p09_status",
            "mechanical_match_status",
            "diameter_used_in_ranking",
            "non_claims",
        ]
    else:
        policy_fields = []
        errors.append(
            "traceability.selection_policy must be a supported selection policy"
        )

    required_fields = common_fields + policy_fields
    for field in required_fields:
        if field not in traceability:
            errors.append(f"traceability missing required field: {field}")

    if traceability.get("candidate_id") != normalized.get("candidate_id"):
        errors.append("traceability.candidate_id must match candidate.candidate_id")

    candidate_source_id = normalized.get("source_candidate_id")
    if candidate_source_id is not None and traceability.get("source_candidate_id") != candidate_source_id:
        errors.append(
            "traceability.source_candidate_id must match candidate.source_candidate_id"
        )

    candidate_target_sha256 = normalized.get("target_sha256")
    if (
        candidate_target_sha256 is not None
        and traceability.get("target_sha256") != candidate_target_sha256
    ):
        errors.append("traceability.target_sha256 must match candidate.target_sha256")

    if not _is_sha256_hex(traceability.get("target_sha256")):
        errors.append("traceability.target_sha256 must be a 64-character SHA256 hex string")
    if not _is_sha256_hex(traceability.get("candidate_manifest_sha256")):
        errors.append(
            "traceability.candidate_manifest_sha256 must be a 64-character SHA256 hex string"
        )
    if not _is_sha256_hex(traceability.get("run_manifest_sha256")):
        errors.append(
            "traceability.run_manifest_sha256 must be a 64-character SHA256 hex string"
        )

    if traceability.get("evidence_level") != "real_150w_candidate":
        errors.append("traceability.evidence_level must be real_150w_candidate")

    if selection_policy == LEGACY_SELECTION_POLICY:
        if traceability.get("gate1_review") != "passed":
            errors.append("traceability.gate1_review must be passed")
        if traceability.get("gate1_review_basis") != "independent_readonly_review":
            errors.append(
                "traceability.gate1_review_basis must be independent_readonly_review"
            )
        if traceability.get("gate1_review_artifact") is not None:
            errors.append("traceability.gate1_review_artifact must be None")
    elif selection_policy == P10_SELECTION_POLICY:
        if traceability.get("selection_authority") != "p10_ranking_manifest":
            errors.append(
                "traceability.selection_authority must be p10_ranking_manifest"
            )
        if not _is_sha256_hex(traceability.get("p10_ranking_manifest_sha256")):
            errors.append(
                "traceability.p10_ranking_manifest_sha256 must be a 64-character SHA256 hex string"
            )
        if traceability.get("p10_schema_version") != "latticegpt_9x_p10_ranking_manifest_v1":
            errors.append(
                "traceability.p10_schema_version must be latticegpt_9x_p10_ranking_manifest_v1"
            )
        if traceability.get("p10_evidence_level") != "TECHNICAL_FIXTURE":
            errors.append("traceability.p10_evidence_level must be TECHNICAL_FIXTURE")
        if traceability.get("p10_ranking_status") != "RANKED":
            errors.append("traceability.p10_ranking_status must be RANKED")
        if (
            traceability.get("p10_selected_status")
            != "SURROGATE_RANKED_GEOMETRY_CANDIDATE"
        ):
            errors.append(
                "traceability.p10_selected_status must be SURROGATE_RANKED_GEOMETRY_CANDIDATE"
            )
        if traceability.get("p09_status") != "PASS":
            errors.append("traceability.p09_status must be PASS")
        if traceability.get("mechanical_match_status") != "NOT_VALIDATED":
            errors.append(
                "traceability.mechanical_match_status must be NOT_VALIDATED"
            )
        if traceability.get("ood_status") != "not_available":
            errors.append("traceability.ood_status must be not_available")
        if traceability.get("diameter_used_in_ranking") is not False:
            errors.append(
                "traceability.diameter_used_in_ranking must be False"
            )
        non_claims = traceability.get("non_claims")
        if not _is_sequence(non_claims) or not P10_REQUIRED_NON_CLAIMS.issubset(
            set(non_claims)
        ):
            errors.append(
                "traceability.non_claims must include all required P-10 non-claims"
            )
    elif selection_policy in PRODUCT_SELECTION_POLICIES:
        if not isinstance(traceability.get("job_id"), str) or not traceability["job_id"]:
            errors.append("traceability.job_id must be a non-empty string")
        if not _is_sha256_hex(traceability.get("selection_manifest_sha256")):
            errors.append(
                "traceability.selection_manifest_sha256 must be a 64-character SHA256 hex string"
            )
        if traceability.get("selected_candidate_id") != normalized.get("candidate_id"):
            errors.append(
                "traceability.selected_candidate_id must match candidate.candidate_id"
            )
        if not isinstance(traceability.get("recommended_candidate_id"), str) or not traceability["recommended_candidate_id"]:
            errors.append("traceability.recommended_candidate_id must be a non-empty string")
        selected_rank = traceability.get("selected_rank")
        if isinstance(selected_rank, bool) or not isinstance(selected_rank, int) or not 1 <= selected_rank <= 3:
            errors.append("traceability.selected_rank must be between 1 and 3")
        if traceability.get("selection_source") not in ({"recommended_candidate", "user_override"} if traceability.get("selection_policy") == "product_candidate_selection_v2" else {"default_rank1", "user_override"}):
            errors.append(
                "traceability.selection_source must be default_rank1 or user_override"
            )
        if traceability.get("p09_status") != "PASS":
            errors.append("traceability.p09_status must be PASS")
        if traceability.get("mechanical_match_status") != "NOT_VALIDATED":
            errors.append("traceability.mechanical_match_status must be NOT_VALIDATED")
        if traceability.get("diameter_used_in_ranking") is not False:
            errors.append("traceability.diameter_used_in_ranking must be False")
        non_claims = traceability.get("non_claims")
        if not _is_sequence(non_claims) or not PRODUCT_REQUIRED_NON_CLAIMS.issubset(set(non_claims)):
            errors.append(
                "traceability.non_claims must include all required product non-claims"
            )

    if errors:
        return errors, None
    return [], {field: traceability.get(field) for field in required_fields}


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _write_static_custom_cell(
    *,
    custom_cell: Mapping[str, Any],
    metadata: Mapping[str, Any],
    output_path: Any,
    overwrite: bool,
    warnings: Sequence[str],
    allowed_output_root: str | Path,
) -> dict[str, Any]:
    path_result = _resolve_output_paths(
        output_path,
        metadata.get("candidate_id"),
        overwrite,
        allowed_output_root=allowed_output_root,
    )
    if path_result["errors"]:
        return {
            "status": "blocked",
            "adapter_status": "not_validated",
            "message": "custom_cell.yml write blocked by path guard",
            "dry_run": False,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": {"format": "cell_library_yml_preview", **custom_cell},
            "errors": path_result["errors"],
            "warnings": list(warnings),
        }

    output_dir = path_result["output_dir"]
    custom_cell_path = path_result["custom_cell_path"]
    metadata_path = path_result["metadata_path"]

    prewrite_errors = _validate_custom_cell_data(custom_cell)
    if prewrite_errors:
        return {
            "status": "invalid",
            "adapter_status": "not_validated",
            "message": "custom_cell data failed static validation before write",
            "dry_run": False,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "custom_cell_preview": {"format": "cell_library_yml_preview", **custom_cell},
            "errors": prewrite_errors,
            "warnings": list(warnings),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_custom_cell_yml(custom_cell_path, custom_cell, full_precision=metadata.get("geometry_contract") == "truss_20261110_unit_cell_v1")

    validation_errors = validate_custom_cell_yml(custom_cell_path)
    if validation_errors:
        return {
            "status": "static_validation_failed",
            "adapter_status": "not_validated",
            "message": "custom_cell.yml was written but failed static validation",
            "dry_run": False,
            "requested_output_path": output_path,
            "overwrite_requested": overwrite,
            "output_dir": str(output_dir),
            "custom_cell_path": str(custom_cell_path),
            "adapter_metadata_path": None,
            "custom_cell_preview": {"format": "cell_library_yml_preview", **custom_cell},
            "errors": validation_errors,
            "warnings": list(warnings),
        }

    metadata_to_write = {
        **metadata,
        "schema_version": ADAPTER_METADATA_SCHEMA_VERSION,
        "adapter_status": "static_validated",
        "custom_cell_path": "custom_cell.yml",
        "runtime_enabled": False,
        "fusion_status": "not_validated",
        "warnings": list(warnings)
        + [
            "custom_cell.yml static validation only",
            "not conformal runtime validation",
            "not dual-space fusion completion",
        ],
    }
    metadata_path.write_text(
        json.dumps(metadata_to_write, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "static_validated",
        "adapter_status": "static_validated",
        "message": "custom_cell.yml and adapter_metadata.json written after static validation",
        "dry_run": False,
        "requested_output_path": output_path,
        "overwrite_requested": overwrite,
        "output_dir": str(output_dir),
        "custom_cell_path": str(custom_cell_path),
        "adapter_metadata_path": str(metadata_path),
        "custom_cell_preview": {"format": "cell_library_yml_preview", **custom_cell},
        "adapter_metadata": metadata_to_write,
        "errors": [],
        "warnings": metadata_to_write["warnings"],
    }


def _resolve_output_paths(
    output_path: Any,
    candidate_id: Any,
    overwrite: bool,
    *,
    allowed_output_root: str | Path,
) -> dict[str, Any]:
    errors: list[str] = []
    root = Path(allowed_output_root).resolve(strict=False)

    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_dir = root / f"{_sanitize_path_segment(candidate_id)}_{timestamp}"
    else:
        raw_path = Path(str(output_path))
        output_dir = raw_path if raw_path.is_absolute() else root / raw_path

    output_dir = output_dir.resolve(strict=False)
    custom_cell_path = output_dir / "custom_cell.yml"
    metadata_path = output_dir / "adapter_metadata.json"

    for path, label in (
        (output_dir, "output_dir"),
        (custom_cell_path, "custom_cell_path"),
        (metadata_path, "metadata_path"),
    ):
        if not _is_relative_to(path, root):
            errors.append(f"{label} must stay inside {root}")

    if output_dir.exists():
        if not output_dir.is_dir():
            errors.append("output_path exists and is not a directory")
        elif not overwrite:
            errors.append("output directory already exists and overwrite is False")
        else:
            allowed_names = {"custom_cell.yml", "adapter_metadata.json"}
            unexpected = [
                child.name for child in output_dir.iterdir() if child.name not in allowed_names
            ]
            if unexpected:
                errors.append(
                    "overwrite=True is blocked because output directory contains "
                    f"unexpected files: {unexpected}"
                )

    return {
        "output_dir": output_dir,
        "custom_cell_path": custom_cell_path,
        "metadata_path": metadata_path,
        "errors": errors,
    }


def _sanitize_path_segment(value: Any) -> str:
    raw = str(value or "candidate")
    sanitized = SAFE_DIR_NAME_RE.sub("_", raw).strip(" ._-")
    if not sanitized or sanitized in {".", ".."}:
        sanitized = "candidate"
    reserved_windows_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if sanitized.upper() in reserved_windows_names:
        sanitized = f"{sanitized}_candidate"
    return sanitized[:80]


def _write_custom_cell_yml(path: Path, custom_cell: Mapping[str, Any], *, full_precision=False) -> None:
    lines = ["nodes:\n"]
    for node_id in sorted(custom_cell["nodes"]):
        coord = custom_cell["nodes"][node_id]
        lines.append(f"  {node_id}: [{_format_float(coord[0], full_precision=full_precision)}, {_format_float(coord[1], full_precision=full_precision)}, {_format_float(coord[2], full_precision=full_precision)}]\n")
    lines.append("\nedges:\n")
    for edge in custom_cell["edges"]:
        lines.append(f"  - [{edge[0]}, {edge[1]}]\n")
    path.write_text("".join(lines), encoding="utf-8")


def _format_float(value: Any, *, full_precision=False) -> str:
    return repr(float(value)) if full_precision else f"{float(value):.12g}"


def validate_custom_cell_yml(path: str | Path) -> list[str]:
    import yaml

    yml_path = Path(path)
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact parser errors are external.
        return [f"custom_cell.yml parse failed: {exc}"]
    return _validate_custom_cell_data(data)


def _validate_custom_cell_data(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["custom_cell.yml root must be a mapping"]
    extra_keys = set(data) - {"nodes", "edges"}
    missing_keys = {"nodes", "edges"} - set(data)
    if extra_keys:
        errors.append(f"custom_cell.yml contains unsupported keys: {sorted(extra_keys)}")
    if missing_keys:
        errors.append(f"custom_cell.yml missing keys: {sorted(missing_keys)}")
        return errors

    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, Mapping):
        errors.append("nodes must be an id -> coord mapping")
        nodes = {}
    if not _is_sequence(edges):
        errors.append("edges must be a list of node id pairs")
        edges = []

    node_ids: set[int] = set()
    for raw_id, coord in nodes.items():
        if not _is_int_index(raw_id) or int(raw_id) <= 0:
            errors.append(f"node id must be a positive integer: {raw_id}")
            continue
        node_id = int(raw_id)
        if node_id in node_ids:
            errors.append(f"duplicate node id: {node_id}")
            continue
        node_ids.add(node_id)
        if not _is_sequence(coord) or len(coord) != 3:
            errors.append(f"node {node_id} coord must contain 3 values")
            continue
        for value in coord:
            if not _is_finite_number(value):
                errors.append(f"node {node_id} coord contains non-finite value")
                break
            if float(value) < -1.0 or float(value) > 1.0:
                errors.append(f"node {node_id} coord must stay within [-1, 1]")
                break

    if node_ids and node_ids != set(range(1, len(node_ids) + 1)):
        errors.append("node ids must be continuous 1-based integers")

    seen_edges: set[tuple[int, int]] = set()
    for index, edge in enumerate(edges):
        if not _is_sequence(edge) or len(edge) != 2:
            errors.append(f"edge {index} must contain exactly 2 node ids")
            continue
        a, b = edge[0], edge[1]
        if not _is_int_index(a) or not _is_int_index(b):
            errors.append(f"edge {index} endpoints must be integers")
            continue
        if a == b:
            errors.append(f"edge {index} must not be a self-loop")
            continue
        if a not in node_ids or b not in node_ids:
            errors.append(f"edge {index} references missing node id")
            continue
        normalized_edge = (a, b) if a < b else (b, a)
        if normalized_edge in seen_edges:
            errors.append(f"edge {index} duplicates an existing edge")
            continue
        seen_edges.add(normalized_edge)

    if not node_ids:
        errors.append("nodes must be non-empty")
    if not seen_edges:
        errors.append("edges must be non-empty")
    return errors


def _normalize_nodes(value: Any, errors: list[str]) -> list[tuple[float, float, float]]:
    if not _is_sequence(value):
        errors.append("nodes must be a non-empty sequence")
        return []
    if len(value) == 0:
        errors.append("nodes must be non-empty")
        return []

    nodes: list[tuple[float, float, float]] = []
    for index, raw_node in enumerate(value):
        coords = _node_coordinates(raw_node)
        if coords is None:
            errors.append(f"nodes[{index}] must contain exactly 3 numeric coordinates")
            continue

        bad_coord = False
        parsed: list[float] = []
        for coord in coords:
            if not _is_finite_number(coord):
                bad_coord = True
                break
            parsed.append(float(coord))
        if bad_coord:
            errors.append(f"nodes[{index}] must contain finite numeric coordinates")
            continue

        out_of_range = [coord for coord in parsed if coord < 0.0 or coord > 1.0]
        if out_of_range:
            errors.append(f"nodes[{index}] coordinates must be within [0, 1]")
            continue

        nodes.append((parsed[0], parsed[1], parsed[2]))
    return nodes


def _node_coordinates(value: Any) -> tuple[Any, Any, Any] | None:
    if isinstance(value, Mapping):
        if all(key in value for key in ("x", "y", "z")):
            return value["x"], value["y"], value["z"]
        if all(key in value for key in ("X", "Y", "Z")):
            return value["X"], value["Y"], value["Z"]
        return None

    if _is_sequence(value) and len(value) == 3:
        return value[0], value[1], value[2]
    return None


def _normalize_edges(
    value: Any,
    node_count: int,
    errors: list[str],
    warnings: list[str],
) -> list[tuple[int, int]]:
    if not _is_sequence(value):
        errors.append("connections must be a non-empty sequence")
        return []
    if len(value) == 0:
        errors.append("connections must be non-empty")
        return []

    seen: set[tuple[int, int]] = set()
    edges: list[tuple[int, int]] = []
    duplicate_count = 0
    for index, raw_edge in enumerate(value):
        if not _is_sequence(raw_edge) or len(raw_edge) != 2:
            errors.append(f"connections[{index}] must contain exactly 2 node indexes")
            continue

        a_raw, b_raw = raw_edge[0], raw_edge[1]
        if not _is_int_index(a_raw) or not _is_int_index(b_raw):
            errors.append(f"connections[{index}] indexes must be integers")
            continue

        a = int(a_raw)
        b = int(b_raw)
        if a == b:
            errors.append(f"connections[{index}] must not be a self-loop")
            continue
        if a < 0 or b < 0 or a >= node_count or b >= node_count:
            errors.append(f"connections[{index}] indexes out of range")
            continue

        edge = (a, b) if a < b else (b, a)
        if edge in seen:
            duplicate_count += 1
            continue
        seen.add(edge)
        edges.append(edge)

    if duplicate_count:
        warnings.append(f"duplicate connections ignored: {duplicate_count}")
    return edges


def _positive_float(value: Any, field_name: str, errors: list[str]) -> float | None:
    if not _is_finite_number(value):
        errors.append(f"{field_name} must be a finite positive number")
        return None
    parsed = float(value)
    if parsed <= 0.0:
        errors.append(f"{field_name} must be > 0")
        return None
    return parsed


def _to_natural_coordinates(node: tuple[float, float, float], *, full_precision=False) -> list[float]:
    return [(coord * 2.0) - 1.0 if full_precision else round((coord * 2.0) - 1.0, 12) for coord in node]


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _is_int_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
