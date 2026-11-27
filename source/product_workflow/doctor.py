"""Readonly fail-closed doctor for the LatticeGPT V1 reference release."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

from .config import LatticeGPTConfig, load_latticegpt_config
from .artifact_manifest import verify_trusted_loader_contract
from .preflight import require_preflight
from .preflight import PreflightStageError
from .manifest_io import sha256_file


CHECK_ORDER = (
    "config",
    "active_release",
    "source",
    "artifact",
    "environment",
    "projection",
    "tolerances",
    "versions",
    "locks",
    "paths",
    "disk",
    "ports",
    "llm",
    "trusted_loader",
    "offline_identity",
    "leak_scan",
)


def _default_version_probe() -> dict[str, str]:
    def command_version(command: list[str]) -> str:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "NOT_AVAILABLE"
        return completed.stdout.strip().lstrip("v")

    import torch

    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "python_implementation": platform.python_implementation(),
        "torch_distribution_version": importlib.metadata.version("torch"),
        "torch_runtime_version": str(torch.__version__),
        "torch_cuda_version": str(torch.version.cuda) if torch.version.cuda is not None else "NOT_PRESENT",
        "node": command_version(["node", "--version"]),
        "npm": command_version(["npm.cmd", "--version"]),
        "windows_build": platform.version().split(".")[-1],
        "architecture": platform.machine(),
    }


def _default_port_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _check(code: str, status: str, detail: object) -> dict[str, object]:
    return {"code": code, "status": status, "detail": detail}


_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:\\|^\\\\)")


def _text_values(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _text_values(key)
            yield from _text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_values(item)


def _redact_leaking_details(
    checks: list[dict[str, object]], *, sensitive_values: tuple[str, ...]
) -> bool:
    leak_found = False
    for item in checks:
        detail = item.get("detail")
        detail_leaks = any(
            _WINDOWS_ABSOLUTE_PATH.search(text)
            or any(secret in text for secret in sensitive_values)
            for text in _text_values(detail)
        )
        if detail_leaks:
            item["detail"] = "REDACTED_BY_LEAK_SCAN"
            leak_found = True
    return leak_found


def run_doctor(
    config: LatticeGPTConfig,
    *,
    profile: str,
    preflight_loader: Callable[..., object] = require_preflight,
    version_probe: Callable[[], Mapping[str, str]] = _default_version_probe,
    disk_probe: Callable[[Path], int] | None = None,
    port_probe: Callable[[str, int], bool] = _default_port_probe,
    environ: Mapping[str, str] | None = None,
    trusted_loader_validator: Callable[[object], object] = verify_trusted_loader_contract,
) -> dict[str, object]:
    """Return a redacted report without creating directories or service state."""

    env = os.environ if environ is None else environ
    checks: list[dict[str, object]] = []
    config_schema = (
        "latticegpt_paths_config_v2"
        if getattr(config.paths, "generation_contract_path", None) is not None
        else "latticegpt_paths_config_v1"
    )
    checks.append(_check("config", "PASS", {"schema": config_schema}))
    preflight = None
    try:
        preflight = preflight_loader(config, profile=profile)
        active_binding_sha256 = getattr(preflight, "active_release_binding_sha256", None)
        checks.extend(
            [
                _check(
                    "active_release",
                    "PASS" if active_binding_sha256 is not None else "NOT_REQUIRED",
                    {"binding_sha256": active_binding_sha256}
                    if active_binding_sha256 is not None
                    else "CONFIG_V1_HAS_NO_ACTIVE_BINDING",
                ),
                _check("source", "PASS", {"manifest_sha256": preflight.source_manifest_sha256}),
                _check("artifact", "PASS", {"manifest_sha256": preflight.artifact_manifest_sha256}),
                _check(
                    "environment",
                    "PASS",
                    {"manifest_sha256": preflight.environment_manifest_sha256},
                ),
                _check("projection", "PASS", "PROJECTION_IDENTITY_VERIFIED"),
            ]
        )
    except Exception as exc:
        failed_stage = exc.stage if isinstance(exc, PreflightStageError) else "source"
        failure_code = exc.code if isinstance(exc, PreflightStageError) else "PRECHECK_INTEGRITY_FAILURE"
        layered = ("active_release", "source", "artifact", "environment", "projection")
        failed_index = layered.index(failed_stage) if failed_stage in layered else 1
        for index, code in enumerate(layered):
            if index < failed_index:
                checks.append(_check(code, "PASS", "VERIFIED_BEFORE_LATER_FAILURE"))
            elif index == failed_index:
                checks.append(_check(code, "FAIL", failure_code))
            else:
                checks.append(_check(code, "SKIP", "PRECHECK_FAILED"))

    try:
        tolerance_path = config.paths.reproduction_tolerances
        tolerance_sha = sha256_file(tolerance_path)
        checks.append(_check("tolerances", "PASS", {"sha256": tolerance_sha}))
    except Exception:
        checks.append(_check("tolerances", "FAIL", "TOLERANCE_IDENTITY_FAILURE"))

    versions = dict(version_probe())
    reference = getattr(getattr(preflight, "environment", None), "reference_profile", {})
    observed_architecture = str(versions.get("architecture", "")).lower()
    expected_architecture = str(reference.get("architecture", "")).lower()
    architecture_matches = observed_architecture == expected_architecture or {
        observed_architecture,
        expected_architecture,
    } == {"amd64", "x86_64"}
    expected_torch = str(reference.get("torch_version", ""))
    allowed_distributions = {expected_torch, expected_torch.split("+", 1)[0]}
    legacy_torch = str(versions.get("torch", ""))
    observed_runtime = str(versions.get("torch_runtime_version", legacy_torch))
    observed_distribution = str(
        versions.get("torch_distribution_version", legacy_torch.split("+", 1)[0])
    )
    observed_cuda = str(versions.get("torch_cuda_version", "NOT_PRESENT"))
    lock_literal = str(versions.get("torch_lock_literal", f"torch=={expected_torch}"))
    version_pass = bool(reference) and (
        versions.get("python") == reference.get("python_version")
        and versions.get("python_implementation") == reference.get("python_implementation")
        and observed_runtime == expected_torch
        and observed_distribution in allowed_distributions
        and observed_cuda == "NOT_PRESENT"
        and lock_literal == f"torch=={expected_torch}"
        and versions.get("node") == reference.get("node_version")
        and versions.get("npm") == reference.get("npm_version")
        and versions.get("windows_build") == reference.get("windows_build")
        and architecture_matches
    )
    checks.append(
        _check(
            "versions",
            "PASS" if version_pass else "FAIL",
            {key: str(value) for key, value in sorted(versions.items())},
        )
    )
    validation = getattr(getattr(preflight, "environment", None), "validation", {})
    locks_pass = (
        validation.get("status") == "PASS"
        and validation.get("python_lock", {}).get("status") == "PASS"
        and validation.get("node_lock", {}).get("status") == "PASS"
    )
    checks.append(
        _check(
            "locks",
            "PASS" if locks_pass else "FAIL" if preflight is not None else "SKIP",
            "SEMANTICALLY_VERIFIED" if locks_pass else "SEMANTIC_EVIDENCE_MISSING",
        )
    )
    paths_ok = not config.paths.runtime_root.exists() or config.paths.runtime_root.is_dir()
    checks.append(
        _check(
            "paths",
            "PASS" if paths_ok else "FAIL",
            {"runtime_root": "EXISTS" if config.paths.runtime_root.exists() else "NOT_CREATED"},
        )
    )
    disk_callable = disk_probe or (lambda path: shutil.disk_usage(path).free)
    disk_parent = config.paths.runtime_root.parent
    while not disk_parent.exists() and disk_parent != disk_parent.parent:
        disk_parent = disk_parent.parent
    free_bytes = int(disk_callable(disk_parent))
    checks.append(
        _check("disk", "PASS" if free_bytes >= 4_000_000_000 else "FAIL", {"free_bytes": free_bytes})
    )
    ports = {
        "chainlit": port_probe(config.services.chainlit_host, config.services.chainlit_port),
        "viewer": port_probe(config.services.viewer_host, config.services.viewer_port),
        "frontend": port_probe(config.services.frontend_host, config.services.frontend_port),
    }
    checks.append(
        _check(
            "ports",
            "PASS",
            {
                "configured": {
                    "chainlit": [config.services.chainlit_host, config.services.chainlit_port],
                    "viewer": [config.services.viewer_host, config.services.viewer_port],
                    "frontend": [config.services.frontend_host, config.services.frontend_port],
                },
                "occupied": ports,
            },
        )
    )
    llm_state = {
        name: "SET" if env.get(name) else "NOT_SET"
        for name in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OLLAMA_API_KEY")
    }
    checks.append(_check("llm", "PASS", llm_state))
    if preflight is not None and profile == "full_product":
        try:
            trusted_loader_validator(preflight.artifact)
            trusted_loader_check = _check("trusted_loader", "PASS", "POLICY_VERIFIED")
        except Exception:
            trusted_loader_check = _check("trusted_loader", "FAIL", "POLICY_MISMATCH")
    else:
        trusted_loader_check = _check("trusted_loader", "NOT_REQUIRED", "VIEWER_ONLY")
    checks.append(trusted_loader_check)
    environment_binding = (
        getattr(preflight, "environment", None) if preflight is not None else None
    )
    offline_records = getattr(environment_binding, "offline", None)
    if offline_records:
        offline_statuses = {
            str(record.get("status"))
            for record in offline_records.values()
            if isinstance(record, Mapping)
        }
        offline_status = (
            next(iter(offline_statuses)) if len(offline_statuses) == 1 else None
        )
    else:
        offline_status = getattr(environment_binding, "status", None)
    if offline_status == "PASS":
        offline_check = _check("offline_identity", "PASS", "VERIFIED")
    elif offline_status == "NOT_REQUIRED_BY_V1_SCOPE":
        offline_check = _check(
            "offline_identity", "NOT_REQUIRED", offline_status
        )
    elif offline_status == "HOLD_OFFLINE_BACKUP_MISSING":
        offline_check = _check("offline_identity", "HOLD", offline_status)
    else:
        offline_check = _check("offline_identity", "SKIP", "PRECHECK_FAILED")
    checks.append(offline_check)
    sensitive_values = tuple(
        value
        for key in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OLLAMA_API_KEY")
        if (value := str(env.get(key, "")))
    )
    leak_found = _redact_leaking_details(checks, sensitive_values=sensitive_values)
    checks.append(
        _check(
            "leak_scan",
            "FAIL" if leak_found else "PASS",
            "REDACTED_LEAK_DETECTED" if leak_found else "NO_SECRET_OR_ABSOLUTE_PATH_EMITTED",
        )
    )

    if tuple(item["code"] for item in checks) != CHECK_ORDER:
        raise RuntimeError("doctor check order drift")
    statuses = {str(item["status"]) for item in checks}
    exit_code = 1 if "FAIL" in statuses else 2 if "HOLD" in statuses else 0
    return {
        "schema_version": "latticegpt_doctor_report_v2",
        "profile": profile,
        "status": "FAIL" if exit_code == 1 else "HOLD" if exit_code == 2 else "PASS",
        "exit_code": exit_code,
        "checks": checks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", choices=("full_product", "viewer_only"), default="full_product")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    try:
        args = parser.parse_args(argv)
        config = load_latticegpt_config(config_path=args.config)
        report = run_doctor(config, profile=args.profile, environ=os.environ)
    except (SystemExit, ValueError, OSError):
        return 64
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8", newline="\n")
    else:
        print(rendered)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
