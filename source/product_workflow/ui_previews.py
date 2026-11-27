"""In-memory candidate previews; no model, service, or artifact writes."""

from __future__ import annotations

import asyncio
import math
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, TypeVar


class PreviewSourceError(ValueError):
    """Candidate identity or normalized geometry is not fit for display."""


class PreviewResourceError(PreviewSourceError):
    """A valid result exceeds the display resource budget."""


class PreviewBusy(RuntimeError):
    """Both preview batch slots are occupied."""


class PreviewTimeout(TimeoutError):
    """This waiter timed out; running work still owns its capacity slot."""


@dataclass(frozen=True)
class CandidatePreview:
    candidate_id: str
    label: str
    png: bytes | None
    error: str | None


@dataclass(frozen=True)
class _Geometry:
    candidate_id: str
    label: str
    nodes: tuple[tuple[float, float, float], ...]
    connections: tuple[tuple[int, int], ...]
    diameter: float


STANDARD_CELL_RENDERER = "owner-cell-png-v1"
_RENDER_LOCK = threading.Lock()
STL_PREVIEW_MAX_BYTES = 300 * 1024 * 1024
STL_PREVIEW_MAX_TRIANGLES = 6_000_000


def render_stl_preview(path: str | Path) -> bytes:
    """Render the caller-verified STL as a solid PNG without changing its geometry.

    File/package identity belongs to the result reader. These limits are the
    same read limits as the Viewer, and are checked before VTK allocates a mesh.
    """
    import re
    import struct

    source = Path(path)
    size = source.stat().st_size
    if size > STL_PREVIEW_MAX_BYTES:
        raise PreviewResourceError("STL exceeds the preview read limit")
    if size <= 0:
        raise PreviewSourceError("STL is empty")
    with source.open("rb") as stream:
        header = stream.read(84)
        count = struct.unpack("<I", header[80:84])[0] if len(header) == 84 else 0
        if count and size == 84 + count * 50:
            triangles = count
        elif header.lstrip().lower().startswith(b"solid"):
            stream.seek(0)
            triangles = len(re.findall(rb"\bfacet\s+normal\b", stream.read(), re.IGNORECASE))
        else:
            raise PreviewSourceError("STL is empty or malformed")
    if triangles > STL_PREVIEW_MAX_TRIANGLES:
        raise PreviewResourceError("STL exceeds the preview triangle limit")
    if triangles <= 0:
        raise PreviewSourceError("STL has no faces")

    import vtk

    with _RENDER_LOCK:
        window = vtk.vtkRenderWindow()
        try:
            reader = vtk.vtkSTLReader()
            reader.SetFileName(str(source))
            reader.Update()
            mesh = reader.GetOutput()
            bounds = mesh.GetBounds()
            if (mesh.GetNumberOfCells() == 0 or mesh.GetNumberOfCells() > STL_PREVIEW_MAX_TRIANGLES
                    or not all(math.isfinite(value) for value in bounds)
                    or max(bounds[2 * a + 1] - bounds[2 * a] for a in range(3)) <= 0):
                raise PreviewSourceError("STL geometry cannot be displayed")
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(reader.GetOutputPort())
            mapper.ScalarVisibilityOff()
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.35, 0.67, 0.64)
            actor.GetProperty().SetAmbient(0.28)
            actor.GetProperty().SetDiffuse(0.70)
            actor.GetProperty().SetSpecular(0.15)
            actor.GetProperty().SetSpecularPower(24)
            renderer = vtk.vtkRenderer()
            renderer.SetBackground(1, 1, 1)
            renderer.AddActor(actor)
            window.SetOffScreenRendering(1)
            window.SetSize(800, 600)
            window.SetMultiSamples(0)
            window.AddRenderer(renderer)
            renderer.ResetCamera()
            camera = renderer.GetActiveCamera()
            camera.Azimuth(35)
            camera.Elevation(22)
            camera.SetParallelProjection(True)
            renderer.ResetCamera()
            # Fit projected bounds rather than the 3D bounding sphere, which
            # leaves large empty margins around the lumbar body's preview.
            camera.OrthogonalizeViewUp()
            up, normal = camera.GetViewUp(), camera.GetViewPlaneNormal()
            right = (up[1]*normal[2]-up[2]*normal[1], up[2]*normal[0]-up[0]*normal[2],
                     up[0]*normal[1]-up[1]*normal[0])
            center = camera.GetFocalPoint()
            corners = [(x, y, z) for x in bounds[:2] for y in bounds[2:4] for z in bounds[4:6]]
            half_width = max(abs(sum((point[a]-center[a])*right[a] for a in range(3))) for point in corners)
            half_height = max(abs(sum((point[a]-center[a])*up[a] for a in range(3))) for point in corners)
            camera.SetParallelScale(max(half_height, half_width*600/800)*1.08)
            renderer.ResetCameraClippingRange()
            window.Render()
            capture = vtk.vtkWindowToImageFilter()
            capture.SetInput(window)
            capture.SetInputBufferTypeToRGB()
            capture.ReadFrontBufferOff()
            capture.Update()
            writer = vtk.vtkPNGWriter()
            writer.SetInputConnection(capture.GetOutputPort())
            writer.WriteToMemoryOn()
            writer.Write()
            result = bytes(memoryview(writer.GetResult()))
            if not result.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PreviewSourceError("STL image could not be encoded")
            return result
        finally:
            window.Finalize()


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool):
        raise PreviewSourceError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreviewSourceError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise PreviewSourceError(f"{label} must be a finite number")
    return result


def _validate_candidates(candidates: list[dict], *, expected_count=3) -> tuple[_Geometry, ...]:
    if not isinstance(candidates, list) or len(candidates) != expected_count:
        raise PreviewSourceError("candidate previews require exactly three candidates")
    result = []
    seen = set()
    for index, candidate in enumerate(candidates, 1):
        if not isinstance(candidate, dict):
            raise PreviewSourceError("candidate must be an object")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip() or candidate_id in seen:
            raise PreviewSourceError("candidate_id must be a nonempty unique string")
        seen.add(candidate_id)
        source_nodes = candidate.get("nodes")
        if not isinstance(source_nodes, list) or not source_nodes:
            raise PreviewSourceError("candidate nodes are missing")
        nodes = []
        for node in source_nodes:
            if not isinstance(node, (list, tuple)) or len(node) != 3:
                raise PreviewSourceError("candidate node must have three coordinates")
            nodes.append(tuple(_finite_number(value, "node coordinate") for value in node))
        source_edges = candidate.get("connections")
        if not isinstance(source_edges, list) or not source_edges:
            raise PreviewSourceError("candidate connections are missing")
        edges = []
        for edge in source_edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                raise PreviewSourceError("candidate connection must have two indexes")
            start, end = edge
            if type(start) is not int or type(end) is not int:
                raise PreviewSourceError("candidate connection indexes must be integers")
            if min(start, end) < 0 or max(start, end) >= len(nodes):
                raise PreviewSourceError("candidate connection index is out of range")
            if nodes[start] == nodes[end]:
                raise PreviewSourceError("candidate connection has zero length")
            edges.append((start, end))
        diameter = _finite_number(candidate.get("normalized_diameter"), "normalized diameter")
        if diameter <= 0:
            raise PreviewSourceError("normalized diameter must be positive")
        rank = candidate.get("rank")
        label = candidate.get("preview_label")
        if not isinstance(label, str) or not label.strip():
            label = f"Rank {rank}" if type(rank) is int and rank > 0 else f"Candidate {index}"
        result.append(_Geometry(candidate_id, label, tuple(nodes), tuple(edges), diameter))
    return tuple(result)


def render_candidate_previews(candidates: list[dict]) -> tuple[CandidatePreview, ...]:
    """Render the caller's ordered snapshot with shared normalized-coordinate bounds.

    Source errors reject the whole batch before any image is produced. A renderer
    failure retains that candidate's slot as a labelled placeholder plus an error.
    No millimetre conversion is performed.
    """
    geometries = _validate_candidates(candidates)
    lower = [min(node[axis] - item.diameter / 2 for item in geometries for node in item.nodes)
             for axis in range(3)]
    upper = [max(node[axis] + item.diameter / 2 for item in geometries for node in item.nodes)
             for axis in range(3)]
    center = tuple(low / 2 + high / 2 for low, high in zip(lower, upper))
    radius = max(high - low for low, high in zip(lower, upper)) * .56
    if not math.isfinite(radius) or radius <= 0 or not all(map(math.isfinite, center)):
        raise PreviewSourceError("candidate geometry bounds are invalid")
    previews = []
    for geometry in geometries:
        try:
            with _RENDER_LOCK:
                png = _render_candidate_png(geometry, center, radius)
            previews.append(CandidatePreview(geometry.candidate_id, geometry.label, png, None))
        except Exception as exc:
            # Exception text can contain paths; the UI needs a bounded safe reason.
            previews.append(CandidatePreview(
                geometry.candidate_id, geometry.label,
                render_candidate_placeholder(geometry.candidate_id, geometry.label),
                f"Preview rendering failed ({type(exc).__name__}).",
            ))
    return tuple(previews)


def render_candidate_placeholder(candidate_id: str, label: str) -> bytes:
    """A same-size failure card, with no invented geometry or success claim.

    Pillow keeps the fallback inexpensive and independent of the 3D renderer;
    render_candidate_previews invokes it inside the existing batch worker.
    """
    from PIL import Image, ImageDraw, ImageFont

    picture = Image.new("RGB", (512, 512), "white")
    draw = ImageDraw.Draw(picture)
    title_font = ImageFont.load_default(size=22)
    small_font = ImageFont.load_default(size=14)
    message_font = ImageFont.load_default(size=25)
    draw.text((26, 14), label, font=title_font, fill="#263544")
    draw.text((26, 49), candidate_id[:24], font=small_font, fill="#657587")
    draw.rounded_rectangle((26, 152, 486, 388), radius=12,
                           fill="#f4f5f7", outline="#dfe2e7", width=1)
    draw.text((256, 245), "Preview unavailable", font=message_font,
              fill="#48505a", anchor="mm")
    draw.text((256, 280), "Geometry image could not be rendered.", font=small_font,
              fill="#657587", anchor="mm")
    with BytesIO() as output:
        picture.save(output, format="PNG")
        return output.getvalue()


def _render_candidate_png(geometry: _Geometry, center: tuple, radius: float) -> bytes:
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.colors import LightSource

    figure = Figure(figsize=(4, 4), dpi=128, facecolor='white')
    canvas = FigureCanvasAgg(figure)
    try:
        axes = figure.add_axes((.02, .015, .96, .88), projection='3d')
        nodes = np.asarray(geometry.nodes, dtype=float)
        light = LightSource(azdeg=225, altdeg=28)
        for start, end in geometry.connections:
            direction = nodes[end]-nodes[start]
            length = np.linalg.norm(direction)
            direction /= length
            reference = np.array([0.,0.,1.]) if abs(direction[2]) < .9 else np.array([1.,0.,0.])
            u = np.cross(direction, reference); u /= np.linalg.norm(u)
            w = np.cross(direction, u)
            t,h = np.meshgrid(np.linspace(0,2*np.pi,14), np.linspace(0.,1.,4))
            surfaces = [nodes[start][axis] + geometry.diameter/2*(np.cos(t)*u[axis]+np.sin(t)*w[axis])
                        + h*length*direction[axis] for axis in range(3)]
            axes.plot_surface(*surfaces, color=(.74,.80,.92), shade=True, lightsource=light,
                              linewidth=0, antialiased=False)
        axes.scatter(nodes[:,0],nodes[:,1],nodes[:,2],s=3.2,c='#5A6A8A',depthshade=False,linewidths=0)
        axes.set_box_aspect((1,1,1),zoom=1.12)
        axes.set_xlim(-.12,1.12); axes.set_ylim(-.12,1.12); axes.set_zlim(-.12,1.12)
        axes.view_init(elev=22,azim=-58); axes.set_axis_off()
        figure.text(.05,.955,geometry.label,fontsize=11,weight='bold',color='#263544')
        figure.text(.05,.915,geometry.candidate_id[:24],fontsize=7,color='#657587')
        with BytesIO() as output:
            canvas.print_png(output)
            return output.getvalue()
    finally:
        figure.clear()


def standard_candidate_label(candidate):
    rank = candidate.get("rank")
    return f"Rank {rank}" if type(rank) is int and rank > 0 else "Unranked " + str(candidate["candidate_id"])


def render_standard_candidate_png(candidate):
    """Same saved-geometry renderer as chat, with no inferred or repaired values."""
    geometry, = _validate_candidates([{**candidate, "preview_label": standard_candidate_label(candidate)}], expected_count=1)
    with _RENDER_LOCK:
        return _render_candidate_png(geometry, (.5,.5,.5), .62)


T = TypeVar("T")


@dataclass
class _Batch:
    future: Future
    waiters: int = 0


class PreviewExecutor:
    """One worker and two unfinished batches, shared safely across event loops."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candidate-preview")
        self._lock = threading.RLock()
        self._batches: dict[str, _Batch] = {}
        self._closed = False

    def _finished(self, key: str, batch: _Batch) -> None:
        with self._lock:
            if self._batches.get(key) is batch:
                self._batches.pop(key)

    async def run(self, key: str, work: Callable[[], T], *, timeout: float = 60) -> T:
        if not isinstance(key, str) or not key:
            raise ValueError("preview batch key is required")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("preview timeout must be finite and nonnegative")
        with self._lock:
            if self._closed:
                raise RuntimeError("preview executor is shut down")
            # A Future may be done just before its completion callback acquires this lock.
            self._batches = {name: batch for name, batch in self._batches.items() if not batch.future.done()}
            batch = self._batches.get(key)
            if batch is None:
                if len(self._batches) >= 2:
                    raise PreviewBusy("Preview worker and waiting slot are busy")
                batch = _Batch(self._pool.submit(work))
                self._batches[key] = batch
                batch.future.add_done_callback(lambda _future: self._finished(key, batch))
            batch.waiters += 1
        wrapped = asyncio.wrap_future(batch.future)
        # A timed-out/closed-loop waiter may never read a later worker exception.
        wrapped.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        try:
            done, _ = await asyncio.wait((wrapped,), timeout=timeout)
            if not done:
                raise PreviewTimeout("Candidate preview timed out")
            return wrapped.result()
        finally:
            with self._lock:
                batch.waiters -= 1
                if batch.waiters == 0 and not batch.future.done():
                    # cancel() succeeds only before the worker starts. Running work keeps
                    # its slot until its actual Future completion, even after a timeout.
                    batch.future.cancel()

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)


class PreviewCache:
    """Byte-only LRU. A hit never substitutes for fresh source verification."""

    def __init__(self, max_items: int = 5, max_bytes: int = 20 * 1024 * 1024):
        if type(max_items) is not int or type(max_bytes) is not int or min(max_items, max_bytes) < 0:
            raise ValueError("preview cache limits must be nonnegative integers")
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, png: bytes) -> None:
        if not isinstance(png, bytes):
            raise TypeError("preview cache accepts bytes only")
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._size -= len(previous)
            if self._max_items == 0 or len(png) > self._max_bytes:
                return
            self._items[key] = png
            self._size += len(png)
            while len(self._items) > self._max_items or self._size > self._max_bytes:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0
