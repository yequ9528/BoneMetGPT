from __future__ import annotations

import re
from pathlib import Path


MAX_MESH_NODES = 5000
MAX_MESH_ELEMENTS = 2000
SUPPORTED_HEX_ELEMENT_TYPES = {"C3D8", "C3D8R"}

_ELEMENT_TYPE_RE = re.compile(r"\btype\s*=\s*([^,\s]+)", re.IGNORECASE)
_HEX_EDGE_PAIRS = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 0),
    (4, 5),
    (5, 6),
    (6, 7),
    (7, 4),
    (0, 4),
    (1, 5),
    (2, 6),
    (3, 7),
)


def build_source_mesh_visualization(
    source_mesh_path: str | Path,
    *,
    max_nodes: int = MAX_MESH_NODES,
    max_elements: int = MAX_MESH_ELEMENTS,
) -> dict:
    parsed = _parse_c3d8_inp(Path(source_mesh_path))
    node_count = len(parsed["nodes"])
    element_count = len(parsed["elements"])
    element_type = _summarize_element_type(parsed["elements"])
    bbox = _calculate_bbox(parsed["nodes"])

    if node_count > max_nodes or element_count > max_elements:
        return {
            "status": "too_large",
            "representation": "hex_wireframe",
            "element_type": element_type,
            "node_count": node_count,
            "element_count": element_count,
            "bbox": bbox,
            "message": "mesh visualization omitted in 6.12b small-fixture contract",
        }

    if not parsed["nodes"] or not parsed["elements"]:
        return {
            "status": "unavailable",
            "representation": "hex_wireframe",
            "element_type": element_type,
            "node_count": node_count,
            "element_count": element_count,
            "bbox": bbox,
            "message": "no C3D8/C3D8R source mesh elements found",
        }

    return {
        "status": "available",
        "representation": "hex_wireframe",
        "element_type": element_type,
        "node_count": node_count,
        "element_count": element_count,
        "nodes": [
            {"id": node_id, "xyz": coords}
            for node_id, coords in sorted(parsed["nodes"].items())
        ],
        "elements": [
            {
                "id": element["id"],
                "type": element["type"],
                "node_ids": element["node_ids"],
            }
            for element in parsed["elements"]
        ],
        "edges": _build_hex_edges(parsed["elements"]),
        "bbox": bbox,
    }


def _parse_c3d8_inp(source_mesh_path: Path) -> dict:
    nodes: dict[int, list[float]] = {}
    elements: list[dict] = []
    section = None
    element_type = None

    for raw_line in source_mesh_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue

        if line.startswith("*"):
            header = line.lower()
            if header.startswith("*node"):
                section = "node"
                element_type = None
                continue

            if header.startswith("*element"):
                matched_type = _extract_element_type(line)
                if matched_type in SUPPORTED_HEX_ELEMENT_TYPES:
                    section = "element"
                    element_type = matched_type
                else:
                    section = None
                    element_type = None
                continue

            section = None
            element_type = None
            continue

        if section == "node":
            node = _parse_node_line(line)
            if node is not None:
                node_id, coords = node
                nodes[node_id] = coords
            continue

        if section == "element" and element_type:
            element = _parse_element_line(line, element_type)
            if element is not None:
                elements.append(element)

    return {"nodes": nodes, "elements": elements}


def _extract_element_type(header_line: str) -> str | None:
    match = _ELEMENT_TYPE_RE.search(header_line)
    if not match:
        return None
    return match.group(1).strip().upper()


def _parse_node_line(line: str) -> tuple[int, list[float]] | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 4:
        return None

    try:
        node_id = int(parts[0])
        coords = [float(parts[1]), float(parts[2]), float(parts[3])]
    except ValueError:
        return None

    return node_id, coords


def _parse_element_line(line: str, element_type: str) -> dict | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 9:
        return None

    try:
        element_id = int(parts[0])
        node_ids = [int(part) for part in parts[1:9]]
    except ValueError:
        return None

    return {"id": element_id, "type": element_type, "node_ids": node_ids}


def _summarize_element_type(elements: list[dict]) -> str | None:
    element_types = sorted({element["type"] for element in elements})
    if not element_types:
        return None
    if len(element_types) == 1:
        return element_types[0]
    return "mixed"


def _build_hex_edges(elements: list[dict]) -> list[list[int]]:
    seen_edges = set()
    for element in elements:
        node_ids = element["node_ids"]
        if len(node_ids) != 8:
            continue

        for start_index, end_index in _HEX_EDGE_PAIRS:
            edge = tuple(sorted((node_ids[start_index], node_ids[end_index])))
            seen_edges.add(edge)

    return [list(edge) for edge in sorted(seen_edges)]


def _calculate_bbox(nodes: dict[int, list[float]]) -> dict | None:
    if not nodes:
        return None

    coords = list(nodes.values())
    mins = [min(point[index] for point in coords) for index in range(3)]
    maxs = [max(point[index] for point in coords) for index in range(3)]

    return {
        "min": mins,
        "max": maxs,
        "size": [maxs[index] - mins[index] for index in range(3)],
    }
