from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from latticegpt_9x_package_contract import (
    finalize_latticegpt_9x_package,
    precheck_latticegpt_9x_case_manifest,
)


SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PHYSICAL_SCALE_FORMULA = "P11_AVERAGE_EQUIVALENT_SCALE_V1"
PHYSICAL_DIAMETER_UNITS = "mm"
PHYSICAL_DIAMETER_SOURCE = (
    "latticegpt_9x_technical_case_preview:physical_diameter"
)
DEFAULT_CONFORMAL_ROOT = (
    Path(__file__).resolve().parent
    / "smart_lattice_conformal_design"
    / "lattice-main1"
    / "smart_lattice-main"
)
DEFAULT_CELL_LIBRARY_DIR = (
    DEFAULT_CONFORMAL_ROOT
    / "core"
    / "truss_generation"
    / "cell_library"
)
TRUSS_GENERATION_ROOT = DEFAULT_CONFORMAL_ROOT / "core" / "truss_generation"
TRACEABILITY_COMMON_REQUIRED_FIELDS = (
    "candidate_id",
    "source_candidate_id",
    "target_sha256",
    "evidence_level",
    "selection_policy",
    "candidate_manifest_sha256",
    "run_manifest_sha256",
)
LEGACY_SELECTION_POLICY = "cad_ready_max_diameter_then_source_id_v1"
LEGACY_TRACEABILITY_REQUIRED_FIELDS = (
    "task_id",
    "run_id",
    "gate1_review",
    "gate1_review_basis",
    "gate1_review_artifact",
)
P10_SELECTION_POLICY = "scaler_range_weighted_RMSE_v1"
P10_TRACEABILITY_REQUIRED_FIELDS = (
    "task_id",
    "run_id",
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
PRODUCT_TRACEABILITY_REQUIRED_FIELDS = (
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


def run_conformal_service(request: dict) -> dict:
    """
    Validate a conformal service request and optionally run the minimal runtime.

    Runtime execution remains opt-in: only request["runtime_enabled"] is True
    imports and calls the conformal runtime dependencies.
    """
    try:
        if not isinstance(request, dict):
            raise ValueError("request must be a dict")
        reproduction_profile = request.get("reproduction_profile")
        if reproduction_profile is not None:
            from product_workflow.reproducibility import validate_reproduction_profile_v1

            reproduction_profile = validate_reproduction_profile_v1(reproduction_profile)
        if request.get("runtime_enabled") is True and not request.get(
            "allowed_output_roots"
        ):
            raise ValueError(
                "allowed_output_roots must be explicit for conformal runtime"
            )

        source_mesh_path = _resolve_source_mesh(request.get("source_mesh_path"))
        cell_type = str(request.get("cell_type") or "BCC")
        cell_config_path = _resolve_cell_config(
            cell_type,
            request.get("cell_config_path"),
        )
        output_root = _resolve_output_root(
            request.get("output_root"),
            request.get("allowed_output_roots"),
        )

        package_id = request.get("package_id")
        if not _is_safe_package_id(package_id):
            raise ValueError("invalid package_id")

        overwrite = bool(request.get("overwrite", False))
        package_dir = _resolve_package_dir(output_root, package_id, overwrite)

        diameter_source = request.get("diameter_source")
        technical_fixture_record_path = request.get("technical_fixture_record_path")
        case_manifest_path = request.get("case_manifest_path")
        product_mesh_binding = request.get("product_mesh_binding")
        source_mesh_contract = _validate_runtime_source_mesh_contract(
            request.get("source_mesh_contract"),
            required=product_mesh_binding is not None,
        )
        if technical_fixture_record_path is not None and (
            case_manifest_path is not None or product_mesh_binding is not None
        ):
            raise ValueError(
                "technical fixture is mutually exclusive with formal case and product mesh binding"
            )
        if technical_fixture_record_path is not None:
            technical_fixture_record_path = Path(technical_fixture_record_path).resolve()
            if not technical_fixture_record_path.is_file():
                raise FileNotFoundError("technical fixture record not found")
        if case_manifest_path is not None:
            case_manifest_path = Path(case_manifest_path).resolve()
            if not case_manifest_path.is_file():
                raise FileNotFoundError("case manifest not found")
        traceability = None
        if request.get("adapter_metadata_path") is not None:
            if "diameter" in request:
                raise ValueError(
                    "ambiguous diameter is not allowed with adapter metadata; "
                    "provide normalized_diameter and physical_diameter"
                )
            traceability = _load_verified_adapter_traceability(
                adapter_metadata_path=request["adapter_metadata_path"],
                cell_config_path=cell_config_path,
                normalized_diameter=request.get("normalized_diameter"),
                physical_diameter=request.get("physical_diameter"),
                reference_length=request.get("reference_length"),
                physical_scale_formula=request.get("physical_scale_formula"),
                physical_diameter_units=request.get("physical_diameter_units"),
                diameter_source=diameter_source,
            )
            diameter = traceability["physical_diameter"]
        else:
            diameter = request.get("diameter")
            if diameter is not None:
                diameter = _require_positive_finite_float(diameter, "diameter")

        normalized_product_binding = None
        if product_mesh_binding is not None:
            if traceability is None or traceability.get("selection_policy") not in PRODUCT_SELECTION_POLICIES:
                raise ValueError("product mesh binding requires product candidate traceability")
            normalized_product_binding = _validate_product_mesh_binding(
                product_mesh_binding,
                source_mesh_path=source_mesh_path,
            )
            binding_identity = _product_mesh_binding_runtime_identity(
                normalized_product_binding
            )
            if (
                source_mesh_contract is None
                or binding_identity["element_type_original"]
                != source_mesh_contract["element_type_original"]
                or binding_identity["element_type_effective"]
                != source_mesh_contract["element_type_effective"]
            ):
                raise ValueError(
                    "runtime source mesh contract does not match product mesh binding"
                )
            if (
                str(traceability.get("case_manifest_sha256") or "").upper()
                != str(binding_identity["mesh_binding_sha256"]).upper()
            ):
                raise ValueError(
                    "product Adapter scale binding does not match MeshBindingV1 identity"
                )

        case_precheck = None
        if case_manifest_path is not None:
            if traceability is None or not traceability.get("case_manifest_sha256"):
                raise ValueError(
                    "formal case runtime requires adapter case_manifest_sha256"
                )
            case_precheck = precheck_latticegpt_9x_case_manifest(
                case_manifest_path,
                source_mesh_path=source_mesh_path,
                expected_case_manifest_sha256=(
                    request.get("case_manifest_sha256")
                    or traceability["case_manifest_sha256"]
                ),
            )
            if source_mesh_contract is not None:
                case_types = case_precheck["mesh"]
                if (
                    case_types["element_type_original"]
                    != source_mesh_contract["element_type_original"]
                    or case_types["element_type_effective"]
                    != source_mesh_contract["element_type_effective"]
                ):
                    raise ValueError(
                        "runtime source mesh contract does not match case precheck"
                    )

        if request.get("runtime_enabled") is True:
            return _run_conformal_runtime(
                package_id=package_id,
                package_dir=package_dir,
                source_mesh_path=source_mesh_path,
                cell_type=cell_type,
                cell_config_path=cell_config_path,
                diameter=diameter,
                diameter_source=diameter_source,
                traceability=traceability,
                technical_fixture_record_path=technical_fixture_record_path,
                case_manifest_path=case_manifest_path,
                source_mesh_contract=(
                    source_mesh_contract
                    if source_mesh_contract is not None
                    else (
                        {
                            "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
                            "element_type_original": case_precheck["mesh"]["element_type_original"],
                            "element_type_effective": case_precheck["mesh"]["element_type_effective"],
                        }
                        if case_precheck is not None
                        else None
                    )
                ),
                product_mesh_binding=normalized_product_binding,
                expected_case_manifest_sha256=request.get(
                    "case_manifest_sha256"
                ),
            )

        return _build_pending_response(
            package_id=package_id,
            package_dir=package_dir,
            source_mesh_path=source_mesh_path,
            cell_type=cell_type,
            cell_config_path=cell_config_path,
            diameter=diameter,
            diameter_source=diameter_source,
            traceability=traceability,
        )
    except FileNotFoundError as exc:
        return _error_response(exc, "file_not_found")
    except ValueError as exc:
        return _error_response(exc, "invalid_request")
    except RuntimeError as exc:
        return _error_response(exc, "runtime_error")


def _is_safe_package_id(package_id: str) -> bool:
    if not isinstance(package_id, str):
        return False
    if not package_id or package_id in {".", ".."}:
        return False
    if ".." in package_id:
        return False
    return bool(SAFE_PACKAGE_ID_RE.fullmatch(package_id))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_output_root(
    output_root: str | Path | None,
    allowed_roots: list[str | Path] | None,
) -> Path:
    if allowed_roots is None:
        if output_root is None:
            raise ValueError("output_root or allowed_output_roots is required")
        roots = [output_root]
    else:
        roots = allowed_roots
    resolved_roots = [Path(root).resolve() for root in roots]
    if not resolved_roots:
        raise ValueError("allowed_output_roots must not be empty")

    resolved_output_root = (
        resolved_roots[0]
        if output_root is None
        else Path(output_root).resolve()
    )
    if not any(
        resolved_output_root == root or _is_relative_to(resolved_output_root, root)
        for root in resolved_roots
    ):
        raise ValueError("output_root is outside allowed roots")

    return resolved_output_root


def _resolve_package_dir(output_root: Path, package_id: str, overwrite: bool) -> Path:
    resolved_output_root = output_root.resolve()
    package_dir = (resolved_output_root / package_id).resolve()

    if not _is_relative_to(package_dir, resolved_output_root):
        raise ValueError("package_dir escapes output_root")
    if package_dir.exists() and not overwrite:
        raise ValueError("package_dir already exists and overwrite is false")

    return package_dir


def _resolve_source_mesh(source_mesh_path: str | Path) -> Path:
    if not source_mesh_path:
        raise FileNotFoundError("source_mesh_path is required")

    path = Path(source_mesh_path).resolve()
    if not path.is_file():
        raise FileNotFoundError("source mesh not found")
    if path.suffix.lower() != ".inp":
        raise ValueError("source mesh must be an .inp file")

    return path


def _resolve_cell_config(
    cell_type: str | None,
    cell_config_path: str | Path | None,
) -> Path:
    if cell_config_path:
        path = Path(cell_config_path).resolve()
    else:
        resolved_cell_type = str(cell_type or "BCC").lower()
        path = (DEFAULT_CELL_LIBRARY_DIR / f"{resolved_cell_type}.yml").resolve()

    if not path.is_file():
        raise FileNotFoundError("cell config not found")
    if path.suffix.lower() not in {".yml", ".yaml"}:
        raise ValueError("cell config must be a .yml or .yaml file")

    return path


def _load_verified_adapter_traceability(
    *,
    adapter_metadata_path: str | Path,
    cell_config_path: Path,
    normalized_diameter: Any,
    physical_diameter: Any,
    reference_length: Any,
    physical_scale_formula: Any,
    physical_diameter_units: Any,
    diameter_source: Any,
) -> dict[str, Any]:
    metadata_path = Path(adapter_metadata_path).resolve()
    if not metadata_path.is_file():
        raise FileNotFoundError("adapter metadata not found")
    if metadata_path.name != "adapter_metadata.json":
        raise ValueError("adapter metadata path must end with adapter_metadata.json")
    if metadata_path.parent != cell_config_path.parent:
        raise ValueError("adapter metadata must be adjacent to the cell config")
    if cell_config_path.name != "custom_cell.yml":
        raise ValueError("traceable runtime requires custom_cell.yml")
    normalized_diameter_value = _require_positive_finite_float(
        normalized_diameter,
        "normalized diameter",
    )
    physical_diameter_value = _require_positive_finite_float(
        physical_diameter,
        "physical diameter",
    )
    reference_length_value = _require_positive_finite_float(
        reference_length,
        "reference length",
    )
    if physical_scale_formula != PHYSICAL_SCALE_FORMULA:
        raise ValueError("physical scale formula is not supported")
    if physical_diameter_units != PHYSICAL_DIAMETER_UNITS:
        raise ValueError("physical diameter units must be mm")
    if diameter_source != PHYSICAL_DIAMETER_SOURCE:
        raise ValueError("physical diameter source is not supported")
    expected_physical_diameter = normalized_diameter_value * reference_length_value
    if not math.isclose(
        physical_diameter_value,
        expected_physical_diameter,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("physical diameter does not match normalized diameter and reference length")

    try:
        adapter_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("adapter metadata is not valid JSON") from exc

    if adapter_metadata.get("adapter_status") != "static_validated":
        raise ValueError("adapter metadata must be static_validated")
    if adapter_metadata.get("custom_cell_path") != cell_config_path.name:
        raise ValueError("adapter metadata custom_cell_path does not match cell config")
    try:
        adapter_diameter = float(adapter_metadata.get("diameter"))
    except (TypeError, ValueError) as exc:
        raise ValueError("adapter metadata diameter is invalid") from exc
    if not math.isclose(
        adapter_diameter,
        normalized_diameter_value,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("adapter metadata diameter does not match normalized diameter")

    adapter_traceability = adapter_metadata.get("traceability")
    if not isinstance(adapter_traceability, dict):
        raise ValueError("adapter metadata traceability is required")
    selection_policy = adapter_traceability.get("selection_policy")
    if selection_policy == LEGACY_SELECTION_POLICY:
        branch_fields = LEGACY_TRACEABILITY_REQUIRED_FIELDS
    elif selection_policy == P10_SELECTION_POLICY:
        branch_fields = P10_TRACEABILITY_REQUIRED_FIELDS
    elif selection_policy in PRODUCT_SELECTION_POLICIES:
        branch_fields = PRODUCT_TRACEABILITY_REQUIRED_FIELDS
    else:
        raise ValueError("adapter metadata selection_policy is not supported")
    missing_fields = [
        field
        for field in (*TRACEABILITY_COMMON_REQUIRED_FIELDS, *branch_fields)
        if field not in adapter_traceability
    ]
    if missing_fields:
        raise ValueError(
            "adapter metadata traceability missing required fields: "
            + ", ".join(missing_fields)
        )
    if adapter_metadata.get("candidate_id") != adapter_traceability["candidate_id"]:
        raise ValueError("adapter metadata candidate_id does not match traceability")
    for hash_field in (
        "target_sha256",
        "candidate_manifest_sha256",
        "run_manifest_sha256",
    ):
        value = adapter_traceability[hash_field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError(f"adapter metadata {hash_field} must be a SHA256 hex string")
    if adapter_traceability["evidence_level"] != "real_150w_candidate":
        raise ValueError("adapter metadata evidence_level must be real_150w_candidate")

    if selection_policy == LEGACY_SELECTION_POLICY:
        if adapter_traceability["gate1_review"] != "passed":
            raise ValueError("adapter metadata gate1_review must be passed")
        if adapter_traceability["gate1_review_basis"] != "independent_readonly_review":
            raise ValueError("adapter metadata gate1_review_basis is not supported")
        if adapter_traceability["gate1_review_artifact"] is not None:
            raise ValueError("adapter metadata gate1_review_artifact must be null")
    elif selection_policy == P10_SELECTION_POLICY:
        if adapter_traceability["selection_authority"] != "p10_ranking_manifest":
            raise ValueError("adapter metadata selection_authority is not supported")
        p10_sha = adapter_traceability["p10_ranking_manifest_sha256"]
        if not isinstance(p10_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", p10_sha):
            raise ValueError(
                "adapter metadata p10_ranking_manifest_sha256 must be a SHA256 hex string"
            )
        if (
            adapter_traceability["p10_schema_version"]
            != "latticegpt_9x_p10_ranking_manifest_v1"
        ):
            raise ValueError("adapter metadata p10_schema_version is not supported")
        if adapter_traceability["p10_evidence_level"] != "TECHNICAL_FIXTURE":
            raise ValueError("adapter metadata p10_evidence_level must be TECHNICAL_FIXTURE")
        if adapter_traceability["p10_ranking_status"] != "RANKED":
            raise ValueError("adapter metadata p10_ranking_status must be RANKED")
        if (
            adapter_traceability["p10_selected_status"]
            != "SURROGATE_RANKED_GEOMETRY_CANDIDATE"
        ):
            raise ValueError("adapter metadata p10_selected_status is not supported")
        if adapter_traceability["p09_status"] != "PASS":
            raise ValueError("adapter metadata p09_status must be PASS")
        if adapter_traceability["mechanical_match_status"] != "NOT_VALIDATED":
            raise ValueError(
                "adapter metadata mechanical_match_status must remain NOT_VALIDATED"
            )
        if adapter_traceability["ood_status"] != "not_available":
            raise ValueError("adapter metadata ood_status must remain not_available")
        if adapter_traceability["diameter_used_in_ranking"] is not False:
            raise ValueError("adapter metadata diameter_used_in_ranking must be false")
        non_claims = adapter_traceability["non_claims"]
        if not isinstance(non_claims, list) or not P10_REQUIRED_NON_CLAIMS.issubset(
            set(non_claims)
        ):
            raise ValueError("adapter metadata P-10 non_claims are incomplete")
    elif selection_policy in PRODUCT_SELECTION_POLICIES:
        selection_sha = adapter_traceability["selection_manifest_sha256"]
        if not isinstance(selection_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", selection_sha):
            raise ValueError(
                "adapter metadata selection_manifest_sha256 must be a SHA256 hex string"
            )
        if adapter_traceability["selected_candidate_id"] != adapter_traceability["candidate_id"]:
            raise ValueError("adapter metadata selected_candidate_id does not match candidate_id")
        if not isinstance(adapter_traceability["recommended_candidate_id"], str) or not adapter_traceability["recommended_candidate_id"]:
            raise ValueError("adapter metadata recommended_candidate_id is invalid")
        selected_rank = adapter_traceability["selected_rank"]
        if isinstance(selected_rank, bool) or not isinstance(selected_rank, int) or not 1 <= selected_rank <= 3:
            raise ValueError("adapter metadata selected_rank must be between 1 and 3")
        if adapter_traceability["selection_source"] not in ({"recommended_candidate", "user_override"} if adapter_traceability.get("selection_policy") == "product_candidate_selection_v2" else {"default_rank1", "user_override"}):
            raise ValueError("adapter metadata selection_source is not supported")
        if adapter_traceability["p09_status"] != "PASS":
            raise ValueError("adapter metadata p09_status must be PASS")
        if adapter_traceability["mechanical_match_status"] != "NOT_VALIDATED":
            raise ValueError(
                "adapter metadata mechanical_match_status must remain NOT_VALIDATED"
            )
        if adapter_traceability["diameter_used_in_ranking"] is not False:
            raise ValueError("adapter metadata diameter_used_in_ranking must be false")
        non_claims = adapter_traceability["non_claims"]
        if not isinstance(non_claims, list) or not PRODUCT_REQUIRED_NON_CLAIMS.issubset(
            set(non_claims)
        ):
            raise ValueError("adapter metadata product non_claims are incomplete")

    formal_binding = _validated_adapter_formal_binding(
        adapter_metadata,
        normalized_diameter=normalized_diameter_value,
        reference_length=reference_length_value,
        physical_diameter=physical_diameter_value,
    )

    return {
        **{
            field: adapter_traceability[field]
            for field in (*TRACEABILITY_COMMON_REQUIRED_FIELDS, *branch_fields)
        },
        "adapter_status": adapter_metadata["adapter_status"],
        "adapter_metadata_path": metadata_path.name,
        "custom_cell_path": cell_config_path.name,
        "adapter_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "custom_cell_sha256": hashlib.sha256(cell_config_path.read_bytes()).hexdigest(),
        "normalized_diameter": normalized_diameter_value,
        "normalized_diameter_source": "adapter_metadata.json:diameter",
        "reference_length": reference_length_value,
        "physical_scale_formula": PHYSICAL_SCALE_FORMULA,
        "physical_diameter": physical_diameter_value,
        "physical_diameter_units": PHYSICAL_DIAMETER_UNITS,
        "physical_diameter_source": PHYSICAL_DIAMETER_SOURCE,
        **formal_binding,
    }


def _validated_adapter_formal_binding(
    adapter_metadata: dict[str, Any],
    *,
    normalized_diameter: float,
    reference_length: float,
    physical_diameter: float,
) -> dict[str, Any]:
    case_manifest_sha256 = adapter_metadata.get("case_manifest_sha256")
    physical_scale = adapter_metadata.get("physical_scale")
    if case_manifest_sha256 is None and physical_scale is None:
        return {}
    if not isinstance(case_manifest_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", case_manifest_sha256
    ):
        raise ValueError("adapter metadata case_manifest_sha256 is invalid")
    if not isinstance(physical_scale, dict):
        raise ValueError("adapter metadata physical_scale is required")
    if (
        physical_scale.get("contract")
        != "average_equivalent_selected_hex_scale_v1"
    ):
        raise ValueError("adapter metadata physical scale contract is invalid")
    if physical_scale.get("units") != "mm":
        raise ValueError("adapter metadata physical scale units must be mm")
    if physical_scale.get("assignment_scope") != "global_uniform":
        raise ValueError("adapter metadata assignment scope must be global_uniform")

    comparisons = (
        ("normalized_diameter", normalized_diameter),
        ("reference_length_mm", reference_length),
        ("physical_diameter_mm", physical_diameter),
    )
    for field, expected in comparisons:
        actual = _require_positive_finite_float(
            physical_scale.get(field),
            f"adapter metadata physical_scale.{field}",
        )
        if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(
                f"adapter metadata physical_scale.{field} does not match request"
            )
    return {
        "case_manifest_sha256": case_manifest_sha256.upper(),
        "physical_scale_contract": physical_scale["contract"],
        "assignment_scope": physical_scale["assignment_scope"],
    }


def _require_traceability_fields(
    traceability: dict[str, Any],
    required_fields: tuple[str, ...],
) -> None:
    missing_fields = [field for field in required_fields if field not in traceability]
    if missing_fields:
        raise ValueError(
            "adapter metadata traceability missing required fields: "
            + ", ".join(missing_fields)
        )


def _require_positive_finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be positive and finite")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive and finite") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return normalized


def _validate_product_mesh_binding(
    binding: Any,
    *,
    source_mesh_path: Path,
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise ValueError("product mesh binding must be a mapping")
    if binding.get("schema_version") == "latticegpt_mesh_binding_v3":
        from product_workflow.mesh_identity import validate_mesh_binding_v3
        if source_mesh_path.name != "selected_hex.inp":
            raise ValueError("selected-domain binding requires the confirmed derivative")
        quality = binding.get("quality_evidence", {})
        if Path(str(quality.get("source", {}).get("path", ""))).resolve() != source_mesh_path.resolve():
            raise ValueError("selected-domain quality evidence path mismatch")
        return validate_mesh_binding_v3(binding,
            original_bytes=(source_mesh_path.parent / "source.inp").read_bytes(),
            derived_bytes=source_mesh_path.read_bytes(),
            expected_relationship=binding.get("relationship", {}), precheck=quality)
    if binding.get("schema_version") == "latticegpt_mesh_binding_v2":
        source = binding.get("source")
        semantics = binding.get("semantics")
        domain = binding.get("design_domain")
        digests = binding.get("digests")
        if not all(isinstance(item, dict) for item in (source, semantics, domain, digests)):
            raise ValueError("MeshBindingV2 nested contract is invalid")
        source_sha = hashlib.sha256(source_mesh_path.read_bytes()).hexdigest().upper()
        if source.get("raw_sha256") != source_sha:
            raise ValueError("product mesh binding SHA256 does not match source mesh")
        if source.get("units") != "mm" or source.get("frame") != "source_native_cartesian_frame":
            raise ValueError("product mesh binding source frame is not supported")
        if domain.get("selector_type") != "WHOLE_FILE_IMPLANT_DOMAIN":
            raise ValueError("product mesh binding design domain is not supported")
        original = semantics.get("element_type_original")
        effective = semantics.get("element_type_effective")
        expected_effective = {"C3D8": "C3D8", "C3D8R": "C3D8_GEOMETRY_COMPATIBLE"}.get(original)
        if effective != expected_effective:
            raise ValueError("product mesh binding element type contract is invalid")
        mesh_binding_sha = digests.get("mesh_binding_sha256")
        if not isinstance(mesh_binding_sha, str) or not re.fullmatch(r"[0-9A-F]{64}", mesh_binding_sha):
            raise ValueError("product mesh binding identity must be SHA256 upper")
        return dict(binding)
    if binding.get("schema_version") != "latticegpt_mesh_binding_v1":
        raise ValueError("product mesh binding schema is not supported")
    if Path(str(binding.get("stored_path") or "")).resolve() != source_mesh_path:
        raise ValueError("product mesh binding path does not match source mesh")
    source_sha = hashlib.sha256(source_mesh_path.read_bytes()).hexdigest().upper()
    if str(binding.get("sha256") or "").upper() != source_sha:
        raise ValueError("product mesh binding SHA256 does not match source mesh")
    if binding.get("units") != "mm":
        raise ValueError("product mesh binding units must be mm")
    if binding.get("frame") != "source_native_cartesian_frame":
        raise ValueError("product mesh binding frame is not supported")
    if binding.get("design_domain") != "whole_file_implant_domain":
        raise ValueError("product mesh binding design domain is not supported")
    original = binding.get("element_type_original")
    effective = binding.get("element_type_effective")
    expected_effective = {
        "C3D8": "C3D8",
        "C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
    }.get(original)
    if effective != expected_effective:
        raise ValueError("product mesh binding element type contract is invalid")
    mesh_binding_sha = binding.get("mesh_binding_sha256")
    if not isinstance(mesh_binding_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", mesh_binding_sha):
        raise ValueError("product mesh binding identity must be a SHA256 hex string")
    return dict(binding)


def _validate_runtime_source_mesh_contract(
    value: Any,
    *,
    required: bool,
) -> dict[str, str] | None:
    if value is None and not required:
        return None
    keys = {
        "schema_version", "element_type_original", "element_type_effective",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("runtime source mesh contract fields are invalid")
    if value["schema_version"] != "latticegpt_runtime_source_mesh_contract_v1":
        raise ValueError("runtime source mesh contract schema is invalid")
    expected = {
        "C3D8": "C3D8",
        "C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
        "C3D8+C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
    }.get(value["element_type_original"])
    if value["element_type_effective"] != expected:
        raise ValueError("runtime source mesh element type contract is invalid")
    return dict(value)


def _product_mesh_binding_runtime_identity(binding: dict[str, Any]) -> dict[str, str]:
    if binding.get("schema_version") in {"latticegpt_mesh_binding_v2", "latticegpt_mesh_binding_v3"}:
        semantics = binding["semantics"]
        digest = binding["digests"]["mesh_binding_sha256"]
        return {
            "element_type_original": semantics["element_type_original"],
            "element_type_effective": semantics["element_type_effective"],
            "mesh_binding_sha256": digest,
        }
    return {
        "element_type_original": binding["element_type_original"],
        "element_type_effective": binding["element_type_effective"],
        "mesh_binding_sha256": binding["mesh_binding_sha256"],
    }


def _build_pending_response(
    *,
    package_id: str,
    package_dir: Path,
    source_mesh_path: Path,
    cell_type: str,
    cell_config_path: Path,
    diameter: Any,
    diameter_source: Any,
    traceability: dict[str, Any] | None,
) -> dict:
    return {
        "status": "pending",
        "message": "conformal runtime execution is intentionally disabled in 6.11b skeleton",
        "package_id": package_id,
        "package_dir": str(package_dir),
        "source_mesh_path": str(source_mesh_path),
        "cell_type": cell_type,
        "cell_config_path": str(cell_config_path),
        "diameter": diameter,
        "diameter_source": diameter_source,
        "traceability": traceability,
        "runtime_enabled": False,
        "would_generate": {
            "source_mesh": "input/source_c3d8.inp",
            "b31": "core/conformal_b31.inp",
            "stl": "visualization/conformal_preview.stl",
            "png": "visualization/conformal_preview.png",
            "metadata": "metadata.json",
            "run_summary": "run_summary.txt",
        },
        "warnings": [
            "6.11b skeleton only",
            "conformal runtime not executed",
            "no conformal_result_package generated",
            "not 150w candidate adapter",
            "not dual-space fusion completion",
        ],
    }


def _run_conformal_runtime(
    *,
    package_id: str,
    package_dir: Path,
    source_mesh_path: Path,
    cell_type: str,
    cell_config_path: Path,
    diameter: Any,
    diameter_source: Any,
    traceability: dict[str, Any] | None,
    technical_fixture_record_path: Path | None,
    case_manifest_path: Path | None,
    source_mesh_contract: dict[str, str] | None,
    product_mesh_binding: dict[str, Any] | None,
    expected_case_manifest_sha256: str | None,
) -> dict:
    read_inp, elements_body_cube, conformal_lattice, build_package = (
        _load_runtime_dependencies()
    )

    input_dir = package_dir / "input"
    work_dir = package_dir.parent / "_work" / package_id
    input_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    source_mesh_copy = input_dir / "source_c3d8.inp"
    generated_b31_path = work_dir / "generated_b31.inp"
    shutil.copy2(source_mesh_path, source_mesh_copy)

    nodes, element_sets = read_inp(str(source_mesh_copy))
    source_element_type, source_element_type_effective, source_elements = (
        _select_runtime_hex_elements(
            element_sets,
            source_mesh_contract=source_mesh_contract,
        )
    )

    source_node_count = len(nodes)
    source_element_count = int(source_elements.shape[0])
    body = elements_body_cube(nodes, source_elements)
    beams = conformal_lattice(body, str(cell_config_path))
    beams.writeEdgesInp(file_=str(generated_b31_path))

    b31_text = generated_b31_path.read_text(encoding="utf-8").upper().replace(" ", "")
    if "*NODE" not in b31_text:
        raise RuntimeError("generated B31 inp is missing *NODE")
    if "*ELEMENT,TYPE=B31" not in b31_text:
        raise RuntimeError("generated B31 inp is missing *ELEMENT,TYPE=B31")

    package_payload = build_package(
        b31_path=str(generated_b31_path),
        output_dir=str(package_dir),
        cell_type=cell_type,
        diameter=None if diameter is None else float(diameter),
        diameter_source=diameter_source,
        result_id=package_id,
    )

    metadata_path = Path(package_payload["metadata_path"])
    run_summary_path = Path(package_payload["run_summary_path"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    warnings = list(metadata.get("warnings") or [])
    if traceability is not None:
        warnings = [
            warning
            for warning in warnings
            if warning != "not 150w candidate adapter"
        ]
    warnings.extend([
        "service runtime validation",
        "not dual-space fusion completion",
        "not real bone defect validation",
        "not spatially-varying generative lattice design",
    ])
    if traceability is None:
        warnings.append("not 150w candidate adapter")
    else:
        warnings.append("candidate traceability carried from adapter metadata")
    warnings = _dedupe_warnings(warnings)
    metadata.update({
        "result_id": package_id,
        "stage": "6.11d-conformal-service-runtime-validation",
        "source_c3d8_path": str(source_mesh_path),
        "source_c3d8_copy_path": str(source_mesh_copy),
        "generated_b31_path": str(generated_b31_path),
        "source_element_type": source_element_type,
        "source_element_type_original": source_element_type,
        "source_element_type_effective": source_element_type_effective,
        "source_node_count": source_node_count,
        "source_element_count": source_element_count,
        "b31_node_count": metadata["node_count"],
        "b31_beam_count": metadata["beam_count"],
        "warnings": warnings,
    })
    if traceability is not None:
        metadata["traceability"] = traceability
    if product_mesh_binding is not None:
        metadata["product_mesh_binding"] = product_mesh_binding
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    run_summary_lines = [
            "6.11d conformal service runtime validation",
            "",
            f"source_mesh_path={source_mesh_path}",
            f"source_element_type_original={source_element_type}",
            f"source_element_type_effective={source_element_type_effective}",
            f"source_node_count={source_node_count}",
            f"source_element_count={source_element_count}",
            f"cell_type={cell_type}",
            "",
            "B31 inp is the conformal design engineering output.",
            "STL/PNG are derived visualization outputs.",
            f"diameter={float(metadata['diameter']):.8f}",
            f"diameter_source={metadata['diameter_source']}",
            "",
    ]
    if traceability is None:
        run_summary_lines.append("This run did not use a 150w candidate.")
    else:
        run_summary_lines.append(
            "Candidate traceability was carried from adapter_metadata.json."
        )
        if traceability.get("selection_policy") in PRODUCT_SELECTION_POLICIES:
            run_summary_lines.extend([
                f"job_id={traceability['job_id']}",
                f"candidate_id={traceability['candidate_id']}",
                f"selection_source={traceability['selection_source']}",
            ])
        else:
            run_summary_lines.extend([
                f"task_id={traceability['task_id']}",
                f"run_id={traceability['run_id']}",
                f"candidate_id={traceability['candidate_id']}",
            ])
    run_summary_lines.extend([
        "This run did not run Abaqus.",
        "This run does not validate a real bone defect large sample.",
        "This run does not mean dual-space fusion is complete.",
        "This run does not mean spatially-varying generative lattice design is complete.",
        "",
    ])
    run_summary_path.write_text(
        "\n".join(run_summary_lines),
        encoding="utf-8",
    )

    package_contract = None
    if technical_fixture_record_path is not None or case_manifest_path is not None:
        package_contract = finalize_latticegpt_9x_package(
            package_dir=package_dir,
            package_id=package_id,
            source_mesh_path=source_mesh_path,
            technical_fixture_record_path=technical_fixture_record_path,
            case_manifest_path=case_manifest_path,
            traceability=traceability,
            source_mesh_contract=source_mesh_contract,
            expected_case_manifest_sha256=expected_case_manifest_sha256,
        )

    return {
        "status": "success",
        "message": "conformal runtime completed",
        "package_id": package_id,
        "package_dir": str(package_dir),
        "source_mesh_path": str(source_mesh_copy),
        "generated_b31_path": str(generated_b31_path),
        "core_b31_path": package_payload["core_b31_path"],
        "stl_path": package_payload["stl_path"],
        "png_path": package_payload["png_path"],
        "metadata_path": package_payload["metadata_path"],
        "run_summary_path": package_payload["run_summary_path"],
        "source_element_type": source_element_type,
        "source_element_type_original": source_element_type,
        "source_element_type_effective": source_element_type_effective,
        "source_node_count": source_node_count,
        "source_element_count": source_element_count,
        "b31_node_count": metadata["b31_node_count"],
        "b31_beam_count": metadata["b31_beam_count"],
        "cell_type": cell_type,
        "traceability": traceability,
        "package_contract": package_contract,
        "runtime_enabled": True,
        "warnings": warnings,
    }


def _select_runtime_hex_elements(
    element_sets: dict[str, Any],
    *,
    source_mesh_contract: dict[str, str] | None = None,
) -> tuple[str, str, Any]:
    supported = [name for name in ("C3D8", "C3D8R") if name in element_sets]
    if (set(element_sets) == {"C3D8", "C3D8R"}
        and source_mesh_contract is not None
        and source_mesh_contract.get("element_type_original") == "C3D8+C3D8R"
        and source_mesh_contract.get("element_type_effective") == "C3D8_GEOMETRY_COMPATIBLE"):
        # Both types use eight-node geometry; this adapter does not map other types.
        import torch
        return "C3D8+C3D8R", "C3D8_GEOMETRY_COMPATIBLE", torch.cat(
            [element_sets[name] for name in supported], dim=0)
    if len(supported) != 1:
        available = ", ".join(str(key) for key in element_sets)
        raise ValueError(
            "runtime source must contain exactly one C3D8 or C3D8R element type; "
            f"available={available}"
        )
    original_type = supported[0]
    effective_type = (
        "C3D8"
        if original_type == "C3D8"
        else "C3D8_GEOMETRY_COMPATIBLE"
    )
    if source_mesh_contract is not None:
        expected_original = source_mesh_contract.get("element_type_original")
        expected_effective = source_mesh_contract.get("element_type_effective")
        if (
            original_type != expected_original
            or effective_type != expected_effective
        ):
            raise ValueError(
                "runtime source element type identity does not match formal mesh contract: "
                f"runtime={original_type}/{effective_type}, "
                f"contract={expected_original}/{expected_effective}"
            )
    return original_type, effective_type, element_sets[original_type]


def _load_runtime_dependencies():
    project_root = Path(__file__).resolve().parent
    for path in [project_root, TRUSS_GENERATION_ROOT]:
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)

    from cad_module import build_conformal_result_package
    from conformalLattice import conformalLattice
    from elementsBody_cube import ElementsBody_cube
    from readInp import readInp

    return (
        readInp,
        ElementsBody_cube,
        conformalLattice,
        build_conformal_result_package,
    )


def _dedupe_warnings(warnings: list[str]) -> list[str]:
    deduped = []
    for warning in warnings:
        if warning not in deduped:
            deduped.append(warning)
    return deduped


def _error_response(exc: Exception, error_type: str) -> dict:
    return {
        "status": "error",
        "error_type": error_type,
        "message": str(exc),
        "runtime_enabled": False,
    }
