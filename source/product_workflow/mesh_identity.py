"""R3 raw, ordered and canonical Abaqus mesh identities."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import PurePath
from typing import Any, Mapping, Sequence

from .manifest_io import canonical_json_object_v1


SHA_RE = re.compile(r"^[0-9A-F]{64}$")
UNSUPPORTED_KEYWORDS = frozenset({"*INCLUDE", "*PART", "*INSTANCE", "*ASSEMBLY"})


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def canonical_jsonl_rows_v1(rows: Sequence[Any]) -> bytes:
    output = bytearray()
    for row in rows:
        if isinstance(row, bool) or not isinstance(row, (list, int)):
            raise TypeError("JSONL identity row must be an array or integer")
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        output.extend(encoded)
        output.extend(b"\n")
    return bytes(output)


def _positive_int(token: str, label: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", token.strip()):
        raise ValueError(f"{label} must be a positive integer")
    return int(token)


def _finite_hex(token: str) -> str:
    value = float(token.strip())
    if not math.isfinite(value):
        raise ValueError("mesh coordinate must be finite")
    if value == 0.0:
        value = 0.0
    return value.hex()


def parse_mesh_identity_v1(raw_mesh_bytes: bytes, *, allow_mixed_hex: bool = False) -> dict[str, Any]:
    if not isinstance(raw_mesh_bytes, bytes) or not raw_mesh_bytes:
        raise ValueError("raw mesh bytes are required")
    try:
        text = raw_mesh_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("mesh must use strict UTF-8/ASCII") from exc
    nodes: list[list[Any]] = []
    elements: list[list[Any]] = []
    elset_declarations: list[list[Any]] = []
    node_ids: set[int] = set()
    element_ids: set[int] = set()
    mode: str | None = None
    element_type: str | None = None
    current_elset: tuple[str, list[int]] | None = None

    def finish_elset() -> None:
        nonlocal current_elset
        if current_elset is not None:
            elset_declarations.append([current_elset[0], list(current_elset[1])])
            current_elset = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            finish_elset()
            header = line.upper()
            tokens = [token.strip() for token in header.split(",")]
            keyword, options = tokens[0], tokens[1:]
            if (
                keyword in UNSUPPORTED_KEYWORDS
                or any(option == "GENERATE" or option.startswith("GENERATE=") for option in options)
                or any(option.startswith("INSTANCE=") for option in options)
            ):
                raise ValueError("unsupported Abaqus identity syntax")
            if header == "*NODE":
                mode = "NODE"
            elif header.startswith("*ELEMENT"):
                match = re.search(r"(?:^|,)\s*TYPE\s*=\s*([A-Z0-9]+)\s*(?:,|$)", header)
                if match is None or match.group(1) not in {"C3D8", "C3D8R"}:
                    raise ValueError("unsupported or missing element type")
                element_type = match.group(1)
                mode = "ELEMENT"
            elif header.startswith("*ELSET"):
                match = re.search(r"(?:^|,)\s*ELSET\s*=\s*([^,]+)", header)
                if match is None or not match.group(1).strip():
                    raise ValueError("ELSET name is required")
                current_elset = (match.group(1).strip().upper(), [])
                mode = "ELSET"
            else:
                mode = None
            continue
        fields = [item.strip() for item in line.split(",")]
        if mode == "NODE":
            if len(fields) != 4:
                raise ValueError("node row must contain ID and three coordinates")
            node_id = _positive_int(fields[0], "node_id")
            if node_id in node_ids:
                raise ValueError("duplicate node ID")
            node_ids.add(node_id)
            nodes.append([node_id, _finite_hex(fields[1]), _finite_hex(fields[2]), _finite_hex(fields[3])])
        elif mode == "ELEMENT":
            if len(fields) != 9:
                raise ValueError("C3D8/C3D8R element must be one explicit eight-node row")
            element_id = _positive_int(fields[0], "element_id")
            if element_id in element_ids:
                raise ValueError("duplicate element ID")
            element_ids.add(element_id)
            connectivity = [_positive_int(item, "connectivity node ID") for item in fields[1:]]
            elements.append([element_id, element_type, connectivity])
        elif mode == "ELSET" and current_elset is not None:
            if not fields or any(not item for item in fields):
                raise ValueError("ELSET members must be explicit integers")
            current_elset[1].extend(_positive_int(item, "ELSET member") for item in fields)
        else:
            raise ValueError("unbound Abaqus data row")
    finish_elset()

    if not nodes or not elements:
        raise ValueError("at least one node and one element are required")
    if len({row[1] for row in elements}) != 1 and not allow_mixed_hex:
        raise ValueError("mixed element types are unsupported")
    for _, _, connectivity in elements:
        if any(node_id not in node_ids for node_id in connectivity):
            raise ValueError("element references a missing node")

    canonical_nodes = sorted(nodes, key=lambda item: item[0])
    canonical_elements = sorted(elements, key=lambda item: item[0])
    grouped_elsets: dict[str, set[int]] = {}
    for name, members in elset_declarations:
        grouped_elsets.setdefault(name, set()).update(members)
    canonical_elsets = [[name, sorted(grouped_elsets[name])] for name in sorted(grouped_elsets)]
    original_type = elements[0][1] if len({row[1] for row in elements}) == 1 else "C3D8+C3D8R"
    semantics = {
        "element_type_original": original_type,
        "element_type_effective": "C3D8" if original_type == "C3D8" else "C3D8_GEOMETRY_COMPATIBLE",
        "node_count": len(nodes),
        "element_count": len(elements),
        "ordered_node_rows_sha256": _digest(canonical_jsonl_rows_v1(nodes)),
        "canonical_nodes_sha256": _digest(canonical_jsonl_rows_v1(canonical_nodes)),
        "ordered_element_rows_sha256": _digest(canonical_jsonl_rows_v1(elements)),
        "canonical_elements_sha256": _digest(canonical_jsonl_rows_v1(canonical_elements)),
        "ordered_elsets_sha256": _digest(canonical_jsonl_rows_v1(elset_declarations)),
        "canonical_elsets_sha256": _digest(canonical_jsonl_rows_v1(canonical_elsets)),
    }
    return {
        "nodes": nodes,
        "elements": elements,
        "elsets": elset_declarations,
        "semantics": semantics,
        "selected_element_ids": sorted(element_ids),
    }


def _validate_relationship(value: Any) -> dict[str, str]:
    keys = (
        "job_id", "upload_id", "reservation_id", "attempt_id", "package_id",
        "candidate_id", "selection_manifest_sha256",
    )
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError("mesh relationship must have the exact seven-key schema")
    result = dict(value)
    for key, item in result.items():
        if not isinstance(item, str) or not item:
            raise ValueError(f"relationship.{key} must be nonempty")
    if SHA_RE.fullmatch(result["selection_manifest_sha256"]) is None:
        raise ValueError("selection_manifest_sha256 must be sha256_upper")
    return result


def build_mesh_binding_v2(
    raw_mesh_bytes: bytes,
    *,
    original_filename: str,
    parser_schema_version: str,
    parser_algorithm_version: str,
    source_manifest_sha256: str,
    relationship: Mapping[str, Any],
) -> dict[str, Any]:
    parsed = parse_mesh_identity_v1(raw_mesh_bytes)
    if PurePath(original_filename).name != original_filename or not original_filename.strip():
        raise ValueError("original_filename must be a nonempty basename")
    if SHA_RE.fullmatch(source_manifest_sha256) is None:
        raise ValueError("source_manifest_sha256 must be sha256_upper")
    parser = {
        "schema_version": parser_schema_version,
        "algorithm_version": parser_algorithm_version,
        "source_manifest_sha256": source_manifest_sha256,
    }
    selected_ids = parsed["selected_element_ids"]
    domain = {
        "selector_type": "WHOLE_FILE_IMPLANT_DOMAIN",
        "selector_value": "WHOLE_FILE",
        "explicit_declaration": True,
        "implant_domain_confirmed": True,
        "selection_is_explicit": True,
        "selection_source": "PRODUCT_WHOLE_FILE_DECLARATION",
        "selected_element_count": len(selected_ids),
        "selected_element_ids_sha256": _digest(canonical_jsonl_rows_v1(selected_ids)),
        "semantic_sha256": "0" * 64,
    }
    domain["semantic_sha256"] = _digest(
        canonical_json_object_v1(domain, self_hash_pointer="/semantic_sha256")
    )
    source = {
        "raw_mesh_relative_path": "source.inp",
        "raw_size_bytes": len(raw_mesh_bytes),
        "raw_sha256": _digest(raw_mesh_bytes),
        "raw_bytes_preserved": True,
        "original_filename": original_filename,
        "units": "mm",
        "frame": "source_native_cartesian_frame",
        "transform": "identity",
        "rotation": "none",
        "reflection": "none",
        "implicit_scale": "none",
    }
    semantics = parsed["semantics"]
    precheck_projection = {
        "schema_version": "latticegpt_mesh_precheck_identity_projection_v1",
        "status": "PASS",
        "source": {"raw_size_bytes": source["raw_size_bytes"], "raw_sha256": source["raw_sha256"]},
        "parser": parser,
        "precheck_schema_version": "latticegpt_mesh_precheck_v2",
        "semantics": semantics,
        "design_domain": domain,
        "diagnostic_codes": [],
    }
    precheck = {
        "schema_version": "latticegpt_mesh_precheck_v2",
        "status": "PASS",
        "evidence_sha256": _digest(canonical_json_object_v1(precheck_projection)),
        "diagnostic_codes": [],
    }
    semantic_projection = {
        "schema_version": "latticegpt_mesh_semantic_projection_v1",
        "parser": parser,
        "precheck_schema_version": precheck["schema_version"],
        "source_frame": {key: source[key] for key in ("units", "frame", "transform", "rotation", "reflection", "implicit_scale")},
        "semantics": semantics,
        "design_domain": domain,
    }
    binding = {
        "schema_version": "latticegpt_mesh_binding_v2",
        "source": source,
        "parser": parser,
        "precheck": precheck,
        "semantics": semantics,
        "design_domain": domain,
        "relationship": _validate_relationship(relationship),
        "digests": {
            "mesh_semantic_sha256": _digest(canonical_json_object_v1(semantic_projection)),
            "mesh_binding_sha256": "0" * 64,
        },
    }
    binding["digests"]["mesh_binding_sha256"] = _digest(
        canonical_json_object_v1(binding, self_hash_pointer="/digests/mesh_binding_sha256")
    )
    return copy.deepcopy(binding)


def validate_mesh_binding_v2(
    value: Mapping[str, Any],
    *,
    raw_mesh_bytes: bytes,
    expected_relationship: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("MeshBindingV2 must be an object")
    top_keys = {"schema_version", "source", "parser", "precheck", "semantics", "design_domain", "relationship", "digests"}
    if set(value) != top_keys or value.get("schema_version") != "latticegpt_mesh_binding_v2":
        raise ValueError("MeshBindingV2 top-level schema mismatch")
    source_keys = {"raw_mesh_relative_path", "raw_size_bytes", "raw_sha256", "raw_bytes_preserved", "original_filename", "units", "frame", "transform", "rotation", "reflection", "implicit_scale"}
    if not isinstance(value["source"], Mapping) or set(value["source"]) != source_keys:
        raise ValueError("MeshBindingV2 source schema mismatch")
    semantic_keys = {"element_type_original", "element_type_effective", "node_count", "element_count", "ordered_node_rows_sha256", "canonical_nodes_sha256", "ordered_element_rows_sha256", "canonical_elements_sha256", "ordered_elsets_sha256", "canonical_elsets_sha256"}
    if not isinstance(value["semantics"], Mapping) or set(value["semantics"]) != semantic_keys:
        raise ValueError("MeshBindingV2 semantics schema mismatch")
    if _validate_relationship(value["relationship"]) != _validate_relationship(expected_relationship):
        raise ValueError("RELATIONSHIP_MISMATCH")
    rebuilt = build_mesh_binding_v2(
        raw_mesh_bytes,
        original_filename=value["source"]["original_filename"],
        parser_schema_version=value["parser"]["schema_version"],
        parser_algorithm_version=value["parser"]["algorithm_version"],
        source_manifest_sha256=value["parser"]["source_manifest_sha256"],
        relationship=expected_relationship,
    )
    if copy.deepcopy(dict(value)) != rebuilt:
        raise ValueError("MeshBindingV2 identity or schema mismatch")
    return rebuilt


__all__ = [
    "build_mesh_binding_v2",
    "canonical_jsonl_rows_v1",
    "parse_mesh_identity_v1",
    "validate_mesh_binding_v2",
    "build_mesh_binding_v3",
    "validate_mesh_binding_v3",
]


def build_mesh_binding_v3(original_bytes: bytes, derived_bytes: bytes, *, original_filename: str,
                          source_manifest_sha256: str, relationship: Mapping[str, Any],
                          confirmation: Mapping[str, Any], precheck: Mapping[str, Any]) -> dict:
    """Bind an explicitly confirmed derivative without changing V2 semantics."""
    from .mesh_domain import extract_hex_domain
    expected, domain = extract_hex_domain(original_bytes)
    if derived_bytes != expected:
        raise ValueError("derived mesh differs from exact selected original rows")
    if PurePath(original_filename).name != original_filename or not original_filename:
        raise ValueError("original filename must be a basename")
    if SHA_RE.fullmatch(source_manifest_sha256) is None:
        raise ValueError("source manifest SHA is required")
    parsed = parse_mesh_identity_v1(derived_bytes, allow_mixed_hex=True)
    confirmation_keys = {"method", "domain_selection_sha256", "candidate_confirmation_sha256", "confirmed_revision", "confirmed_at", "session_generation"}
    if (not isinstance(confirmation, Mapping) or set(confirmation) != confirmation_keys
        or confirmation["method"] != "explicit_user_domain_selection"
        or confirmation["domain_selection_sha256"] != domain["selection_sha256"]
        or type(confirmation["confirmed_revision"]) is not int or confirmation["confirmed_revision"] < 1
        or not isinstance(confirmation["confirmed_at"], str) or not confirmation["confirmed_at"]
        or not isinstance(confirmation["session_generation"], str) or not confirmation["session_generation"]
        or SHA_RE.fullmatch(str(confirmation["candidate_confirmation_sha256"])) is None):
        raise ValueError("explicit domain confirmation is invalid")
    mesh = precheck.get("mesh", {})
    if (precheck.get("status") != "passed" or precheck.get("errors")
        or precheck.get("source", {}).get("sha256") != domain["derived_sha256"]
        or precheck.get("source", {}).get("size_bytes") != len(derived_bytes)
        or any(mesh.get(k) != parsed["semantics"][k] for k in ("node_count", "element_count", "element_type_original", "element_type_effective"))):
        raise ValueError("derived mesh requires matching passed quality evidence")
    binding = {"schema_version": "latticegpt_mesh_binding_v3",
        "original_upload": {"raw_mesh_relative_path": "source.inp", "original_filename": original_filename,
            "raw_sha256": _digest(original_bytes), "raw_size_bytes": len(original_bytes), "raw_bytes_preserved": True},
        "source": {"raw_mesh_relative_path": "selected_hex.inp", "original_filename": "selected_hex.inp",
            "raw_sha256": _digest(derived_bytes), "raw_size_bytes": len(derived_bytes), "units": "mm",
            "frame": "source_native_cartesian_frame", "transform": "identity", "node_and_element_ids_preserved": True},
        "parser": {"schema_version": "latticegpt_mesh_domain_parser_v1", "algorithm_version": "EXPLICIT_HEX_SUBSET_V1", "source_manifest_sha256": source_manifest_sha256},
        "precheck": {"status": "PASS", "evidence_sha256": _digest(canonical_json_object_v1(precheck))},
        "quality_evidence": copy.deepcopy(dict(precheck)),
        "semantics": parsed["semantics"], "design_domain": domain, "confirmation": dict(confirmation),
        "relationship": _validate_relationship(relationship), "digests": {"mesh_binding_sha256": "0" * 64}}
    binding["digests"]["mesh_binding_sha256"] = _digest(canonical_json_object_v1(binding, self_hash_pointer="/digests/mesh_binding_sha256"))
    return binding


def validate_mesh_binding_v3(value: Mapping[str, Any], *, original_bytes: bytes, derived_bytes: bytes,
                             expected_relationship: Mapping[str, Any], precheck: Mapping[str, Any]) -> dict:
    rebuilt = build_mesh_binding_v3(original_bytes, derived_bytes,
        original_filename=value["original_upload"]["original_filename"],
        source_manifest_sha256=value["parser"]["source_manifest_sha256"],
        relationship=expected_relationship, confirmation=value["confirmation"], precheck=precheck)
    if dict(value) != rebuilt:
        raise ValueError("MeshBindingV3 identity or relationship mismatch")
    return rebuilt
