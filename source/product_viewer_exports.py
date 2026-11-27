"""Scientific downloads built from verified Job evidence, independently of Viewer layout."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from functools import lru_cache

from PIL import Image
from product_workflow.ui_previews import render_standard_candidate_png, STL_PREVIEW_MAX_BYTES, STL_PREVIEW_MAX_TRIANGLES

from product_viewer_display import KNOWN_CLAIMS, identifier, filename as display_filename
from product_workflow.artifacts import (
    PRODUCT_FILE_MAP, ProductFileIdentityError, _expected_file_identity, resolve_product_package_file,
)
from product_workflow.config import require_within
from product_workflow.contracts import STIFFNESS_NAMES, candidate_ranking_view, canonical_sha256
from product_workflow.store import JOB_ID_PATTERN, _parse_viewer_json_object


EXPORTS = {
    "metadata": ("design-summary.json", "application/json"),
    "run_summary": ("design-summary.txt", "text/plain"),
    "package_metadata": ("package-summary.json", "application/json"),
    "geometry_ledger": ("geometry-diagnostics.json", "application/json"),
    "source_element_map": ("source-element-map.csv", "text/csv"),
}
RAW_ROLES = ("source_mesh", "b31", "stl", "png")
SUMMARY_ROLES = ("metadata", "package_metadata", "run_summary")
NON_CLAIMS = KNOWN_CLAIMS
KNOWN_WARNINGS = (
    "minimal runtime only", "default diameter used", "explicit diameter used",
    "not real bone defect validation", "not viewer page integration", "not 150w candidate adapter",
    "service runtime validation", "not dual-space fusion completion", "not spatially-varying generative lattice design",
    "candidate traceability carried from adapter metadata", "readonly display only",
    "per-beam source-element lineage is not produced by the current core",
    "interface node set was not provided and was not inferred",
)
WARNING_EXPLANATIONS = {
    "default diameter used": "The model uses the default diameter.",
    "explicit diameter used": "The model uses the specified diameter.",
    "interface node set was not provided and was not inferred": "No interface node set was supplied or inferred.",
    "per-beam source-element lineage is not produced by the current core": "Individual beams are not mapped back to their source elements.",
    "not real bone defect validation": "This result has no validation against a real bone defect.",
    "not spatially-varying generative lattice design": "This result does not establish spatially varying lattice design.",
    "not dual-space fusion completion": "Full dual-space fusion has not been validated.",
    "readonly display only": "The Viewer provides read-only access to the recorded result.",
}
FIXED_BOUNDARIES = ("NOT_MECHANICAL_PASS", "NOT_OOD_PASS", "NOT_FEA_VALIDATION",
                    "NOT_MANUFACTURING_VALIDATION", "NOT_CLINICAL_VALIDATION", "NOT_FULL_DUAL_SPACE_FUSION")
HEX_TYPES = {"C3D8", "C3D8R", "C3D8+C3D8R", "C3D8_GEOMETRY_COMPATIBLE"}
SOURCE_MAP_COLUMNS = ["source_element_id", "selected", "selected_index", "element_type_original",
                      "element_type_effective", "node_ids"]


def _known(values, allowed):
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v in allowed)) if isinstance(values, list) else []


def safe_warnings(metadata, package_metadata=None):
    """Only known limitations and warnings can cross the user-facing boundary."""
    acceptance = (package_metadata or {}).get("acceptance") or {}
    warnings = _known(metadata.get("warnings"), WARNING_EXPLANATIONS)
    warnings.extend(_known(acceptance.get("warnings"), WARNING_EXPLANATIONS))
    unknown = sum(1 for values in (metadata.get("warnings"), acceptance.get("warnings")) if isinstance(values, list)
                  for item in values if not isinstance(item, str) or item not in KNOWN_WARNINGS)
    if unknown:
        warnings.append(f"Additional recorded warnings: {unknown}. Review the result with the project owner.")
    warnings.extend(["not dual-space fusion completion", "not real bone defect validation",
                     "not spatially-varying generative lattice design", "readonly display only"])
    return list(dict.fromkeys(warnings))


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid scientific number")
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid scientific count")
    return value


def _vector(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("invalid 3D vector")
    return [_number(item) for item in value]


def _ids(value):
    if not isinstance(value, list) or any(type(v) is not int or v <= 0 for v in value):
        raise ValueError("invalid element IDs")
    return list(value)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9A-Fa-f]{64}", value):
        raise ValueError("invalid evidence digest")
    return value.upper()


def _enum(*options):
    def validate(value):
        if not isinstance(value, str) or value not in options:
            raise ValueError("invalid scientific classification")
        return value
    return validate


def _fields(data, schema):
    if not isinstance(data, dict):
        raise ValueError("invalid scientific record")
    result = {}
    for key, kind in schema.items():
        if key not in data:
            continue
        value = data[key]
        result[key] = None if value is None else _fields(value, kind) if isinstance(kind, dict) else kind(value)
    return result


BBOX = {"min": _vector, "max": _vector, "size": _vector, "L_char": _number, "max_dim": _number}
QUALITY = {
    "status": _enum("PASS", "HOLD", "FAIL", "NOT_EVALUATED"), "contract": _enum("PROJECT_TOLERANCE_V1"),
    "volume_rule": _enum("2x2x2_GAUSS_POSITIVE_DETJ"), "jacobian_condition_metric": _enum("SPECTRAL_2_NORM"),
    **dict.fromkeys(("selected_element_count", "valid_element_count", "design_domain_component_count"), _count),
    **dict.fromkeys(("total_volume", "average_element_volume", "reference_length", "minimum_jacobian_determinant",
                     "minimum_scaled_jacobian", "maximum_jacobian_condition_2"), _number),
    "non_positive_jacobian_element_ids": _ids,
}
LEDGER_SCHEMA = {
    "schema_version": _enum("latticegpt_9x_geometry_ledger_v1"), "contract": _enum("PROJECT_TOLERANCE_V1"),
    "evidence_level": _enum("TECHNICAL_FIXTURE", "FORMAL_ENGINEERING_CASE", "BOUNDED_REAL_CASE"),
    "technical_disposition": _enum("PASS", "FAIL", "HOLD", "NOT_EVALUATED"),
    "formal_gate_disposition": _enum("NOT_EVALUATED", "NOT_CLAIMED", "PASS_GATE_A4B_GATE_A5_NOT_EVALUATED",
                                    "PASS_GATE_B4B_GATE_B5_NOT_EVALUATED"),
    "source_geometry": QUALITY,
    "output_geometry": {"element_type": _enum("B31"), "node_count": _count, "beam_count": _count, "bbox": BBOX,
                        "beam_length": dict.fromkeys(("minimum","maximum","mean","zero_length_tolerance"), _number),
                        "connected_component_count": _count},
    "required_zero_counts": dict.fromkeys(("invalid_references", "zero_length_beams", "duplicate_undirected_beams", "isolated_nodes"), _count),
    "component_rule": {"contract": _enum("output_components_equal_source_design_domain_components"), "source_count": _count,
                       "output_count": _count, "status": _enum("PASS","FAIL","HOLD")},
    "stable_sets": {"NSET_IMPLANT_ALL": _count, "ELSET_IMPLANT_B31_ALL": _count,
                    "NSET_IMPLANT_INTERFACE": _enum("NOT_PROVIDED_NOT_INFERRED")},
    "source_element_map": {"schema_version": _enum("latticegpt_9x_source_element_map_v1"),
                           "mapping_scope": _enum("ORIGINAL_TO_SELECTED_DESIGN_DOMAIN_IDENTITY"),
                           "beam_origin_mapping_status": _enum("NOT_RECORDED_BY_CURRENT_CORE"), "sha256": _digest},
    "non_claims": lambda values: _known(values, NON_CLAIMS),
}


class PublicExportUnavailable(ValueError):
    """Verified source exists, but a typed public representation is unavailable."""


def project_geometry_ledger(ledger):
    try:
        result = _fields(ledger, LEDGER_SCHEMA)
        result["source_schema_version"] = result.pop("schema_version")
        result["schema_version"] = "bonemetagpt_geometry_diagnostics_v1"
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise PublicExportUnavailable("invalid public geometry diagnostic") from exc
    return result


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _identity(filename, data):
    return {"filename": filename, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest().upper()}


def verify_delivery_bytes(package, role, sha256, size):
    """Match the bytes actually captured, after role/path resolution, to its bound record."""
    expected = _expected_file_identity(package, role)
    if expected is None or _digest(expected.get("sha256")) != sha256 or expected.get("size_bytes", size) != size:
        raise ProductFileIdentityError("download bytes differ from verified artifact")


def _read_role(package, role):
    raw = resolve_product_package_file(package, role).read_bytes()
    if role == "package_metadata":
        if canonical_sha256(_parse_viewer_json_object(raw)) != canonical_sha256(package.package_metadata):
            raise ProductFileIdentityError("P13 manifest changed during export")
    else:
        verify_delivery_bytes(package, role, hashlib.sha256(raw).hexdigest().upper(), len(raw))
    return raw


def _validated_csv(data, selected_count=None):
    # Validate the literal CSV syntax as well as values: no extra columns, quoted
    # formulas, control characters, paths, or unused node data can be returned.
    positive = re.compile(r"[1-9][0-9]*")
    try:
        reader = csv.reader(io.StringIO(data.decode("ascii"), newline=""), strict=True)
        if next(reader, None) != SOURCE_MAP_COLUMNS:
            raise ValueError("invalid source map header")
        source_ids, indices = set(), set()
        for row in reader:
            if len(row) != len(SOURCE_MAP_COLUMNS):
                raise ValueError("invalid source map column count")
            element, selected, index, original, effective, nodes = row
            if not positive.fullmatch(element) or element in source_ids or selected not in {"true", "false"}:
                raise ValueError("invalid source element identity")
            source_ids.add(element)
            if original not in {"C3D8","C3D8R"} or effective not in {"C3D8", "C3D8_GEOMETRY_COMPATIBLE"}:
                raise ValueError("invalid source map element type")
            node_ids = nodes.split(" ")
            if len(node_ids) != 8 or len(set(node_ids)) != 8 or not all(positive.fullmatch(n) for n in node_ids):
                raise ValueError("invalid HEX8 connectivity")
            if selected == "true":
                if not positive.fullmatch(index) or int(index) in indices:
                    raise ValueError("invalid design domain index")
                indices.add(int(index))
            elif index:
                raise ValueError("excluded source element has design domain index")
        if not source_ids or indices != set(range(1, len(indices)+1)) or (selected_count is not None and len(indices) != selected_count):
            raise ValueError("inconsistent design domain indices")
    except (csv.Error, UnicodeError) as exc:
        raise ValueError("invalid source map encoding or syntax") from exc
    return data


def _diagnostic(package, role, ledger):
    if role == "geometry_ledger":
        projected = project_geometry_ledger(ledger)
        return _json_bytes(projected), projected
    domain = package.package_metadata.get("design_domain") or {}
    raw = _read_role(package, "source_element_map")
    try:
        mapping = _validated_csv(raw, domain.get("selected_element_count"))
    except (ValueError, TypeError) as exc:
        raise PublicExportUnavailable("invalid public source map") from exc
    mapping_record = ledger.get("source_element_map")
    expected = mapping_record.get("sha256") if isinstance(mapping_record, dict) else None
    if expected is not None and _identity("", mapping)["sha256"] != expected:
        raise ProductFileIdentityError("source map differs from geometry ledger")
    return mapping, None


def _delivered_files(package, diagnostics):
    result = {}
    for role in RAW_ROLES:
        # Reuse sealed size/digest pairs for receipt-backed six-file packages.
        record = _expected_file_identity(package, role)
        filename = PRODUCT_FILE_MAP[role]
        if not record or record.get("path") != filename.as_posix():
            raise ValueError("raw file identity missing")
        path = require_within(package.package_dir / filename, package.package_dir, label="product file")
        expected_sha = _digest(record["sha256"])
        if "size_bytes" in record:
            size = record["size_bytes"]
            if path.stat().st_size != size:
                raise ProductFileIdentityError("raw file size changed")
        else:
            # P13 does not seal byte lengths. Pair one measured length with its
            # digest in the same streaming read, including under file replacement.
            size = 0
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            verify_delivery_bytes(package, role, digest.hexdigest().upper(), size)
        result[role] = {"filename": filename.name, "size_bytes": size, "sha256": expected_sha}
    for role, data in diagnostics.items():
        result[role] = _identity(EXPORTS[role][0], data)
    return result


def _check_job(package, job):
    if (not JOB_ID_PATTERN.fullmatch(package.job_id) or package.job_id != job.get("job_id")
            or package.package_id != package.job_id + "-attempt-0001" or package.attempt_id != package.package_id):
        raise ValueError("export Job identity mismatch")


def _bounds(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("invalid design domain bounds")
    return [_vector(v) for v in value]


def _summarize(package, job, files, ledger):
    meta, p13 = package.metadata, package.package_metadata or {}
    mesh, binding = package.mesh_view or {}, job.get("mesh_binding") or {}
    target = job.get("target") or {}
    stiffness = dict.fromkeys(STIFFNESS_NAMES, _number)
    target_values = _fields(target, {"physical_9d_gpa": stiffness, "normalized_9d": stiffness, "proposal_sha256": _digest})
    target_values.update(stiffness_unit="GPa", normalized_unit="dimensionless")
    source = target.get("source") or {}
    target_values["source"] = _fields(source, {"record_index": _count, "record_count": _count})
    source_kind = source.get("kind")
    if source_kind in ("formal_150w_database", "csv_upload", "csv_record", "manual_input", "manual"):
        target_values["source"]["kind"] = source_kind
    source_digest = source.get("source_sha256") or source.get("sha256")
    if source_digest is not None:
        target_values["source"]["sha256"] = _digest(source_digest)
    target_values["base_material"] = _fields(target.get("base_material") or {}, {
        "name": _enum("TC4"), "elastic_modulus_gpa": _number})
    collection = job.get("candidate_set") or {}
    metric = candidate_ranking_view(collection)["metric"]
    selected = identifier((job.get("selection") or {}).get("selected_candidate_id"))
    candidates = []
    for row in collection.get("candidates") or []:
        candidate = _fields(row, {"rank": _count, metric: _number, "relative_density": _number,
                                  "normalized_diameter": _number, "candidate_id": identifier,
                                  "eligible": lambda v: v if type(v) is bool else None})
        candidate["predicted_9d_gpa"] = _fields(row.get("predicted_stiffness") or {}, stiffness)
        candidates.append(candidate)
    geometry = _fields(meta, {"b31_node_count": _count, "b31_beam_count": _count, "node_count": _count, "beam_count": _count,
                              "source_node_count": _count, "source_element_count": _count,
                              "source_element_type_original": _enum(*HEX_TYPES), "source_element_type_effective": _enum(*HEX_TYPES),
                              "diameter": _number, "bbox": BBOX})
    for key, mesh_key in (("source_node_count", "node_count"), ("source_element_count", "element_count")):
        if geometry.get(key) is None and mesh.get(mesh_key) is not None:
            geometry[key] = _count(mesh[mesh_key])
    trace, scale = meta.get("traceability") or {}, p13.get("diameter") or {}
    # A scale-unit is evidence for the reported diameter only when its linked value matches.
    unit = meta.get("diameter_unit")
    if unit is None and geometry.get("diameter") is not None:
        for value, units in ((trace.get("physical_diameter"), trace.get("physical_diameter_units")),
                             (scale.get("physical_value"), scale.get("physical_units"))):
            if type(value) in (float, int) and value == geometry["diameter"] and units == "mm":
                unit = units
    geometry["diameter_unit"] = unit if unit in ("mm","m","µm") else None
    units = mesh.get("units") or (p13.get("source_mesh") or {}).get("mesh_units")
    geometry["mesh_length_unit"] = units if units in ("mm","m","µm") else None
    geometry["scale"] = _fields(trace, dict.fromkeys(("normalized_diameter","physical_diameter","reference_length"), _number))
    geometry["p13_diameter"] = _fields(scale, {"normalized_value": _number, "reference_length": _number, "physical_value": _number,
                                               "physical_units": _enum("mm"), "unique_physical_diameter_count": _count,
                                               "assignment_scope": _enum("global_uniform")})
    if meta.get("cell_type") in ("BCC","FCC","Octet","octet","custom","CUSTOM","P10_CUSTOM_CELL","custom_cell"):
        geometry["cell_type"] = meta["cell_type"]
    domain = mesh.get("design_domain") or binding.get("design_domain") or p13.get("design_domain") or {}
    geometry["design_domain"] = _fields(domain, {
        **dict.fromkeys(("selected_element_count", "selected_node_count", "excluded_element_count", "excluded_solid_count"), _count),
        "selected_element_ids_sha256": _digest, "selected_types": dict.fromkeys(("C3D8","C3D8R"), _count),
        "selected_bounds_mm": _bounds, "original_bounds_mm": _bounds,
    })
    geometry["mesh_filename"] = display_filename(mesh.get("original_filename"))
    geometry["reference_length_mm"] = _fields(mesh, {"reference_length_mm": _number}).get("reference_length_mm")
    geometry["mesh_binding_sha256"] = _fields(mesh, {"binding_sha256": _digest}).get("binding_sha256")
    geometry["element_type"] = _fields(meta, {"element_type": _enum("B31")}).get("element_type")
    quality = (binding.get("precheck") or {}).get("geometry_quality") or {}
    for upload in job.get("mesh_uploads") or []:
        if mesh.get("upload_id") and upload.get("upload_id") == mesh["upload_id"]:
            quality = (upload.get("domain_precheck") or upload.get("precheck") or {}).get("geometry_quality") or quality
    geometry["quality"] = ledger.get("source_geometry") or _fields(quality, QUALITY)
    geometry["required_zero_counts"] = ledger.get("required_zero_counts")
    limitations = list(FIXED_BOUNDARIES)
    limitations.extend(_known(collection.get("non_claims"), NON_CLAIMS))
    limitations.extend(ledger.get("non_claims") or [])
    if binding.get("schema_version") == "latticegpt_mesh_binding_v3":
        limitations.extend(("NOT_FULL_BONE_COVERAGE", "NOT_EXCLUDED_ELEMENT_MAPPING", "NOT_SHELL_MECHANICAL_VALIDATION"))
    return {
        "schema_version": "bonemetagpt_design_summary_v1", "job_id": package.job_id,
        "attempt_id": package.attempt_id, "package_id": package.package_id, "revision": _fields(job, {"revision": _count}).get("revision"),
        "target": target_values, "ranking_metric": metric, "selected_candidate_id": selected,
        "recommended_candidate_id": identifier(collection.get("recommended_candidate_id")), "candidates": candidates,
        "selection": _fields(job.get("selection") or {}, {"selected_rank": _count, "selection_sha256": _digest,
                                                       "selected_candidate_payload_sha256": _digest, "candidate_set_sha256": _digest}),
        "candidate_set_sha256": _fields(collection, {"candidate_set_sha256": _digest}).get("candidate_set_sha256"),
        "geometry": geometry, "p13_status": "verified" if package.package_metadata is not None else "not_evaluated",
        "missing_value_convention": "Absent and null values mean no publishable measurement was recorded.",
        "warnings": [WARNING_EXPLANATIONS.get(warning, warning) for warning in safe_warnings(meta, package.package_metadata)], "non_claims": list(dict.fromkeys(limitations)), "files": files,
    }


def _summary_bytes(summary, role):
    if role == "run_summary":
        lines = ["BoneMetGPT design summary", ""]
        for key, value in summary.items():
            lines.append(key + " = " + json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return ("\n".join(lines) + "\n").encode("utf-8")
    return _json_bytes(summary)


def _components(package, job):
    _check_job(package, job)
    if canonical_sha256(_parse_viewer_json_object(_read_role(package, "metadata"))) != canonical_sha256(package.metadata):
        raise ProductFileIdentityError("metadata changed during export")
    ledger, diagnostics, unavailable = {}, {}, []
    if package.package_metadata is not None:
        _read_role(package, "package_metadata")
        raw_ledger = _parse_viewer_json_object(_read_role(package, "geometry_ledger"))
        for role in ("geometry_ledger", "source_element_map"):
            try:
                data, geometry = _diagnostic(package, role, raw_ledger)
                diagnostics[role] = data
                if geometry is not None:
                    ledger = geometry
            except PublicExportUnavailable:
                unavailable.append(role)
    return ledger, _delivered_files(package, diagnostics), unavailable


def _summary(package, job, files, ledger, unavailable):
    try:
        summary = _summarize(package, job, files, ledger)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise PublicExportUnavailable("invalid public design summary") from exc
    summary["unavailable_diagnostics"] = unavailable
    return summary


def build_user_export(package, job, role):
    if role not in EXPORTS:
        raise ValueError("unsupported export role")
    _check_job(package, job)
    if role in SUMMARY_ROLES:
        if role == "package_metadata" and package.package_metadata is None:
            raise FileNotFoundError("package summary unavailable")
        if role == "run_summary":
            _read_role(package, role)
        ledger, files, unavailable = _components(package, job)
        data = _summary_bytes(_summary(package, job, files, ledger, unavailable), role)
    else:
        if package.package_metadata is None:
            raise FileNotFoundError("diagnostic role unavailable")
        _read_role(package, "package_metadata")
        ledger = _parse_viewer_json_object(_read_role(package, "geometry_ledger"))
        data, _ = _diagnostic(package, role, ledger)
    return EXPORTS[role][0], EXPORTS[role][1], data


def build_export_identities(package, job):
    """Only presentation failures are local to a role. Resolver and integrity errors propagate."""
    ledger, files, unavailable = _components(package, job)
    result = dict(files)
    try:
        summary = _summary(package, job, files, ledger, unavailable)
    except PublicExportUnavailable:
        return result
    for role in SUMMARY_ROLES:
        if role == "package_metadata" and package.package_metadata is None:
            continue
        result[role] = _identity(EXPORTS[role][0], _summary_bytes(summary, role))
    return result

FILES = {
    "unit_png": ("selected-unit-cell.png", "image/png"),
    "unit_stl": ("selected-unit-cell.stl", "model/stl"),
    "conformal_inp": ("conformal-lattice-b31.inp", "text/plain"),
}
SCENES = {"triangles": "stl", "poster": "png"}


class DeliveryUnavailable(ValueError):
    """The exact file role cannot be safely represented."""


def sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def snapshot(job, package, role):
    candidate = job["selection"]["selected_candidate_id"]
    return canonical_sha256([job["job_id"], job["revision"], package.package_id, candidate,
                             job["selection"]["selection_sha256"], job["candidate_set"].get("candidate_set_sha256"), role])


def endpoint(job, package, group, role):
    return f"/api/viewer/product/jobs/{job['job_id']}/{group}/{role}?snapshot={snapshot(job, package, role)}"


def original(package, role):
    data = resolve_product_package_file(package, role).read_bytes()
    verify_delivery_bytes(package, role, sha(data), len(data))
    return data


def clean_image(data, fmt):
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG":
            raise DeliveryUnavailable("Expected a PNG")
        pixels = image.convert("RGBA")
        pixels.info.clear()
        stream = io.BytesIO()
        pixels.save(stream, format=fmt, **({"lossless": True} if fmt == "WEBP" else {}))
        return stream.getvalue()


def checked_geometry(job):
    collection = job["candidate_set"]
    identity = collection.get("candidate_set_sha256")
    if not identity:
        raise DeliveryUnavailable("No saved unit-cell identity")
    if canonical_sha256({k:v for k,v in collection.items() if k != "candidate_set_sha256"}) != identity:
        raise ProductFileIdentityError("Candidate set changed")
    selected = job["selection"]["selected_candidate_id"]
    rows = [c for c in collection["candidates"] if c["candidate_id"] == selected]
    if len(rows) != 1 or not identifier(selected):
        raise ProductFileIdentityError("Selected candidate changed")
    from product_workflow.ui_previews import _validate_candidates
    from product_workflow.ui_previews import PreviewSourceError
    try:
        geom, = _validate_candidates(rows, expected_count=1)
    except PreviewSourceError as exc:
        raise DeliveryUnavailable("Incomplete saved unit-cell geometry") from exc
    return selected, rows[0].get("rank"), geom.nodes, geom.connections, geom.diameter


@lru_cache(maxsize=12)
def cell_bytes(role, candidate_id, rank, nodes, edges, diameter):
    if role == "unit_png":
        candidate = dict(candidate_id=candidate_id, rank=rank, nodes=list(nodes),
                         connections=list(edges), normalized_diameter=diameter)
        return clean_image(render_standard_candidate_png(candidate), "PNG")
    from cad_module import build_beam_mesh_from_graph
    mesh = build_beam_mesh_from_graph(nodes, edges, diameter)
    raw = mesh.export(file_type="stl")
    if len(mesh.faces) != 64 * len(edges) or len(raw) != 84 + 50 * len(mesh.faces):
        raise DeliveryUnavailable("Incomplete unit-cell surface")
    return bytes(80) + raw[80:]


def clean_inp(data):
    # The production writer emits nodes and B31 connectivity. Reject unrecognised
    # engineering sections wholesale instead of dropping materials, sets or sections.
    lines = []
    state = None
    nodes, elements = set(), set()
    for line in data.splitlines(keepends=True):
        value = line.strip()
        if not value or value.startswith(b"**"):
            continue
        if value.startswith(b"*"):
            keyword = re.sub(rb"\s+", b"", value).upper()
            if keyword == b"*HEADING":
                state = "heading"
                continue
            if keyword not in (b"*NODE", b"*ELEMENT,TYPE=B31"):
                raise DeliveryUnavailable("Unsupported engineering section")
            state = "nodes" if keyword == b"*NODE" else "elements"
        elif state == "heading":
            continue
        else:
            fields = [field.strip() for field in value.split(b",")]
            if state == "nodes" and len(fields) == 4 and fields[0].isdigit() and all(
                re.fullmatch(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", v) and math.isfinite(float(v)) for v in fields[1:]):
                i = int(fields[0])
                if i < 1 or i in nodes:
                    raise DeliveryUnavailable("Invalid node identity")
                nodes.add(i)
            elif state == "elements" and len(fields) == 3 and all(v.isdigit() for v in fields):
                i, a, b = map(int, fields)
                if i < 1 or i in elements or a == b or a not in nodes or b not in nodes:
                    raise DeliveryUnavailable("Invalid beam connectivity")
                elements.add(i)
            else:
                raise DeliveryUnavailable("Invalid engineering record")
        lines.append(line)
    if not elements:
        raise DeliveryUnavailable("No B31 elements")
    return b"".join(lines)


def delivery(job, package, role):
    if role == "conformal_inp":
        return clean_inp(original(package, "b31"))
    return cell_bytes(role, *checked_geometry(job))


def delivery_catalog(job, package):
    links, identities = {}, {}
    for role, (filename, _) in FILES.items():
        try:
            data = delivery(job, package, role)
        except DeliveryUnavailable:
            continue
        links[role] = endpoint(job, package, "deliver", role)
        identities[role] = dict(filename=filename, size_bytes=len(data), sha256=sha(data))
    return links, identities


def scene_bytes(package, role):
    if role == "poster":
        return clean_image(original(package, "png"), "WEBP"), "image/webp"
    path = resolve_product_package_file(package, "stl")
    if path.stat().st_size > STL_PREVIEW_MAX_BYTES:
        raise DeliveryUnavailable("Geometry exceeds preview budget")
    raw = original(package, "stl")
    import numpy as np
    count = int.from_bytes(raw[80:84], "little")
    if count and len(raw) == 84 + count * 50:
        if count > STL_PREVIEW_MAX_TRIANGLES:
            raise DeliveryUnavailable("Triangle budget exceeded")
        dtype = np.dtype([("normal","<f4",3), ("vertices","<f4",9), ("attr","<u2")])
        coords = np.frombuffer(raw, dtype=dtype, offset=84)["vertices"]
    else:
        import trimesh
        if not raw.lstrip().lower().startswith(b"solid") or len(re.findall(rb"\bfacet\s+normal\b", raw, re.I)) > STL_PREVIEW_MAX_TRIANGLES:
            raise DeliveryUnavailable("Unsupported triangle data")
        mesh = trimesh.load_mesh(io.BytesIO(raw), file_type="stl", process=False)
        coords = np.asarray(mesh.triangles, dtype="<f4")
    if not coords.size or not np.isfinite(coords).all():
        raise DeliveryUnavailable("Invalid scene vertices")
    return coords.tobytes(), "application/octet-stream"

