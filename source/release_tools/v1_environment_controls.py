"""Fail-closed builder for the six controlled Windows/CPU V1 environment files."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Mapping

from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from product_workflow.environment_manifest import ENVIRONMENT_FILE_LAYOUT, REFERENCE_PROFILE
from product_workflow.environment_validation import (
    ENV_EXAMPLE_KEYS,
    verify_environment_control_files,
    verify_node_package_lock,
)
from product_workflow.manifest_io import canonical_json_bytes, sha256_file


_DIRECT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!]+)$")
WINDOWS_CP310_TAGS: frozenset[Tag] = frozenset(
    [
        *cpython_tags((3, 10), platforms=["win_amd64"]),
        *compatible_tags(
            (3, 10), interpreter="cp310", platforms=["win_amd64"]
        ),
    ]
)


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _read_direct_requirements(path: Path) -> dict[str, str]:
    direct: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DIRECT_RE.fullmatch(stripped)
        if match is None:
            raise ValueError("direct requirement must use one exact name==version")
        name = _normalize_distribution(match.group(1))
        version = match.group(2).lower()
        if name in direct:
            raise ValueError(f"direct requirements contain duplicate distribution: {name}")
        direct[name] = version
    if not direct:
        raise ValueError("direct requirements are empty")
    return direct


def _parse_wheel_name(
    path: Path,
    *,
    supported_tags: frozenset[Tag] = WINDOWS_CP310_TAGS,
) -> tuple[str, str]:
    if path.suffix != ".whl":
        raise ValueError(f"wheel root contains an unexpected file: {path.name}")
    try:
        distribution, version, _build, wheel_tags = parse_wheel_filename(path.name)
    except InvalidWheelFilename as exc:
        raise ValueError(f"wheel filename is invalid: {path.name}") from exc
    if not wheel_tags.intersection(supported_tags):
        raise ValueError(
            "wheel is not compatible with Windows x64 / CPython 3.10: "
            f"{path.name}"
        )
    return str(distribution), str(version).lower()


def build_python_lock(
    wheel_root: str | Path, direct_requirements_path: str | Path
) -> str:
    root = Path(wheel_root)
    if not root.is_dir():
        raise ValueError("wheel root is missing")
    direct = _read_direct_requirements(Path(direct_requirements_path))
    wheels: dict[str, tuple[str, str]] = {}
    entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    if not entries:
        raise ValueError("wheel root is empty")
    for path in entries:
        if not path.is_file():
            raise ValueError(f"wheel root contains an unexpected entry: {path.name}")
        name, version = _parse_wheel_name(path)
        if name in wheels:
            raise ValueError(f"wheel root contains duplicate distribution: {name}")
        wheels[name] = (version, sha256_file(path).lower())
    for name, version in direct.items():
        selected = wheels.get(name)
        if selected is None:
            raise ValueError(f"direct requirement is missing from wheel root: {name}")
        if selected[0] != version:
            raise ValueError(f"direct requirement version does not match wheel: {name}")
    return "".join(
        f"{name}=={version} --hash=sha256:{wheel_sha}\n"
        for name, (version, wheel_sha) in sorted(wheels.items())
    )


def _read_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def build_environment_control_payloads(
    *,
    wheel_root: str | Path,
    direct_requirements_path: str | Path,
    package_lock_path: str | Path,
    node_runtime_template_path: str | Path,
    windows_cpu_reference_path: str | Path,
    env_example_path: str | Path,
) -> dict[str, bytes]:
    direct_path = Path(direct_requirements_path)
    package_path = Path(package_lock_path)
    node_path = Path(node_runtime_template_path)
    windows_path = Path(windows_cpu_reference_path)
    env_path = Path(env_example_path)
    for path, label in (
        (direct_path, "direct requirements"),
        (package_path, "package-lock"),
        (node_path, "node runtime template"),
        (windows_path, "Windows/CPU reference"),
        (env_path, ".env.example"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} is missing")

    package_payload = _read_json(package_path, label="package-lock")
    verify_node_package_lock(package_payload)
    node_template = _read_json(node_path, label="node runtime template")
    expected_node_template = {
        "schema_version": "latticegpt_node_runtime_v1",
        "platform": "win32",
        "architecture": "x64",
        "node_version": REFERENCE_PROFILE["node_version"],
        "npm_version": REFERENCE_PROFILE["npm_version"],
        "package_lock": {"size_bytes": None, "sha256": None},
    }
    if node_template != expected_node_template:
        raise ValueError("node runtime template does not match the locked V1 reference")
    windows = _read_json(windows_path, label="Windows/CPU reference")
    expected_windows = {
        "schema_version": "latticegpt_windows_cpu_reference_v1",
        "windows_build": REFERENCE_PROFILE["windows_build"],
        "architecture": REFERENCE_PROFILE["architecture"],
        "execution_profile": REFERENCE_PROFILE["execution_profile"],
        "cuda_required": REFERENCE_PROFILE["cuda_required"],
        "python_version": REFERENCE_PROFILE["python_version"],
        "torch_version": REFERENCE_PROFILE["torch_version"],
    }
    if windows != expected_windows:
        raise ValueError("Windows/CPU reference does not match the locked V1 reference")
    if env_path.read_text(encoding="utf-8").splitlines() != [
        f"{key}=" for key in ENV_EXAMPLE_KEYS
    ]:
        raise ValueError(".env.example is not the approved empty template")

    package_bytes = package_path.read_bytes()
    node_runtime = dict(expected_node_template)
    node_runtime["package_lock"] = {
        "size_bytes": len(package_bytes),
        "sha256": sha256_file(package_path),
    }
    return {
        "direct_requirements": direct_path.read_bytes(),
        "python_lock": build_python_lock(wheel_root, direct_path).encode("utf-8"),
        "node_runtime": canonical_json_bytes(node_runtime),
        "windows_cpu_reference": windows_path.read_bytes(),
        "package_lock": package_bytes,
        "env_example": env_path.read_bytes(),
    }


def write_environment_controls(
    *,
    output_root: str | Path,
    wheel_root: str | Path,
    direct_requirements_path: str | Path,
    package_lock_path: str | Path,
    node_runtime_template_path: str | Path,
    windows_cpu_reference_path: str | Path,
    env_example_path: str | Path,
) -> dict[str, object]:
    output = Path(output_root)
    if output.exists():
        raise ValueError("environment control output root already exists")
    if not output.parent.is_dir():
        raise ValueError("environment control output parent is missing")
    payloads = build_environment_control_payloads(
        wheel_root=wheel_root,
        direct_requirements_path=direct_requirements_path,
        package_lock_path=package_lock_path,
        node_runtime_template_path=node_runtime_template_path,
        windows_cpu_reference_path=windows_cpu_reference_path,
        env_example_path=env_example_path,
    )
    staging = output.parent / f".{output.name}.assembly-{uuid.uuid4().hex}"
    staging.mkdir()
    files: dict[str, Path] = {}
    for logical_id, relative in ENVIRONMENT_FILE_LAYOUT.items():
        path = staging / relative
        path.write_bytes(payloads[logical_id])
        files[logical_id] = path
    validation = verify_environment_control_files(files, reference_profile=REFERENCE_PROFILE)
    staging.replace(output)
    return {
        "status": validation["status"],
        "output_root": str(output.resolve()),
        "file_count": len(files),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-root", required=True)
    parser.add_argument("--direct-requirements", required=True)
    parser.add_argument("--package-lock", required=True)
    parser.add_argument("--node-runtime-template", required=True)
    parser.add_argument("--windows-cpu-reference", required=True)
    parser.add_argument("--env-example", required=True)
    parser.add_argument("--output-root", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    kwargs = {
        "wheel_root": args.wheel_root,
        "direct_requirements_path": args.direct_requirements,
        "package_lock_path": args.package_lock,
        "node_runtime_template_path": args.node_runtime_template,
        "windows_cpu_reference_path": args.windows_cpu_reference,
        "env_example_path": args.env_example,
    }
    if args.write:
        report = write_environment_controls(output_root=args.output_root, **kwargs)
    else:
        payloads = build_environment_control_payloads(**kwargs)
        report = {"status": "PASS", "file_count": len(payloads), "write": False}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "WINDOWS_CP310_TAGS",
    "build_environment_control_payloads",
    "build_python_lock",
    "main",
    "write_environment_controls",
]
