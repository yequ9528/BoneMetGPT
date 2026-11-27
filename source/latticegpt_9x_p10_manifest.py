"""Identity-bound P-09/P-10 manifest production for a technical fixture."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import torch

from latticegpt_9x_candidate_ranking import (
    STIFFNESS_NAMES,
    check_target_scaler_range,
    rank_candidates_p10,
    target_validation_failure_kind,
    validate_target_p09,
    validate_restricted_9d_p09,
)


SCHEMA_VERSION = "latticegpt_9x_p10_ranking_manifest_v1"
OUTPUT_FILENAME = "p10_ranking_manifest.json"
REQUIRED_NON_CLAIMS = (
    "TECHNICAL_VALIDATION_ONLY",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_CURRENT_FORMAL_150W_PROVENANCE",
    "NOT_ORIGINAL_FULL_6X6",
    "NOT_DOWNSTREAM_ADAPTER_REBOUND",
    "NOT_FULL_DUAL_SPACE_FUSION_COMPLETION",
)


def produce_p10_ranking_manifest(
    *,
    run_manifest_path: str | Path,
    candidate_manifest_paths: Iterable[str | Path],
    scaler_y_path: str | Path,
    expected_scaler_y_sha256: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Read bounded inputs and exclusively create one P-10 ranking manifest."""

    run_path = _require_file(run_manifest_path, "run manifest")
    candidate_paths = [
        _require_file(path, "candidate manifest")
        for path in candidate_manifest_paths
    ]
    if not candidate_paths:
        raise ValueError("at least one candidate manifest is required")
    scaler_path = _require_file(scaler_y_path, "scaler_Y")
    expected_scaler_sha = _require_sha256(
        expected_scaler_y_sha256,
        "expected scaler_Y SHA256",
    )
    actual_scaler_sha = _sha256(scaler_path)
    if actual_scaler_sha != expected_scaler_sha:
        raise ValueError("scaler_Y SHA256 mismatch")

    resolved_output_dir = Path(output_dir).resolve()
    if resolved_output_dir.exists():
        raise FileExistsError(f"output directory already exists: {resolved_output_dir}")

    run_manifest = _read_json_object(run_path, "run manifest")
    run_id = _require_nonempty_string(run_manifest.get("run_id"), "run_id")
    task_id = _require_nonempty_string(run_manifest.get("task_id"), "task_id")
    candidate_count = run_manifest.get("candidate_count")
    if candidate_count != len(candidate_paths):
        raise ValueError("candidate manifest count does not match run manifest")

    source_context = run_manifest.get("source_context")
    if not isinstance(source_context, dict):
        raise ValueError("run manifest source_context is required")
    target_sha = _require_sha256(source_context.get("target_sha256"), "target SHA256")
    physical_target = source_context.get("physical_target_9d")
    if not isinstance(physical_target, dict) or physical_target.get("units") != "GPa":
        raise ValueError("physical target must be recorded in GPa")
    target = _require_nine(physical_target.get("values"), "physical target")

    scaler_min, scaler_max = _load_scaler_y_bounds(scaler_path)
    scaler_min_by_name = dict(zip(STIFFNESS_NAMES, scaler_min, strict=True))
    scaler_max_by_name = dict(zip(STIFFNESS_NAMES, scaler_max, strict=True))
    target_range = _check_target_range(
        target, scaler_min_by_name, scaler_max_by_name,
        scaler_y_identity={"path": str(scaler_path), "sha256": actual_scaler_sha, "size_bytes": scaler_path.stat().st_size},
    )
    target_p09 = validate_target_p09(target, target_range)
    failure_kind = target_validation_failure_kind(target_range, target_p09)

    ranking_inputs: list[dict[str, Any]] = []
    candidate_inputs: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    for candidate_path in candidate_paths:
        manifest = _read_json_object(candidate_path, "candidate manifest")
        if manifest.get("run_id") != run_id or manifest.get("task_id") != task_id:
            raise ValueError("candidate run/task identity mismatch")
        if str(manifest.get("target_sha256", "")).upper() != target_sha:
            raise ValueError("candidate target SHA256 mismatch")

        candidate_id = _require_nonempty_string(
            manifest.get("candidate_id"),
            "candidate_id",
        )
        if candidate_id in seen_candidate_ids:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen_candidate_ids.add(candidate_id)
        source_candidate_id = _require_nonempty_string(
            manifest.get("source_candidate_id"),
            "source_candidate_id",
        )
        candidate = manifest.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("candidate payload is required")
        candidate_target = _require_nine(
            candidate.get("target_stiffness"),
            "candidate target stiffness",
        )
        if candidate_target != target:
            raise ValueError("candidate target stiffness mismatch")
        prediction = _require_nine(
            candidate.get("predicted_stiffness"),
            "candidate predicted stiffness",
        )
        p09_validation = validate_restricted_9d_p09(prediction)

        ranking_inputs.append(
            {
                "source_candidate_id": source_candidate_id,
                "cad_ready": candidate.get("cad_ready") is True,
                "b14_status": p09_validation["status"],
                "predicted_stiffness": prediction,
                "diameter": candidate.get("diameter"),
            }
        )
        candidate_inputs.append(
            {
                "path": str(candidate_path),
                "sha256": _sha256(candidate_path),
                "candidate_id": candidate_id,
                "source_candidate_id": source_candidate_id,
                "cad_ready": candidate.get("cad_ready") is True,
                "p09_validation": p09_validation,
            }
        )

    effective_range_status = (
        "PASS"
        if failure_kind is None
        else "HOLD"
    )
    ranking = rank_candidates_p10(
        ranking_inputs,
        target_stiffness_gpa=target,
        scaler_y_min=scaler_min_by_name,
        scaler_y_max=scaler_max_by_name,
        target_range_status=effective_range_status,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_level": "TECHNICAL_FIXTURE",
        "producer": {
            "entrypoint": "latticegpt_9x_p10_manifest.produce_p10_ranking_manifest",
            "source_sha256": _sha256(Path(__file__).resolve()),
            "writes_only": OUTPUT_FILENAME,
            "overwrite": False,
        },
        "run_input": {
            "path": str(run_path),
            "sha256": _sha256(run_path),
            "task_id": task_id,
            "run_id": run_id,
            "target_sha256": target_sha,
        },
        "superseded_selection": run_manifest.get("selection"),
        "scaler_y": {
            "path": str(scaler_path),
            "sha256": actual_scaler_sha,
            "min": scaler_min_by_name,
            "max": scaler_max_by_name,
            "units": "dimensionless_C_over_Eb",
        },
        "target": {
            "units": "GPa",
            "values": target,
            "range_check": target_range,
            "validation_failure_kind": failure_kind,
        },
        "p09_target_validation": target_p09,
        "candidate_inputs": candidate_inputs,
        "ranking": ranking,
        "non_claims": list(REQUIRED_NON_CLAIMS),
        "runs_model": False,
        "runs_adapter": False,
        "runs_conformal": False,
        "runs_viewer": False,
        "formal_gate_effect": "NONE",
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    resolved_output_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = resolved_output_dir / OUTPUT_FILENAME
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(manifest_text)
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "selected_source_candidate_id": ranking["selected_source_candidate_id"],
        "status": ranking["status"],
    }


def _load_scaler_y_bounds(path: Path) -> tuple[list[float], list[float]]:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict) or set(payload) != {"min", "max"}:
        raise ValueError("scaler_Y payload must contain exactly min and max")
    minimum = _tensor_to_nine(payload["min"], "scaler_Y min")
    maximum = _tensor_to_nine(payload["max"], "scaler_Y max")
    for index, (low, high) in enumerate(zip(minimum, maximum, strict=True)):
        if high <= low:
            raise ValueError(f"scaler_Y range at index {index} must be positive")
    return minimum, maximum


def _tensor_to_nine(value: Any, label: str) -> list[float]:
    if not isinstance(value, torch.Tensor) or value.numel() != len(STIFFNESS_NAMES):
        raise ValueError(f"{label} must be a nine-value tensor")
    values = [float(item) for item in value.detach().cpu().reshape(-1).tolist()]
    if any(not math.isfinite(item) for item in values):
        raise ValueError(f"{label} must contain finite values")
    return values


def _check_target_range(
    target_gpa: dict[str, float],
    scaler_min: dict[str, float],
    scaler_max: dict[str, float],
    *,
    scaler_y_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return check_target_scaler_range(
        target_gpa, scaler_y_min=scaler_min, scaler_y_max=scaler_max,
        scaler_y_identity=scaler_y_identity,
    )


def _require_file(value: str | Path, label: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a 64-character SHA256")
    normalized = value.upper()
    if any(character not in "0123456789ABCDEF" for character in normalized):
        raise ValueError(f"{label} must be hexadecimal")
    return normalized


def _require_nine(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(STIFFNESS_NAMES):
        raise ValueError(f"{label} must contain exactly the supported nine fields")
    normalized: dict[str, float] = {}
    for name in STIFFNESS_NAMES:
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{label} must contain finite numeric values")
        normalized[name] = float(item)
    return normalized


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()
