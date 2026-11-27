from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

from .v1_import_closure import _resolve_js, _resolve_module
from .v1_scope import classify_source, validate_allowlist, validate_legacy


MAINLINE = "LLM-assisted dual-space generative design for bone implants"


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def file_identity(path: Path) -> dict:
    payload = path.read_bytes()
    return {"size_bytes": len(payload), "sha256": sha256_bytes(payload)}


def discover_local_paths(project_root: str | Path, seeds: list[str]) -> list[str]:
    root = Path(project_root).resolve()
    pending = list(seeds)
    discovered = set()
    js_re = re.compile(r"(?:from\s+|import\s*\()(['\"])(\.[^'\"]+)\1")
    while pending:
        relative = Path(pending.pop()).as_posix()
        if relative in discovered:
            continue
        path = root / Path(relative)
        if not path.is_file():
            raise FileNotFoundError(f"R1 seed source is missing: {relative}")
        discovered.add(relative)
        suffix = path.suffix.lower()
        text = path.read_text(encoding="utf-8", errors="strict") if suffix not in {".json"} else ""
        targets = []
        if suffix == ".py":
            tree = ast.parse(text, filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets.extend(
                        target for alias in node.names
                        if (target := _resolve_module(root, alias.name, current=path)) is not None
                    )
                elif isinstance(node, ast.ImportFrom):
                    target = _resolve_module(root, node.module or "", current=path, level=node.level)
                    if target is not None:
                        targets.append(target)
        elif suffix in {".js", ".jsx", ".mjs"}:
            for match in js_re.finditer(text):
                target = _resolve_js(path, match.group(2))
                if target is not None:
                    targets.append(target)
        elif suffix == ".html":
            for value in re.findall(r"src=['\"](/src/[^'\"]+)['\"]", text):
                targets.append((root / "viewer_frontend" / value.lstrip("/")).resolve())
        for target in targets:
            target_relative = target.relative_to(root).as_posix()
            if target_relative not in discovered:
                pending.append(target_relative)
    return sorted(discovered, key=str.casefold)


def build_allowlist(
    project_root: str | Path,
    seeds: list[str],
    *,
    frozen: bool,
    stage: str = "R1",
) -> dict:
    files = []
    for path in discover_local_paths(project_root, seeds):
        role, layers = classify_source(path)
        files.append({"path": path, "role": role, "mainline_layers": layers})
    return validate_allowlist({
        "schema_version": "latticegpt_v1_source_allowlist_v1",
        "release_version": "1.0",
        "status": f"FROZEN_{stage}" if frozen else "CANDIDATE_NOT_FROZEN",
        "entry_points": {
            "chainlit": "latticegpt_v1_app.py",
            "viewer_backend": "product_viewer_server.py",
            "viewer_frontend": "viewer_frontend/product.html",
            "launcher": "start_latticegpt_v1.bat",
        },
        "files": files,
    }, project_root=project_root)


def build_legacy_exclusion() -> dict:
    entries = []
    mapping = {
        "SUPERSEDED_MIXED_ENTRY": [
            "app.py", "viewer_server.py", "viewer_frontend/src/App.jsx",
            "scripts/start_latticegpt_dev.ps1", "scripts/stop_latticegpt_dev.ps1",
        ],
        "HISTORICAL_DUAL_SPACE_CONTROL_PLANE_EVIDENCE": [
            "candidate_adapter_preview_wrapper.py", "conformal_service_precheck_wrapper.py",
            "mechanical_evidence_wrapper.py", "viewer_evidence_link_wrapper.py", "llm_tool_registry.py",
        ],
        "HISTORICAL_VERSION_COMPARISON": [
            "dl_module.py", "rag_module.py", "viz_module.py",
            "truss_candidate_acquisition.py", "truss_candidate_persistence.py",
        ],
        "TRAINING_OR_DEVELOPMENT_ONLY": [
            "TrussInvertibleDesign/main_export_mlp_test.py",
            "TrussInvertibleDesign/main_train_inv_MLP_test.py",
            "TrussInvertibleDesign/main_train_inv_test.py",
            "TrussInvertibleDesign/main_train_test.py",
        ],
        "TEST_OR_FIXTURE_ONLY": [
            "tests/2026-4-24", "tests/test_product_viewer_package_resolution.py",
        ],
    }
    for classification, paths in mapping.items():
        for path in paths:
            entries.append({
                "path": path,
                "classification": classification,
                "release_status": "EXCLUDED_FROM_V1_RELEASE",
                "retention_required": True,
                "evidence_role": classification.lower(),
                "comment_out_required": False,
                "delete_authorized": False,
            })
    return validate_legacy({
        "schema_version": "latticegpt_v1_legacy_exclusion_v1",
        "release_version": "1.0",
        "status": "RETAINED_AND_EXCLUDED",
        "external_evidence_exclusions": [f"conformal_runtime_{index}" for index in range(60, 70)],
        "entries": entries,
    })


def build_source_manifest(
    project_root: str | Path,
    allowlist: dict,
    *,
    allowlist_bytes: bytes,
    legacy_bytes: bytes,
    import_report_bytes: bytes,
    hardcoded_report_bytes: bytes,
    stage: str = "R1",
    release_prefix: str = "release/v1",
) -> dict:
    root = Path(project_root).resolve()
    files = []
    for entry in allowlist["files"]:
        identity = file_identity(root / Path(entry["path"]))
        files.append({**entry, **identity})
    generator_path = "scripts/build_latticegpt_v1_source_release.py"
    generator_identity = file_identity(root / generator_path)
    return {
        "schema_version": "latticegpt_v1_source_manifest_v1",
        "release_version": "1.0",
        "status": f"PASS_{stage}_SOURCE_IDENTITY",
        "mainline": MAINLINE,
        "dual_space_mainline_static_contract": "PASS",
        "full_fusion_claim": "NOT_MADE",
        "non_claims": ["NOT_FULL_DUAL_SPACE_FUSION"],
        "controlled_artifact_boundary": "SOURCE_MANIFEST_EXCLUDES_CONTROLLED_ARTIFACT_BYTES",
        "entry_points": allowlist["entry_points"],
        "generator": {"path": generator_path, **generator_identity},
        "control_files": {
            "allowlist": {"path": f"{release_prefix}/v1_source_allowlist.json", "size_bytes": len(allowlist_bytes), "sha256": sha256_bytes(allowlist_bytes)},
            "legacy": {"path": f"{release_prefix}/LEGACY_EXCLUDED_FROM_V1_RELEASE.json", "size_bytes": len(legacy_bytes), "sha256": sha256_bytes(legacy_bytes)},
            "import_closure": {"path": f"{release_prefix}/reports/import_closure_report_v1.json", "size_bytes": len(import_report_bytes), "sha256": sha256_bytes(import_report_bytes), "status": "PASS"},
            "hardcoded_paths": {"path": f"{release_prefix}/reports/hardcoded_path_scan_report_v1.json", "size_bytes": len(hardcoded_report_bytes), "sha256": sha256_bytes(hardcoded_report_bytes), "status": "PASS"},
        },
        "files": files,
    }


def detached_sha_bytes(manifest_bytes: bytes) -> bytes:
    return f"{sha256_bytes(manifest_bytes)}  source_manifest_v1.json\n".encode("ascii")
