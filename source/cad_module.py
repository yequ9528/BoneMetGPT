from __future__ import annotations
import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import trimesh
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from typing import List, Dict, Any

def _to_np(p):
    return np.array(p, dtype=float)
def _create_cylinder_between_points(
    p1: np.ndarray,
    p2: np.ndarray,
    radius: float,
    sections: int = 16
) -> trimesh.Trimesh | None:
    vec = p2 - p1
    length = np.linalg.norm(vec)

    if length < 1e-9:
        return None

    cyl = trimesh.creation.cylinder(
        radius=radius,
        height=length,
        sections=sections
    )

    z_axis = np.array([0.0, 0.0, 1.0])
    direction = vec / length

    cross = np.cross(z_axis, direction)
    dot = np.dot(z_axis, direction)

    if np.linalg.norm(cross) < 1e-12:
        if dot > 0:
            rot = np.eye(4)
        else:
            rot = trimesh.transformations.rotation_matrix(
                angle=np.pi,
                direction=[1, 0, 0],
                point=[0, 0, 0]
            )
    else:
        angle = math.acos(np.clip(dot, -1.0, 1.0))
        rot = trimesh.transformations.rotation_matrix(
            angle=angle,
            direction=cross,
            point=[0, 0, 0]
        )

    cyl.apply_transform(rot)

    midpoint = (p1 + p2) / 2.0
    trans = trimesh.transformations.translation_matrix(midpoint)
    cyl.apply_transform(trans)

    return cyl


def build_beam_mesh_from_graph(
    nodes: List[List[float]],
    connections: List[List[int]],
    diameter: float,
    sections: int = 16
) -> trimesh.Trimesh:
    radius = float(diameter) / 2.0
    if radius <= 0:
        raise ValueError("diameter 必须大于 0。")

    meshes = []
    for conn in connections:
        i, j = int(conn[0]), int(conn[1])
        p1 = _to_np(nodes[i])
        p2 = _to_np(nodes[j])

        beam = _create_cylinder_between_points(
            p1=p1,
            p2=p2,
            radius=radius,
            sections=sections
        )
        if beam is not None:
            meshes.append(beam)

    if not meshes:
        raise ValueError("未生成任何梁单元网格。")

    return trimesh.util.concatenate(meshes)


def export_graph_preview_png(
    nodes: List[List[float]],
    connections: List[List[int]],
    save_path: str,
    title: str = "CAD Preview"
) -> str:
    """
    导出一个与 STL 对应的线框 PNG 预览图。
    注意：这是结构预览图，不是带阴影的 STL 渲染图。
    """
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    fig = plt.figure(figsize=(8, 8))
    ax = fig.add_subplot(111, projection="3d")
    ax.set_title(title)

    xs = [p[0] for p in nodes]
    ys = [p[1] for p in nodes]
    zs = [p[2] for p in nodes]
    ax.scatter(xs, ys, zs, s=10)

    for conn in connections:
        i, j = int(conn[0]), int(conn[1])
        p1 = nodes[i]
        p2 = nodes[j]
        ax.plot(
            [p1[0], p2[0]],
            [p1[1], p2[1]],
            [p1[2], p2[2]],
            linewidth=1
        )

    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")

    plt.tight_layout()
    plt.savefig(save_path, dpi=200)
    plt.close(fig)

    return save_path
def render_stl_preview_png(
    stl_path: str,
    png_path: str,
    elev: float = 20,
    azim: float = 45
) -> str:
    """
    将 STL 网格渲染为杆件实体预览图 PNG。
    注意：这是基于 STL 三角面片的静态渲染图，不是交互式 3D。
    """
    if not os.path.exists(stl_path):
        raise FileNotFoundError(f"STL 文件不存在: {stl_path}")

    os.makedirs(os.path.dirname(png_path), exist_ok=True)

    mesh = trimesh.load_mesh(stl_path)

    fig = plt.figure(figsize=(8, 8))
    ax = fig.add_subplot(111, projection="3d")
    ax.set_title(os.path.basename(os.path.splitext(png_path)[0]))

    # 取三角面片顶点
    vertices = mesh.vertices
    faces = mesh.faces

    # 构造 Poly3DCollection
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    triangles = vertices[faces]

    poly = Poly3DCollection(
        triangles,
        facecolor="lightgray",
        edgecolor="none",
        alpha=1.0
    )
    ax.add_collection3d(poly)

    # 自动设置坐标范围，避免模型被裁切
    x = vertices[:, 0]
    y = vertices[:, 1]
    z = vertices[:, 2]

    max_range = max(
        x.max() - x.min(),
        y.max() - y.min(),
        z.max() - z.min()
    ) / 2.0

    mid_x = (x.max() + x.min()) / 2.0
    mid_y = (y.max() + y.min()) / 2.0
    mid_z = (z.max() + z.min()) / 2.0

    ax.set_xlim(mid_x - max_range, mid_x + max_range)
    ax.set_ylim(mid_y - max_range, mid_y + max_range)
    ax.set_zlim(mid_z - max_range, mid_z + max_range)

    ax.view_init(elev=elev, azim=azim)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")

    plt.tight_layout()
    plt.savefig(png_path, dpi=200)
    plt.close(fig)

    return png_path

def export_graph_to_stl(
    nodes: List[List[float]],
    connections: List[List[int]],
    diameter: float,
    save_path: str,
    sections: int = 16
) -> Dict[str, Any]:
    """
    导出 STL，并自动生成同名 PNG 预览图。
    返回统一协议，供前端 viewer / 上层模块复用。
    """
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        mesh = build_beam_mesh_from_graph(
            nodes=nodes,
            connections=connections,
            diameter=diameter,
            sections=sections
        )
        mesh.export(save_path)

        preview_path = os.path.splitext(save_path)[0] + ".png"
        export_graph_preview_png(
            nodes=nodes,
            connections=connections,
            save_path=preview_path,
            title=os.path.basename(os.path.splitext(save_path)[0])
        )

        return {
            "status": "success",
            "stl_path": save_path,
            "preview_path": preview_path,
            "node_count": len(nodes),
            "connection_count": len(connections),
            "diameter": float(diameter),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "stl_path": "",
            "preview_path": "",
            "node_count": 0,
            "connection_count": 0,
            "diameter": 0.0,
        }


def parse_b31_inp_to_graph(b31_path: str | os.PathLike[str]) -> Dict[str, Any]:
    """
    Parse a minimal Abaqus B31 beam inp into graph data.

    This is intentionally limited to *NODE and *ELEMENT,TYPE=B31 sections so it
    can preserve conformal B31 output while reusing the existing STL exporter.
    """
    path = Path(b31_path)
    if not path.exists():
        raise FileNotFoundError(f"B31 inp 文件不存在: {path}")

    nodes_by_id: Dict[int, List[float]] = {}
    b31_edges: List[List[int]] = []
    section: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper_compact = line.upper().replace(" ", "")
        if upper_compact.startswith("*NODE"):
            section = "node"
            continue
        if upper_compact.startswith("*ELEMENT"):
            section = "b31" if "TYPE=B31" in upper_compact else None
            continue
        if line.startswith("*"):
            section = None
            continue

        parts = [part.strip() for part in line.split(",") if part.strip()]
        if section == "node":
            if len(parts) < 4:
                raise ValueError(f"无效的 *NODE 行: {raw_line}")
            node_id = int(parts[0])
            nodes_by_id[node_id] = [
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
            ]
        elif section == "b31":
            if len(parts) < 3:
                raise ValueError(f"无效的 B31 单元行: {raw_line}")
            b31_edges.append([int(parts[1]), int(parts[2])])

    if not nodes_by_id:
        raise ValueError("未解析到 *NODE 节点段。")
    if not b31_edges:
        raise ValueError("未解析到 *ELEMENT,TYPE=B31 梁单元段。")

    sorted_node_ids = sorted(nodes_by_id)
    id_to_index = {node_id: index for index, node_id in enumerate(sorted_node_ids)}
    nodes = [nodes_by_id[node_id] for node_id in sorted_node_ids]
    connections = [[id_to_index[a], id_to_index[b]] for a, b in b31_edges]

    return {
        "nodes": nodes,
        "connections": connections,
        "node_id_map": {str(node_id): index for node_id, index in id_to_index.items()},
        "stats": {
            "node_count": len(nodes),
            "beam_count": len(connections),
            "element_type": "B31",
        },
    }


def compute_graph_bbox(nodes: List[List[float]]) -> Dict[str, List[float] | float]:
    if not nodes:
        raise ValueError("nodes 不能为空。")

    arr = np.array(nodes, dtype=float)
    mins = arr.min(axis=0)
    maxs = arr.max(axis=0)
    size = maxs - mins
    return {
        "min": mins.tolist(),
        "max": maxs.tolist(),
        "size": size.tolist(),
        "max_dim": float(size.max()),
    }


def default_diameter_from_bbox(
    bbox: Dict[str, List[float] | float],
    ratio: float = 0.03
) -> float:
    max_dim = float(bbox["max_dim"])
    diameter = max_dim * float(ratio)
    if diameter <= 0:
        raise ValueError("默认 diameter 必须大于 0。")
    return diameter


def build_conformal_result_package(
    b31_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    cell_type: str = "BCC",
    diameter: float | None = None,
    diameter_source: str | None = None,
    result_id: str | None = None
) -> Dict[str, Any]:
    """
    Build a minimal conformal result package from an existing B31 inp.

    The B31 inp is copied as the conformal engineering output. STL/PNG are
    generated only as derived visualization artifacts.
    """
    source_b31 = Path(b31_path)
    package_dir = Path(output_dir)
    core_dir = package_dir / "core"
    visualization_dir = package_dir / "visualization"
    core_dir.mkdir(parents=True, exist_ok=True)
    visualization_dir.mkdir(parents=True, exist_ok=True)

    graph = parse_b31_inp_to_graph(source_b31)
    nodes = graph["nodes"]
    connections = graph["connections"]
    stats = graph["stats"]
    bbox = compute_graph_bbox(nodes)

    if diameter is None:
        diameter = default_diameter_from_bbox(bbox, ratio=0.03)
        diameter_source = diameter_source or "bbox_max_dim * 0.03"
    elif diameter <= 0:
        raise ValueError("diameter 必须大于 0。")
    else:
        diameter = float(diameter)
        diameter_source = diameter_source or "user_provided"

    resolved_result_id = result_id or f"conformal_result_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    core_b31_path = core_dir / "conformal_b31.inp"
    stl_path = visualization_dir / "conformal_preview.stl"
    metadata_path = package_dir / "metadata.json"
    run_summary_path = package_dir / "run_summary.txt"

    shutil.copy2(source_b31, core_b31_path)

    export_result = export_graph_to_stl(
        nodes=nodes,
        connections=connections,
        diameter=float(diameter),
        save_path=str(stl_path),
        sections=16,
    )
    if export_result.get("status") != "success":
        raise RuntimeError(f"STL/PNG 导出失败: {export_result.get('message', '')}")

    png_path = Path(str(export_result["preview_path"]))
    warnings = [
        "minimal runtime only",
        "default diameter used" if diameter_source == "bbox_max_dim * 0.03" else "explicit diameter used",
        "not real bone defect validation",
        "not viewer page integration",
        "not 150w candidate adapter",
    ]

    metadata = {
        "result_id": resolved_result_id,
        "stage": "6.3-conformal-result-package-minimal-impl",
        "source_b31_path": str(source_b31),
        "core_b31_path": str(core_b31_path),
        "stl_path": str(stl_path),
        "png_path": str(png_path),
        "input_type": "B31_INP",
        "element_type": "B31",
        "node_count": int(stats["node_count"]),
        "beam_count": int(stats["beam_count"]),
        "bbox": bbox,
        "diameter": float(diameter),
        "diameter_source": diameter_source,
        "cell_type": cell_type,
        "cell_source": f"cell_library/{cell_type.lower()}.yml from 6.0 minimal runtime",
        "core_output_role": "B31 inp is the conformal design engineering output",
        "visualization_output_role": "STL/PNG are derived visualization outputs",
        "created_by": "cad_module.build_conformal_result_package",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    run_summary = "\n".join([
        "6.3 conformal result package minimal validation",
        "",
        "B31 inp is the conformal design engineering output.",
        "STL/PNG are derived visualization outputs.",
        f"diameter={float(diameter):.8f}",
        f"diameter_source={diameter_source}",
        "",
        "This run did not use a 150w candidate.",
        "This run did not integrate a Viewer page.",
        "This run did not run Abaqus.",
        "This run does not validate a real bone defect large sample.",
        "This run does not mean dual-space fusion is complete.",
        "This run does not mean spatially-varying generative lattice design is complete.",
    ])
    run_summary_path.write_text(run_summary + "\n", encoding="utf-8")

    return {
        "result_id": resolved_result_id,
        "package_dir": str(package_dir),
        "core_b31_path": str(core_b31_path),
        "stl_path": str(stl_path),
        "png_path": str(png_path),
        "metadata_path": str(metadata_path),
        "run_summary_path": str(run_summary_path),
        "node_count": int(stats["node_count"]),
        "beam_count": int(stats["beam_count"]),
        "diameter": float(diameter),
        "warnings": warnings,
    }
