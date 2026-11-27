"""Explicit hex-domain extraction and preview; no model or geometry repair."""
from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter

from .manifest_io import canonical_json_object_v1

HEX_TYPES = frozenset({"C3D8", "C3D8R"})
MIXED_HEX_TYPE = "C3D8+C3D8R"
ARITY = {"C3D8": 8, "C3D8R": 8, "C3D6": 6, "C3D4": 4, "C3D10": 10,
         "S3": 3, "S3R": 3, "S4": 4, "S4R": 4, "B31": 2}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def parse_original_mesh(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("INP为空；请上传包含显式节点和单元的文件。")
    text = raw.decode("utf-8-sig", errors="strict")
    nodes, elements, node_rows, element_rows = {}, {}, {}, {}
    mode, kind = None, None
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            header = [s.strip().upper() for s in line.split(",")]
            keyword = header[0]
            if keyword in {"*INCLUDE", "*PART", "*INSTANCE", "*ASSEMBLY", "*SYSTEM", "*TRANSFORM", "*PARAMETER", "*NGEN", "*NFILL", "*NCOPY", "*NMAP", "*ELGEN", "*ELCOPY"}:
                raise ValueError(f"第{number}行使用{keyword}；请导出单一原生坐标系、显式节点连接的网格。")
            mode = None
            if keyword == "*NODE":
                if any(item.startswith("SYSTEM") and re.fullmatch(r"SYSTEM\s*=\s*R", item) is None for item in header):
                    raise ValueError("节点必须使用原生笛卡尔坐标；不进行隐式坐标转换。")
                mode = "node"
            elif keyword == "*ELEMENT":
                match = re.search(r"(?:^|,)\s*TYPE\s*=\s*([A-Z0-9]+)(?:\s*,|\s*$)", line.upper())
                if not match:
                    raise ValueError(f"第{number}行缺少单元TYPE。")
                kind, mode = match.group(1), "element"
            continue
        if mode is None:
            continue  # Sets, sections and loads are not a geometric transform.
        fields = [s.strip() for s in line.split(",")]
        if any(not s for s in fields):
            raise ValueError(f"第{number}行有空值或跨行连接；请导出显式单行连接。")
        if not re.fullmatch(r"[1-9][0-9]*", fields[0]):
            raise ValueError(f"第{number}行ID必须为正整数。")
        index = int(fields[0])
        if mode == "node":
            if len(fields) != 4 or index in nodes:
                raise ValueError(f"第{number}行节点格式错误或ID重复。")
            xyz = tuple(float(s) for s in fields[1:])
            if not all(math.isfinite(v) for v in xyz):
                raise ValueError(f"节点{index}坐标不是有限值。")
            nodes[index], node_rows[index] = xyz, raw_line
        else:
            if index in elements or any(not re.fullmatch(r"[1-9][0-9]*", s) for s in fields[1:]):
                raise ValueError(f"第{number}行单元ID重复或连接ID无效。")
            ids = [int(s) for s in fields[1:]]
            if not ids or (kind in ARITY and len(ids) != ARITY[kind]) or len(set(ids)) != len(ids):
                raise ValueError(f"单元{index}连接数量错误或节点重复；请修正原始网格。")
            elements[index], element_rows[index] = {"type": kind, "node_ids": ids}, raw_line
    if not nodes or not elements:
        raise ValueError("未找到显式节点和单元；请检查INP导出设置。")
    missing = sorted({n for e in elements.values() for n in e["node_ids"] if n not in nodes})
    if missing:
        raise ValueError(f"连接引用缺失节点{missing[:12]}；不能忽略坏连接。")
    return {"nodes": nodes, "elements": elements, "node_rows": node_rows, "element_rows": element_rows}


def _bounds(parsed, ids):
    points = [parsed["nodes"][n] for i in ids for n in parsed["elements"][i]["node_ids"]]
    return [[min(p[a] for p in points) for a in range(3)], [max(p[a] for p in points) for a in range(3)]] if points else None


def extract_hex_domain(raw: bytes) -> tuple[bytes, dict]:
    parsed = parse_original_mesh(raw)
    elements = parsed["elements"]
    selected = sorted(i for i, e in elements.items() if e["type"] in HEX_TYPES)
    if not selected:
        raise ValueError("没有可计算的C3D8/C3D8R六面体；请重新划分六面体网格，不能用修改白名单代替映射支持。")
    needed = {n for i in selected for n in elements[i]["node_ids"]}
    selected_set = set(selected)
    lines = ["** LatticeGPT explicitly selected hex domain; original upload retained", "*Node"]
    lines.extend(row for i, row in parsed["node_rows"].items() if i in needed)
    last_type = None
    for i, e in elements.items():
        if i not in selected_set:
            continue
        if e["type"] != last_type:
            lines.append("*Element, type=" + e["type"])
            last_type = e["type"]
        lines.append(parsed["element_rows"][i])
    derived = ("\n".join(lines) + "\n").encode("utf-8")
    excluded = {}
    for kind in sorted({e["type"] for e in elements.values()} - HEX_TYPES):
        ids = sorted(i for i, e in elements.items() if e["type"] == kind)
        excluded[kind] = {"count": len(ids), "element_ids": ids, "bounds_mm": _bounds(parsed, ids),
                          "category": "solid" if kind.startswith("C3D") else "shell" if kind.startswith("S") else "other"}
    manifest = {"schema_version": "latticegpt_selected_hex_domain_v1", "selector": "ALL_EXPLICIT_C3D8_C3D8R",
        "original_sha256": digest(raw), "original_size_bytes": len(raw), "derived_sha256": digest(derived),
        "derived_size_bytes": len(derived), "units": "mm", "frame": "source_native_cartesian_frame",
        "transform": "identity", "selected_element_ids": selected,
        "selected_element_ids_sha256": digest(b"".join((str(i)+"\n").encode("ascii") for i in selected)),
        "selected_element_count": len(selected), "selected_node_count": len(needed),
        "selected_types": dict(sorted(Counter(elements[i]["type"] for i in selected).items())),
        "selected_bounds_mm": _bounds(parsed, selected), "original_bounds_mm": _bounds(parsed, list(elements)),
        "excluded_types": excluded, "excluded_element_count": sum(e["count"] for e in excluded.values()),
        "excluded_solid_count": sum(e["count"] for e in excluded.values() if e["category"] == "solid"),
        "non_claims": ["SELECTED_DOMAIN_ONLY", "EXCLUDED_REGIONS_NOT_COMPUTED", "NOT_FULL_BONE_COVERAGE", "NOT_MECHANICAL_VALIDATION"]}
    manifest["selection_sha256"] = digest(canonical_json_object_v1(manifest))
    return derived, manifest


def render_domain_preview(raw: bytes) -> bytes:
    """Original, selected and excluded views share the native coordinate frame."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from mpl_toolkits.mplot3d.art3d import Line3DCollection
    parsed = parse_original_mesh(raw)
    _, domain = extract_hex_domain(raw)
    figure = Figure(figsize=(16, 5), dpi=130)
    FigureCanvasAgg(figure)
    edges = {8: [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)],
             6: [(0,1),(1,2),(2,0),(3,4),(4,5),(5,3),(0,3),(1,4),(2,5)],
             4: [(0,1),(1,2),(2,3),(3,0)], 3: [(0,1),(1,2),(2,0)], 2: [(0,1)]}
    groups = {"selected": [], "excluded solid": [], "excluded shell/other": []}
    for element in parsed["elements"].values():
        ids = element["node_ids"]; kind = element["type"]
        group = "selected" if kind in HEX_TYPES else "excluded solid" if kind.startswith("C3D") else "excluded shell/other"
        pairs = edges.get(len(ids), [(i, (i+1) % len(ids)) for i in range(len(ids))])
        if kind == "C3D4": pairs = [(i,j) for i in range(4) for j in range(i+1,4)]
        groups[group].extend([parsed["nodes"][ids[a]], parsed["nodes"][ids[b]]] for a,b in pairs)
    bounds = domain["original_bounds_mm"]
    span = max(max(bounds[1][a]-bounds[0][a] for a in range(3)), 1e-6)
    for panel, title in enumerate(("Original mesh", "Selected hex domain", "Excluded regions (not computed)"), 1):
        ax = figure.add_subplot(1, 3, panel, projection="3d", computed_zorder=False)
        for name, color in (("excluded shell/other", "#999999"), ("selected", "#1479b8"), ("excluded solid", "#df342a")):
            if (panel == 2 and name != "selected") or (panel == 3 and name == "selected"):
                continue
            if groups[name]: ax.add_collection3d(Line3DCollection(groups[name], colors=color,
                linewidths=1.8 if name == "excluded solid" else 0.45,
                alpha=1 if name == "excluded solid" else 0.65, zorder=5 if name == "excluded solid" else 1))
        for axis, setter in enumerate((ax.set_xlim, ax.set_ylim, ax.set_zlim)):
            center=(bounds[0][axis]+bounds[1][axis])/2; setter(center-span*.52, center+span*.52)
        ax.set_box_aspect((1,1,1)); ax.view_init(elev=22, azim=35); ax.set_title(title)
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)"); ax.set_zlabel("Z (mm)")
    figure.suptitle(f"BLUE: selected {domain['selected_element_count']} | RED: excluded solids {domain['excluded_solid_count']} | GRAY: excluded shells/other\nGeometry preview - see the recorded quality status in the conversation")
    output=io.BytesIO(); figure.savefig(output, format="png"); return output.getvalue()


def render_mesh_views(raw: bytes) -> dict[str, bytes]:
    """Individual views for the chat card, sharing native bounds and camera."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    parsed = parse_original_mesh(raw)
    _, domain = extract_hex_domain(raw)
    pairs_by_size = {
        8: [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)],
        6: [(0,1),(1,2),(2,0),(3,4),(4,5),(5,3),(0,3),(1,4),(2,5)],
        4: [(0,1),(1,2),(2,3),(3,0)], 3: [(0,1),(1,2),(2,0)], 2: [(0,1)]}
    groups = {"selected": [], "solid": [], "other": []}
    for item in parsed["elements"].values():
        ids, kind = item["node_ids"], item["type"]
        group = "selected" if kind in HEX_TYPES else "solid" if kind.startswith("C3D") else "other"
        pairs = ([(a,b) for a in range(4) for b in range(a+1,4)] if kind == "C3D4"
                 else pairs_by_size.get(len(ids), [(a,(a+1) % len(ids)) for a in range(len(ids))]))
        groups[group].extend([parsed["nodes"][ids[a]], parsed["nodes"][ids[b]]] for a,b in pairs)
    bounds = domain["original_bounds_mm"]
    span = max(max(bounds[1][a]-bounds[0][a] for a in range(3)), 1e-6)
    result = {}
    for name, title in (("original", "Original mesh"), ("selected", "Selected computational domain"),
                        ("excluded", "Excluded regions")):
        figure = Figure(figsize=(8, 6), dpi=100, facecolor="white")
        FigureCanvasAgg(figure)
        try:
            ax = figure.add_axes((.06, .05, .89, .89), projection="3d", computed_zorder=False)
            for group, color in (("other", "#999999"), ("selected", "#1479b8"), ("solid", "#df342a")):
                if (name == "selected" and group != "selected") or (name == "excluded" and group == "selected"):
                    continue
                if groups[group]:
                    ax.add_collection3d(Line3DCollection(groups[group], colors=color,
                        linewidths=1.5 if group == "solid" else .65,
                        alpha=1 if group == "solid" else .75,
                        zorder=5 if group == "solid" else 1))
            for axis, setter in enumerate((ax.set_xlim, ax.set_ylim, ax.set_zlim)):
                center=(bounds[0][axis]+bounds[1][axis])/2
                setter(center-span*.52, center+span*.52)
            ax.set_box_aspect((1,1,1))
            ax.set_proj_type("ortho")
            ax.view_init(elev=22, azim=35)
            ax.set_title(title, color="#233e55", pad=8)
            ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)"); ax.set_zlabel("Z (mm)")
            ax.tick_params(labelsize=8)
            if name == "excluded" and not groups["solid"] and not groups["other"]:
                figure.text(.5, .05, "No excluded elements", ha="center", color="#64748b")
            with io.BytesIO() as output:
                figure.savefig(output, format="png")
                result[name] = output.getvalue()
        finally:
            figure.clear()
    return result
