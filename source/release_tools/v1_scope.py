from __future__ import annotations

from pathlib import Path, PurePosixPath


ROLES = {
    "entry", "product_runtime", "inference_runtime", "conformal_runtime",
    "viewer_frontend", "launcher", "release_tool", "test", "config_example",
    "release_document",
}
LAYERS = {
    "mechanical_space", "geometric_space", "fusion_bridge",
    "output_and_visualization", "control_and_evidence",
}
LEGACY_CLASSIFICATIONS = {
    "SUPERSEDED_MIXED_ENTRY",
    "HISTORICAL_DUAL_SPACE_CONTROL_PLANE_EVIDENCE",
    "HISTORICAL_VERSION_COMPARISON",
    "TRAINING_OR_DEVELOPMENT_ONLY",
    "TEST_OR_FIXTURE_ONLY",
}


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("scope path must be a non-empty string")
    if "*" in value or "?" in value or "\\" in value:
        raise ValueError("scope paths must be exact POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError("scope path must be project-relative")
    text = path.as_posix()
    if "conformal_runtime_" in text or "candidate_cell_adapter_runtime_68" in text:
        raise ValueError("runtime artifacts cannot enter the V1 source allowlist")
    return text


def validate_allowlist(payload: dict, *, project_root: str | Path | None = None) -> dict:
    if payload.get("schema_version") != "latticegpt_v1_source_allowlist_v1":
        raise ValueError("allowlist schema mismatch")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("allowlist files must be non-empty")
    seen = set()
    normalized = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("allowlist entry must be an object")
        path = normalize_relative_path(entry.get("path"))
        folded = path.casefold()
        if folded in seen:
            raise ValueError("allowlist paths must be case-fold unique")
        seen.add(folded)
        role = entry.get("role")
        layers = entry.get("mainline_layers")
        if role not in ROLES:
            raise ValueError(f"unknown allowlist role: {role}")
        if not isinstance(layers, list) or not layers or any(layer not in LAYERS for layer in layers):
            raise ValueError("invalid mainline_layers")
        if project_root is not None and not (Path(project_root) / Path(path)).is_file():
            raise FileNotFoundError(f"allowlisted source is missing: {path}")
        normalized.append({"path": path, "role": role, "mainline_layers": sorted(set(layers))})
    result = dict(payload)
    result["files"] = sorted(normalized, key=lambda item: item["path"].casefold())
    return result


def validate_legacy(payload: dict) -> dict:
    if payload.get("schema_version") != "latticegpt_v1_legacy_exclusion_v1":
        raise ValueError("legacy exclusion schema mismatch")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("legacy entries must be a list")
    normalized = []
    for entry in entries:
        path = normalize_relative_path(entry.get("path"))
        if entry.get("classification") not in LEGACY_CLASSIFICATIONS:
            raise ValueError("unknown legacy classification")
        if entry.get("retention_required") is not True:
            raise ValueError("legacy retention_required must be true")
        if entry.get("comment_out_required") is not False:
            raise ValueError("legacy comment_out_required must be false")
        if entry.get("delete_authorized") is not False:
            raise ValueError("legacy delete_authorized must be false")
        normalized.append({**entry, "path": path})
    result = dict(payload)
    result["entries"] = sorted(normalized, key=lambda item: item["path"].casefold())
    return result


def classify_source(path: str) -> tuple[str, list[str]]:
    if path.startswith("tests/") or path.startswith("viewer_frontend/tests/"):
        return "test", ["control_and_evidence"]
    if path.startswith("viewer_frontend/"):
        return "viewer_frontend", ["output_and_visualization", "control_and_evidence"]
    if path.startswith("release_tools/") or path == "scripts/build_latticegpt_v1_source_release.py":
        return "release_tool", ["control_and_evidence"]
    if path.startswith("docs/"):
        return "release_document", ["control_and_evidence"]
    if path.startswith("environment/"):
        return "config_example", ["control_and_evidence"]
    if path.startswith("scripts/") or path.endswith(".bat"):
        return "launcher", ["control_and_evidence"]
    if path.startswith("config/"):
        return "config_example", ["control_and_evidence"]
    if path.startswith("TrussInvertibleDesign/") or path.startswith("services/"):
        return "inference_runtime", ["mechanical_space"]
    if path.startswith("smart_lattice_conformal_design/") or path in {
        "candidate_cell_adapter.py", "conformal_service.py", "cad_module.py",
        "latticegpt_9x_package_contract.py", "conformal_mesh_payloads.py", "viewer_payloads.py",
    }:
        return "conformal_runtime", ["geometric_space", "fusion_bridge"]
    if path in {"latticegpt_v1_app.py", "product_viewer_server.py"}:
        return "entry", ["mechanical_space", "geometric_space", "fusion_bridge", "output_and_visualization"]
    return "product_runtime", ["control_and_evidence"]
