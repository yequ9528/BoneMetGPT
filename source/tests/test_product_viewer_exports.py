"""Synthetic Job-bound download acceptance, without a service or runtime engines."""
import hashlib
import json
import shutil
import uuid
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from product_viewer_exports import PublicExportUnavailable, build_export_identities, build_user_export, endpoint
from product_workflow.artifacts import PRODUCT_FILE_MAP, resolve_product_package
from product_workflow.contracts import canonical_sha256
from tests.test_viewer_delivery_roundtrip import B31_GEOMETRY, B31_SOURCE, PUBLIC_FILES
from product_workflow.config import load_latticegpt_config
from product_workflow.contracts import STIFFNESS_NAMES
from tests.test_product_artifact_resolution import write_p13_fixture
from tests.test_product_viewer_server import viewer_preflight


def sha(data):
    return hashlib.sha256(data).hexdigest().upper()


PRIVATE = r"I:\private\SYNTHETIC_SENTINEL"
JOB = "job-1234567890ABCDEF12345678"
PACKAGE = JOB + "-attempt-0001"
VECTOR = {key: index + 0.25 for index, key in enumerate(STIFFNESS_NAMES)}
HEADER = b"source_element_id,selected,selected_index,element_type_original,element_type_effective,node_ids\r\n"
ROW = b"41,true,1,C3D8R,C3D8_GEOMETRY_COMPATIBLE,11 12 13 14 15 16 17 18\r\n"
LEDGER = {
    "schema_version": "latticegpt_9x_geometry_ledger_v1", "contract": "PROJECT_TOLERANCE_V1",
    "evidence_level": "TECHNICAL_FIXTURE", "technical_disposition": "PASS",
    "formal_gate_disposition": "NOT_EVALUATED", "unknown_internal": PRIVATE,
    "source_geometry": {
        "contract": "PROJECT_TOLERANCE_V1", "status": "PASS", "volume_rule": "2x2x2_GAUSS_POSITIVE_DETJ",
        "jacobian_condition_metric": "SPECTRAL_2_NORM", "selected_element_count": 1,
        "valid_element_count": 1, "total_volume": 8.0, "average_element_volume": 8.0,
        "reference_length": 2.0, "minimum_jacobian_determinant": 1.0, "minimum_scaled_jacobian": 1.0,
        "maximum_jacobian_condition_2": 1.0, "non_positive_jacobian_element_ids": [],
        "design_domain_component_count": 1, "log": PRIVATE},
    "output_geometry": {
        "element_type": "B31", "node_count": 2, "beam_count": 1,
        "bbox": {"min": [0,0,0], "max": [1,1,1], "size": [1,1,1], "L_char": 1.732},
        "beam_length": {"minimum": 1.732, "maximum": 1.732, "mean": 1.732, "zero_length_tolerance": 1e-10},
        "connected_component_count": 1},
    "required_zero_counts": {"invalid_references": 0, "zero_length_beams": 0, "duplicate_undirected_beams": 0, "isolated_nodes": 0},
    "component_rule": {"contract": "output_components_equal_source_design_domain_components", "source_count": 1, "output_count": 1, "status": "PASS"},
    "stable_sets": {"NSET_IMPLANT_ALL": 2, "ELSET_IMPLANT_B31_ALL": 1, "NSET_IMPLANT_INTERFACE": "NOT_PROVIDED_NOT_INFERRED"},
    "source_element_map": {"schema_version": "latticegpt_9x_source_element_map_v1", "mapping_scope": "ORIGINAL_TO_SELECTED_DESIGN_DOMAIN_IDENTITY",
                           "beam_origin_mapping_status": "NOT_RECORDED_BY_CURRENT_CORE", "sha256": sha(HEADER + ROW)},
    "non_claims": ["NOT_FEA_VALIDATION", PRIVATE],
}


class UserExportTests(unittest.TestCase):
    def setUp(self):
        from product_viewer_server import create_product_viewer_app
        temp = Path.cwd() / ('e-' + uuid.uuid4().hex[:8])
        temp.mkdir()
        self.addCleanup(shutil.rmtree, temp)
        config = load_latticegpt_config(explicit={"release_root": str(temp)}, environ={})
        self.folder = config.paths.conformal_root / JOB / PACKAGE
        self.folder.mkdir(parents=True)
        write_p13_fixture(self.folder, PACKAGE)
        self.manifest = json.loads(self.path("package_metadata").read_bytes())
        self.manifest.update(case={"case_manifest_path": PRIVATE})
        self.manifest["source_mesh"]["mesh_units"] = "mm"
        self.manifest["acceptance"].update(formal_gate_disposition="NOT_EVALUATED",
                                          warnings=["interface node set was not provided and was not inferred", PRIVATE])
        self.replace("geometry_ledger", json.dumps(LEDGER).encode())
        self.replace("source_element_map", HEADER + ROW)
        self.replace("run_summary", PRIVATE.encode())
        self.replace("b31", B31_SOURCE)
        self.replace("metadata", json.dumps({
            "result_id": PACKAGE, "source_c3d8_path": PRIVATE, "warnings": ["explicit diameter used", PRIVATE],
            "element_type": "B31", "source_element_type_original": "C3D8R", "source_element_type_effective": "C3D8_GEOMETRY_COMPATIBLE",
            "source_node_count": 8, "source_element_count": 1, "node_count": 2, "b31_node_count": 3,
            "beam_count": 1, "b31_beam_count": 2, "diameter": .5,
            "bbox": {"min": [0,0,0], "max": [1,1,1], "size": [1,1,1], "max_dim": 1},
            "traceability": {"physical_diameter": .5, "physical_diameter_units": "mm", "reference_length": 2}}).encode())
        self.job = {
            "job_id": JOB, "revision": 12, "status": "VIEWER_READY",
            "target": {"physical_9d_gpa": dict(VECTOR), "normalized_9d": dict(VECTOR), "proposal_sha256": "A"*64,
                       "base_material": {"name": "TC4", "elastic_modulus_gpa": 110}},
            "candidate_set": {"schema_version": "latticegpt_candidate_set_v2", "candidates": [
                {"candidate_id": "inverse_150w_0001", "predicted_stiffness": VECTOR, "relative_density": .2, "rank": 1,
                 "eligible": True, "normalized_diameter": .25, "nodes": [[0, 0, 0], [1, 1, 1]], "connections": [[0, 1]], "nmse": .007, "score": 5823}], "recommended_candidate_id": "inverse_150w_0001"},
            "selection": {"selected_candidate_id": "inverse_150w_0001", "selection_sha256": "D"*64},
            "attempts": [{"attempt_id": PACKAGE, "package_id": PACKAGE, "status": "VIEWER_READY", "conformal_output_dir": str(self.folder),
                          "package_verification": {"package_id": PACKAGE, "status": "passed"}}]}
        first = self.job["candidate_set"]["candidates"][0]
        for rank in (2, 3):
            self.job["candidate_set"]["candidates"].append(dict(first, rank=rank, nmse=rank*.01,
                                                          candidate_id=f"inverse_150w_{rank:04d}"))
        collection = self.job["candidate_set"]
        collection["candidate_set_sha256"] = canonical_sha256(collection)
        app = create_product_viewer_app(config, preflight=viewer_preflight())
        self.client = app.test_client()
        self.store = app.extensions["latticegpt_job_store"]
        read = patch.object(self.store, "get_consistent_for_viewer", return_value=self.job)
        read.start()
        self.addCleanup(read.stop)

    def path(self, role):
        return self.folder / PRODUCT_FILE_MAP[role]

    def replace(self, role, data):
        self.path(role).write_bytes(data)
        key = "legacy_metadata" if role == "metadata" else role
        self.manifest["outputs"][key]["sha256"] = sha(data)
        self.path("package_metadata").write_text(json.dumps(self.manifest), encoding="utf-8")

    def package(self):
        return resolve_product_package(self.store, JOB, self.folder.parent.parent)

    def public_url(self, role):
        return endpoint(self.job, self.package(), "deliver", role)

    def diagnostic(self, role):
        # Diagnostics still have internal projection tests, while their former public routes must refuse.
        return build_user_export(self.package(), self.job, role)[2]

    def test_direct_old_urls_are_closed_and_deliveries_describe_served_bytes(self):
        prefix = f"/api/viewer/product/jobs/{JOB}"
        before = {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
        for role in (*PRODUCT_FILE_MAP, *PUBLIC_FILES):
            with self.subTest(retired=role):
                self.assertEqual(self.client.get(prefix + "/files/" + role).status_code, 404)
        for role in PRODUCT_FILE_MAP:
            self.assertEqual(self.client.get(prefix + "/deliver/" + role).status_code, 404)
        for role, (filename, mime) in PUBLIC_FILES.items():
            with self.subTest(role=role), self.client.get(self.public_url(role)) as response:
                self.assertEqual(response.status_code, 200, response.data[:200])
                self.assertEqual(sha(response.data), response.get_etag()[0].upper())
                self.assertEqual(len(response.data), response.content_length)
                self.assertEqual(response.mimetype, mime)
                self.assertIn(filename, response.headers["Content-Disposition"])
                self.assertNotIn(b"SYNTHETIC_SENTINEL", response.data)
                if role == "conformal_inp":
                    self.assertEqual(response.data, B31_GEOMETRY)
        for role in ("metadata", "package_metadata", "run_summary"):
            data = self.diagnostic(role)
            self.assertNotIn(b"SYNTHETIC_SENTINEL", data)
            self.assertIn(b"The model uses the specified diameter.", data)
            self.assertIn(b"No interface node set was supplied or inferred.", data)
            self.assertIn(b"0.007", data)
            self.assertNotIn(b"5823", data)
        self.assertEqual(before, {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()})

    def test_scientific_content_and_fixed_diagnostic_schema(self):
        summary = json.loads(self.diagnostic("metadata"))
        self.assertEqual(summary["job_id"], JOB)
        self.assertEqual(summary["target"]["physical_9d_gpa"], VECTOR)
        self.assertEqual(summary["target"]["stiffness_unit"], "GPa")
        self.assertEqual(summary["candidates"][0]["predicted_9d_gpa"], VECTOR)
        self.assertEqual(summary["candidates"][0]["relative_density"], .2)
        self.assertEqual(summary["candidates"][0]["rank"], 1)
        self.assertEqual(summary["geometry"]["diameter_unit"], "mm")
        self.assertEqual(summary["geometry"]["beam_count"], 1)
        self.assertEqual(summary["geometry"]["b31_beam_count"], 2)
        self.assertEqual(summary["geometry"]["b31_node_count"], 3)
        self.assertEqual(summary["geometry"]["node_count"], 2)
        self.assertTrue(summary["candidates"][0]["eligible"])
        self.assertEqual(summary["geometry"]["bbox"]["size"], [1,1,1])
        self.assertEqual(set(summary["files"]), {"b31","stl","png","source_mesh","geometry_ledger","source_element_map"})
        ledger = json.loads(self.diagnostic("geometry_ledger"))
        expected = deepcopy(LEDGER)
        del expected["unknown_internal"], expected["source_geometry"]["log"]
        expected.update(non_claims=["NOT_FEA_VALIDATION"], source_schema_version=expected["schema_version"],
                        schema_version="bonemetagpt_geometry_diagnostics_v1")
        self.assertEqual(ledger, expected)

    def test_genuine_p10_score_retained_and_missing_units_explicit(self):
        self.job["candidate_set"]["schema_version"] = "latticegpt_candidate_set_v1"
        metadata = json.loads(self.path("metadata").read_bytes())
        del metadata["traceability"]
        self.replace("metadata", json.dumps(metadata).encode())
        summary = self.diagnostic("run_summary")
        self.assertIn(b"5823", summary)
        self.assertNotIn(b"0.007", summary)
        self.assertIsNone(json.loads(self.diagnostic("metadata"))["geometry"]["diameter_unit"])

    def test_invalid_csv_cells_and_extra_columns_reject_before_response(self):
        for row in (ROW.replace(b"11 12", b"=opaque 12"), ROW.replace(b"41,", b"../,"), ROW.replace(b"true,1", b"false,1"),
                    ROW.replace(b"true,1", b"true,0"), ROW.replace(b"11 12 13 14 15 16 17 18", b"11"),
                    ROW.replace(b"C3D8R,", b"TOKEN,"), ROW.replace(b"17 18", b"18"), ROW.rstrip() + b",extra\n"):
            with self.subTest(row=row):
                self.replace("source_element_map", HEADER + row)
                with self.assertRaises(PublicExportUnavailable):
                    self.diagnostic("source_element_map")
                response = self.client.get(f"/api/viewer/product/jobs/{JOB}/files/source_element_map")
                self.assertEqual(response.status_code, 404)

    def test_failed_integrity_missing_and_path_guard_remain_typed(self):
        url = self.public_url("conformal_inp")
        self.path("run_summary").write_bytes(b"changed")
        self.assertEqual(self.client.get(url).json["code"], "FILE_IDENTITY_MISMATCH")
        self.replace("run_summary", PRIVATE.encode())
        self.path("metadata").unlink()
        self.assertEqual(self.client.get(url).json["code"], "FILE_MISSING")
        self.job["attempts"][0]["conformal_output_dir"] = str(self.folder.parents[3])
        self.assertEqual(self.client.get(url).json["code"], "PACKAGE_CONTRACT_INVALID")
        for path in ("unknown", "..%2Fmetadata.json", "metadata.json"):
            self.assertEqual(self.client.get(f"/api/viewer/product/jobs/{JOB}/deliver/{path}").status_code, 404)

    def test_valid_opaque_identifiers_and_role_failure_isolation(self):
        for candidate_id in ("cell-B17", "unit_cell_04"):
            self.job["candidate_set"]["candidates"][0]["candidate_id"] = candidate_id
            self.job["selection"]["selected_candidate_id"] = candidate_id
            self.assertEqual(json.loads(self.diagnostic("metadata"))["selected_candidate_id"], candidate_id)
        bad = deepcopy(LEDGER)
        bad["output_geometry"]["beam_length"]["mean"] = PRIVATE
        self.replace("geometry_ledger", json.dumps(bad).encode())
        package = resolve_product_package(self.store, JOB, self.folder.parent.parent)
        files = build_export_identities(package, self.job)
        self.assertNotIn("geometry_ledger", files)
        self.assertIn("source_element_map", files)
        with self.assertRaises(PublicExportUnavailable):
            self.diagnostic("geometry_ledger")
        self.assertEqual(json.loads(self.diagnostic("metadata"))["unavailable_diagnostics"], ["geometry_ledger"])
        self.assertEqual(self.diagnostic("source_element_map"), HEADER + ROW)
        self.job["target"]["physical_9d_gpa"]["C11"] = PRIVATE
        files = build_export_identities(package, self.job)
        self.assertEqual(set(files), {"b31","stl","png","source_mesh","source_element_map"})
        with self.assertRaises(PublicExportUnavailable):
            self.diagnostic("metadata")
        with self.client.get(self.public_url("conformal_inp")) as response:
            self.assertEqual(response.status_code, 200)

    def test_raw_snapshot_resists_replacement_and_supports_stream_ranges_and_head(self):
        import product_viewer_exports as exports
        url = self.public_url("conformal_inp")
        # Pad the private heading; the public stream is only the independently known engineering data.
        self.replace("b31", b"*Heading\n" + b"0" * (8 * 1024 * 1024 + 1) + b"\n" + B31_GEOMETRY)
        with self.client.head(url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.content_length, response.data), (len(B31_GEOMETRY), b""))
        with self.client.get(url, headers={"Range": "bytes=3-6"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, B31_GEOMETRY[3:7])
            self.assertEqual(response.headers["Content-Range"], f"bytes 3-6/{len(B31_GEOMETRY)}")
            self.assertEqual(response.get_etag()[0].upper(), sha(B31_GEOMETRY))
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=99999999999-"}).status_code, 416)
        resolve = exports.resolve_product_package_file
        def swap_after_check(package, role):
            checked = resolve(package, role)
            checked.write_bytes(b"swapped")
            return checked
        with patch.object(exports, "resolve_product_package_file", side_effect=swap_after_check):
            response = self.client.get(url)
        self.assertEqual(response.json["code"], "FILE_IDENTITY_MISMATCH")
        self.assertNotIn(b"swapped", response.data)

    def test_job_change_and_metadata_change_during_derived_read_fail_closed(self):
        import product_viewer_exports as exports
        from product_workflow.artifacts import ProductFileIdentityError
        url = self.public_url("conformal_inp")
        before = deepcopy(self.job)
        after = dict(before, revision=before["revision"] + 1)
        with patch.object(self.store, "get_consistent_for_viewer", side_effect=[before, before, after]):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["code"], "FILE_IDENTITY_MISMATCH")
        resolve = exports.resolve_product_package_file
        def swap_after_check(package, role):
            path = resolve(package, role)
            if role == "metadata":
                metadata = json.loads(path.read_bytes())
                metadata["node_count"] = 837291
                path.write_text(json.dumps(metadata), encoding="utf-8")
            return path
        with patch.object(exports, "resolve_product_package_file", side_effect=swap_after_check):
            with self.assertRaises(ProductFileIdentityError):
                self.diagnostic("metadata")
        response = self.client.get(url)
        self.assertEqual(response.json["code"], "FILE_IDENTITY_MISMATCH")
        self.assertNotIn(b"837291", response.data)

    def test_snapshot_role_job_revision_and_candidate_bindings_fail_closed(self):
        urls = {role: self.public_url(role) for role in PUBLIC_FILES}
        for role, url in urls.items():
            with self.subTest(role=role):
                for invalid in (url.split("?")[0], url.replace("snapshot=", "snapshot=0"),
                                url.split("?")[0] + "?" + next(u for k,u in urls.items() if k != role).split("?")[1]):
                    response = self.client.get(invalid)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json["code"], "FILE_IDENTITY_MISMATCH")
                wrong_job = url.replace(JOB, "job-" + "F" * 24)
                self.assertNotEqual(self.client.get(wrong_job).status_code, 200)
        self.job["revision"] += 1
        for role, url in urls.items():
            self.assertEqual(self.client.get(url).status_code, 409)
            self.assertEqual(self.client.get(self.public_url(role)).status_code, 200)
        collection = self.job["candidate_set"]
        identity = collection.pop("candidate_set_sha256")
        for role in ("unit_png", "unit_stl"):
            self.assertEqual(self.client.get(self.public_url(role)).json["code"], "FILE_MISSING")
        collection["candidate_set_sha256"] = identity
        collection["candidates"][0]["rank"] = 888
        for role in ("unit_png", "unit_stl"):
            self.assertEqual(self.client.get(self.public_url(role)).json["code"], "FILE_IDENTITY_MISMATCH")
        self.assertEqual(self.client.get(self.public_url("conformal_inp")).status_code, 200)
