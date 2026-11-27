from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import math

from candidate_cell_adapter import (
    ADAPTER_METADATA_SCHEMA_VERSION,
    build_custom_cell_from_candidate,
    validate_custom_cell_yml,
)
from conformal_service import (
    PHYSICAL_DIAMETER_SOURCE,
    PHYSICAL_SCALE_FORMULA,
    run_conformal_service,
)
from .contracts import build_adapter_run_result, canonical_sha256
from .config import require_within


def _mesh_binding_sha256(binding: Mapping[str, Any]) -> str:
    if binding.get("schema_version") in {"latticegpt_mesh_binding_v2", "latticegpt_mesh_binding_v3"}:
        return str(((binding.get("digests") or {}).get("mesh_binding_sha256") or ""))
    return str(binding.get("mesh_binding_sha256") or "")


def adapter_controlled_write(**request: Any) -> dict[str, Any]:
    if request.get("overwrite") is not False:
        raise ValueError("product Adapter overwrite must be false")
    output_dir = request.get("output_dir")
    if not isinstance(output_dir, (str, Path)) or not str(output_dir).strip():
        raise ValueError("product Adapter output directory is required")
    allowed_output_root = request.get("allowed_output_root")
    if not isinstance(allowed_output_root, (str, Path)) or not str(allowed_output_root).strip():
        raise ValueError("product Adapter allowed output root is required")
    controlled_output_dir = require_within(
        output_dir,
        allowed_output_root,
        label="product Adapter output directory",
    )
    binding = request.get("mesh_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("a validated mesh binding is required")
    reference_length = request.get("reference_length_mm")
    if (
        not isinstance(reference_length, (int, float))
        or isinstance(reference_length, bool)
        or not math.isfinite(reference_length)
        or reference_length <= 0
    ):
        raise ValueError("reference_length_mm must be a finite positive number")
    reference_length = float(reference_length)
    result = build_custom_cell_from_candidate(
        request.get("candidate"),
        output_path=controlled_output_dir,
        overwrite=False,
        dry_run=False,
        traceability=request.get("traceability"),
        formal_case_binding={
            "schema_version": "latticegpt_9x_adapter_case_binding_v1",
            "case_manifest_sha256": _mesh_binding_sha256(binding),
            "physical_scale_contract": "average_equivalent_selected_hex_scale_v1",
            "reference_length_mm": reference_length,
            "assignment_scope": "global_uniform",
        },
        allowed_output_root=allowed_output_root,
    )
    return _normalize_adapter_result(
        result,
        candidate=request.get("candidate"),
        traceability=request.get("traceability"),
        mesh_binding=binding,
        output_dir=controlled_output_dir,
    )


def _normalize_adapter_result(
    result: Any,
    *,
    candidate: Any,
    traceability: Any,
    mesh_binding: Mapping[str, Any],
    output_dir: Any,
) -> dict[str, Any]:
    candidate_id = str((candidate or {}).get("candidate_id") or "")
    selection_sha = str((traceability or {}).get("selection_manifest_sha256") or "")
    mesh_sha = _mesh_binding_sha256(mesh_binding)
    if isinstance(result, Mapping):
        raw_result = dict(result)
        producer_status = str(raw_result.get("status") or "missing")
        producer_result_sha = canonical_sha256(_json_safe_mapping(raw_result))
        errors = [str(item) for item in (raw_result.get("errors") or [])]
        warnings = [str(item) for item in (raw_result.get("warnings") or [])]
    else:
        raw_result = {}
        producer_status = "invalid_result_type"
        producer_result_sha = hashlib.sha256(repr(result).encode("utf-8")).hexdigest().upper()
        errors = ["Adapter producer result must be a mapping"]
        warnings = []

    artifacts: dict[str, dict[str, Any] | None] = {
        "custom_cell": None,
        "adapter_metadata": None,
    }
    producer_success = (
        producer_status == "static_validated"
        and raw_result.get("adapter_status") == "static_validated"
        and raw_result.get("dry_run") is False
        and not errors
    )
    if not producer_success:
        errors.append(
            "Adapter producer did not return the required static_validated write result"
        )
    else:
        expected_dir = Path(str(output_dir or "")).resolve()
        expected_paths = {
            "custom_cell": expected_dir / "custom_cell.yml",
            "adapter_metadata": expected_dir / "adapter_metadata.json",
        }
        raw_paths = {
            "custom_cell": raw_result.get("custom_cell_path"),
            "adapter_metadata": raw_result.get("adapter_metadata_path"),
        }
        for name in ("custom_cell", "adapter_metadata"):
            raw_path = raw_paths[name]
            if not raw_path:
                errors.append(f"{name} path is missing from Adapter result")
                continue
            path = Path(str(raw_path)).resolve()
            if path != expected_paths[name]:
                errors.append(f"{name} path does not match the controlled output path")
                continue
            if not path.is_file():
                errors.append(f"{name} output file is missing")
                continue
            artifacts[name] = _artifact_identity(path)

        custom_cell = artifacts["custom_cell"]
        if custom_cell is not None:
            errors.extend(validate_custom_cell_yml(custom_cell["path"]))

        metadata_artifact = artifacts["adapter_metadata"]
        if metadata_artifact is not None:
            try:
                metadata = json.loads(
                    Path(metadata_artifact["path"]).read_text(encoding="utf-8")
                )
            except Exception as exc:
                errors.append(f"adapter_metadata.json parse failed: {exc}")
            else:
                if not isinstance(metadata, Mapping):
                    errors.append("adapter_metadata.json must contain an object")
                else:
                    metadata_traceability = metadata.get("traceability") or {}
                    if metadata.get("schema_version") != ADAPTER_METADATA_SCHEMA_VERSION:
                        errors.append("adapter metadata schema mismatch")
                    if metadata.get("adapter_status") != "static_validated":
                        errors.append("adapter metadata status mismatch")
                    if metadata.get("custom_cell_path") != "custom_cell.yml":
                        errors.append("adapter metadata custom-cell path mismatch")
                    if metadata.get("candidate_id") != candidate_id:
                        errors.append("adapter metadata candidate identity mismatch")
                    if metadata_traceability.get("selected_candidate_id") != candidate_id:
                        errors.append("adapter metadata selected candidate identity mismatch")
                    if metadata_traceability.get("selection_manifest_sha256") != selection_sha:
                        errors.append("adapter metadata selection identity mismatch")
                    if metadata.get("case_manifest_sha256") != mesh_sha:
                        errors.append("adapter metadata mesh binding identity mismatch")

    status = "SUCCESS" if not errors else "FAILED"
    return build_adapter_run_result(
        status=status,
        producer="candidate_cell_adapter",
        producer_status=producer_status,
        producer_result_sha256=producer_result_sha,
        artifacts=artifacts,
        candidate_id=candidate_id,
        selection_manifest_sha256=selection_sha,
        mesh_binding_sha256=mesh_sha,
        errors=errors,
        warnings=warnings,
    )


def _artifact_identity(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
    }


def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))


def _runtime_source_mesh_contract(value: Any) -> dict[str, str]:
    keys = {
        "schema_version", "element_type_original", "element_type_effective",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("runtime source mesh contract fields are invalid")
    result = dict(value)
    if result["schema_version"] != "latticegpt_runtime_source_mesh_contract_v1":
        raise ValueError("runtime source mesh contract schema is invalid")
    expected = {
        "C3D8": "C3D8",
        "C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
        "C3D8+C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
    }.get(result["element_type_original"])
    if result["element_type_effective"] != expected:
        raise ValueError("runtime source mesh element type contract is invalid")
    return result


def conformal_controlled_run(**request: Any) -> dict[str, Any]:
    if request.get("overwrite") is not False:
        raise ValueError("product conformal overwrite must be false")
    package_id = str(request.get("package_id") or "")
    allowed_output_root = request.get("allowed_output_root")
    if not isinstance(allowed_output_root, (str, Path)) or not str(allowed_output_root).strip():
        raise ValueError("product conformal allowed output root is required")
    resolved_allowed_root = Path(allowed_output_root).resolve()
    package_dir = require_within(
        request.get("output_dir") or "",
        resolved_allowed_root,
        label="product conformal output directory",
    )
    if package_dir.name != package_id:
        raise ValueError("product conformal output directory must end with package_id")
    reference_length = float(request["reference_length"])
    normalized_diameter = float(request["normalized_diameter"])
    source_mesh_contract = _runtime_source_mesh_contract(
        request.get("source_mesh_contract")
    )
    result = run_conformal_service({
        "source_mesh_path": request["source_mesh_path"],
        "cell_type": "CUSTOM",
        "cell_config_path": request["custom_cell_path"],
        "adapter_metadata_path": request["adapter_metadata_path"],
        "normalized_diameter": normalized_diameter,
        "physical_diameter": normalized_diameter * reference_length,
        "reference_length": reference_length,
        "physical_scale_formula": PHYSICAL_SCALE_FORMULA,
        "physical_diameter_units": "mm",
        "diameter_source": PHYSICAL_DIAMETER_SOURCE,
        "product_mesh_binding": request["mesh_binding"],
        "source_mesh_contract": source_mesh_contract,
        "case_manifest_path": request.get("case_manifest_path"),
        "case_manifest_sha256": request.get("case_manifest_sha256"),
        "reproduction_profile": request.get("reproduction_profile"),
        "output_root": str(package_dir.parent),
        "allowed_output_roots": [str(resolved_allowed_root)],
        "package_id": package_id,
        "overwrite": False,
        "runtime_enabled": True,
    })
    if result.get("status") == "success":
        metadata_path = Path(result["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["product_context"] = request["product_context"]
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


def verify_product_package(**request: Any) -> dict[str, Any]:
    package_id = str(request.get("package_id") or "")
    package_dir = Path(str(request.get("package_dir") or "")).resolve()
    metadata_path = package_dir / "metadata.json"
    if not metadata_path.is_file():
        return {"status": "failed", "reason": "metadata.json is missing"}
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("result_id") != package_id:
        return {"status": "failed", "reason": "package identity mismatch"}
    traceability = metadata.get("traceability") or {}
    frozen = request.get("frozen_inputs") or {}
    if traceability.get("target_sha256") != frozen.get("target_sha256"):
        return {"status": "failed", "reason": "target identity mismatch"}
    if traceability.get("selection_manifest_sha256") != frozen.get("selection_sha256"):
        return {"status": "failed", "reason": "selection identity mismatch"}
    binding = metadata.get("product_mesh_binding") or {}
    if _mesh_binding_sha256(binding) != frozen.get("mesh_binding_sha256"):
        return {"status": "failed", "reason": "mesh identity mismatch"}
    context = metadata.get("product_context") or {}
    if context.get("selection_sha256") != frozen.get("selection_sha256"):
        return {"status": "failed", "reason": "Viewer selection identity mismatch"}
    verified = {"status": "passed", "package_id": package_id, "package_dir": str(package_dir)}
    if request.get("producer") is not None:
        from .artifacts import validate_product_six_file_package
        verified["readonly_receipt"] = validate_product_six_file_package(
            job_snapshot=request["job_snapshot"], package_dir=package_dir,
            normalized_mesh=request["normalized_mesh"], producer=request["producer"], expected_file_identities=None)
    return verified
