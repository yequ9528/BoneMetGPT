"""Closed, read-only field projections shared by the product Viewer paths."""
import math
import re
from datetime import datetime
from pathlib import PureWindowsPath

from latticegpt_9x_package_contract import (
    BOUNDED_CASE_NON_CLAIMS, FORMAL_CASE_NON_CLAIMS, REQUIRED_NON_CLAIMS,
)
from product_workflow.contracts import STIFFNESS_NAMES


DOMAIN_CLAIMS = ("SELECTED_DOMAIN_ONLY", "EXCLUDED_REGIONS_NOT_COMPUTED", "NOT_FULL_BONE_COVERAGE",
                 "NOT_MECHANICAL_VALIDATION", "NOT_EXCLUDED_ELEMENT_MAPPING", "NOT_SHELL_MECHANICAL_VALIDATION")
KNOWN_CLAIMS = frozenset((*DOMAIN_CLAIMS, *REQUIRED_NON_CLAIMS, *FORMAL_CASE_NON_CLAIMS,
    *BOUNDED_CASE_NON_CLAIMS, "NOT_OOD_PASS", "NOT_MANUFACTURING_VALIDATION", "NOT_CLINICAL_VALIDATION",
    "NOT_FULL_DUAL_SPACE_FUSION", "REPRODUCIBILITY_NOT_VERIFIED", "NOT_ORIGINAL_FULL_6X6"))
PHYSICAL_SOURCE = "latticegpt_9x_technical_case_preview:physical_diameter"
ELEMENT_TYPES = ("C3D8", "C3D8R", "C3D8_GEOMETRY_COMPATIBLE", "C3D8+C3D8R", "mixed", "B31",
                 "C3D4", "C3D6", "S4", "S4R", "S3", "S3R")
SELECTION_SOURCES = ("default_rank1", "recommended_candidate", "user_override")
GEOMETRY_CONTRACT = "truss_20261110_unit_cell_v1"


def mapping(value):
    return value if isinstance(value, dict) else {}


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def matching(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def digest(value):
    return matching(value, r"[A-Fa-f0-9]{64}")


def identifier(value):
    # Candidate identities are used in the exact preview route. Never stringify arbitrary objects.
    return matching(value, r"[A-Za-z0-9][A-Za-z0-9_-]{0,191}")


def filename(value):
    if not isinstance(value, str):
        return None
    name = PureWindowsPath(value).name
    if not name or name in (".", "..") or len(name) > 255 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name):
        return None
    return name


def timestamp(value):
    if not matching(value, r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)"):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def number(value):
    if type(value) in (int, float):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return None


def count(value):
    return value if type(value) is int and value >= 0 else None


def project(value, fields):
    """Each named field supplies its own scalar type, enum or nested schema."""
    record = mapping(value)
    out = {}
    for key, reader in fields.items():
        if key in record:
            safe = reader(record[key])
            if safe is not None:
                out[key] = safe
    return out


def enum(*values):
    return lambda value: choice(value, values)


def list_of(value, reader, *, length=None):
    if not isinstance(value, (list, tuple)) or (length is not None and len(value) != length):
        return None
    items = [reader(item) for item in value]
    return items if all(item is not None for item in items) else None


def codes(value, allowed):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if choice(item, allowed)))


def nine(value):
    return project(value, dict.fromkeys(STIFFNESS_NAMES, number))


def vec3(value):
    return list_of(value, number, length=3)


def bbox(value):
    return project(value, dict.fromkeys(("min", "max", "size"), vec3)) if isinstance(value, dict) else None


def node_ids(value):
    return list_of(value, count)


def edges(value):
    return list_of(value, lambda edge: list_of(edge, count, length=2))


def mesh_visualization(value):
    source = mapping(value)
    safe = project(source, {
        "representation": enum("original_inp_wireframe", "hex_wireframe"),
        "units": enum("mm"), "frame": enum("source_native_cartesian_frame"),
        "element_type": enum(*ELEMENT_TYPES), "element_count": count, "node_count": count,
        "bbox": bbox,
    })
    status = choice(source.get("status"), ("available", "too_large", "unavailable")) or "unavailable"
    safe["status"] = status
    if status != "available":
        safe["message"] = ("The input mesh exceeds the reference display limit." if status == "too_large"
                           else "The input mesh cannot be displayed completely.")
        return safe
    if "nodes" in source:
        safe["nodes"] = [project(item, {"id": count, "xyz": vec3}) for item in source["nodes"]]
        if any(len(row) != 2 for row in safe["nodes"]):
            raise ValueError("product mesh node is invalid")
    if "elements" in source:
        safe["elements"] = [project(item, {"id": count, "type": enum(*ELEMENT_TYPES), "node_ids": node_ids})
                            for item in source["elements"]]
        if any(len(row) != 3 for row in safe["elements"]):
            raise ValueError("product mesh element is invalid")
    if "edges" in source:
        safe["edges"] = edges(source["edges"])
        if safe["edges"] is None:
            raise ValueError("product mesh edge is invalid")
    if "groups" in source:
        safe["groups"] = project(source["groups"], dict.fromkeys(("selected", "excluded_solid", "excluded_other"),
            lambda group: project(group, {"element_ids": node_ids, "element_count": count, "edges": edges})))
    return safe


def design_domain(value):
    if not isinstance(value, dict):
        return None
    bounds = lambda points: list_of(points, vec3, length=2)
    # Excluded types are input element codes. Use the Abaqus family grammar, never arbitrary map keys.
    def by_type(items, reader):
        result = {}
        for key, item in mapping(items).items():
            if matching(key, r"(?:C3D|S|B|T3D|CPS|CPE|M3D|R3D|SC)\d{1,3}[A-Z]{0,3}"):
                safe = reader(item)
                if safe is not None:
                    result[key] = safe
        return result
    return project(value, {
        "schema_version": enum("latticegpt_selected_hex_domain_v1"), "selector": enum("ALL_EXPLICIT_C3D8_C3D8R"),
        "units": enum("mm"), "frame": enum("source_native_cartesian_frame"), "transform": enum("identity"),
        **dict.fromkeys(("original_sha256", "derived_sha256", "selected_element_ids_sha256", "selection_sha256"), digest),
        **dict.fromkeys(("original_size_bytes", "derived_size_bytes", "selected_element_count", "selected_node_count",
                         "excluded_element_count", "excluded_solid_count"), count),
        "selected_element_ids": node_ids, "original_bounds_mm": bounds, "selected_bounds_mm": bounds,
        "selected_types": lambda rows: by_type(rows, count),
        "excluded_types": lambda rows: by_type(rows, lambda row: project(row, {
            "count": count, "element_ids": node_ids, "bounds_mm": bounds, "category": enum("solid", "shell", "other")})),
        "non_claims": lambda claims: codes(claims, KNOWN_CLAIMS),
    })


def quality_summary(report):
    quality = mapping(report.get("geometry_quality"))
    result = project(quality, {
        "status": enum("PASS", "HOLD", "FAIL", "NOT_EVALUATED"),
        **dict.fromkeys(("minimum_jacobian_determinant", "minimum_scaled_jacobian", "maximum_jacobian_condition_2"), number),
        "valid_element_count": count,
    })
    known = {"geometry quality HOLD: project tolerance review required": "Mesh quality requires tolerance review.",
             "geometry quality FAIL: Jacobian condition exceeds contract": "A mesh Jacobian condition exceeds its limit."}
    for key in ("warnings", "errors"):
        messages = []
        for rows in (quality.get(key), report.get(key)):
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, str) and item in known:
                        messages.append(known[item])
        if messages:
            result[key] = list(dict.fromkeys(messages))
    return result


def compatibility(value):
    if not isinstance(value, dict):
        return None
    fields = {"generation": lambda v: matching(v, r"r4_lite_single_reference_workstation_v\d{1,6}")}
    return project(value, {"producer": lambda x: project(x, fields), "reader": lambda x: project(x, fields),
        "package_format": enum("PRODUCT_SIX_FILES_V1", "LEGACY_SIX_FILES_V1"), "p13_status": enum("NOT_EVALUATED")})


def p13_evidence(value, package):
    if package.package_metadata is None:
        return {"status": "not_available_legacy_package", "integrity_status": "NOT_EVALUATED"}
    from product_viewer_exports import PublicExportUnavailable, project_geometry_ledger
    contract = package.package_metadata
    claims = codes(mapping(contract.get("claims")).get("non_claims"), KNOWN_CLAIMS)
    acceptance = project(contract.get("acceptance"), {
        "technical_disposition": enum("PASS", "FAIL", "HOLD", "NOT_EVALUATED"),
        "formal_gate_disposition": enum("PASS_GATE_A4B_GATE_A5_NOT_EVALUATED", "PASS_GATE_B4B_GATE_B5_NOT_EVALUATED",
                                        "NOT_EVALUATED", "NOT_CLAIMED", "HOLD", "FAIL")})
    diameter = project(contract.get("diameter"), {
        **dict.fromkeys(("normalized_value", "reference_length", "physical_value"), number), "physical_units": enum("mm"),
        "unique_physical_diameter_count": count, "assignment_scope": enum("global_uniform"),
        "transform_contract": enum("P11_AVERAGE_EQUIVALENT_SCALE_V1")})
    try:
        ledger = project_geometry_ledger(mapping(value.get("geometry_ledger")))
    except PublicExportUnavailable:
        ledger = None
    return {
        "status": "verified", "integrity_status": "PASS", "package_metadata_sha256": digest(value.get("package_metadata_sha256")),
        "package_contract": {"schema_version": "latticegpt_9x_package_metadata_v1", "package": {"package_id": package.package_id},
                             "acceptance": acceptance,
                             "design_domain": project(contract.get("design_domain"), {"selected_element_count": count}),
                             "claims": {"non_claims": claims}},
        "geometry_ledger": ledger,
        "source_element_map": project(value.get("source_element_map"), {"data_row_count": count, "selected_element_count": count,
                                                                      "sha256": digest}),
        "diameter": diameter, "non_claims": claims,
        "verified_artifacts": project(value.get("verified_artifacts"), dict.fromkeys(("b31", "geometry_ledger", "source_element_map"), digest)),
    }

