from __future__ import annotations

import re
from pathlib import Path


DRIVE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
UNC_RE = re.compile(r"\\\\[^\\\s]+\\[^\\\s]+")
RUNTIME_RE = re.compile(r"conformal_runtime_6[0-9]|candidate_cell_adapter_runtime_68")


def scan_hardcoded_paths(project_root: str | Path, allowlist: dict) -> dict:
    root = Path(project_root).resolve()
    findings = []
    for entry in allowlist["files"]:
        relative = entry["path"]
        path = root / Path(relative)
        text = path.read_text(encoding="utf-8", errors="strict")
        for line_number, line in enumerate(text.splitlines(), 1):
            matches = []
            if DRIVE_RE.search(line): matches.append("WINDOWS_DRIVE_PATH")
            if UNC_RE.search(line): matches.append("UNC_MACHINE_PATH")
            if RUNTIME_RE.search(line): matches.append("HISTORICAL_RUNTIME_PATH")
            for rule_id in matches:
                if entry["role"] == "test":
                    status, classification = "ALLOWED", "SYNTHETIC_TEST_PATH"
                elif line.lstrip().startswith("#"):
                    status, classification = "ALLOWED", "COMMENT_OR_DOC_EXAMPLE"
                elif entry["role"] == "release_tool" and (
                    "re.compile" in line
                    or " in text" in line
                    or " in line" in line
                    or "line.strip" in line
                ):
                    status, classification = "ALLOWED", "SCANNER_RULE_DEFINITION"
                elif "_PRIVATE_DETAIL_PATTERNS" in text and line.strip().strip("\",'") in {"I:\\\\", "C:\\\\"}:
                    status, classification = "ALLOWED", "PATH_SANITIZER_PATTERN"
                else:
                    status, classification = "BLOCKER", rule_id
                findings.append({
                    "rule_id": rule_id,
                    "path": relative,
                    "line": line_number,
                    "classification": classification,
                    "status": status,
                })
            if ("127.0.0.1" in line or "localhost" in line) and relative in {
                "product_workflow/config.py", "config/latticegpt_v1.paths.example.json",
                "viewer_frontend/src/api/productViewerApi.js", "llm_provider.py",
            }:
                findings.append({
                    "rule_id": "LOCALHOST_DEFAULT",
                    "path": relative,
                    "line": line_number,
                    "classification": "OVERRIDEABLE_LOCALHOST_DEFAULT",
                    "status": "ALLOWED",
                })
    blockers = [item for item in findings if item["status"] == "BLOCKER"]
    return {
        "schema_version": "latticegpt_v1_hardcoded_path_scan_report_v1",
        "status": "PASS" if not blockers else "HOLD_HARDCODED_PATHS",
        "scanned_files": len(allowlist["files"]),
        "blocker_count": len(blockers),
        "findings": findings,
    }
