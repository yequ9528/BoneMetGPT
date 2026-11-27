from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path, PurePosixPath


JS_IMPORT_RE = re.compile(r"(?:from\s+|import\s*\()(['\"])(\.[^'\"]+)\1")
JS_BARE_IMPORT_RE = re.compile(
    r"(?:from\s+|import\s+(?:[^'\"]+?\s+from\s+)?)(['\"])([^'\"]+)\1"
)
HTML_SCRIPT_RE = re.compile(r"(?:src|href)=['\"]([^'\"]+)['\"]")
SCRIPT_FILE_RE = re.compile(r"[\"']([A-Za-z0-9_./\\-]+\.(?:py|ps1|bat|html))[\"']")


def _resolve_module(root: Path, module: str, *, current: Path, level: int = 0) -> Path | None:
    if level:
        base = current.parent
        for _ in range(level - 1):
            base = base.parent
        candidate = base.joinpath(*module.split(".")) if module else base
    else:
        candidate = root.joinpath(*module.split("."))
    for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if path.is_file():
            return path.resolve()
    return None


def _resolve_js(source: Path, value: str) -> Path | None:
    candidate = (source.parent / value).resolve()
    for path in (
        candidate, candidate.with_suffix(".js"), candidate.with_suffix(".jsx"),
        candidate.with_suffix(".mjs"), candidate / "index.js", candidate / "index.jsx",
    ):
        if path.is_file():
            return path
    return None


def analyze_import_closure(
    project_root: str | Path,
    allowlist: dict,
    legacy: dict,
    *,
    dynamic_local_edges: list[dict] | None = None,
    dynamic_local_import_roots: set[str] | None = None,
    python_distribution_map: dict[str, str] | None = None,
    python_direct_requirements_path: str | None = None,
    node_package_json_path: str | None = None,
    node_package_lock_path: str | None = None,
) -> dict:
    root = Path(project_root).resolve()
    allowed = {entry["path"] for entry in allowlist["files"]}
    legacy_paths = {entry["path"] for entry in legacy.get("entries", [])}
    local_edges = []
    python_import_roots = set()
    node_import_roots = set()
    unresolved = []
    outside = []
    legacy_dependencies = []

    def record(source_rel: str, target: Path | None, raw: str):
        if target is None:
            unresolved.append({"source": source_rel, "import": raw})
            return
        try:
            target_rel = target.relative_to(root).as_posix()
        except ValueError:
            unresolved.append({"source": source_rel, "import": raw})
            return
        edge = {"source": source_rel, "target": target_rel}
        local_edges.append(edge)
        if target_rel in legacy_paths:
            legacy_dependencies.append(edge)
        elif target_rel not in allowed:
            outside.append(edge)

    for source_rel in sorted(allowed, key=str.casefold):
        source = root / Path(source_rel)
        suffix = source.suffix.lower()
        text = source.read_text(encoding="utf-8", errors="strict")
        if suffix == ".py":
            tree = ast.parse(text, filename=source_rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target = _resolve_module(root, alias.name, current=source)
                        if target is None:
                            python_import_roots.add(alias.name.split(".")[0])
                        else:
                            record(source_rel, target, alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    target = _resolve_module(root, module, current=source, level=node.level)
                    if target is None and node.level == 0:
                        python_import_roots.add(module.split(".")[0])
                    else:
                        record(source_rel, target, "." * node.level + module)
        elif suffix in {".js", ".jsx", ".mjs"}:
            for match in JS_IMPORT_RE.finditer(text):
                record(source_rel, _resolve_js(source, match.group(2)), match.group(2))
            for match in JS_BARE_IMPORT_RE.finditer(text):
                value = match.group(2)
                if value.startswith("."):
                    continue
                if value.startswith("node:"):
                    node_import_roots.add(value)
                elif value.startswith("@"):
                    node_import_roots.add("/".join(value.split("/")[:2]))
                else:
                    node_import_roots.add(value.split("/")[0])
        elif suffix == ".html":
            for value in HTML_SCRIPT_RE.findall(text):
                if value.startswith("/src/"):
                    record(source_rel, (root / "viewer_frontend" / value.lstrip("/")).resolve(), value)
        elif suffix in {".ps1", ".bat"}:
            for value in SCRIPT_FILE_RE.findall(text):
                normalized = value.replace("\\", "/")
                candidate = (root / normalized).resolve()
                if candidate.is_file():
                    record(source_rel, candidate, value)

    declared = []
    for edge in dynamic_local_edges or []:
        target = (root / Path(edge["target"])).resolve()
        declared.append({"source": edge["source"], "target": edge["target"]})
        record(edge["source"], target if target.is_file() else None, edge["target"])

    def unique(values):
        return sorted({tuple(sorted(item.items())) for item in values})

    def objects(values):
        return [dict(items) for items in unique(values)]

    dynamic_roots = set(dynamic_local_import_roots or ())
    stdlib = sorted(
        root_name
        for root_name in python_import_roots
        if root_name in sys.stdlib_module_names or root_name == "__future__"
    )
    local_dynamic = sorted(python_import_roots & dynamic_roots)
    unresolved_external = python_import_roots - set(stdlib) - set(local_dynamic)
    distribution_map = python_distribution_map or {}
    mapped_python = [
        {"import_root": root_name, "distribution": distribution_map[root_name]}
        for root_name in sorted(unresolved_external)
        if root_name in distribution_map
    ]
    unmapped_python = sorted(unresolved_external - set(distribution_map))

    controlled_requirements: set[str] = set()
    if python_direct_requirements_path:
        requirements_path = root / python_direct_requirements_path
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*==", line)
            if match:
                controlled_requirements.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    missing_python_controls = sorted(
        item["distribution"]
        for item in mapped_python
        if re.sub(r"[-_.]+", "-", item["distribution"]).lower()
        not in controlled_requirements
    )

    node_control = {
        "package_json": node_package_json_path,
        "package_lock": node_package_lock_path,
        "status": "NOT_CONFIGURED",
        "runtime_imports": sorted(node_import_roots - {item for item in node_import_roots if item.startswith("node:")}),
        "node_stdlib_imports": sorted(item for item in node_import_roots if item.startswith("node:")),
        "missing_from_lock": [],
    }
    if node_package_json_path and node_package_lock_path:
        package_payload = json.loads((root / node_package_json_path).read_text(encoding="utf-8"))
        lock_payload = json.loads((root / node_package_lock_path).read_text(encoding="utf-8"))
        declared_node = set((package_payload.get("dependencies") or {})) | set(
            package_payload.get("devDependencies") or {}
        )
        lock_root = (lock_payload.get("packages") or {}).get("") or {}
        locked_node = set(lock_root.get("dependencies") or {}) | set(
            lock_root.get("devDependencies") or {}
        )
        missing_node = sorted((node_import_roots - {item for item in node_import_roots if item.startswith("node:")}) - locked_node)
        declaration_drift = sorted(declared_node ^ locked_node)
        node_control["missing_from_lock"] = missing_node
        node_control["declaration_drift"] = declaration_drift
        node_control["status"] = "PASS" if not missing_node and not declaration_drift else "HOLD"

    report = {
        "schema_version": "latticegpt_v1_import_closure_report_v1",
        "status": "PASS",
        "entry_points": sorted(allowlist.get("entry_points", {}).values()),
        "local_edges": objects(local_edges),
        "dynamic_local_edges": sorted(declared, key=lambda item: (item["source"], item["target"])),
        "external_dependencies": [item["import_root"] for item in mapped_python],
        "python_dependency_control": {
            "stdlib_imports": stdlib,
            "dynamic_local_imports": local_dynamic,
            "third_party_imports": mapped_python,
            "unmapped_imports": unmapped_python,
            "direct_requirements": python_direct_requirements_path,
            "missing_from_direct_requirements": missing_python_controls,
            "lock_file": "environment/python-requirements-v1.lock.txt",
            "lock_status": "NOT_IN_R2_1_SOURCE_IDENTITY_TASK_15_BOUND",
        },
        "node_dependency_control": node_control,
        "unresolved_local_imports": objects(unresolved),
        "imports_outside_allowlist": objects(outside),
        "legacy_dependencies": objects(legacy_dependencies),
    }
    if (
        report["unresolved_local_imports"]
        or report["imports_outside_allowlist"]
        or report["legacy_dependencies"]
        or unmapped_python
        or missing_python_controls
        or node_control["status"] == "HOLD"
    ):
        report["status"] = "HOLD_IMPORT_CLOSURE"
    return report
