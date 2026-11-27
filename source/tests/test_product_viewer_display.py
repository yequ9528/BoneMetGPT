"""Exercise the actual product read builder using synthetic on-disk records."""
import hashlib
import json
import unittest
from copy import deepcopy

from product_viewer_payloads import build_product_job_viewer_payload
from tests import test_product_viewer_job_payload as fixtures


MARK = "private-diagnostic-marker"
PRIVATE = {"opaque_extension": MARK, "value": "C:/scratch/" + MARK}


class ProductDisplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProductViewerJobPayloadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store

    def read(self, job, **kwargs):
        return build_product_job_viewer_payload(job["job_id"], store=self.store,
            conformal_root=self.fixture.conformal_root, **kwargs)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.fixture.root.rglob("*") if p.is_file()}

    def test_candidate_progress_and_target_records_are_reconstructed_on_every_read(self):
        job = self.fixture._candidate_job()
        candidate = job["candidate_set"]["candidates"][1]
        value = .12345678901234567
        candidate.update(nmse=value, exclusion_reasons=["CAD_READY_REQUIRED", PRIVATE, MARK],
                         predicted_stiffness={"C11": value, "other": PRIVATE}, geometry_contract=PRIVATE)
        job["target"]["physical_9d_gpa"].update(C11=value, other=PRIVATE)
        job["target"]["source"]["kind"] = MARK
        job["candidate_set"].update(p10_contract=PRIVATE, non_claims=["NOT_FEA_VALIDATION", "NOT_" + MARK])
        job["status"] = "FAILED"
        job["workflow_disposition"] = "HOLD"
        job["failure"] = {"stage": "C:/scratch/" + MARK, "message": MARK}
        self.store.replace(job, expected_revision=job["revision"], event=MARK)
        before = self.snapshot()
        first = self.read(job)
        self.assertEqual(first, self.read(job))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(first["target"]["physical_9d_gpa"], {"C11": value})
        self.assertEqual(first["candidates"][1]["predicted_stiffness"], {"C11": value})
        self.assertEqual(first["candidates"][1]["nmse"], value)
        self.assertIn("CAD_READY_REQUIRED", first["candidates"][1]["exclusion_reasons"])
        self.assertIn("NOT_FEA_VALIDATION", first["non_claims"])
        self.assertNotIn(MARK, json.dumps(first))

    def test_selected_domain_reference_preserves_groups_ranges_and_checksums(self):
        from tests.test_product_mesh_domain import DomainWorkflowTests, MIXED
        synthetic = DomainWorkflowTests()
        synthetic.setUp()
        self.addCleanup(synthetic.doCleanups)
        job = synthetic.confirm(synthetic.propose(MIXED))
        store = synthetic.service.store
        root = synthetic.service.runtime_root
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        ordinary = build_product_job_viewer_payload(job["job_id"], store=store)["mesh"]
        mesh = build_product_job_viewer_payload(job["job_id"], store=store, include_original_mesh=True)["mesh"]
        self.assertNotIn("original_visualization", ordinary)
        self.assertNotIn("raw_mesh_relative_path", json.dumps(mesh))
        reference = mesh["original_visualization"]
        self.assertEqual(reference["source_sha256"], hashlib.sha256(MIXED).hexdigest().upper())
        self.assertEqual(reference["binding_sha256"], mesh["mesh_binding_sha256"])
        self.assertEqual(reference["groups"]["selected"]["element_ids"], [101])
        self.assertEqual(reference["groups"]["excluded_other"]["element_ids"], [201])
        self.assertEqual(reference["groups"]["excluded_solid"]["element_ids"], [301])
        self.assertEqual(mesh["design_domain"]["excluded_types"]["S4R"]["element_ids"], [201])
        self.assertEqual(mesh["design_domain"]["original_bounds_mm"], [reference["bbox"]["min"], reference["bbox"]["max"]])
        self.assertEqual(mesh["visualization"]["elements"][0]["node_ids"], list(range(1, 9)))
        self.assertEqual(synthetic.adapter_calls, [])
        self.assertEqual(synthetic.conformal_calls, [])
        self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_invalid_recorded_candidate_prediction_still_fails_closed(self):
        job = self.fixture._candidate_job()
        job["candidate_set"]["candidates"][0]["predicted_stiffness"] = {"C11": PRIVATE}
        self.store.replace(job, expected_revision=job["revision"], event="candidates_ready")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.read(job)
        self.assertEqual(before, self.snapshot())

    def _ready(self):
        job = self.fixture._mesh_job()
        self.fixture._current_job_id = job["job_id"]
        package_id = job["job_id"] + "-attempt-0001"
        folder = self.fixture._write_package(package_id)
        from product_workflow.artifacts import PRODUCT_FILE_MAP
        from product_workflow.contracts import canonical_sha256
        from tests.test_viewer_delivery_roundtrip import B31_SOURCE
        job["candidate_set"]["candidate_set_sha256"] = canonical_sha256(job["candidate_set"])
        manifest = folder / PRODUCT_FILE_MAP["package_metadata"]
        contract = json.loads(manifest.read_bytes())
        (folder / PRODUCT_FILE_MAP["b31"]).write_bytes(B31_SOURCE)
        contract["outputs"]["b31"]["sha256"] = hashlib.sha256(B31_SOURCE).hexdigest().upper()
        manifest.write_text(json.dumps(contract), encoding="utf-8")
        job.update(status="VIEWER_READY", attempts=[dict(attempt_id=package_id, package_id=package_id,
            status="VIEWER_READY", conformal_output_dir=str(folder),
            package_verification={"status": "passed", "package_id": package_id})])
        self.store.replace(job, expected_revision=job["revision"], event="viewer_ready")
        return job, folder

    def test_real_p13_read_hides_unknown_nested_fields_and_preserves_verified_identity(self):
        job, folder = self._ready()
        meta_path, contract_path, ledger_path = (folder / n for n in
            ("metadata.json", "package_metadata.json", "geometry_ledger.json"))
        meta, contract, ledger = (json.loads(p.read_bytes()) for p in (meta_path, contract_path, ledger_path))
        meta["product_context"]["target"]["private"] = PRIVATE
        meta["product_context"]["candidate_prediction_9d_gpa"] = {"C11": 1.25, MARK: PRIVATE}
        # Additional known field names in a frozen package cannot introduce unrelated identities.
        meta["product_context"]["recommended_candidate_id"] = MARK
        meta["product_context"]["mesh_original_filename"] = MARK
        meta["traceability"]["source_candidate_id"] = MARK
        meta["traceability"]["debug_binding"] = PRIVATE
        meta.update(warnings=[MARK], diameter_source=PRIVATE, cell_type=PRIVATE)
        ledger.update(output_geometry={"beam_count": 7, "detail": PRIVATE}, private=PRIVATE)
        contract["claims"] = {"non_claims": ["NOT_FEA_VALIDATION", "NOT_" + MARK], "internal": PRIVATE}
        contract["package"]["other"] = PRIVATE
        contract["diameter"] = {"physical_value": .12345678901234567, "private": PRIVATE}
        for path, document, role in ((meta_path, meta, "legacy_metadata"), (ledger_path, ledger, "geometry_ledger")):
            path.write_text(json.dumps(document), encoding="utf-8")
            contract["outputs"][role]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        before = self.snapshot()
        result = self.read(job, include_original_mesh=True)
        self.assertEqual(result, self.read(job, include_original_mesh=True))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(MARK, json.dumps(result))
        evidence = result["result"]["p13_evidence"]
        self.assertEqual(evidence["integrity_status"], "PASS")
        self.assertEqual(evidence["package_contract"]["package"]["package_id"], result["result"]["package_id"])
        self.assertEqual(evidence["geometry_ledger"]["output_geometry"]["beam_count"], 7)
        self.assertEqual(evidence["diameter"]["physical_value"], .12345678901234567)
        self.assertIn("NOT_FEA_VALIDATION", evidence["non_claims"])
        self.assertEqual(result["mesh"]["original_visualization"]["node_count"], 8)

    def test_recorded_legacy_diameter_sources_preserve_only_verified_units(self):
        job, folder = self._ready()
        meta_path, contract_path = folder / "metadata.json", folder / "package_metadata.json"
        metadata, contract = (json.loads(path.read_bytes()) for path in (meta_path, contract_path))
        for source, label, unit in (
                ("user_provided", "User-provided diameter", "mm"),
                ("bbox_max_dim * 0.03", "3% of the maximum bounding-box dimension", "µm"),
                ("user_provided", "User-provided diameter", None)):
            with self.subTest(source=source, unit=unit):
                metadata.update(diameter=.145678901234567, diameter_source=source, diameter_unit=unit)
                meta_path.write_text(json.dumps(metadata), encoding="utf-8")
                contract["outputs"]["legacy_metadata"]["sha256"] = hashlib.sha256(meta_path.read_bytes()).hexdigest().upper()
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
                before = self.snapshot()
                parameters = self.read(job)["result"]["parameters"]
                self.assertEqual(parameters["diameter"], .145678901234567)
                self.assertEqual(parameters.get("diameter_source"), label)
                self.assertEqual(parameters.get("diameter_unit"), unit)
                self.assertEqual(before, self.snapshot())

    def test_unavailable_optional_export_does_not_remove_verified_models(self):
        job, folder = self._ready()
        # The scientific package is internally consistent, but the optional public ledger
        # has a historical metric classification outside the current display vocabulary.
        ledger_path = folder / "geometry_ledger.json"
        contract_path = folder / "package_metadata.json"
        contract, ledger = (json.loads(p.read_bytes()) for p in (contract_path, ledger_path))
        ledger["evidence_level"] = MARK
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        contract["outputs"]["geometry_ledger"]["sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest().upper()
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        before = self.snapshot()
        result = self.read(job)["result"]
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(MARK, json.dumps(result))
        self.assertEqual(result["p13_evidence"]["integrity_status"], "PASS")
        expected = {"unit_png", "unit_stl", "conformal_inp"}
        self.assertEqual(result["unavailable_downloads"], [])
        self.assertEqual(set(result["downloads"]), expected)
        self.assertEqual(set(result["download_identities"]), expected)
        self.assertEqual(set(result["scene"]), {"triangles", "poster"})

    def test_custom_mesh_and_result_builder_cannot_bypass_display_boundary(self):
        job, _ = self._ready()
        mesh = self.fixture.source_mesh_builder("synthetic")
        mesh["nodes"][0].update(xyz=[.12345678901234567, 1.0, -4.2], debug=PRIVATE)
        mesh["elements"][0]["diagnostic"] = PRIVATE
        mesh["bbox"]["debug"] = PRIVATE
        mesh["debug"] = PRIVATE
        before_mesh = deepcopy(mesh)
        def custom(package):
            value = self.fixture.conformal_payload_builder(package)
            value.update(debug=PRIVATE, compatibility={"debug": PRIVATE})
            value["downloads"]["arbitrary"] = MARK
            value["downloads"]["unit_png"] = "https://invalid/" + MARK
            value["downloads"]["png"] = "https://invalid/" + MARK
            return value
        before = self.snapshot()
        payload = self.read(job, source_mesh_builder=lambda _: mesh, conformal_payload_builder=custom)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(mesh, before_mesh)
        self.assertNotIn(MARK, json.dumps(payload))
        projected = payload["mesh"]["visualization"]
        self.assertEqual(projected["nodes"][0], {"id": 1, "xyz": [.12345678901234567, 1.0, -4.2]})
        self.assertEqual(projected["elements"][0]["node_ids"], list(range(1, 9)))
        self.assertEqual(projected["bbox"]["max"], [1.0, 1.0, 1.0])
        from urllib.parse import urlsplit
        result = payload["result"]
        self.assertEqual(set(result["downloads"]), {"unit_png", "unit_stl", "conformal_inp"})
        for role, url in result["downloads"].items():
            parts = urlsplit(url)
            self.assertFalse(parts.netloc)
            self.assertEqual(parts.path, "/api/viewer/product/jobs/" + job["job_id"] + "/deliver/" + role)
            self.assertRegex(parts.query, r"^snapshot=[A-F0-9]{64}$")
