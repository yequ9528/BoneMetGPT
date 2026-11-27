"""Shared semantic validators for the locked Windows/CPU V1 environment."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .manifest_io import sha256_file, validate_relative_path


_LOCK_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+) --hash=sha256:([0-9a-f]{64})$"
)
_DIRECT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)$")

ENV_EXAMPLE_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "LATTICEGPT_CONFIG",
    "LATTICEGPT_PYTHON",
)


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _require_upper_sha(value: object, *, label: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 64 or any(ch not in "0123456789ABCDEF" for ch in normalized):
        raise ValueError(f"{label} must be an uppercase SHA256")
    return normalized


def parse_python_lock(lock_text: str) -> Mapping[tuple[str, str], str]:
    required: dict[tuple[str, str], str] = {}
    for line in lock_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LOCK_RE.fullmatch(stripped)
        if match is None:
            raise ValueError("python lock line requires exact version and sha256 hash")
        item = (_normalized_distribution(match.group(1)), match.group(2).lower())
        if item in required:
            raise ValueError("python lock contains duplicate distribution")
        required[item] = match.group(3).upper()
    if not required:
        raise ValueError("python lock is empty")
    return MappingProxyType(required)


def _parse_direct_requirements(text: str) -> Mapping[tuple[str, str], None]:
    direct: dict[tuple[str, str], None] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DIRECT_RE.fullmatch(stripped)
        if match is None:
            raise ValueError("direct requirement must use an exact version")
        item = (_normalized_distribution(match.group(1)), match.group(2).lower())
        if item in direct:
            raise ValueError("direct requirements contain a duplicate distribution")
        direct[item] = None
    if not direct:
        raise ValueError("direct requirements are empty")
    return MappingProxyType(direct)


def verify_python_lock(
    lock_text: str, wheel_entries: Iterable[Mapping[str, object]]
) -> dict[str, object]:
    required = parse_python_lock(lock_text)
    available: dict[tuple[str, str], str] = {}
    for entry in wheel_entries:
        relative = validate_relative_path(
            str(entry.get("relative_path") or ""), label="wheel relative path"
        )
        filename = Path(relative).name
        parts = filename[:-4].split("-") if filename.endswith(".whl") else []
        if len(parts) < 5:
            raise ValueError("wheel set contains an invalid wheel filename")
        item = (_normalized_distribution(parts[0]), parts[1].lower())
        if item in available:
            raise ValueError("wheel set contains duplicate distributions")
        wheel_sha = _require_upper_sha(entry.get("sha256"), label="wheel SHA256")
        if item in required and wheel_sha != required[item]:
            raise ValueError(f"wheel SHA256 does not match python lock: {filename}")
        available[item] = wheel_sha
    if set(available) != set(required):
        raise ValueError("wheel set does not exactly match python lock")
    return {"status": "PASS", "distribution_count": len(required)}


def verify_node_package_lock(payload: Mapping[str, object]) -> dict[str, object]:
    if payload.get("lockfileVersion") != 3:
        raise ValueError("Node package-lock must use lockfileVersion 3")
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("Node package-lock packages are required")
    checked = 0
    for name, record in packages.items():
        if name == "":
            continue
        if not isinstance(record, dict):
            raise ValueError("Node package-lock record is invalid")
        resolved = str(record.get("resolved") or "")
        if resolved.startswith(("http://", "https://")):
            if not str(record.get("integrity") or "").startswith("sha512-"):
                raise ValueError("every registry package requires integrity")
            checked += 1
    return {"status": "PASS", "registry_package_count": checked}


def _read_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _validate_env_example(path: Path) -> None:
    expected = [f"{key}=" for key in ENV_EXAMPLE_KEYS]
    if path.read_text(encoding="utf-8").splitlines() != expected:
        raise ValueError(".env.example must contain only approved empty values")


def verify_environment_control_files(
    files: Mapping[str, Path], *, reference_profile: Mapping[str, object]
) -> Mapping[str, object]:
    required_ids = {
        "direct_requirements",
        "python_lock",
        "node_runtime",
        "windows_cpu_reference",
        "package_lock",
        "env_example",
    }
    if set(files) != required_ids:
        raise ValueError("environment control file set is invalid")

    direct = _parse_direct_requirements(
        files["direct_requirements"].read_text(encoding="utf-8")
    )
    lock = parse_python_lock(files["python_lock"].read_text(encoding="utf-8"))
    missing = sorted(set(direct) - set(lock))
    if missing:
        raise ValueError(f"direct requirement is missing from python lock: {missing[0][0]}")

    package_lock_path = files["package_lock"]
    package_lock = _read_json(package_lock_path, label="viewer package-lock")
    node_validation = verify_node_package_lock(package_lock)

    node_runtime = _read_json(files["node_runtime"], label="node runtime")
    expected_node = {
        "schema_version": "latticegpt_node_runtime_v1",
        "platform": "win32",
        "architecture": "x64",
        "node_version": reference_profile["node_version"],
        "npm_version": reference_profile["npm_version"],
        "package_lock": {
            "size_bytes": package_lock_path.stat().st_size,
            "sha256": sha256_file(package_lock_path),
        },
    }
    if node_runtime != expected_node:
        raise ValueError("node runtime package-lock identity or version contract mismatch")

    windows = _read_json(files["windows_cpu_reference"], label="Windows/CPU reference")
    expected_windows = {
        "schema_version": "latticegpt_windows_cpu_reference_v1",
        "windows_build": reference_profile["windows_build"],
        "architecture": reference_profile["architecture"],
        "execution_profile": reference_profile["execution_profile"],
        "cuda_required": reference_profile["cuda_required"],
        "python_version": reference_profile["python_version"],
        "torch_version": reference_profile["torch_version"],
    }
    if windows != expected_windows:
        raise ValueError("Windows/CPU reference profile mismatch")
    _validate_env_example(files["env_example"])

    return MappingProxyType(
        {
            "status": "PASS",
            "python_lock": MappingProxyType(
                {"status": "PASS", "distribution_count": len(lock), "direct_count": len(direct)}
            ),
            "node_lock": MappingProxyType(dict(node_validation)),
            "reference_profile": "PASS",
            "env_example": "PASS",
        }
    )


__all__ = [
    "ENV_EXAMPLE_KEYS",
    "parse_python_lock",
    "verify_environment_control_files",
    "verify_node_package_lock",
    "verify_python_lock",
]
