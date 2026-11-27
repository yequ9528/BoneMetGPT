"""Pure P-10 candidate ranking for the bounded LatticeGPT 9.x contract."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = "latticegpt_9x_candidate_ranking_v1"
RANKING_CONTRACT = "scaler_range_weighted_RMSE_v1"
P09_SCHEMA_VERSION = "latticegpt_9x_restricted_9d_validation_v1"
P09_CONTRACT = "restricted_model_supported_9D_v1"
P09_STABILITY_RATIO_THRESHOLD = 1e-8
HOLD_NO_ELIGIBLE_CANDIDATE = "HOLD_NO_ELIGIBLE_CANDIDATE"
SELECTED_STATUS = "SURROGATE_RANKED_GEOMETRY_CANDIDATE"
MECHANICAL_MATCH_STATUS = "NOT_VALIDATED"
BASE_MATERIAL_MODULUS_GPA = 110.0
STIFFNESS_NAMES = (
    "C11",
    "C12",
    "C13",
    "C22",
    "C23",
    "C33",
    "C44",
    "C55",
    "C66",
)
WEIGHTS = {
    name: 2.0 if name in {"C11", "C22", "C33"} else 1.0
    for name in STIFFNESS_NAMES
}
REQUIRED_NON_CLAIMS = (
    "NOT_MECHANICAL_PASS",
    "NOT_OOD_PASS",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
)
P09_NON_CLAIMS = (
    "NOT_ORIGINAL_FULL_6X6",
    "NOT_DATABASE_ROW_FULL_MATRIX_QC",
    "NOT_GENERAL_9D_ASSEMBLY_APPROVAL",
    "NOT_MECHANICAL_PASS",
)


def check_target_scaler_range(
    target_stiffness_gpa: Mapping[str, Any],
    *,
    scaler_y_min: Mapping[str, Any],
    scaler_y_max: Mapping[str, Any],
    raw_normalized_9d: Mapping[str, Any] | None = None,
    scaler_y_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the input actually received by inference, without changing inputs.

    main_export_test divides the physical GPa target by 110 before converting
    it to float32. Keep both precisions in evidence; do not round the bounds,
    add a tolerance, or clip a target to make the comparison pass.
    """
    required = set(STIFFNESS_NAMES)
    inputs = (target_stiffness_gpa, scaler_y_min, scaler_y_max)
    shape_valid = all(isinstance(item, Mapping) and set(item) == required for item in inputs)
    if raw_normalized_9d is not None:
        shape_valid = shape_valid and isinstance(raw_normalized_9d, Mapping) and set(raw_normalized_9d) == required
    fields: list[dict[str, Any]] = []
    def value(mapping, name):
        return _finite_number(mapping.get(name)) if isinstance(mapping, Mapping) else None

    for name in STIFFNESS_NAMES:
        physical = value(target_stiffness_gpa, name)
        normalized = None if physical is None else physical / BASE_MATERIAL_MODULUS_GPA
        raw = normalized if raw_normalized_9d is None else value(raw_normalized_9d, name)
        low, high = value(scaler_y_min, name), value(scaler_y_max, name)
        error = None
        effective = None
        if not shape_valid:
            error = "COMPLETE_NINE_COMPONENTS_REQUIRED"
        elif physical is None or raw is None:
            error = "FINITE_NUMERIC_TARGET_REQUIRED"
        elif low is None or high is None or high <= low or not math.isfinite(high - low):
            error = "INVALID_SCALER_BOUNDARY"
        else:
            with np.errstate(over="ignore", invalid="ignore", under="ignore"):
                converted = float(np.float32(normalized))
                converted_raw = float(np.float32(raw))
            if not math.isfinite(converted) or not math.isfinite(converted_raw):
                error = "NONFINITE_FLOAT32_CONVERSION"
            elif converted_raw != converted:
                error = "NORMALIZED_PHYSICAL_MISMATCH"
            else:
                effective = converted

        raw_in_range = None if raw is None or low is None or high is None else low <= raw <= high
        effective_in_range = None if effective is None else low <= effective <= high
        if error:
            rounding = "INVALID_CONVERSION"
        elif raw == effective:
            rounding = "UNCHANGED"
        elif not raw_in_range and effective_in_range:
            rounding = "ROUNDED_TO_BOUNDARY" if effective in (low, high) else "ROUNDED_INTO_RANGE"
        elif raw_in_range and not effective_in_range:
            rounding = "ROUNDED_OUT_OF_RANGE"
        else:
            rounding = "ROUNDED_WITHIN_RANGE" if effective_in_range else "REMAINS_OUT_OF_RANGE"
        fields.append({
            "field": name,
            "physical_value_gpa": physical,
            "raw_value_C_over_Eb": raw,
            "value_C_over_Eb": raw,
            "effective_value_C_over_Eb": effective,
            "minimum": low,
            "maximum": high,
            "raw_delta_from_minimum": None if raw is None or low is None else _finite_number(raw - low),
            "raw_delta_from_maximum": None if raw is None or high is None else _finite_number(raw - high),
            "effective_delta_from_minimum": None if effective is None else _finite_number(effective - low),
            "effective_delta_from_maximum": None if effective is None else _finite_number(effective - high),
            "raw_in_range": raw_in_range,
            "effective_in_range": effective_in_range,
            "rounding_classification": rounding,
            "status": "HOLD_INVALID_CONVERSION" if error else "PASS" if effective_in_range else "HOLD_OUT_OF_RANGE",
            "error_code": error,
        })
    invalid = any(item["error_code"] for item in fields)
    passed = all(item["status"] == "PASS" for item in fields)
    return {
        "schema_version": "latticegpt_target_scaler_range_v1",
        "criterion": "strict_float32_C_over_Eb_v1",
        "status": "PASS" if passed else "HOLD",
        "failure_kind": "INVALID_CONVERSION" if invalid else None if passed else "SCALER_RANGE",
        "comparison_dtype": "float32",
        "input_units": "GPa",
        "comparison_units": "dimensionless_C_over_Eb",
        "conversion": "float32(physical_9d_gpa / 110.0)",
        "base_material_modulus_gpa": BASE_MATERIAL_MODULUS_GPA,
        "scaler_y_identity": dict(scaler_y_identity) if scaler_y_identity is not None else None,
        "fields": fields,
        "OOD_status": "not_available",
        "range_PASS_is_OOD_or_reliability_PASS": False,
    }


def target_validation_failure_kind(
    range_check: Mapping[str, Any], p09_validation: Mapping[str, Any]
) -> str | None:
    """Keep invalid conversion, scaler support and P-09 failures distinct."""
    if range_check.get("failure_kind") == "INVALID_CONVERSION" or p09_validation.get("evaluated") is False:
        return "INVALID_CONVERSION"
    range_failed = range_check.get("status") != "PASS"
    p09_failed = p09_validation.get("status") != "PASS"
    if range_failed and p09_failed:
        return "BOTH"
    return "SCALER_RANGE" if range_failed else "P09" if p09_failed else None


def validate_target_p09(
    target_stiffness_gpa: Mapping[str, Any], range_check: Mapping[str, Any]
) -> dict[str, Any]:
    """Guard target evaluation and JSON evidence without changing P-09 math."""
    error = None
    if range_check.get("failure_kind") == "INVALID_CONVERSION":
        error = "INVALID_FLOAT32_CONVERSION"
    else:
        try:
            result = validate_restricted_9d_p09(target_stiffness_gpa)
        except (np.linalg.LinAlgError, FloatingPointError, OverflowError):
            error = "P09_NUMERIC_EVALUATION_FAILED"
        else:
            numeric = (result.get(name) for name in ("lambda_min", "lambda_max", "lambda_min_over_lambda_max"))
            if any(item is not None and _finite_number(item) is None for item in numeric):
                error = "NONFINITE_P09_RESULT"
            else:
                return {**result, "evaluated": True}
    return {
        "schema_version": P09_SCHEMA_VERSION,
        "contract": P09_CONTRACT,
        "status": "HOLD",
        "evaluated": False,
        "reasons": [error],
        "error_code": error,
        "stiffness_order": list(STIFFNESS_NAMES),
        "lambda_min": None,
        "lambda_max": None,
        "lambda_min_over_lambda_max": None,
        "threshold": P09_STABILITY_RATIO_THRESHOLD,
        "non_claims": list(P09_NON_CLAIMS),
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def validate_restricted_9d_p09(stiffness: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the approved restricted 9D view without claiming a full matrix."""

    result: dict[str, Any] = {
        "schema_version": P09_SCHEMA_VERSION,
        "contract": P09_CONTRACT,
        "status": None,
        "reasons": [],
        "stiffness_order": list(STIFFNESS_NAMES),
        "selected_view_not_original_full_matrix": True,
        "reduced_matrix_is_database_original_full_matrix": False,
        "stability_operator": "reduced_9D_block_diagonal_comparison_v1",
        "lambda_min": None,
        "lambda_max": None,
        "lambda_min_over_lambda_max": None,
        "threshold": P09_STABILITY_RATIO_THRESHOLD,
        "non_claims": list(P09_NON_CLAIMS),
    }

    normalized = _optional_finite_nine(stiffness)
    if normalized is None:
        result["status"] = "FAIL"
        result["reasons"].append("COMPLETE_FINITE_9D_REQUIRED")
        return result

    diagonal_names = ("C11", "C22", "C33", "C44", "C55", "C66")
    if any(normalized[name] <= 0 for name in diagonal_names):
        result["status"] = "FAIL"
        result["reasons"].append("POSITIVE_DIAGONAL_REQUIRED")
        return result

    coupling_names = ("C12", "C13", "C23")
    if any(normalized[name] < 0 for name in coupling_names):
        result["status"] = "HOLD"
        result["reasons"].append("NEGATIVE_COUPLING_UNSUPPORTED_SCOPE")
        return result

    comparison_matrix = np.array(
        [
            [normalized["C11"], normalized["C12"], normalized["C13"], 0, 0, 0],
            [normalized["C12"], normalized["C22"], normalized["C23"], 0, 0, 0],
            [normalized["C13"], normalized["C23"], normalized["C33"], 0, 0, 0],
            [0, 0, 0, normalized["C44"], 0, 0],
            [0, 0, 0, 0, normalized["C55"], 0],
            [0, 0, 0, 0, 0, normalized["C66"]],
        ],
        dtype=float,
    )
    eigenvalues = np.linalg.eigvalsh(comparison_matrix)
    lambda_min = float(eigenvalues[0])
    lambda_max = float(eigenvalues[-1])
    ratio = lambda_min / lambda_max
    result["lambda_min"] = lambda_min
    result["lambda_max"] = lambda_max
    result["lambda_min_over_lambda_max"] = ratio

    if ratio <= 0:
        result["status"] = "FAIL"
        result["reasons"].append("NONPOSITIVE_REDUCED_9D_STABILITY")
    elif ratio <= P09_STABILITY_RATIO_THRESHOLD:
        result["status"] = "HOLD"
        result["reasons"].append("BORDERLINE_REDUCED_9D_STABILITY")
    else:
        result["status"] = "PASS"
    return result


def rank_candidates_p10(
    candidates: Sequence[Mapping[str, Any]],
    *,
    target_stiffness_gpa: Mapping[str, Any],
    scaler_y_min: Mapping[str, Any],
    scaler_y_max: Mapping[str, Any],
    target_range_status: str,
) -> dict[str, Any]:
    """Rank eligible candidates without asserting a mechanical acceptance result."""

    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise TypeError("candidates must be a sequence of mappings")

    target = _require_finite_nine(target_stiffness_gpa, "target_stiffness_gpa")
    scaler_min = _require_finite_nine(scaler_y_min, "scaler_y_min")
    scaler_max = _require_finite_nine(scaler_y_max, "scaler_y_max")
    scaler_ranges = _build_scaler_ranges(scaler_min, scaler_max)

    source_ids: set[str] = set()
    evaluated: list[dict[str, Any]] = []
    range_pass = target_range_status == "PASS"

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise TypeError("each candidate must be a mapping")
        source_candidate_id = candidate.get("source_candidate_id")
        if not isinstance(source_candidate_id, str) or not source_candidate_id:
            raise ValueError("source_candidate_id must be a non-empty string")
        if source_candidate_id in source_ids:
            raise ValueError(f"duplicate source_candidate_id: {source_candidate_id}")
        source_ids.add(source_candidate_id)

        exclusion_reasons, prediction = _candidate_eligibility(candidate, range_pass)

        normalized_errors: dict[str, float] | None = None
        score: float | None = None
        max_component_error: float | None = None
        if not exclusion_reasons and prediction is not None:
            normalized_errors = {
                name: (
                    (prediction[name] / BASE_MATERIAL_MODULUS_GPA)
                    - (target[name] / BASE_MATERIAL_MODULUS_GPA)
                )
                / scaler_ranges[name]
                for name in STIFFNESS_NAMES
            }
            weighted_square_sum = sum(
                WEIGHTS[name] * normalized_errors[name] ** 2
                for name in STIFFNESS_NAMES
            )
            score = math.sqrt(weighted_square_sum / sum(WEIGHTS.values()))
            max_component_error = max(
                abs(normalized_errors[name]) for name in STIFFNESS_NAMES
            )

        evaluated.append(
            {
                "source_candidate_id": source_candidate_id,
                "eligible": not exclusion_reasons,
                "exclusion_reasons": exclusion_reasons,
                "normalized_errors": normalized_errors,
                "score": score,
                "max_absolute_component_error": max_component_error,
                "rank": None,
                "selected": False,
                "diameter_used_in_ranking": False,
            }
        )

    eligible = [item for item in evaluated if item["eligible"]]
    eligible.sort(
        key=lambda item: (
            item["score"],
            item["max_absolute_component_error"],
            item["source_candidate_id"],
        )
    )
    for rank, item in enumerate(eligible, start=1):
        item["rank"] = rank
    selected = eligible[0] if eligible else None
    if selected is not None:
        selected["selected"] = True

    return {
        "schema_version": SCHEMA_VERSION,
        "contract": RANKING_CONTRACT,
        "status": "RANKED" if selected is not None else HOLD_NO_ELIGIBLE_CANDIDATE,
        "selected_source_candidate_id": (
            selected["source_candidate_id"] if selected is not None else None
        ),
        "selected_status": SELECTED_STATUS if selected is not None else None,
        "mechanical_match_status": MECHANICAL_MATCH_STATUS,
        "validated_mechanical_pass_threshold": None,
        "OOD_status": "not_available",
        "range_PASS_is_OOD_or_reliability_PASS": False,
        "target_range_status": target_range_status,
        "base_material_modulus_gpa": BASE_MATERIAL_MODULUS_GPA,
        "stiffness_order": list(STIFFNESS_NAMES),
        "weights": dict(WEIGHTS),
        "scaler_y_ranges": scaler_ranges,
        "diameter_used_in_ranking": False,
        "candidates": evaluated,
        "non_claims": list(REQUIRED_NON_CLAIMS),
        "runs_model": False,
        "runs_runtime": False,
        "writes_files": False,
    }


def _candidate_eligibility(candidate, range_pass):
    reasons = []
    if candidate.get('cad_ready') is not True: reasons.append('CAD_READY_REQUIRED')
    if candidate.get('b14_status') != 'PASS': reasons.append('B14_PASS_REQUIRED')
    if not range_pass: reasons.append('TARGET_RANGE_PASS_REQUIRED')
    prediction = _optional_finite_nine(candidate.get('predicted_stiffness'))
    if prediction is None: reasons.append('COMPLETE_FINITE_9D_REQUIRED')
    return reasons, prediction


def rank_candidates_nmse(candidates, *, target_stiffness_gpa, target_range_status):
    target = _require_finite_nine(target_stiffness_gpa, 'target_stiffness_gpa')
    denominator = sum(v*v for v in target.values())
    evaluated, seen = [], set()
    for order, candidate in enumerate(candidates, 1):
        identity = candidate.get('source_candidate_id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('candidate identities must be non-empty and unique')
        seen.add(identity)
        reasons, prediction = _candidate_eligibility(candidate, target_range_status == 'PASS')
        raw = candidate.get('nmse')
        if isinstance(raw, bool) or raw is None or not math.isfinite(float(raw)) or float(raw) < 0:
            raise ValueError('NMSE must be a finite nonnegative number')
        nmse = float(raw)
        if prediction is None or denominator <= 0 or not math.isclose(nmse, sum((prediction[k]-target[k])**2 for k in target)/denominator, rel_tol=1e-3, abs_tol=1e-10):
            raise ValueError('NMSE is inconsistent with target and prediction')
        evaluated.append(dict(source_candidate_id=identity, source_order=order, nmse=nmse,
            eligible=not reasons, exclusion_reasons=reasons, selected=False, diameter_used_in_ranking=False))
    ordered = sorted(evaluated, key=lambda c: (c['nmse'], c['source_order']))
    for rank, item in enumerate(ordered, 1): item['rank'] = rank
    selected = next((c for c in ordered if c['eligible']), None)
    if selected is not None: selected['selected'] = True
    return dict(contract='truss_owner_nmse_v1', status='RANKED' if selected else HOLD_NO_ELIGIBLE_CANDIDATE,
        selected_source_candidate_id=selected['source_candidate_id'] if selected else None, candidates=evaluated)


def _require_finite_nine(value: Any, label: str) -> dict[str, float]:
    normalized = _optional_finite_nine(value)
    if normalized is None:
        raise ValueError(f"{label} must contain exactly nine finite stiffness values")
    return normalized


def _optional_finite_nine(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or set(value) != set(STIFFNESS_NAMES):
        return None
    normalized: dict[str, float] = {}
    for name in STIFFNESS_NAMES:
        item = value[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            return None
        normalized[name] = float(item)
    return normalized


def _build_scaler_ranges(
    scaler_min: Mapping[str, float],
    scaler_max: Mapping[str, float],
) -> dict[str, float]:
    ranges: dict[str, float] = {}
    for name in STIFFNESS_NAMES:
        value = scaler_max[name] - scaler_min[name]
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"scaler range for {name} must be positive and finite")
        ranges[name] = value
    return ranges
