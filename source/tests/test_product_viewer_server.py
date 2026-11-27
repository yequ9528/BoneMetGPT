import hashlib
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from product_workflow.config import load_latticegpt_config
from product_workflow.preflight import PreflightBindingV1


def viewer_preflight():
    return PreflightBindingV1(
        "viewer_only", None, None, None, "A" * 64, "B" * 64, "C" * 64
    )


class ProductViewerServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f".runtime_product_server_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.config = load_latticegpt_config(
            explicit={
                "release_root": self.root,
                "services": {
                    "viewer_host": "localhost",
                    "viewer_port": 8123,
                    "frontend_host": "localhost",
                    "frontend_port": 5188,
                },
            },
            environ={},
        )
        self.job_id = "job-1234567890ABCDEF12345678"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_exposes_only_exact_product_payload_and_file_roles(self):
        import product_viewer_server

        payload_builder = Mock(return_value={"schema_version": "latticegpt_product_viewer_state_v1"})
        app = product_viewer_server.create_product_viewer_app(
            self.config,
            preflight=viewer_preflight(),
            payload_builder=payload_builder,
        )
        client = app.test_client()
        response = client.get(f"/api/viewer/product/jobs/{self.job_id}")
        self.assertEqual(response.status_code, 200)
        payload_builder.assert_called_once()
        _, kwargs = payload_builder.call_args
        self.assertEqual(kwargs["conformal_root"], self.config.paths.conformal_root)

        from tests.test_viewer_delivery_roundtrip import B31_GEOMETRY, PUBLIC_FILES, make_reader
        real_app, job_id, _, _ = make_reader()
        client = real_app.test_client()
        base = "/api/viewer/product/jobs/" + job_id
        result = client.get(base).json["data"]["result"]
        self.assertEqual(set(result["downloads"]), set(PUBLIC_FILES))
        with client.get(result["downloads"]["conformal_inp"]) as file_response:
            self.assertEqual(file_response.status_code, 200)
            self.assertEqual(file_response.data, B31_GEOMETRY)
            self.assertEqual(file_response.content_length, len(B31_GEOMETRY))
            self.assertEqual(file_response.get_etag()[0], hashlib.sha256(B31_GEOMETRY).hexdigest())
        for role in ("stl", "png", "source_mesh", "b31", "metadata", "run_summary",
                     "package_metadata", "geometry_ledger", "source_element_map", "view", "cell_3d"):
            with self.subTest(role=role):
                self.assertEqual(client.get(base + "/files/" + role).status_code, 404)
                self.assertEqual(client.get(base + "/deliver/" + role).status_code, 404)
        for role in result["scene"]:
            self.assertEqual(client.get(base + "/deliver/" + role).status_code, 404)
        for role in result["downloads"]:
            self.assertEqual(client.get(base + "/scene/" + role).status_code, 404)

        for legacy_path in (
            "/api/database",
            "/api/database_v2",
            "/api/prediction",
            "/api/artifacts/example",
            "/api/conformal/packages/example/files/b31",
            "/api/viewer/product/jobs/latest",
        ):
            with self.subTest(path=legacy_path):
                self.assertEqual(client.get(legacy_path).status_code, 404)

    def test_typed_errors_keep_safe_code_and_job_id_on_both_routes(self):
        import product_viewer_server as server
        from product_workflow.store import JobNotFoundError, JobContractError
        from product_workflow.artifacts import ProductPackageContractError, ProductFileMissingError, ProductFileIdentityError
        for error, status in ((JobNotFoundError, 404), (JobContractError, 400), (ProductPackageContractError, 400), (ProductFileMissingError, 404), (ProductFileIdentityError, 400)):
            app = server.create_product_viewer_app(self.config, preflight=viewer_preflight(), payload_builder=Mock(side_effect=error("secret/path")))
            store = app.extensions["latticegpt_job_store"]
            with patch.object(store, "get_consistent_for_viewer", return_value={}), patch.object(
                    server, "resolve_product_package", side_effect=error("secret/path")):
                for suffix in ("", "/deliver/conformal_inp", "/scene/triangles"):
                    response = app.test_client().get(f"/api/viewer/product/jobs/{self.job_id}{suffix}")
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json["code"], error.code)
                    self.assertEqual(response.json["job_id"], self.job_id)
                    self.assertNotIn("secret", response.get_data(as_text=True))

    def test_cors_uses_configured_frontend_origin_only(self):
        import product_viewer_server

        app = product_viewer_server.create_product_viewer_app(
            self.config,
            preflight=viewer_preflight(),
            payload_builder=lambda *_args, **_kwargs: {},
        )
        client = app.test_client()
        allowed = client.get(
            f"/api/viewer/product/jobs/{self.job_id}",
            headers={"Origin": self.config.services.viewer_frontend_origin},
        )
        denied = client.get(
            f"/api/viewer/product/jobs/{self.job_id}",
            headers={"Origin": "http://192.0.2.10:5188"},
        )
        self.assertEqual(
            allowed.headers.get("Access-Control-Allow-Origin"),
            self.config.services.viewer_frontend_origin,
        )
        self.assertIsNone(denied.headers.get("Access-Control-Allow-Origin"))

    def test_errors_are_sanitized(self):
        import product_viewer_server

        app = product_viewer_server.create_product_viewer_app(
            self.config,
            preflight=viewer_preflight(),
            payload_builder=Mock(
                side_effect=ValueError("Traceback I:\\private\\job base_url=http://secret")
            ),
        )
        response = app.test_client().get(f"/api/viewer/product/jobs/{self.job_id}")
        serialized = json.dumps(response.get_json())
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid product Job request.", serialized)
        for forbidden in ("Traceback", "I:\\\\", "base_url", "secret"):
            self.assertNotIn(forbidden, serialized)

    def test_run_uses_configured_host_port_without_debug_or_reloader(self):
        import product_viewer_server

        app = Mock()
        binding = viewer_preflight()
        with patch.object(product_viewer_server, "require_preflight", return_value=binding):
            with patch.object(product_viewer_server, "create_product_viewer_app", return_value=app) as create:
                product_viewer_server.run_product_viewer_server(self.config)
        create.assert_called_once_with(self.config, preflight=binding)
        app.run.assert_called_once_with(
            host="localhost",
            port=8123,
            debug=False,
            use_reloader=False,
            request_handler=product_viewer_server.ProductRequestHandler,
        )


if __name__ == "__main__":
    unittest.main()
