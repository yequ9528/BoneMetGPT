"""Service wrapper for the TrussInvertibleDesign inverse-design pipeline.

This module provides a callable service layer around the legacy script workflow.
It does not execute anything at import time and is intentionally independent of
Chainlit, Agent tools, and app-level routing.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


C_NAMES = ["C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66"]
POINT_NAMES = [
    "point1_x",
    "point1_y",
    "point1_z",
    "point2_x",
    "point2_y",
    "point4_x",
    "point4_z",
    "point6_y",
    "point6_z",
]

AUTHOR_CASE_MIN = {
    "C11": 0.066,
    "C12": 0.015,
    "C13": 0.014,
    "C22": 0.056,
    "C23": 0.014,
    "C33": 0.044,
    "C44": 0.021,
    "C55": 0.024,
    "C66": 0.025,
}

LOW_TARGET_WARNING = "target_stiffness_out_of_recommended_range"
LOW_TARGET_MESSAGE = (
    "目标刚度低于当前 150w 逆向设计模型的建议输入范围，可能导致 "
    "relative_density=0 或无效杆径。请提高目标刚度，或从数据库中选择"
    "主刚度、剪切刚度和相对密度更高的样本作为目标。"
)

TOTAL_LENGTH_FIELD = "总有效杆长"
MEMBER_COUNT_FIELD = "有效杆件数"
DIAMETER_FIELD = "杆统一直径"
SCHEMA_VERSION = "truss_candidate_v1"
SOURCE_MODEL = "truss_inverse_150w"
MODEL_FAMILY = "TrussInvertibleDesign"
SUPPORTED_MODEL_VARIANT = "150w"
LEGACY_NUM_CANDIDATES = 3
FORMAL_BUNDLE_SCHEMA_VERSION = "latticegpt_formal_bundle_binding_v1"
FORMAL_BUNDLE_ARTIFACT_NAMES = (
    "formal_csv",
    "inverse_model",
    "forward_model",
    "scaler_x",
    "scaler_y",
)
TRUSTED_RUNTIME_ARTIFACT_PATHS = {
    "inverse_model": "models/inverse_cvae150w.pt",
    "forward_model": "models/forward_stiffness150w.pt",
    "scaler_x": "scalers/scaler_X.pt",
    "scaler_y": "scalers/scaler_Y.pt",
}
SUPPORTED_RUNTIME_MODES = {"formal_product", "legacy_technical"}


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _parse_float(value: Any, field_name: str, warnings: list[str], errors: list[str]) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field_name} is not numeric")
        return None
    if not math.isfinite(number):
        errors.append(f"{field_name} is not finite")
        return None
    return number


def _target_dict_from_sequence(values: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    if len(values) != len(C_NAMES):
        raise ValueError(f"target stiffness must contain {len(C_NAMES)} values")
    return dict(zip(C_NAMES, values))


def _normalize_target(target: dict[str, Any] | list[Any] | tuple[Any, ...]) -> dict[str, float]:
    if isinstance(target, dict):
        raw = target
    elif isinstance(target, (list, tuple)):
        raw = _target_dict_from_sequence(target)
    else:
        raise ValueError("target stiffness must be a dict, list, or tuple")

    values: dict[str, float] = {}
    missing = [name for name in C_NAMES if name not in raw]
    if missing:
        raise ValueError(f"target stiffness missing fields: {missing}")

    for name in C_NAMES:
        value = float(raw[name])
        if not math.isfinite(value):
            raise ValueError(f"target stiffness field {name} is not finite")
        values[name] = value
    return values


def validate_target_stiffness(target: dict[str, Any] | list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """Validate a single 9D stiffness target for the 150w inverse model."""

    warnings: list[str] = []
    errors: list[str] = []
    normalized: dict[str, float] = {}

    try:
        normalized = _normalize_target(target)
    except Exception as exc:
        errors.append(str(exc))
        return {
            "valid": False,
            "severity": "reject",
            "target": {},
            "metrics": {"main_mean": None, "shear_mean": None, "low_fields": []},
            "warnings": warnings,
            "errors": errors,
        }

    for name, value in normalized.items():
        if value <= 0:
            errors.append(f"{name} must be > 0")

    main_mean = sum(normalized[name] for name in ["C11", "C22", "C33"]) / 3.0
    shear_mean = sum(normalized[name] for name in ["C44", "C55", "C66"]) / 3.0
    low_fields = [
        name
        for name in C_NAMES
        if normalized[name] < 0.8 * AUTHOR_CASE_MIN[name]
    ]

    if low_fields or main_mean < 0.03 or shear_mean < 0.005:
        warnings.append(LOW_TARGET_WARNING)
        warnings.append(LOW_TARGET_MESSAGE)

    severity = "reject" if errors else ("warning" if warnings else "ok")
    return {
        "valid": not errors,
        "severity": severity,
        "target": normalized,
        "metrics": {
            "main_mean": main_mean,
            "shear_mean": shear_mean,
            "low_fields": low_fields,
        },
        "warnings": warnings,
        "errors": errors,
    }


def _read_target_csv(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        raise FileNotFoundError(f"target stiffness CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        raise ValueError(f"target stiffness CSV has no rows: {path}")
    return [_normalize_target(row) for row in rows]


def _targets_from_input(target_stiffness: Any) -> list[dict[str, float]]:
    if isinstance(target_stiffness, (str, Path)):
        return _read_target_csv(Path(target_stiffness))
    if isinstance(target_stiffness, list) and target_stiffness and all(
        isinstance(item, dict) for item in target_stiffness
    ):
        return [_normalize_target(item) for item in target_stiffness]
    return [_normalize_target(target_stiffness)]


def _write_target_csv(path: Path, targets: list[dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=C_NAMES)
        writer.writeheader()
        for target in targets:
            writer.writerow({name: target[name] for name in C_NAMES})


def _read_csv_rows(path: Path, required_columns: list[str], label: str) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames or []
    missing = [column for column in required_columns if column not in columns]
    if missing:
        raise ValueError(f"{label} missing columns {missing}; columns={columns}")
    return rows


def _literal_list(value: Any, field_name: str, warnings: list[str]) -> list[Any]:
    try:
        parsed = ast.literal_eval(str(value))
    except (SyntaxError, ValueError):
        warnings.append(f"{field_name} parse failed")
        return []
    if not isinstance(parsed, list):
        warnings.append(f"{field_name} parse failed")
        return []
    return parsed


def _float_or_none(value: Any, field_name: str, warnings: list[str]) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        warnings.append(f"{field_name} missing")
        return None
    if not math.isfinite(number):
        warnings.append(f"{field_name} missing")
        return None
    return number


def _int_or_none(value: Any, field_name: str, warnings: list[str]) -> int | None:
    number = _float_or_none(value, field_name, warnings)
    return None if number is None else int(number)


def _stiffness_from_row(row: dict[str, str], warnings: list[str]) -> dict[str, float]:
    stiffness: dict[str, float] = {}
    for name in C_NAMES:
        value = _float_or_none(row.get(name), name, warnings)
        if value is not None:
            stiffness[name] = value
    if set(stiffness) != set(C_NAMES):
        warnings.append("predicted_stiffness missing")
    return stiffness


def _compressed_parameters_from_row(row: dict[str, str], warnings: list[str]) -> dict[str, float]:
    params: dict[str, float] = {}
    for name in POINT_NAMES:
        value = _float_or_none(row.get(name), name, warnings)
        if value is not None:
            params[name] = value
    if set(params) != set(POINT_NAMES):
        warnings.append("compressed_parameters missing")
    return params


def _candidate_id(model_variant: str, index: int) -> str:
    return f"inverse_{model_variant}_{index:04d}"


def build_candidate_schema(
    prediction_csv: str | Path,
    fem_csv: str | Path,
    diameter_csv: str | Path,
    target_stiffness: dict[str, float] | list[dict[str, float]],
    *,
    model_variant: str = SUPPORTED_MODEL_VARIANT,
) -> list[dict[str, Any]]:
    """Build normalized candidates from the legacy CSV outputs."""

    prediction_path = Path(prediction_csv)
    fem_path = Path(fem_csv)
    diameter_path = Path(diameter_csv)
    targets = target_stiffness if isinstance(target_stiffness, list) else [target_stiffness]

    prediction_rows = _read_csv_rows(
        prediction_path,
        ["relative_density", *POINT_NAMES, *C_NAMES],
        "prediction_csv",
    )
    fem_rows = _read_csv_rows(
        fem_path,
        ["relative_density", "nodes", "connectivity", *C_NAMES],
        "fem_csv",
    )
    diameter_rows = _read_csv_rows(
        diameter_path,
        [
            "relative_density",
            "nodes",
            "connectivity",
            *C_NAMES,
            TOTAL_LENGTH_FIELD,
            MEMBER_COUNT_FIELD,
            DIAMETER_FIELD,
        ],
        "diameter_csv",
    )

    row_counts = {len(prediction_rows), len(fem_rows), len(diameter_rows)}
    if len(row_counts) != 1:
        raise ValueError(
            "row count mismatch: "
            f"prediction={len(prediction_rows)} fem={len(fem_rows)} diameter={len(diameter_rows)}"
        )

    candidates: list[dict[str, Any]] = []
    for index, (prediction_row, fem_row, diameter_row) in enumerate(
        zip(prediction_rows, fem_rows, diameter_rows), start=1
    ):
        warnings: list[str] = []
        relative_density = _float_or_none(
            prediction_row.get("relative_density"), "relative_density", warnings
        )
        nodes = _literal_list(fem_row.get("nodes"), "nodes", warnings)
        connections = _literal_list(fem_row.get("connectivity"), "connections", warnings)
        diameter = _float_or_none(diameter_row.get(DIAMETER_FIELD), "diameter", warnings)
        total_effective_length = _float_or_none(
            diameter_row.get(TOTAL_LENGTH_FIELD), "total_effective_length", warnings
        )
        effective_member_count = _int_or_none(
            diameter_row.get(MEMBER_COUNT_FIELD), "effective_member_count", warnings
        )
        compressed_parameters = _compressed_parameters_from_row(prediction_row, warnings)
        predicted_stiffness = _stiffness_from_row(prediction_row, warnings)

        if relative_density is None or relative_density <= 0:
            warnings.append("relative_density <= 0")
        if diameter is None:
            warnings.append("diameter missing")
        elif diameter <= 0:
            warnings.append("diameter <= 0")
        if len(nodes) != 15:
            warnings.append("node_count unexpected")
        if len(connections) not in (30, 32):
            warnings.append("connection_count unexpected")

        cad_ready = (
            relative_density is not None
            and relative_density > 0
            and diameter is not None
            and diameter > 0
            and bool(nodes)
            and bool(connections)
        )
        if not cad_ready:
            warnings.append("not CAD-ready")

        target_index = min((index - 1) // LEGACY_NUM_CANDIDATES, len(targets) - 1)
        nmse = None
        if 'nmse' in prediction_row:
            if int(prediction_row.get('target_id', 0)) != target_index + 1:
                raise ValueError('prediction target_id does not match the requested target')
            nmse = float(prediction_row['nmse'])
            target = targets[target_index]
            expected_nmse = sum((predicted_stiffness[k] - target[k]) ** 2 for k in C_NAMES) / sum(target[k] ** 2 for k in C_NAMES)
            if not math.isfinite(nmse) or nmse < 0 or not math.isclose(nmse, expected_nmse, rel_tol=1e-3, abs_tol=1e-10):
                raise ValueError('NMSE is invalid or inconsistent with target and prediction')
        candidate = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": _candidate_id(model_variant, index),
            "source_model": SOURCE_MODEL,
            "model_family": MODEL_FAMILY,
            "model_variant": model_variant,
            "pipeline_stage": "diameter_calculated",
            "target_stiffness": targets[target_index],
            "predicted_stiffness": predicted_stiffness,
            "relative_density": relative_density,
            "compressed_parameters": compressed_parameters,
            "nodes": nodes,
            "connections": connections,
            "diameter": diameter,
            "num_nodes": len(nodes),
            "num_connections": len(connections),
            "total_effective_length": total_effective_length,
            "effective_member_count": effective_member_count,
            "cad_ready": cad_ready,
            "warnings": list(dict.fromkeys(warnings)),
            "raw_files": {
                "prediction_csv": str(prediction_path),
                "fem_csv": str(fem_path),
                "diameter_csv": str(diameter_path),
            },
        }
        if nmse is not None:
            candidate['nmse'] = nmse
        candidates.append(candidate)

    return candidates


def build_delivered_candidates(prediction_csv, target_stiffness, *, model_variant=SUPPORTED_MODEL_VARIANT):
    """Convert the owner export directly, preserving output order and raw NMSE."""
    from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
    targets = target_stiffness if isinstance(target_stiffness, list) else [target_stiffness]
    rows = _read_csv_rows(Path(prediction_csv), ['target_id', 'nmse', 'relative_density', *POINT_NAMES, *C_NAMES], 'prediction_csv')
    if len(rows) != 3 * len(targets):
        raise ValueError('delivered export must contain three candidates per target')
    result = []
    for index, row in enumerate(rows, 1):
        target_index = (index - 1) // 3
        if float(row['target_id']) != target_index + 1:
            raise ValueError('prediction target_id does not match the requested target')
        target = targets[target_index]
        prediction = {k: float(row[k]) for k in C_NAMES}
        nmse = float(row['nmse'])
        expected = sum((prediction[k]-target[k])**2 for k in C_NAMES) / sum(target[k]**2 for k in C_NAMES)
        if not math.isfinite(nmse) or nmse < 0 or not math.isfinite(expected) or not math.isclose(nmse, expected, rel_tol=1e-3, abs_tol=1e-10):
            raise ValueError('NMSE is invalid or inconsistent with target and prediction')
        rho = float(row['relative_density'])
        parameters = {k: float(row[k]) for k in POINT_NAMES}
        geometry = build_unit_cell([rho, *parameters.values()])
        result.append(dict(schema_version=SCHEMA_VERSION, candidate_id=_candidate_id(model_variant,index),
            source_model=SOURCE_MODEL, model_family=MODEL_FAMILY, model_variant=model_variant,
            source_order=(index-1)%3+1, pipeline_stage='owner_geometry_calculated',
            target_stiffness=dict(target), predicted_stiffness=prediction, nmse=nmse,
            relative_density=rho, compressed_parameters=parameters, **geometry,
            cad_ready=True, warnings=[], raw_files={'prediction_csv':str(prediction_csv)}))
    return result


def export_candidate_cad(
    candidate: dict[str, Any],
    output_dir: str | Path,
    prefix: str | None = None,
) -> dict[str, Any]:
    """Export a CAD-ready candidate using cad_module.export_graph_to_stl."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    candidate_id = str(candidate.get("candidate_id") or "candidate")
    stem = prefix or candidate_id
    stl_path = output_path / f"{stem}.stl"

    try:
        from cad_module import export_graph_to_stl

        result = export_graph_to_stl(
            nodes=candidate["nodes"],
            connections=candidate["connections"],
            diameter=float(candidate["diameter"]),
            save_path=str(stl_path),
        )
        if result.get("status") != "success":
            return {
                "status": "failed",
                "candidate_id": candidate_id,
                "stl_path": result.get("stl_path") or "",
                "preview_path": result.get("preview_path") or "",
                "node_count": result.get("node_count", 0),
                "connection_count": result.get("connection_count", 0),
                "diameter": result.get("diameter", 0.0),
                "error": result.get("message", "cad export failed"),
            }
        return {
            "status": "success",
            "candidate_id": candidate_id,
            "stl_path": result.get("stl_path"),
            "preview_path": result.get("preview_path"),
            "node_count": result.get("node_count"),
            "connection_count": result.get("connection_count"),
            "diameter": result.get("diameter"),
            "error": None,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "candidate_id": candidate_id,
            "stl_path": "",
            "preview_path": "",
            "node_count": 0,
            "connection_count": 0,
            "diameter": 0.0,
            "error": str(exc),
        }


def _run_step(
    name: str,
    command: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str],
) -> dict[str, Any]:
    started = time.perf_counter()
    returncode = None
    step_roles = {
        "inference": ("main_export_test.py", "TrussInvertibleDesign"),
        "data2FEM": ("data2FEM.py", "datatest"),
        "diameter_calculate": ("diameter_calculate.py", "datatest"),
    }
    script, directory = step_roles.get(name, ("", ""))
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        returncode = completed.returncode
        error = None if returncode == 0 else "inverse child failed"
    except subprocess.TimeoutExpired:
        error = "inverse child timed out"
    except Exception:
        error = "inverse child launch failed"
    # Closed Job schema also reads pre-existing full receipts. For new runs
    # record only the registered script role; launch paths are transient.
    return {
        "step": name if name in step_roles else "child",
        "command": ["python", script] if script else [],
        "cwd": directory,
        "returncode": returncode,
        "stdout_tail": "",
        "stderr_tail": "",
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "status": "success" if error is None else "failed",
        "error": error,
    }


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _parse_strict_json_object_bytes(raw: bytes, label: str) -> dict[str, Any]:
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n"):
        raise ValueError(f"{label} must be UTF-8/no-BOM/LF with final LF")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"{label} contains forbidden JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _parse_closed_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    value = _parse_strict_json_object_bytes(raw, label)
    canonical = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if canonical != raw:
        raise ValueError(f"{label} bytes are not canonical")
    return value


def _parse_source_manifest_bytes(raw: bytes) -> dict[str, Any]:
    label = "source manifest"
    value = _parse_strict_json_object_bytes(raw, label)
    canonical = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if canonical != raw:
        raise ValueError(f"{label} bytes are not canonical")
    return value


def _is_reparse(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_plain_path(path: Path, *, kind: str, label: str) -> Path:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be an absolute normalized path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not current.exists():
            raise ValueError(f"{label} is missing: {current}")
        if _is_reparse(current):
            raise ValueError(f"{label} contains symlink/reparse point: {current}")
    resolved = path.resolve(strict=True)
    if kind == "directory" and not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    if kind == "file" and not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _relative_source_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} is not a canonical relative path")
    candidate = Path(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{label} is not a canonical relative path")
    return value


def _verified_child_import_root(project_root: Path, source_manifest_sha256: str) -> Path:
    expected_sha = str(source_manifest_sha256).strip().upper()
    if len(expected_sha) != 64 or any(ch not in "0123456789ABCDEF" for ch in expected_sha):
        raise ValueError("source manifest identity must be sha256_upper")
    root = _require_plain_path(project_root, kind="directory", label="source import root")
    registry_path = _require_plain_path(
        root / "config/r3_release_generation_registry_v1.json",
        kind="file",
        label="release generation registry",
    )
    registry = _parse_closed_json_bytes(registry_path.read_bytes(), "release generation registry")
    if registry.get("schema_version") != "latticegpt_r3_release_generation_registry_v1":
        raise ValueError("release generation registry schema mismatch")
    entries = registry.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("release generation registry entries are required")
    registered = [
        (entry, "PASS_R3_SOURCE_IDENTITY", "config/r3_release_generation_registry_v1.json")
        for entry in entries
    ]
    r4_registry_relative = "config/r4_lite_release_generation_registry_v1.json"
    r4_registry_path = root / r4_registry_relative
    if r4_registry_path.exists():
        from product_workflow.r4_lite_release_generation import load_generation_registry_v1

        checked_registry = _require_plain_path(
            r4_registry_path, kind="file", label="R4-Lite release generation registry"
        )
        r4_registry = load_generation_registry_v1(checked_registry)
        registered.append((r4_registry, "PASS_R4_LITE_SOURCE_IDENTITY", r4_registry_relative))
    manifest_matches = []
    generations = set()
    for entry, source_status, registry_relative in registered:
        if not isinstance(entry, Mapping):
            raise ValueError("release generation registry entry must be an object")
        generation = entry.get("generation")
        if not isinstance(generation, str) or not generation or generation in generations:
            raise ValueError("release generation registry generation is invalid or duplicate")
        generations.add(generation)
        prefix = _relative_source_path(
            entry.get("source_release_prefix"), "source_release_prefix"
        )
        manifest_path = root / Path(prefix.replace("/", os.sep)) / "source_manifest_v1.json"
        if manifest_path.exists():
            checked = _require_plain_path(
                manifest_path, kind="file", label="source manifest"
            )
            if _file_sha256(checked) == expected_sha:
                manifest_matches.append((checked, source_status, registry_relative))
    if len(manifest_matches) != 1:
        raise ValueError("source manifest identity must match exactly one registered generation")

    manifest_path, source_status, registry_relative = manifest_matches[0]
    detached_path = _require_plain_path(
        Path(str(manifest_path) + ".sha256"),
        kind="file",
        label="source manifest detached SHA",
    )
    expected_detached = f"{expected_sha}  source_manifest_v1.json\n".encode("ascii")
    if detached_path.read_bytes() != expected_detached:
        raise ValueError("source manifest detached SHA identity mismatch")
    manifest_raw = manifest_path.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest().upper() != expected_sha:
        raise ValueError("source manifest identity changed before parsing")
    manifest = _parse_source_manifest_bytes(manifest_raw)
    if (
        manifest.get("schema_version") != "latticegpt_v1_source_manifest_v1"
        or manifest.get("status") != source_status
    ):
        raise ValueError("source manifest status/schema mismatch")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("source manifest files are required")
    verified_paths = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("source manifest file record must be an object")
        relative = _relative_source_path(record.get("path"), "source manifest file path")
        folded = relative.casefold()
        if folded in verified_paths:
            raise ValueError("source manifest file path is duplicate")
        verified_paths.add(folded)
        source_path = _require_plain_path(
            root / Path(relative.replace("/", os.sep)),
            kind="file",
            label="source manifest file",
        )
        try:
            source_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("source manifest file escapes source root") from exc
        size = record.get("size_bytes")
        sha = str(record.get("sha256") or "").strip().upper()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"source manifest size is invalid: {relative}")
        if source_path.stat().st_size != size or _file_sha256(source_path) != sha:
            raise ValueError(f"source manifest file identity mismatch: {relative}")
    required = {
        "config/r3_release_generation_registry_v1.json",
        "product_workflow/__init__.py",
        "product_workflow/manifest_io.py",
        "product_workflow/reproducibility.py",
        "TrussInvertibleDesign/src/inference_contract.py",
    }
    if registry_relative == r4_registry_relative:
        required.update({
            r4_registry_relative,
            "product_workflow/r4_lite_release_generation.py",
        })
    if not {item.casefold() for item in required}.issubset(verified_paths):
        raise ValueError("source manifest does not close the child import dependency set")
    return root


def _child_environment(
    project_root: Path,
    reproduction_profile: Mapping[str, Any] | None,
) -> dict[str, str]:
    base = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() != "pythonpath"
    }
    if reproduction_profile is None:
        import_root = _require_plain_path(
            project_root, kind="directory", label="source import root"
        )
        env = base
    else:
        from product_workflow.reproducibility import reproduction_child_environment

        import_root = _verified_child_import_root(
            project_root,
            reproduction_profile["identities"]["source_manifest_sha256"],
        )
        env = reproduction_child_environment(
            reproduction_profile,
            base_environment=base,
        )
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "LATTICEGPT_SCALER_POLICY": "require_existing",
            "PYTHONPATH": str(import_root),
        }
    )
    return env


def _validate_formal_bundle_binding(binding: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise ValueError("FormalBundleBindingV1 is required for formal_product runtime")
    value = dict(binding)
    if value.get("schema_version") != FORMAL_BUNDLE_SCHEMA_VERSION:
        raise ValueError("FormalBundleBindingV1 is required for formal_product runtime")
    expected_binding_sha = str(value.pop("binding_sha256", "")).strip().upper()
    if _canonical_sha256(value) != expected_binding_sha:
        raise ValueError("FormalBundleBindingV1 identity mismatch")
    if value.get("status") != "PASS_5_OF_5":
        raise ValueError("formal bundle status must be PASS_5_OF_5")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping) or tuple(artifacts) != FORMAL_BUNDLE_ARTIFACT_NAMES:
        raise ValueError("formal bundle must contain the five authoritative artifacts")
    for name in FORMAL_BUNDLE_ARTIFACT_NAMES:
        artifact = artifacts[name]
        path = Path(str(artifact.get("path") or ""))
        if not path.is_file():
            raise ValueError(f"{name} is missing: {path}")
        if artifact.get("status") != "PASS":
            raise ValueError(f"{name} is not verified")
        expected_sha = str(artifact.get("sha256") or "").strip().upper()
        if _file_sha256(path) != expected_sha:
            raise ValueError(f"{name} SHA256 mismatch")
        if path.stat().st_size != artifact.get("size_bytes"):
            raise ValueError(f"{name} size mismatch")
    checked = dict(binding)
    checked["binding_sha256"] = expected_binding_sha
    return checked


def _stage_trusted_runtime_tree(
    truss_source: Path,
    temp_truss: Path,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """Copy executable source while replacing model/scaler trees with verified bytes."""

    source_root = truss_source.resolve()

    def ignore_source_artifacts(directory: str, names: list[str]) -> set[str]:
        if Path(directory).resolve() == source_root:
            return {name for name in names if name in {"models", "scalers"}}
        return set()

    shutil.copytree(truss_source, temp_truss, ignore=ignore_source_artifacts)
    staged: dict[str, str] = {}
    for logical_id, relative_path in TRUSTED_RUNTIME_ARTIFACT_PATHS.items():
        artifact = artifacts[logical_id]
        source = Path(str(artifact.get("path") or "")).resolve()
        destination = temp_truss / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        expected_size = artifact.get("size_bytes")
        expected_sha = str(artifact.get("sha256") or "").strip().upper()
        if destination.stat().st_size != expected_size:
            raise ValueError(f"{logical_id} post-copy size mismatch")
        if _file_sha256(destination) != expected_sha:
            raise ValueError(f"{logical_id} post-copy SHA256 mismatch")
        staged[logical_id] = expected_sha
    return staged


def _base_result(
    model_variant: str,
    *,
    runtime_mode: str,
    formal_bundle_binding_sha256: str | None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "model_variant": model_variant,
        "runtime_mode": runtime_mode,
        "formal_bundle_binding_sha256": formal_bundle_binding_sha256,
        "trusted_artifact_sha256": {},
        "target_validation": {},
        "work_dir": None,
        "work_dir_retained": False,
        "cleanup": {
            "status": "not_started",
            "work_dir_removed": False,
            "raw_files_available": False,
        },
        "candidates": [],
        "cad_ready_count": 0,
        "selected_candidate_id": None,
        "exported_files": {"stl": None, "png": None},
        "raw_files": {
            "target_stiffness_csv": None,
            "prediction_csv": None,
            "fem_csv": None,
            "diameter_csv": None,
        },
        "steps": [],
        "warnings": [],
        "errors": [],
    }


def _finalize_status(result: dict[str, Any], run_cad: bool) -> None:
    if result["errors"]:
        result["status"] = "failed"
    elif result["cad_ready_count"] <= 0:
        result["status"] = "warning"
        result["warnings"].append("cad_ready_count=0")
    elif run_cad and not result["exported_files"].get("stl"):
        result["status"] = "failed"
        result["errors"].append("CAD export requested but no STL was exported")
    elif result["warnings"]:
        result["status"] = "warning"
    else:
        result["status"] = "success"


def run_truss_inverse_service(
    target_stiffness: Any,
    *,
    num_candidates: int = LEGACY_NUM_CANDIDATES,
    run_cad: bool = False,
    keep_work_dir: bool = False,
    output_dir: str | Path | None = None,
    model_variant: str = SUPPORTED_MODEL_VARIANT,
    force_run_low_target: bool = False,
    project_root: str | Path | None = None,
    python_exe: str | Path | None = None,
    timeout_inference: int = 120,
    timeout_data2fem: int = 30,
    timeout_diameter: int = 30,
    timeout_cad: int = 60,
    runtime_mode: str = "legacy_technical",
    formal_bundle_binding: Mapping[str, Any] | None = None,
    reproduction_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the isolated 150w TrussInvertibleDesign pipeline."""

    del timeout_cad  # CAD export is in-process in the current implementation.

    if runtime_mode not in SUPPORTED_RUNTIME_MODES:
        raise ValueError(f"unsupported runtime_mode: {runtime_mode}")
    checked_binding = None
    binding_sha = None
    if runtime_mode == "formal_product":
        try:
            checked_binding = _validate_formal_bundle_binding(formal_bundle_binding)
            binding_sha = checked_binding["binding_sha256"]
        except Exception as exc:
            result = _base_result(
                model_variant,
                runtime_mode=runtime_mode,
                formal_bundle_binding_sha256=None,
            )
            result["errors"].append(str(exc))
            return result
    elif formal_bundle_binding is not None:
        raise ValueError("formal_bundle_binding is only valid for formal_product runtime")
    if reproduction_profile is not None:
        from product_workflow.reproducibility import validate_reproduction_profile_v1

        reproduction_profile = validate_reproduction_profile_v1(reproduction_profile)

    result = _base_result(
        model_variant,
        runtime_mode=runtime_mode,
        formal_bundle_binding_sha256=binding_sha,
    )
    project_path = Path(project_root) if project_root is not None else _default_project_root()
    truss_source = project_path / "TrussInvertibleDesign"
    py = str(python_exe or sys.executable)

    if model_variant != SUPPORTED_MODEL_VARIANT:
        result["errors"].append("unsupported model_variant; first version only supports 150w")
        return result
    if num_candidates != LEGACY_NUM_CANDIDATES:
        result["warnings"].append("num_candidates_ignored_due_to_legacy_script")

    try:
        targets = _targets_from_input(target_stiffness)
    except Exception as exc:
        result["errors"].append(str(exc))
        return result

    validations = [validate_target_stiffness(target) for target in targets]
    result["target_validation"] = validations[0] if len(validations) == 1 else validations
    for validation in validations:
        result["warnings"].extend(validation.get("warnings", []))
        result["errors"].extend(validation.get("errors", []))
    if result["errors"]:
        return result
    if any(v.get("severity") == "warning" for v in validations) and not force_run_low_target:
        result["warnings"].append("low_target_warning_only_service_continued")

    if not truss_source.exists():
        result["errors"].append(f"TrussInvertibleDesign not found: {truss_source}")
        return result
    try:
        child_env = _child_environment(project_path, reproduction_profile)
        from product_workflow.artifact_manifest import DELIVERED_INVERSE_IDENTITY
        delivered = (checked_binding is not None and
                     checked_binding['artifacts']['inverse_model']['sha256'] == DELIVERED_INVERSE_IDENTITY['sha256'])
        child_env['LATTICEGPT_INVERSE_ALGORITHM'] = 'truss_latent_20261110' if delivered else 'truss_direct_legacy'
        if delivered:
            result.update(algorithm={'version': 'truss_latent_20261110', 'prediction_kind': 'forward_surrogate'},
                          diagnostics=[], elapsed_ms=0, failure=None)
    except Exception as exc:
        result["errors"].append(str(exc))
        return result

    temp_root = Path(tempfile.gettempdir()) / (
        "truss_inverse_service_"
        + time.strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    temp_truss = temp_root / "TrussInvertibleDesign"
    result["work_dir"] = str(temp_root)

    try:
        if runtime_mode == "formal_product":
            result["trusted_artifact_sha256"] = _stage_trusted_runtime_tree(
                truss_source,
                temp_truss,
                checked_binding["artifacts"],
            )
        else:
            shutil.copytree(truss_source, temp_truss)
        datatest = temp_truss / "datatest"
        target_csv = datatest / "target_stiffness.csv"
        prediction_csv = datatest / "prediction_target_test.csv"
        prediction2_csv = datatest / "prediction_target_test_2.csv"
        fem_csv = datatest / "Prediciton_FEM_test_150w.csv"
        dia_csv = datatest / "dia_cal.csv"
        diameter_csv = datatest / "truss_diameter_result.csv"

        _write_target_csv(target_csv, targets)
        result["raw_files"]["target_stiffness_csv"] = str(target_csv)

        steps = [
            _run_step(
                "inference",
                [py, "main_export_test.py"],
                temp_truss,
                timeout_inference,
                child_env,
            ),
        ]
        result["steps"].extend(steps)
        if steps[-1]["status"] != "success":
            result["errors"].append(steps[-1]["error"] or "inference failed")
            return result

        if delivered:
            with (datatest / 'inverse_metadata.json').open(encoding='utf-8') as stream:
                metadata = json.load(stream)
            if (set(metadata) != {'schema_version', 'algorithm', 'diagnostics', 'elapsed_ms'}
                or metadata['schema_version'] != 'truss_inverse_metadata_v1'
                or metadata['algorithm'] != result['algorithm']
                or type(metadata['elapsed_ms']) is not int or metadata['elapsed_ms'] < 0
                or not isinstance(metadata['diagnostics'], list) or len(metadata['diagnostics']) != len(targets)):
                raise ValueError('inverse metadata contract mismatch')
            for i, diagnostic in enumerate(metadata['diagnostics'], 1):
                if (set(diagnostic) != {'target_index', 'diversity_threshold', 'diversity_fallback'}
                    or diagnostic['target_index'] != i
                    or diagnostic['diversity_threshold'] not in (0., .16, .11, .08, .055, .035, .02)
                    or type(diagnostic['diversity_fallback']) is not bool
                    or diagnostic['diversity_fallback'] != (diagnostic['diversity_threshold'] == 0.)):
                    raise ValueError('inverse diversity diagnostic mismatch')
            result['diagnostics'] = metadata['diagnostics']
            result['raw_files']['inverse_metadata'] = str(datatest / 'inverse_metadata.json')

        if delivered:
            result['raw_files']['prediction_csv'] = str(prediction_csv)
            candidates = build_delivered_candidates(prediction_csv, targets, model_variant=model_variant)
        else:
            shutil.copy2(prediction_csv, prediction2_csv)
            step = _run_step(
                "data2FEM", [py, "data2FEM.py"], datatest, timeout_data2fem, child_env
            )
            result["steps"].append(step)
            if step["status"] != "success":
                result["errors"].append(step["error"] or "data2FEM failed")
                return result

            shutil.copy2(fem_csv, dia_csv)
            step = _run_step(
                "diameter_calculate",
                [py, "diameter_calculate.py"],
                datatest,
                timeout_diameter,
                child_env,
            )
            result["steps"].append(step)
            if step["status"] != "success":
                result["errors"].append(step["error"] or "diameter_calculate failed")
                return result

            result["raw_files"].update(
                {
                    "prediction_csv": str(prediction_csv),
                    "fem_csv": str(fem_csv),
                    "diameter_csv": str(diameter_csv),
                }
            )

            candidates = build_candidate_schema(
                prediction_csv,
                fem_csv,
                diameter_csv,
                targets,
                model_variant=model_variant,
            )
        result["candidates"] = candidates
        if delivered and (len(candidates) != 3 * len(targets) or any('nmse' not in c for c in candidates)):
            raise ValueError('delivered optimizer must return three candidates with NMSE per target')
        result["cad_ready_count"] = sum(1 for candidate in candidates if candidate["cad_ready"])

        if run_cad:
            cad_ready_candidates = [candidate for candidate in candidates if candidate["cad_ready"]]
            if not cad_ready_candidates:
                result["errors"].append("CAD export requested but no CAD-ready candidates exist")
            else:
                selected = sorted(
                    cad_ready_candidates,
                    key=lambda item: (-float(item["diameter"]), str(item["candidate_id"])),
                )[0]
                result["selected_candidate_id"] = selected["candidate_id"]
                cad_output_dir = (
                    Path(output_dir)
                    if output_dir is not None
                    else project_path / "generated_images" / "truss_inverse_service"
                )
                cad_result = export_candidate_cad(
                    selected,
                    cad_output_dir,
                    prefix=selected["candidate_id"],
                )
                if cad_result["status"] == "success":
                    result["exported_files"]["stl"] = cad_result["stl_path"]
                    result["exported_files"]["png"] = cad_result["preview_path"]
                else:
                    result["errors"].append(cad_result.get("error") or "CAD export failed")
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        if delivered:
            result['elapsed_ms'] = sum(s['elapsed_ms'] for s in result['steps'])
            if result['errors']:
                failed_step = next((s for s in result['steps'] if s['status'] != 'success'), None)
                message = '; '.join(result['errors'])
                code = ('INVERSE_TIMEOUT' if failed_step and 'timed out' in (failed_step.get('error') or '')
                        else 'INVERSE_EXECUTION_FAILED' if failed_step else 'INVERSE_OUTPUT_INVALID')
                result['failure'] = {'code': code, 'compute_stage': failed_step['step'] if failed_step else 'result_conversion',
                                     'message': message, 'next_step': 'Inspect the saved diagnostics before a new manually confirmed run.'}
        if keep_work_dir:
            result["work_dir_retained"] = True
            result["cleanup"] = {
                "status": "skipped_keep_work_dir_true",
                "work_dir_removed": False,
                "raw_files_available": True,
            }
        else:
            try:
                if temp_root.exists():
                    shutil.rmtree(temp_root)
                result["work_dir_retained"] = False
                result["cleanup"] = {
                    "status": "complete",
                    "work_dir_removed": True,
                    "raw_files_available": False,
                }
            except Exception as exc:
                result["work_dir_retained"] = True
                result["cleanup"] = {
                    "status": "failed",
                    "work_dir_removed": False,
                    "raw_files_available": True,
                }
                result["warnings"].append(f"work_dir_cleanup_failed: {exc}")

    _finalize_status(result, run_cad)
    return result
