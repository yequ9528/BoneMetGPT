"""Round-trip checks against the real Flask reader and a sealed synthetic six-file result."""
import hashlib
import io
import json
import struct
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import trimesh
from PIL import Image, PngImagePlugin
from unittest.mock import patch

from product_viewer_server import create_product_viewer_app
from product_workflow import runtime
from product_workflow.artifacts import PRODUCT_FILE_MAP
from product_workflow.config import load_latticegpt_config
from tests.test_product_viewer_server import viewer_preflight
from tests.test_product_workflow_mesh_runtime import ProductSixFileChainTests as _SixFileFixture

# A function alias avoids discovering the fixture's entire TestCase in this module.
_synthetic_chain = _SixFileFixture().chain
del _SixFileFixture

INTERNAL_MARKER = "PRIVATE_VIEWER_TEST_43A9"


B31_GEOMETRY = b"*Node\n1, 0, 0, 0\n2, 1, 1, 1\n*Element, type=B31\n1, 1, 2\n"
B31_SOURCE = b"*Heading\n" + INTERNAL_MARKER.encode() + b"\n" + B31_GEOMETRY
PUBLIC_FILES = {
    "unit_png": ("selected-unit-cell.png", "image/png"),
    "unit_stl": ("selected-unit-cell.stl", "model/stl"),
    "conformal_inp": ("conformal-lattice-b31.inp", "text/plain"),
}


def make_sealed_service():
    """Insert known artifacts before sealing. The original product verifier still signs the receipt."""
    verify = runtime.verify_product_package

    def seal_fixture(**request):
        package = Path(request["package_dir"])
        metadata_path = package / PRODUCT_FILE_MAP["metadata"]
        metadata = json.loads(metadata_path.read_bytes())
        metadata.update(node_count=17, beam_count=11, source_node_count=8, source_element_count=1,
                        b31_node_count=17, private_test_field={"cwd": "C:/synthetic/" + INTERNAL_MARKER})
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        (package / PRODUCT_FILE_MAP["run_summary"]).write_text("cwd=C:/synthetic/" + INTERNAL_MARKER, encoding="utf-8")
        # One valid triangle for the renderer, not the result of a design calculation.
        triangle = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
        (package / PRODUCT_FILE_MAP["stl"]).write_bytes(bytes(80) + struct.pack("<I", 1) + triangle)
        (package / PRODUCT_FILE_MAP["b31"]).write_bytes(B31_SOURCE)
        image = Image.new("RGBA", (7, 5), (24, 38, 72, 255))
        image.putpixel((2, 1), (211, 121, 64, 255))
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private-test", INTERNAL_MARKER)
        image.save(package / PRODUCT_FILE_MAP["png"], pnginfo=metadata)
        return verify(**request)

    with patch.object(runtime, "verify_product_package", side_effect=seal_fixture):
        service, job = _synthetic_chain()
    if job["status"] != "VIEWER_READY":
        raise AssertionError("synthetic six-file result did not pass production sealing")
    return service, job


def make_reader():
    service, job = make_sealed_service()
    config = load_latticegpt_config(explicit={"release_root": service.runtime_root.parent,
                                               "runtime_root": service.runtime_root}, environ={})
    package = Path(job["attempts"][0]["conformal_output_dir"])
    return create_product_viewer_app(config, preflight=viewer_preflight()), job["job_id"], service.runtime_root, package


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class DeliveryRoundTripTests(unittest.TestCase):
    def test_viewer_file_identities_match_each_route_and_science_bytes(self):
        app, job_id, root, package = make_reader()
        before = hashes(root)
        prefix = "/api/viewer/product/jobs/" + job_id
        client = app.test_client()
        for options in ("", "", "?include=original_mesh"):
            with self.subTest(options=options):
                reply = client.get(prefix + options)
                self.assertEqual(reply.status_code, 200)
                self.assertNotIn(INTERNAL_MARKER.encode(), reply.data)
                data = reply.json["data"]
                self.assertEqual(data["job_id"], job_id)
        result = data["result"]
        self.assertEqual(set(result["downloads"]), set(PUBLIC_FILES))
        self.assertEqual(set(result["download_identities"]), set(PUBLIC_FILES))
        self.assertEqual(result["unavailable_downloads"], [])
        selected, = (c for c in data["candidates"] if c["selected"])
        for role, (filename, mime) in PUBLIC_FILES.items():
            url = result["downloads"][role]
            with self.subTest(role=role), client.get(url) as file:
                parts = urlsplit(url)
                self.assertEqual(parts.path, prefix + "/deliver/" + role)
                token, = parse_qs(parts.query)["snapshot"]
                self.assertRegex(token, r"^[A-F0-9]{64}$")
                self.assertEqual(file.status_code, 200, file.data[:200])
                self.assertNotIn(INTERNAL_MARKER.encode(), file.data)
                digest = hashlib.sha256(file.data).hexdigest().upper()
                self.assertEqual(result["download_identities"][role],
                                 dict(filename=filename, size_bytes=len(file.data), sha256=digest))
                self.assertEqual(file.content_length, len(file.data))
                self.assertEqual(file.get_etag()[0].upper(), digest)
                self.assertEqual(file.mimetype, mime)
                self.assertIn("attachment", file.headers["Content-Disposition"])
                self.assertIn(filename, file.headers["Content-Disposition"])
                if role == "conformal_inp":
                    self.assertEqual(file.data, B31_GEOMETRY)
                    self.assertEqual((package / PRODUCT_FILE_MAP["b31"]).read_bytes(), B31_SOURCE)
                elif role == "unit_png":
                    with Image.open(io.BytesIO(file.data)) as image, client.get(selected["preview"]["url"]) as preview:
                        self.assertEqual(image.format, "PNG")
                        self.assertEqual(image.info, {})
                        self.assertEqual(preview.status_code, 200)
                        with Image.open(io.BytesIO(preview.data)) as standard:
                            np.testing.assert_array_equal(image.convert("RGBA"), standard.convert("RGBA"))
                else:
                    from cad_module import build_beam_mesh_from_graph
                    self.assertEqual(file.data[:80], bytes(80))
                    mesh = trimesh.load_mesh(io.BytesIO(file.data), file_type="stl", process=False)
                    expected = build_beam_mesh_from_graph(selected["nodes"], selected["connections"], selected["diameter"])
                    np.testing.assert_allclose(mesh.triangles, expected.triangles, atol=1e-7)
        self.assertEqual(set(result["scene"]), {"triangles", "poster"})
        for role, url in result["scene"].items():
            self.assertEqual(urlsplit(url).path, prefix + "/scene/" + role)
            with client.get(url) as scene:
                self.assertEqual(scene.status_code, 200)
                self.assertNotIn("attachment", scene.headers.get("Content-Disposition", ""))
                self.assertNotIn(INTERNAL_MARKER.encode(), scene.data)
                if role == "triangles":
                    self.assertEqual(scene.mimetype, "application/octet-stream")
                    original = trimesh.load_mesh(package / PRODUCT_FILE_MAP["stl"], process=False)
                    np.testing.assert_array_equal(np.frombuffer(scene.data, dtype="<f4").reshape(-1, 3, 3), original.triangles)
                else:
                    with Image.open(io.BytesIO(scene.data)) as image, Image.open(package / PRODUCT_FILE_MAP["png"]) as original:
                        self.assertEqual(image.format, "WEBP")
                        np.testing.assert_array_equal(image.convert("RGBA"), original.convert("RGBA"))
        self.assertEqual(hashes(root), before)

    def test_tampered_sealed_diagnostic_blocks_direct_and_summary_access(self):
        app, job_id, root, package = make_reader()
        base = "/api/viewer/product/jobs/" + job_id
        client = app.test_client()
        result = client.get(base).json["data"]["result"]
        target = package / PRODUCT_FILE_MAP["run_summary"]
        target.write_bytes(target.read_bytes() + b"\nsynthetic-drift")
        snapshot = hashes(root)
        for url in (base, *result["downloads"].values(), *result["scene"].values()):
            reply = client.get(url)
            self.assertEqual(reply.status_code, 400)
            self.assertEqual(reply.json["code"], "FILE_IDENTITY_MISMATCH")
            self.assertNotIn(INTERNAL_MARKER.encode(), reply.data)
        for role in ("run_summary", "metadata"):
            self.assertEqual(client.get(base + "/files/" + role).status_code, 404)
        self.assertEqual(hashes(root), snapshot)

    def test_real_revision_change_and_wrong_job_refuse_all_original_links(self):
        app, job_id, root, _ = make_reader()
        config = load_latticegpt_config(explicit={"release_root": root.parent, "runtime_root": root}, environ={})
        client = app.test_client()
        base = "/api/viewer/product/jobs/" + job_id
        store = app.extensions["latticegpt_job_store"]
        current = store.get_consistent_for_viewer(job_id)
        result = client.get(base).json["data"]["result"]
        links = {**result["downloads"], **result["scene"]}
        before = hashes(root)
        for url in links.values():
            with client.get(url, headers={"Origin": config.services.viewer_frontend_origin}) as allowed:
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual(allowed.headers["Access-Control-Allow-Origin"], config.services.viewer_frontend_origin)
                self.assertIn("ETag", allowed.headers["Access-Control-Expose-Headers"])
            with client.get(url, headers={"Origin": "https://unrelated.invalid"}) as foreign:
                self.assertIsNone(foreign.headers.get("Access-Control-Allow-Origin"))
            wrong = client.get(url.replace(job_id, "job-" + "F" * 24))
            self.assertEqual(wrong.status_code, 404)
            self.assertEqual(wrong.json["code"], "JOB_NOT_FOUND")
        self.assertEqual(hashes(root), before)
        updated = store.replace(current, expected_revision=current["revision"], event="synthetic_revision_advanced")
        self.assertEqual(updated["revision"], current["revision"] + 1)
        after = hashes(root)
        fresh = client.get(base).json["data"]["result"]
        new_links = {**fresh["downloads"], **fresh["scene"]}
        self.assertEqual(set(links), set(new_links))
        for role, url in links.items():
            with self.subTest(role=role):
                old = client.get(url)
                self.assertEqual(old.status_code, 409)
                self.assertEqual(old.json["code"], "FILE_IDENTITY_MISMATCH")
                self.assertEqual(client.get(new_links[role]).status_code, 200)
        self.assertEqual(hashes(root), after)
