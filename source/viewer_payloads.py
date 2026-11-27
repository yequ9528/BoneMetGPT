from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import conformal_mesh_payloads


_CLIENT_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "authorization_header",
    "base_url",
    "python_executable",
    "raw_files",
    "stored_path",
    "temporary_directory",
    "temp_dir",
    "work_dir",
}
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _sanitize_client_value(value):
    if isinstance(value, dict):
        return {
            key: _sanitize_client_value(item)
            for key, item in value.items()
            if str(key).lower() not in _CLIENT_FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_client_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_client_value(item) for item in value]
    if isinstance(value, str):
        if "Traceback" in value:
            return "internal detail withheld"
        if _WINDOWS_ABSOLUTE_PATH_RE.match(value) or value.startswith("/"):
            return Path(value.replace("\\", "/")).name or "local path withheld"
    return value


def build_conformal_viewer_payload(
    package_id: str,
    *,
    resolve_package_dir,
    resolve_package_file,
    file_url_builder,
) -> dict:
    package_dir = resolve_package_dir(package_id)
    metadata_path = resolve_package_file(package_dir, "metadata")
    source_mesh_path = resolve_package_file(package_dir, "source_mesh")

    required_paths = {
        required_file_key: resolve_package_file(package_dir, required_file_key)
        for required_file_key in ("b31", "stl", "png", "run_summary")
    }
    p13_paths = _resolve_p13_paths(package_dir, resolve_package_file)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    downloads = _build_conformal_downloads(
        package_id,
        file_url_builder,
        include_p13=p13_paths is not None,
    )
    p13_evidence = _build_p13_evidence(
        package_id=package_id,
        p13_paths=p13_paths,
        b31_path=required_paths["b31"],
    )

    return {
        "status": "success",
        "mode": "conformal",
        "viewer_contract_version": "dual_space_v1",
        "package_id": package_id,
        "space_type": "geometric_space",
        "input_mesh": _build_conformal_input_mesh(metadata),
        "core_output": _build_conformal_core_output(metadata, downloads),
        "visualization": _build_conformal_visualization(downloads),
        "downloads": downloads,
        "parameters": _build_conformal_parameters(metadata),
        "traceability": _build_traceability(metadata),
        "product_identity": _build_product_identity(metadata, package_id),
        "mechanical_space": _build_mechanical_space(metadata),
        "geometric_space": _build_geometric_space(metadata, downloads, source_mesh_path),
        "fusion_workflow": _build_fusion_workflow(),
        "p13_evidence": p13_evidence,
        "warnings": _sanitize_client_value(
            _build_conformal_warnings(metadata, p13_evidence)
        ),
    }


def _build_conformal_downloads(
    package_id: str,
    file_url_builder,
    *,
    include_p13: bool,
) -> dict:
    downloads = {
        "b31": file_url_builder(package_id, "b31"),
        "stl": file_url_builder(package_id, "stl"),
        "png": file_url_builder(package_id, "png"),
        "metadata": file_url_builder(package_id, "metadata"),
        "run_summary": file_url_builder(package_id, "run_summary"),
    }
    if include_p13:
        downloads.update({
            "package_metadata": file_url_builder(package_id, "package_metadata"),
            "geometry_ledger": file_url_builder(package_id, "geometry_ledger"),
            "source_element_map": file_url_builder(package_id, "source_element_map"),
        })
    return downloads


def _resolve_p13_paths(package_dir: Path, resolve_package_file) -> dict | None:
    try:
        package_metadata_path = resolve_package_file(package_dir, "package_metadata")
    except FileNotFoundError:
        return None

    return {
        "package_metadata": package_metadata_path,
        "geometry_ledger": resolve_package_file(package_dir, "geometry_ledger"),
        "source_element_map": resolve_package_file(package_dir, "source_element_map"),
    }


def _build_p13_evidence(
    *,
    package_id: str,
    p13_paths: dict | None,
    b31_path: Path,
) -> dict:
    if p13_paths is None:
        return {
            "status": "not_available_legacy_package",
            "integrity_status": "NOT_EVALUATED",
        }

    package_metadata = _read_json_object(
        p13_paths["package_metadata"],
        "package_metadata",
    )
    geometry_ledger = _read_json_object(
        p13_paths["geometry_ledger"],
        "geometry_ledger",
    )
    recorded_package_id = (package_metadata.get("package") or {}).get("package_id")
    if recorded_package_id != package_id:
        raise ValueError("package_metadata package_id mismatch")

    _verify_output_sha256(
        package_metadata,
        "b31",
        b31_path,
    )
    geometry_sha256 = _verify_output_sha256(
        package_metadata,
        "geometry_ledger",
        p13_paths["geometry_ledger"],
    )
    source_map_sha256 = _verify_output_sha256(
        package_metadata,
        "source_element_map",
        p13_paths["source_element_map"],
    )

    source_map_row_count = _count_source_map_rows(p13_paths["source_element_map"])
    selected_element_count = (package_metadata.get("design_domain") or {}).get(
        "selected_element_count"
    )
    if source_map_row_count != selected_element_count:
        raise ValueError("source_element_map row count mismatch")

    acceptance = package_metadata.get("acceptance") or {}
    if geometry_ledger.get("technical_disposition") != acceptance.get(
        "technical_disposition"
    ):
        raise ValueError("geometry ledger technical disposition mismatch")
    if geometry_ledger.get("formal_gate_disposition") != acceptance.get(
        "formal_gate_disposition"
    ):
        raise ValueError("geometry ledger formal gate disposition mismatch")

    return {
        "status": "verified",
        "integrity_status": "PASS",
        "package_metadata_sha256": _sha256(p13_paths["package_metadata"]),
        "package_contract": _sanitize_client_value(package_metadata),
        "geometry_ledger": _sanitize_client_value(geometry_ledger),
        "source_element_map": {
            "path": "source_element_map.csv",
            "sha256": source_map_sha256,
            "data_row_count": source_map_row_count,
            "selected_element_count": selected_element_count,
        },
        "stable_sets": package_metadata.get("stable_sets") or {},
        "diameter": package_metadata.get("diameter") or {},
        "non_claims": list((package_metadata.get("claims") or {}).get("non_claims") or []),
        "verified_artifacts": {
            "b31": _sha256(b31_path),
            "geometry_ledger": geometry_sha256,
            "source_element_map": source_map_sha256,
        },
    }


def _read_json_object(path: Path, label: str) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _verify_output_sha256(package_metadata: dict, output_key: str, path: Path) -> str:
    outputs = package_metadata.get("outputs")
    output = outputs.get(output_key) if isinstance(outputs, dict) else None
    expected_sha256 = output.get("sha256") if isinstance(output, dict) else None
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError(f"{output_key} SHA256 is missing from package_metadata")

    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256.upper():
        raise ValueError(f"{output_key} SHA256 mismatch")
    return actual_sha256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _count_source_map_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required_columns = {
            "source_element_id",
            "selected",
            "selected_index",
            "element_type_original",
            "element_type_effective",
            "node_ids",
        }
        if not required_columns.issubset(set(reader.fieldnames or [])):
            raise ValueError("source_element_map schema mismatch")
        return sum(1 for _row in reader)


def _build_conformal_input_mesh(metadata: dict) -> dict:
    return {
        "path": "input/source_c3d8.inp",
        "element_type": metadata.get("source_element_type"),
        "node_count": metadata.get("source_node_count"),
        "element_count": metadata.get("source_element_count"),
    }


def _build_conformal_core_output(metadata: dict, downloads: dict) -> dict:
    return {
        "type": "B31_INP",
        "path": "core/conformal_b31.inp",
        "node_count": metadata.get("b31_node_count", metadata.get("node_count")),
        "beam_count": metadata.get("b31_beam_count", metadata.get("beam_count")),
        "role": "conformal design engineering output",
        "download_url": downloads["b31"],
    }


def _build_conformal_visualization(downloads: dict) -> dict:
    return {
        "stl_url": downloads["stl"],
        "png_url": downloads["png"],
        "role": "derived visualization output",
    }


def _build_conformal_parameters(metadata: dict) -> dict:
    return {
        "cell_type": metadata.get("cell_type"),
        "diameter": metadata.get("diameter"),
        "diameter_source": metadata.get("diameter_source"),
    }


def _build_traceability(metadata: dict) -> dict | None:
    traceability = metadata.get("traceability")
    return _sanitize_client_value(traceability) if isinstance(traceability, dict) else None


def _build_product_identity(metadata: dict, package_id: str) -> dict | None:
    context = metadata.get("product_context")
    binding = metadata.get("product_mesh_binding")
    if not isinstance(context, dict):
        return None
    if context.get("schema_version") != "latticegpt_product_viewer_context_v1":
        raise ValueError("product Viewer context schema mismatch")
    if not isinstance(binding, dict):
        raise ValueError("product Viewer mesh binding is missing")
    if context.get("mesh_binding_sha256") != binding.get("mesh_binding_sha256"):
        raise ValueError("product Viewer mesh binding identity mismatch")
    traceability = _build_traceability(metadata) or {}
    if context.get("selection_sha256") != traceability.get("selection_manifest_sha256"):
        raise ValueError("product Viewer selection identity mismatch")
    if context.get("target", {}).get("target_sha256") != traceability.get("target_sha256"):
        raise ValueError("product Viewer target identity mismatch")
    return _sanitize_client_value({
        **context,
        "package_id": package_id,
        "mesh": {
            "original_filename": context.get("mesh_original_filename"),
            "sha256": context.get("mesh_sha256"),
            "binding_sha256": context.get("mesh_binding_sha256"),
            "element_type_original": binding.get("element_type_original"),
            "element_type_effective": binding.get("element_type_effective"),
        },
    })


def _build_mechanical_space(metadata: dict) -> dict:
    traceability = _build_traceability(metadata)
    if traceability is not None:
        return {
            "candidate_adapter_status": traceability.get("adapter_status", "static_validated"),
            "candidate_id": traceability.get("candidate_id"),
            "candidate_graph": None,
            "candidate_stl_url": None,
            "candidate_png_url": None,
            "evidence_status": "traceability carried from adapter metadata",
        }
    return {
        "candidate_adapter_status": "not_validated",
        "candidate_graph": None,
        "candidate_stl_url": None,
        "candidate_png_url": None,
        "evidence_status": "future / not connected",
    }


def _build_geometric_space(metadata: dict, downloads: dict, source_mesh_path) -> dict:
    source_mesh = _build_conformal_input_mesh(metadata)
    return {
        "source_mesh": source_mesh,
        "source_mesh_visualization": conformal_mesh_payloads.build_source_mesh_visualization(
            source_mesh_path
        ),
        "b31_visualization": {
            "status": "future",
            "representation": "beam_graph",
        },
        "stl_png": {
            "stl_url": downloads["stl"],
            "png_url": downloads["png"],
            "role": "derived visualization output",
        },
    }


def _build_fusion_workflow() -> dict:
    return {
        "status": "not_validated",
        "steps": [],
    }


def _build_conformal_warnings(metadata: dict, p13_evidence: dict) -> list:
    warnings = list(metadata.get("warnings") or [])
    required_nonclaims = [
        "not dual-space fusion completion",
        "not real bone defect validation",
        "not spatially-varying generative lattice design",
        "readonly display only",
    ]
    if _build_traceability(metadata) is None:
        required_nonclaims.insert(0, "not 150w candidate adapter")
    if p13_evidence.get("status") == "verified":
        required_nonclaims.extend(p13_evidence.get("non_claims") or [])
    for warning in required_nonclaims:
        if warning not in warnings:
            warnings.append(warning)
    return warnings
