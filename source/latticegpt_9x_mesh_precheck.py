"""9.x technical-fixture mesh checks with no model or conformal runtime imports."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "latticegpt_9x_mesh_precheck_v1"
FIXTURE_RECORD_SCHEMA = "latticegpt_9x_technical_fixture_v1"
SUPPORTED_HEX_TYPES = {"C3D8", "C3D8R"}
WHOLE_FILE_TECHNICAL_DOMAIN = "WHOLE_FILE_TECHNICAL_DOMAIN"
WHOLE_FILE_SELECTOR_VALUE = "ALL_PARSED_ELEMENTS"
WHOLE_FILE_TECHNICAL_PURPOSE = "OWNER_CONFIRMED_CROPPED_FILL_REGION"
WHOLE_FILE_PRODUCT_DOMAIN = "WHOLE_FILE_IMPLANT_DOMAIN"
WHOLE_FILE_PRODUCT_PURPOSE = "PRODUCT_WHOLE_FILE_IMPLANT_CONFIRMATION"
ELEMENT_TYPE_EFFECTIVE = {
    "C3D8": "C3D8",
    "C3D8R": "C3D8_GEOMETRY_COMPATIBLE",
}
GEOMETRY_CONTRACT = "PROJECT_TOLERANCE_V1"
VOLUME_RULE = "2x2x2_GAUSS_POSITIVE_DETJ"
MIN_SCALED_JACOBIAN_PASS = 0.20
MAX_JACOBIAN_CONDITION_PASS = 20.0
MAX_JACOBIAN_CONDITION_HOLD = 50.0
REQUIRED_NON_CLAIMS = [
    "TECHNICAL_VALIDATION_ONLY",
    "NOT_FORMAL_ENGINEERING_CASE",
    "NOT_GATE_A1A_PASS",
    "NOT_REAL_BONE_DEFECT_VALIDATION",
]

_ELEMENT_TYPE_RE = re.compile(r"\btype\s*=\s*([^,\s]+)", re.IGNORECASE)
_ELSET_NAME_RE = re.compile(r"\belset\s*=\s*([^,\s]+)", re.IGNORECASE)


def precheck_inp_mesh(
    source_mesh_path: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
    expected_node_count: int | None = None,
    expected_element_count: int | None = None,
    expected_element_type_original: str | None = None,
    expected_element_type_effective: str | None = None,
    selector_type: str | None,
    selector_value: str | None,
    expected_selected_element_count: int | None = None,
    selector_explicit_declaration: bool | None = None,
    selector_purpose: str | None = None,
    expected_selected_element_ids_sha256: str | None = None,
    allow_mixed_hex: bool = False,
) -> dict[str, Any]:
    """Inspect an Abaqus input mesh and fail closed on type or domain drift."""

    path = Path(source_mesh_path).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not path.is_file():
        return _failed_file_result(path, "source mesh not found")
    if path.suffix.lower() != ".inp":
        return _failed_file_result(path, "source mesh must be an .inp file")

    content = path.read_bytes()
    source_sha256 = hashlib.sha256(content).hexdigest().upper()
    size_bytes = len(content)
    if expected_sha256 and source_sha256 != str(expected_sha256).upper():
        errors.append("source mesh SHA256 does not match fixture record")
    if expected_size_bytes is not None and size_bytes != int(expected_size_bytes):
        errors.append("source mesh size does not match fixture record")

    parsed = _parse_inp(path)
    errors.extend(parsed["errors"])

    nodes: dict[int, tuple[float, float, float]] = parsed["nodes"]
    elements: dict[int, dict[str, Any]] = parsed["elements"]
    element_types = sorted({element["type"] for element in elements.values()})
    unsupported_types = sorted(set(element_types) - SUPPORTED_HEX_TYPES)
    if unsupported_types:
        errors.append("unsupported element types: " + ", ".join(unsupported_types))
    if len(element_types) > 1 and not (allow_mixed_hex and set(element_types) == SUPPORTED_HEX_TYPES):
        errors.append("mixed element types are not allowed")

    element_type_original = element_types[0] if len(element_types) == 1 else None
    element_type_effective = ELEMENT_TYPE_EFFECTIVE.get(element_type_original)
    if allow_mixed_hex and set(element_types) == SUPPORTED_HEX_TYPES:
        element_type_original, element_type_effective = "C3D8+C3D8R", "C3D8_GEOMETRY_COMPATIBLE"
    missing_node_ids = sorted(
        {
            node_id
            for element in elements.values()
            for node_id in element["node_ids"]
            if node_id not in nodes
        }
    )
    if missing_node_ids:
        errors.append(
            "element references missing nodes: "
            + ", ".join(str(value) for value in missing_node_ids)
        )

    invalid_arity_ids = sorted(
        element_id
        for element_id, element in elements.items()
        if element["type"] in SUPPORTED_HEX_TYPES and len(element["node_ids"]) != 8
    )
    if invalid_arity_ids:
        errors.append(
            "C3D8/C3D8R elements must reference exactly 8 nodes: "
            + ", ".join(str(value) for value in invalid_arity_ids)
        )

    _check_expected("node count", len(nodes), expected_node_count, errors)
    _check_expected("element count", len(elements), expected_element_count, errors)
    _check_expected(
        "original element type",
        element_type_original,
        expected_element_type_original,
        errors,
    )
    _check_expected(
        "effective element type",
        element_type_effective,
        expected_element_type_effective,
        errors,
    )

    design_domain, selected_element_ids = _select_design_domain(
        parsed["elsets"],
        elements,
        selector_type=selector_type,
        selector_value=selector_value,
        expected_selected_element_count=expected_selected_element_count,
        selector_explicit_declaration=selector_explicit_declaration,
        selector_purpose=selector_purpose,
        expected_selected_element_ids_sha256=expected_selected_element_ids_sha256,
        errors=errors,
    )

    geometry_quality = _evaluate_geometry_quality(
        nodes,
        elements,
        selected_element_ids,
        errors=errors,
        warnings=warnings,
    )

    if not nodes:
        errors.append("source mesh contains no nodes")
    if not elements:
        errors.append("source mesh contains no elements")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed" if errors else "passed",
        "source": {
            "path": str(path),
            "size_bytes": size_bytes,
            "sha256": source_sha256,
        },
        "mesh": {
            "node_count": len(nodes),
            "element_count": len(elements),
            "element_types_original": element_types,
            "element_type_original": element_type_original,
            "element_type_effective": element_type_effective,
            "unsupported_element_types": unsupported_types,
            "missing_node_references": missing_node_ids,
            "invalid_arity_element_ids": invalid_arity_ids,
        },
        "design_domain": design_domain,
        "geometry_quality": geometry_quality,
        "errors": errors,
        "warnings": warnings,
        "non_claims": list(REQUIRED_NON_CLAIMS),
        "runs_runtime": False,
        "writes_files": False,
    }


def precheck_technical_fixture_record(record_path: str | Path) -> dict[str, Any]:
    """Validate one technical-fixture record and its bound source mesh."""

    path = Path(record_path).resolve()
    if not path.is_file():
        return _failed_file_result(path, "technical fixture record not found")

    record_sha256 = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed_file_result(path, f"technical fixture record is invalid: {exc}")

    record_errors = _validate_fixture_record(record)
    domain = record.get("design_domain") if isinstance(record, dict) else {}
    if not isinstance(domain, dict):
        domain = {}

    result = precheck_inp_mesh(
        record.get("source_mesh_path", ""),
        expected_sha256=record.get("source_mesh_sha256"),
        expected_size_bytes=record.get("source_mesh_size_bytes"),
        expected_node_count=record.get("expected_node_count"),
        expected_element_count=record.get("expected_element_count"),
        expected_element_type_original=record.get("expected_element_type_original"),
        expected_element_type_effective=record.get("expected_element_type_effective"),
        selector_type=domain.get("selector_type"),
        selector_value=domain.get("selector_value"),
        expected_selected_element_count=domain.get("expected_selected_element_count"),
        selector_explicit_declaration=domain.get("explicit_declaration"),
        selector_purpose=domain.get("purpose"),
        expected_selected_element_ids_sha256=domain.get(
            "expected_selected_element_ids_sha256"
        ),
    )
    result["fixture_record"] = {
        "path": str(path),
        "sha256": record_sha256,
        "fixture_id": record.get("fixture_id"),
        "classification": record.get("classification"),
        "permitted_use": record.get("permitted_use"),
        "mesh_units": record.get("mesh_units"),
        "nominal_mesh_size": record.get("nominal_mesh_size"),
        "nominal_mesh_size_units": record.get("nominal_mesh_size_units"),
        "mesh_frame": record.get("mesh_frame"),
        "source_model_status": record.get("source_model_status"),
        "supporting_evidence": deepcopy(record.get("supporting_evidence")),
        "formal_gate_effect": record.get("formal_gate_effect"),
    }
    result["errors"] = record_errors + result["errors"]
    result["status"] = "failed" if result["errors"] else "passed"
    return result


def _parse_inp(path: Path) -> dict[str, Any]:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, dict[str, Any]] = {}
    elsets: dict[str, list[int]] = {}
    errors: list[str] = []
    section: str | None = None
    element_type: str | None = None
    elset_name: str | None = None

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            section = None
            element_type = None
            elset_name = None
            lower = line.lower()
            if lower.startswith("*node"):
                section = "node"
            elif lower.startswith("*element"):
                match = _ELEMENT_TYPE_RE.search(line)
                if not match:
                    errors.append(f"element section missing TYPE at line {line_number}")
                else:
                    section = "element"
                    element_type = match.group(1).strip().upper()
            elif lower.startswith("*elset"):
                match = _ELSET_NAME_RE.search(line)
                if not match:
                    errors.append(f"ELSET section missing name at line {line_number}")
                else:
                    section = "elset"
                    elset_name = match.group(1).strip()
                    elsets.setdefault(elset_name, [])
            continue

        values = [value.strip() for value in line.split(",") if value.strip()]
        try:
            if section == "node":
                if len(values) < 4:
                    errors.append(f"invalid node row at line {line_number}")
                    continue
                node_id = int(values[0])
                if node_id in nodes:
                    errors.append(f"duplicate node id: {node_id}")
                nodes[node_id] = (
                    float(values[1]),
                    float(values[2]),
                    float(values[3]),
                )
            elif section == "element" and element_type:
                if len(values) < 2:
                    errors.append(f"invalid element row at line {line_number}")
                    continue
                element_id = int(values[0])
                if element_id in elements:
                    errors.append(f"duplicate element id: {element_id}")
                elements[element_id] = {
                    "type": element_type,
                    "node_ids": tuple(int(value) for value in values[1:]),
                }
            elif section == "elset" and elset_name:
                elsets[elset_name].extend(int(value) for value in values)
        except ValueError:
            errors.append(f"invalid numeric data at line {line_number}")

    return {"nodes": nodes, "elements": elements, "elsets": elsets, "errors": errors}


def _select_design_domain(
    elsets: dict[str, list[int]],
    elements: dict[int, dict[str, Any]],
    *,
    selector_type: str | None,
    selector_value: str | None,
    expected_selected_element_count: int | None,
    selector_explicit_declaration: bool | None,
    selector_purpose: str | None,
    expected_selected_element_ids_sha256: str | None,
    errors: list[str],
) -> tuple[dict[str, Any], list[int]]:
    normalized_type = str(selector_type or "").upper()
    normalized_value = str(selector_value or "").strip()
    selected_ids: list[int] = []
    selection_is_explicit = False
    selection_source: str | None = None

    if normalized_type == "ELSET":
        if not normalized_value:
            errors.append("selector_value is required")
        elif normalized_value not in elsets:
            errors.append(f"selected ELSET was not found: {normalized_value}")
        else:
            selected_ids = sorted(set(elsets[normalized_value]))
            selection_is_explicit = True
            selection_source = "ELSET"
            if not selected_ids:
                errors.append(f"selected ELSET is empty: {normalized_value}")
            unknown_ids = sorted(set(selected_ids) - set(elements))
            if unknown_ids:
                errors.append(
                    "ELSET references unknown elements: "
                    + ", ".join(str(value) for value in unknown_ids)
                )
    elif normalized_type in {WHOLE_FILE_TECHNICAL_DOMAIN, WHOLE_FILE_PRODUCT_DOMAIN}:
        if selector_explicit_declaration is not True:
            errors.append("whole-file technical domain requires explicit declaration")
        elif normalized_value != WHOLE_FILE_SELECTOR_VALUE:
            errors.append(
                "whole-file technical domain selector_value must be "
                + WHOLE_FILE_SELECTOR_VALUE
            )
        elif selector_purpose not in {
            WHOLE_FILE_TECHNICAL_PURPOSE
            if normalized_type == WHOLE_FILE_TECHNICAL_DOMAIN
            else WHOLE_FILE_PRODUCT_PURPOSE
        }:
            errors.append(
                "whole-file technical domain requires owner-confirmed fill purpose"
            )
        else:
            selected_ids = sorted(elements)
            selection_is_explicit = True
            selection_source = (
                "FIXTURE_RECORD_WHOLE_FILE_DECLARATION"
                if normalized_type == WHOLE_FILE_TECHNICAL_DOMAIN
                else "PRODUCT_WHOLE_FILE_DECLARATION"
            )
            if not selected_ids:
                errors.append("whole-file technical domain contains no elements")
    else:
        errors.append(
            "selector_type must be ELSET, "
            + WHOLE_FILE_TECHNICAL_DOMAIN
            + ", or "
            + WHOLE_FILE_PRODUCT_DOMAIN
        )

    digest_input = ",".join(str(value) for value in selected_ids).encode("ascii")
    selected_ids_sha256 = hashlib.sha256(digest_input).hexdigest().upper()
    _check_expected(
        "selected element count",
        len(selected_ids),
        expected_selected_element_count,
        errors,
    )
    if (
        expected_selected_element_ids_sha256 is not None
        and selected_ids_sha256
        != str(expected_selected_element_ids_sha256).upper()
    ):
        errors.append("selected element IDs SHA256 does not match fixture record")
    return (
        {
            "selector_type": normalized_type or None,
            "selector_value": normalized_value or None,
            "selected_element_count": len(selected_ids),
            "selected_element_ids_sha256": selected_ids_sha256,
            "selection_is_explicit": selection_is_explicit,
            "selection_source": selection_source,
            "fallback_to_all_elements": False,
        },
        selected_ids,
    )


def _evaluate_geometry_quality(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, dict[str, Any]],
    selected_element_ids: list[int],
    *,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    base = {
        "contract": GEOMETRY_CONTRACT,
        "volume_rule": VOLUME_RULE,
        "jacobian_condition_metric": "SPECTRAL_2_NORM",
        "selected_element_count": len(selected_element_ids),
        "valid_element_count": 0,
        "total_volume": None,
        "average_element_volume": None,
        "reference_length": None,
        "minimum_jacobian_determinant": None,
        "minimum_scaled_jacobian": None,
        "maximum_jacobian_condition_2": None,
        "non_positive_jacobian_element_ids": [],
    }
    if not selected_element_ids:
        return {"status": "NOT_EVALUATED", **base}

    metrics: list[dict[str, Any]] = []
    unavailable_ids: list[int] = []
    for element_id in selected_element_ids:
        element = elements.get(element_id)
        if (
            not element
            or element.get("type") not in SUPPORTED_HEX_TYPES
            or len(element.get("node_ids", ())) != 8
            or any(node_id not in nodes for node_id in element.get("node_ids", ()))
        ):
            unavailable_ids.append(element_id)
            continue
        coordinates = [nodes[node_id] for node_id in element["node_ids"]]
        metrics.append({"element_id": element_id, **_hex8_metrics(coordinates)})

    if unavailable_ids:
        errors.append(
            "geometry quality unavailable for selected elements: "
            + ", ".join(str(value) for value in unavailable_ids)
        )
    if not metrics:
        return {"status": "NOT_EVALUATED", **base}

    non_positive_ids = [
        item["element_id"] for item in metrics if item["minimum_det_jacobian"] <= 0
    ]
    finite_positive = [
        item
        for item in metrics
        if item["minimum_det_jacobian"] > 0
        and math.isfinite(item["volume"])
        and item["volume"] > 0
    ]
    minimum_det = min(item["minimum_det_jacobian"] for item in metrics)
    minimum_scaled = min(item["minimum_scaled_jacobian"] for item in metrics)
    maximum_condition = max(item["maximum_condition_2"] for item in metrics)

    status = "PASS"
    if non_positive_ids or minimum_scaled <= 0 or not math.isfinite(maximum_condition):
        status = "FAIL"
    elif maximum_condition > MAX_JACOBIAN_CONDITION_HOLD:
        status = "FAIL"
    elif (
        minimum_scaled < MIN_SCALED_JACOBIAN_PASS
        or maximum_condition > MAX_JACOBIAN_CONDITION_PASS
    ):
        status = "HOLD"

    if non_positive_ids:
        errors.append(
            "geometry quality FAIL: non-positive Jacobian in selected elements: "
            + ", ".join(str(value) for value in non_positive_ids)
        )
    elif status == "FAIL":
        errors.append("geometry quality FAIL: Jacobian condition exceeds contract")
    elif status == "HOLD":
        warnings.append("geometry quality HOLD: project tolerance review required")

    total_volume = sum(item["volume"] for item in finite_positive)
    average_volume = (
        total_volume / len(finite_positive) if finite_positive else None
    )
    reference_length = (
        average_volume ** (1.0 / 3.0)
        if status == "PASS" and average_volume is not None
        else None
    )
    return {
        "status": status,
        **base,
        "valid_element_count": len(finite_positive),
        "total_volume": total_volume if finite_positive else None,
        "average_element_volume": average_volume,
        "reference_length": reference_length,
        "minimum_jacobian_determinant": minimum_det,
        "minimum_scaled_jacobian": minimum_scaled,
        "maximum_jacobian_condition_2": maximum_condition,
        "non_positive_jacobian_element_ids": non_positive_ids,
    }


def _hex8_metrics(
    coordinates: list[tuple[float, float, float]],
) -> dict[str, float]:
    local_signs = (
        (-1.0, -1.0, -1.0),
        (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0),
        (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0),
        (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0),
    )
    gauss = 1.0 / math.sqrt(3.0)
    determinants: list[float] = []
    scaled_jacobians: list[float] = []
    conditions: list[float] = []

    for xi in (-gauss, gauss):
        for eta in (-gauss, gauss):
            for zeta in (-gauss, gauss):
                jacobian = [[0.0, 0.0, 0.0] for _ in range(3)]
                for coordinate, (sx, sy, sz) in zip(coordinates, local_signs):
                    derivatives = (
                        sx * (1.0 + sy * eta) * (1.0 + sz * zeta) / 8.0,
                        sy * (1.0 + sx * xi) * (1.0 + sz * zeta) / 8.0,
                        sz * (1.0 + sx * xi) * (1.0 + sy * eta) / 8.0,
                    )
                    for physical_axis in range(3):
                        for natural_axis in range(3):
                            jacobian[physical_axis][natural_axis] += (
                                coordinate[physical_axis] * derivatives[natural_axis]
                            )

                determinant = _determinant_3x3(jacobian)
                determinants.append(determinant)
                column_norms = [
                    math.sqrt(sum(jacobian[row][column] ** 2 for row in range(3)))
                    for column in range(3)
                ]
                denominator = math.prod(column_norms)
                scaled_jacobians.append(
                    determinant / denominator if denominator > 0 else float("-inf")
                )
                conditions.append(_condition_number_2(jacobian))

    return {
        "volume": sum(determinants),
        "minimum_det_jacobian": min(determinants),
        "minimum_scaled_jacobian": min(scaled_jacobians),
        "maximum_condition_2": max(conditions),
    }


def _determinant_3x3(matrix: list[list[float]]) -> float:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _condition_number_2(matrix: list[list[float]]) -> float:
    gram = [
        [sum(matrix[k][row] * matrix[k][column] for k in range(3)) for column in range(3)]
        for row in range(3)
    ]
    eigenvalues = _symmetric_eigenvalues_3x3(gram)
    minimum = max(min(eigenvalues), 0.0)
    maximum = max(eigenvalues)
    if minimum <= 1e-30 or not math.isfinite(maximum):
        return float("inf")
    return math.sqrt(maximum / minimum)


def _symmetric_eigenvalues_3x3(matrix: list[list[float]]) -> tuple[float, float, float]:
    off_diagonal_square = (
        matrix[0][1] ** 2 + matrix[0][2] ** 2 + matrix[1][2] ** 2
    )
    if off_diagonal_square <= 1e-30:
        return tuple(sorted((matrix[0][0], matrix[1][1], matrix[2][2])))

    mean = (matrix[0][0] + matrix[1][1] + matrix[2][2]) / 3.0
    spread_square = (
        (matrix[0][0] - mean) ** 2
        + (matrix[1][1] - mean) ** 2
        + (matrix[2][2] - mean) ** 2
        + 2.0 * off_diagonal_square
    )
    scale = math.sqrt(spread_square / 6.0)
    normalized = [
        [
            (matrix[row][column] - (mean if row == column else 0.0)) / scale
            for column in range(3)
        ]
        for row in range(3)
    ]
    half_determinant = _determinant_3x3(normalized) / 2.0
    angle = math.acos(max(-1.0, min(1.0, half_determinant))) / 3.0
    largest = mean + 2.0 * scale * math.cos(angle)
    smallest = mean + 2.0 * scale * math.cos(angle + 2.0 * math.pi / 3.0)
    middle = 3.0 * mean - largest - smallest
    return tuple(sorted((smallest, middle, largest)))


def _validate_fixture_record(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["technical fixture record must be an object"]

    errors: list[str] = []
    if record.get("schema_version") != FIXTURE_RECORD_SCHEMA:
        errors.append("unsupported technical fixture record schema")
    if record.get("classification") != "TEMPORARY_TECHNICAL_E2E_FIXTURE":
        errors.append("technical fixture classification is invalid")
    if record.get("formal_gate_effect") != "NONE":
        errors.append("technical fixture must not affect a formal Gate")
    non_claims = record.get("non_claims")
    if not isinstance(non_claims, list) or any(
        item not in non_claims for item in REQUIRED_NON_CLAIMS
    ):
        errors.append("technical fixture record is missing required non-claims")
    return errors


def _check_expected(
    label: str,
    actual: Any,
    expected: Any,
    errors: list[str],
) -> None:
    if expected is not None and actual != expected:
        errors.append(f"{label} does not match fixture record")


def _failed_file_result(path: Path, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "source": {"path": str(path)},
        "mesh": {},
        "design_domain": {},
        "geometry_quality": {"status": "NOT_EVALUATED"},
        "errors": [message],
        "warnings": [],
        "non_claims": list(REQUIRED_NON_CLAIMS),
        "runs_runtime": False,
        "writes_files": False,
    }
