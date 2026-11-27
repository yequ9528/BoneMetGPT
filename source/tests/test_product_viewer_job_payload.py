import json
import hashlib
import os
import tempfile
from copy import deepcopy
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from product_workflow.store import JobStore
from product_workflow.job_state import build_product_job_projection


class _SourceMeshBuilder:
    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return {
            "status": "available",
            "representation": "hex_wireframe",
            "element_type": "C3D8R",
            "node_count": 8,
            "element_count": 1,
            "nodes": [{"id": 1, "xyz": [0.0, 0.0, 0.0]}],
            "elements": [
                {
                    "id": 1,
                    "type": "C3D8R",
                    "node_ids": [1, 2, 3, 4, 5, 6, 7, 8],
                }
            ],
            "edges": [[1, 2]],
            "bbox": {
                "min": [0.0, 0.0, 0.0],
                "max": [1.0, 1.0, 1.0],
                "size": [1.0, 1.0, 1.0],
            },
        }


class _ConformalPayloadBuilder:
    def __init__(self):
        self.package_ids = []

    def __call__(self, package):
        package_id = package.package_id
        self.package_ids.append(package_id)
        return {
            "status": "success",
            "mode": "conformal",
            "package_id": package_id,
            "input_mesh": {
                "path": "input/source_c3d8.inp",
                "element_type": "C3D8R",
                "node_count": 8,
                "element_count": 1,
            },
            "core_output": {
                "type": "B31",
                "path": "artifacts/conformal_b31.inp",
                "role": "engineering_output",
            },
            "visualization": {
                "stl_url": f"/api/conformal/packages/{package_id}/files/stl",
                "png_url": f"/api/conformal/packages/{package_id}/files/png",
                "role": "derived_visualization",
            },
            "downloads": {
                "b31": f"/api/conformal/packages/{package_id}/files/b31",
                "stl": f"/api/conformal/packages/{package_id}/files/stl",
                "png": f"/api/conformal/packages/{package_id}/files/png",
            },
            "warnings": ["not FEA validation"],
        }


class ProductViewerJobPayloadTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT") or Path(__file__).resolve().parent)
        self.root = scratch / f"viewer-{uuid.uuid4().hex[:16]}"
        self.store = JobStore(self.root)
        self.conformal_root = self.root / "conformal"
        self.source_mesh_builder = _SourceMeshBuilder()
        self.conformal_payload_builder = _ConformalPayloadBuilder()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _build_payload(self, job_id):
        module_path = Path(__file__).resolve().parents[1] / "product_viewer_payloads.py"
        self.assertTrue(
            module_path.is_file(),
            "product_viewer_payloads.py is missing",
        )
        import product_viewer_payloads

        return product_viewer_payloads.build_product_job_viewer_payload(
            job_id,
            store=self.store,
            conformal_root=self.conformal_root,
            source_mesh_builder=self.source_mesh_builder,
            conformal_payload_builder=self.conformal_payload_builder,
        )

    def _create_job(self):
        return self.store.create(
            {
                "proposal_sha256": "A" * 64,
                "range_status": "PASS",
                "source": {
                    "kind": "formal_150w_query",
                    "sample_id": 42,
                    "private_path": "C:/private/target.csv",
                    "base_url": "http://private-model",
                    "planner_evidence_sha256": "9" * 64,
                },
                "physical_9d_gpa": {"C11": 50.0},
                "normalized_9d": {"C11": 0.5},
            },
            model_runtime_profile={"profile_sha256": "B" * 64},
            formal_bundle_binding={"binding_sha256": "C" * 64},
        )

    @staticmethod
    def _candidates():
        candidates = []
        for index in range(1, 4):
            candidates.append({
                "candidate_id": f"candidate-{index}",
                "rank": index,
                "eligible": True,
                "normalized_diameter": 0.1 * index,
                "nodes": [
                    [0.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                ],
                "connections": [[0, 1], [0, 2]],
                "raw_files": ["C:/private/candidate.npy"],
                "model_service_url": "http://127.0.0.1:9999",
            })
        return candidates

    def _candidate_job(self):
        job = self._create_job()
        job["status"] = "CANDIDATES_READY"
        job["candidate_set"] = {
            "recommended_candidate_id": "candidate-1",
            "candidates": self._candidates(),
            "non_claims": [
                "NOT_MECHANICAL_PASS",
                "NOT_OOD_PASS",
                "NOT_FEA_VALIDATION",
            ],
        }
        job["selection"] = {
            "recommended_candidate_id": "candidate-1",
            "selected_candidate_id": "candidate-2",
            "selected_rank": 2,
            "selection_source": "user_override",
            "selection_sha256": "D" * 64,
        }
        return self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_candidates_ready",
        )

    def _mesh_job(self):
        job = self._candidate_job()
        job["status"] = "AWAITING_CONFORMAL_AUTHORIZATION"
        from tests.test_product_workflow_mesh_runtime import VALID_INP
        from product_workflow.contracts import canonical_sha256
        from product_workflow.manifest_io import sha256_bytes
        from latticegpt_9x_mesh_precheck import precheck_inp_mesh
        mesh_bytes = VALID_INP.replace(b'type=C3D8', b'type=C3D8R')
        stored = self.store.job_directory(job['job_id']) / 'source.inp'
        stored.write_bytes(mesh_bytes)
        precheck = precheck_inp_mesh(stored, selector_type='WHOLE_FILE_IMPLANT_DOMAIN', selector_value='ALL_PARSED_ELEMENTS', selector_explicit_declaration=True, selector_purpose='PRODUCT_WHOLE_FILE_IMPLANT_CONFIRMATION')
        job["mesh_binding"] = {
            "schema_version": "latticegpt_mesh_binding_v1",
            "original_filename": "implant.inp",
            "stored_path": str(stored),
            "sha256": sha256_bytes(mesh_bytes),
            "size_bytes": len(mesh_bytes),
            "upload_id": "upload-0001",
            "mesh_binding_sha256": "F" * 64,
            "element_type_original": "C3D8R",
            "element_type_effective": "C3D8_GEOMETRY_COMPATIBLE",
            "node_count": 8,
            "element_count": 1,
            "units": "mm",
            "frame": "source_native_cartesian_frame",
            "precheck": precheck,
        }
        job['mesh_binding']['mesh_binding_sha256'] = canonical_sha256({k: v for k, v in job['mesh_binding'].items() if k != 'mesh_binding_sha256'})
        return self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_mesh_ready",
        )

    def _write_package(self, package_id):
        package_dir = self.conformal_root / self._current_job_id / package_id
        package_dir.mkdir(parents=True)
        from tests.test_product_artifact_resolution import write_p13_fixture
        write_p13_fixture(package_dir, package_id)
        job = self.store.get_consistent_for_viewer(self._current_job_id)
        mesh, selection = job['mesh_binding'], job['selection']
        source = package_dir / 'input/source_c3d8.inp'
        source.write_bytes(Path(mesh['stored_path']).read_bytes())
        metadata_path = package_dir / 'metadata.json'
        metadata = json.loads(metadata_path.read_text())
        metadata.update(product_mesh_binding=mesh, product_context={
            'schema_version': 'latticegpt_product_viewer_context_v1',
            'job_id': job['job_id'], 'selected_candidate_id': selection['selected_candidate_id'],
            'selection_sha256': selection['selection_sha256'],
            'mesh_binding_sha256': mesh['mesh_binding_sha256'], 'mesh_sha256': mesh['sha256'],
            'target': {'target_sha256': job['target']['proposal_sha256']},
        }, traceability={
            'job_id': job['job_id'], 'candidate_id': selection['selected_candidate_id'],
            'selection_manifest_sha256': selection['selection_sha256'],
            'target_sha256': job['target']['proposal_sha256'],
            'case_manifest_sha256': mesh['mesh_binding_sha256'],
        })
        metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
        identity_path = package_dir / 'package_metadata.json'
        identity = json.loads(identity_path.read_text())
        identity['source_mesh']['package_copy_sha256'] = mesh['sha256']
        identity['outputs']['legacy_metadata']['sha256'] = hashlib.sha256(metadata_path.read_bytes()).hexdigest().upper()
        identity_path.write_text(json.dumps(identity), encoding='utf-8')
        return package_dir

    def test_p13_original_metadata_mismatch_is_not_hidden_by_projection(self):
        from product_workflow.artifacts import resolve_product_package
        job = self._mesh_job()
        self._current_job_id = job['job_id']
        package_id = job['job_id'] + '-attempt-0001'
        package_dir = self._write_package(package_id)
        job['status'] = 'VIEWER_READY'
        job['attempts'] = [{'attempt_id': package_id, 'package_id': package_id,
                            'status': 'VIEWER_READY', 'conformal_output_dir': str(package_dir),
                            'package_verification': {'status': 'passed', 'package_id': package_id}}]
        self.store.replace(job, expected_revision=job['revision'], event='fixture_viewer_ready')
        metadata_path = package_dir / 'metadata.json'
        original = json.loads(metadata_path.read_text())
        identity_path = package_dir / 'package_metadata.json'
        identity = json.loads(identity_path.read_text())
        for field in ('mesh', 'context_job', 'context_target', 'trace_selection', 'source_copy'):
            with self.subTest(field=field):
                metadata = deepcopy(original)
                source = package_dir / 'input/source_c3d8.inp'
                source.write_bytes(Path(job['mesh_binding']['stored_path']).read_bytes())
                if field == 'mesh':
                    metadata['product_mesh_binding']['mesh_binding_sha256'] = '0' * 64
                elif field == 'context_job':
                    metadata['product_context']['job_id'] = 'job-' + '0' * 24
                elif field == 'context_target':
                    metadata['product_context']['target']['target_sha256'] = '0' * 64
                elif field == 'trace_selection':
                    metadata['traceability']['selection_manifest_sha256'] = '0' * 64
                else:
                    source.write_bytes(b'different mesh bytes')
                metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
                identity['outputs']['legacy_metadata']['sha256'] = hashlib.sha256(metadata_path.read_bytes()).hexdigest().upper()
                identity['source_mesh']['package_copy_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest().upper()
                identity_path.write_text(json.dumps(identity), encoding='utf-8')
                with self.assertRaises(ValueError):
                    resolve_product_package(self.store, job['job_id'], self.conformal_root)

    def test_candidates_ready_maps_to_safe_candidate_state(self):
        job = self._candidate_job()

        payload = self._build_payload(job["job_id"])

        self.assertEqual(
            payload["schema_version"],
            "latticegpt_product_viewer_state_v1",
        )
        self.assertEqual(payload["view_state"], "candidate")
        self.assertEqual(payload["revision"], job["revision"])
        self.assertEqual(payload["updated_at"], job["updated_at"])
        self.assertEqual(payload["workflow_disposition"], "ACTIVE")
        self.assertEqual(
            payload["view_state"],
            build_product_job_projection(job)["view_state"],
        )
        self.assertTrue(payload["capabilities"]["view_in_viewer"])
        self.assertFalse(payload["capabilities"]["start_conformal"])
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertEqual(
            set(payload["candidates"][0]),
            {
                "candidate_id",
                "rank",
                "eligible",
                "recommended",
                "selected",
                "nodes",
                "connections",
                "diameter", "preview", "exclusion_reasons", "confirmed", "score", "ranking", "geometry_contract", "source_order", "nmse",
            },
        )
        self.assertTrue(payload["candidates"][0]["recommended"])
        self.assertTrue(payload["candidates"][1]["selected"])
        serialized = json.dumps(payload)
        self.assertNotIn("raw_files", serialized)
        self.assertNotIn("model_service_url", serialized)
        self.assertNotIn("C:/private", serialized)
        self.assertNotIn("127.0.0.1:9999", serialized)
        self.assertNotIn("base_url", serialized)
        self.assertNotIn("Traceback", serialized)
        self.assertEqual(
            payload["target"]["source"]["planner_evidence_sha256"],
            "9" * 64,
        )
        self.assertNotIn("planner_evidence", payload["target"]["source"])
        self.assertEqual(
            payload["dual_space_status"]["mechanical_space"]["status"],
            "CANDIDATES_RECORDED",
        )
        self.assertEqual(
            payload["dual_space_status"]["fusion_bridge"]["full_fusion_validation"],
            "NOT_VALIDATED",
        )
        self.assertIn("NOT_FULL_DUAL_SPACE_FUSION", payload["non_claims"])
        self.assertEqual(payload['generation_classification']['classification'], 'HOLD_UNKNOWN_JOB_GENERATION')
        self.assertFalse(payload['generation_classification']['runtime_permitted'])
        self.assertEqual(payload['selection']['confirmation_status'], 'UNCONFIRMED')
        self.assertIsNone(payload['selection']['confirmation'])

    def test_historical_v1_payload_retains_reproducibility_warning(self):
        job = self._candidate_job()
        job.update(source_status='PASS_R2_SOURCE_IDENTITY', source_prefix='release/v1/r2')
        self.store.replace(job, expected_revision=job['revision'], event='fixture_historical_identity')
        payload = self._build_payload(job['job_id'])
        self.assertEqual(payload['generation_classification']['classification'], 'HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED')
        self.assertFalse(payload['generation_classification']['runtime_permitted'])
        self.assertIn('REPRODUCIBILITY_NOT_VERIFIED', payload['non_claims'])

    def test_mesh_binding_before_attempt_maps_to_mesh_state(self):
        job = self._mesh_job()

        payload = self._build_payload(job["job_id"])

        self.assertEqual(payload["view_state"], "mesh")
        self.assertEqual(payload["mesh"]["status"], "available")
        self.assertEqual(payload["mesh"]["original_filename"], "implant.inp")
        self.assertEqual(payload["mesh"]["element_type_original"], "C3D8R")
        self.assertEqual(payload["mesh"]["node_count"], 8)
        self.assertEqual(payload["mesh"]["element_count"], 1)
        self.assertEqual(payload["mesh"]["precheck_status"], "passed")
        self.assertEqual(
            payload["mesh"]["visualization"]["status"],
            "available",
        )
        self.assertNotIn("stored_path", payload["mesh"])
        self.assertEqual(
            self.source_mesh_builder.paths,
            [str(self.store.job_directory(job['job_id']) / 'source.inp')],
        )
        self.assertEqual(
            payload["dual_space_status"]["geometric_space"]["status"],
            "SOURCE_MESH_RECORDED",
        )
        self.assertEqual(
            payload["dual_space_status"]["fusion_bridge"]["status"],
            "AWAITING_EXPLICIT_AUTHORIZATION",
        )

    def test_attempt_started_maps_to_running_state(self):
        job = self._mesh_job()
        job["status"] = "ADAPTER_RUNNING"
        job["attempts"] = [{
            "attempt_id": f"{job['job_id']}-attempt-0001",
            "package_id": f"{job['job_id']}-attempt-0001",
            "status": "ADAPTER_RUNNING",
            "adapter_output_dir": "C:/private/adapter",
            "conformal_output_dir": "C:/private/conformal",
        }]
        job = self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_adapter_started",
        )

        payload = self._build_payload(job["job_id"])

        self.assertEqual(payload["view_state"], "running")
        self.assertEqual(payload["progress"]["attempt_count"], 1)
        self.assertEqual(payload["progress"]["current_stage"], "ADAPTER_RUNNING")
        self.assertNotIn("C:/private", json.dumps(payload))
        self.assertEqual(
            payload["dual_space_status"]["fusion_bridge"]["status"],
            "AWAITING_EXPLICIT_AUTHORIZATION",
        )

    def test_verified_viewer_ready_job_maps_to_result_state(self):
        job = self._mesh_job()
        self._current_job_id = job["job_id"]
        package_id = f"{job['job_id']}-attempt-0001"
        package_dir = self._write_package(package_id)
        job["status"] = "VIEWER_READY"
        job["attempts"] = [{
            "attempt_id": package_id,
            "package_id": package_id,
            "status": "VIEWER_READY",
            "conformal_output_dir": str(package_dir),
            "package_verification": {
                "status": "passed",
                "package_id": package_id,
            },
        }]
        job = self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_viewer_ready",
        )

        payload = self._build_payload(job["job_id"])

        self.assertEqual(payload["view_state"], "result")
        self.assertEqual(payload["result"]["package_id"], package_id)
        self.assertEqual(
            self.conformal_payload_builder.package_ids,
            [package_id],
        )
        self.assertEqual(
            payload["dual_space_status"]["geometric_space"]["status"],
            "GEOMETRIC_OUTPUT_RECORDED",
        )
        self.assertEqual(
            payload["dual_space_status"]["fusion_bridge"]["status"],
            "TRACEABLE_BRIDGE_RECORDED",
        )
        self.assertEqual(
            payload["dual_space_status"]["output_and_visualization"]["status"],
            "READONLY_RESULT_AVAILABLE",
        )

    def test_failure_present_maps_to_failed_state(self):
        job = self._candidate_job()
        job["status"] = "FAILED"
        job["workflow_disposition"] = "HOLD"
        job["failure"] = {
            "stage": "INVERSE_RUNNING",
            "message": "fixture failure",
        }
        job = self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_failed",
        )

        payload = self._build_payload(job["job_id"])

        self.assertEqual(payload["view_state"], "failed")
        self.assertEqual(payload["progress"]["failure"]["stage"], "INVERSE_RUNNING")
        self.assertEqual(payload["result"], None)

    def test_result_rejects_package_identity_mismatch(self):
        job = self._mesh_job()
        self._current_job_id = job["job_id"]
        package_id = f"{job['job_id']}-attempt-0001"
        package_dir = self._write_package(package_id)
        job["status"] = "VIEWER_READY"
        job["attempts"] = [{
            "attempt_id": package_id,
            "package_id": package_id,
            "status": "VIEWER_READY",
            "conformal_output_dir": str(package_dir),
            "package_verification": {
                "status": "passed",
                "package_id": f"{job['job_id']}-attempt-9999",
            },
        }]
        job = self.store.replace(
            job,
            expected_revision=job["revision"],
            event="fixture_mismatch",
        )

        with self.assertRaisesRegex(ValueError, "package identity"):
            self._build_payload(job["job_id"])

    def test_rejects_invalid_and_missing_job_ids(self):
        with self.assertRaisesRegex(ValueError, "invalid job_id"):
            self._build_payload("../../latest")
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            self._build_payload("job-000000000000000000000000")

    def test_optional_target_display_fields_preserve_source_and_missing_values(self):
        job = self._candidate_job()
        payload = self._build_payload(job['job_id'])
        self.assertIsNone(payload['target']['base_material'])
        job['target']['base_material'] = {
            'name': 'TC4', 'elastic_modulus_gpa': 110, 'private_note': 'not projected',
        }
        job['target']['source'].update(name=r'C:\private\sample.csv', sha256='B' * 64)
        self.store.replace(job, expected_revision=job['revision'], event='fixture_display_source')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        payload = self._build_payload(job['job_id'])
        self.assertEqual(payload['target']['base_material'], {'name': 'TC4', 'elastic_modulus_gpa': 110})
        self.assertEqual(payload['target']['source']['source_name'], 'sample.csv')
        self.assertEqual(payload['target']['source']['source_sha256'], 'B' * 64)
        self.assertNotIn('private', json.dumps(payload['target']))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_target_alias_conflicts_and_invalid_material_fail_closed(self):
        from product_viewer_payloads import _build_target
        for source in (
            {'name': 'a.csv', 'source_name': 'b.csv'},
            {'sha256': 'A' * 64, 'source_sha256': 'B' * 64},
            {'source_sha256': 'invalid'},
            {'source_name': 42},
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                _build_target({'target': {'source': source}})
        for material in ({'name': 2}, {'elastic_modulus_gpa': float('nan')},
                         {'elastic_modulus_gpa': 0}, {'elastic_modulus_gpa': True}):
            with self.subTest(material=material), self.assertRaises(ValueError):
                _build_target({'target': {'base_material': material}})
        source = {'name': 'sample.csv', 'source_name': 'sample.csv', 'sha256': 'A' * 64, 'source_sha256': 'A' * 64}
        self.assertEqual(_build_target({'target': {'source': source}})['source']['source_name'], 'sample.csv')

    def test_p13_display_does_not_fabricate_six_file_identity(self):
        job = self._mesh_job()
        self._current_job_id = job['job_id']
        package_id = job['job_id'] + '-attempt-0001'
        package_dir = self._write_package(package_id)
        job['status'] = 'VIEWER_READY'
        job['attempts'] = [{'attempt_id': package_id, 'package_id': package_id,
                            'status': 'VIEWER_READY', 'conformal_output_dir': str(package_dir),
                            'package_verification': {'status': 'passed', 'package_id': package_id}}]
        self.store.replace(job, expected_revision=job['revision'], event='fixture_p13_display')
        from product_viewer_payloads import build_product_job_viewer_payload
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        payload = build_product_job_viewer_payload(job['job_id'], store=self.store, conformal_root=self.conformal_root)
        self.assertNotIn('file_identities', payload['result'])
        self.assertEqual(payload['result']['downloads'], {})
        self.assertEqual(payload['result']['download_identities'], {})
        self.assertEqual(set(payload['result']['unavailable_downloads']), {'unit_png', 'unit_stl', 'conformal_inp'})
        self.assertIsNone(payload['result']['compatibility'])
        self.assertNotEqual(payload['result']['p13_evidence']['status'], 'not_available_legacy_package')
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)


class ProductReadonlyDisplayReceiptTests(unittest.TestCase):
    def assert_public_deliveries(self, payload, package, receipt, job):
        from urllib.parse import urlsplit
        from product_viewer_exports import delivery
        from tests.test_viewer_delivery_roundtrip import PUBLIC_FILES
        result = payload["result"]
        self.assertNotIn("file_identities", result)
        self.assertEqual(package.file_identities, receipt["files"])
        self.assertEqual(set(result["downloads"]), set(PUBLIC_FILES))
        self.assertEqual(set(result["download_identities"]), set(PUBLIC_FILES))
        for role, (filename, _) in PUBLIC_FILES.items():
            raw = delivery(job, package, role)
            entry = result["download_identities"][role]
            self.assertEqual(entry, dict(filename=filename, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest().upper()))
            url = urlsplit(result["downloads"][role])
            self.assertEqual(url.path, "/api/viewer/product/jobs/" + job["job_id"] + "/deliver/" + role)
            self.assertRegex(url.query, r"^snapshot=[A-F0-9]{64}$")
        self.assertNotIn("active_release_binding_sha256", json.dumps(payload))
        self.assertNotIn("source_manifest_sha256", json.dumps(payload))
        self.assertNotIn("config_sha256", json.dumps(payload))

    def test_exact_registered_synthetic_legacy_retains_file_identity_and_readonly_boundary(self):
        from tests.test_viewer_delivery_roundtrip import make_sealed_service
        from product_viewer_payloads import build_product_job_viewer_payload
        from product_workflow.manifest_io import canonical_json_object_v1
        service, job = make_sealed_service()
        root = service.runtime_root.resolve()
        receipt = job['attempts'][0]['package_verification'].pop('readonly_receipt')
        job_path = service.store.job_directory(job['job_id']) / 'job.json'
        # This is an isolated synthetic old-format fixture, never a reference Job.
        job_path.write_bytes(canonical_json_object_v1(job))
        package = Path(job['attempts'][0]['conformal_output_dir']).resolve()

        def identity(path):
            raw = path.read_bytes()
            return {'path': path.resolve().relative_to(root).as_posix(),
                    'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest().upper()}

        package_paths = {role: package / record['path'] for role, record in receipt['files'].items()}
        entry = {
            'job': identity(job_path), 'events': identity(job_path.parent / 'events.jsonl'),
            'producer': receipt['producer'], 'package_id': receipt['package_id'],
            'package_format': 'LEGACY_SIX_FILES_V1',
            'allowed_job_extensions': sorted({'inverse_result', 'mesh_precheck'}.intersection(job)),
            'package_files': {role: identity(path) for role, path in package_paths.items()},
            'supporting_files': [identity(path) for path in root.rglob('*') if path.is_file()
                                and path not in {job_path, job_path.parent / 'events.jsonl', *package_paths.values()}],
        }
        policy = {'schema_version': 'latticegpt_viewer_readonly_compatibility_v1', 'jobs': {job['job_id']: entry}}
        store = JobStore(root, viewer_compatibility=policy,
                         reader_identity={'generation': 'r4_lite_single_reference_workstation_v12'})
        before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
        payload = build_product_job_viewer_payload(job['job_id'], store=store, conformal_root=service.conformal_output_root)
        self.assertEqual(payload['result']['compatibility']['package_format'], 'LEGACY_SIX_FILES_V1')
        from product_workflow.artifacts import resolve_product_package
        resolved = resolve_product_package(store, job["job_id"], service.conformal_output_root)
        self.assert_public_deliveries(payload, resolved, receipt, job)
        self.assertFalse(payload['generation_classification']['runtime_permitted'])
        self.assertTrue(all(value is False for name, value in payload['capabilities'].items()
                            if name not in ('refresh', 'view_in_viewer')))
        self.assertEqual({path: path.read_bytes() for path in root.rglob('*') if path.is_file()}, before)
        store.viewer_compatibility['jobs'][job['job_id']]['job']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'identity'):
            build_product_job_viewer_payload(job['job_id'], store=store, conformal_root=service.conformal_output_root)
        self.assertEqual({path: path.read_bytes() for path in root.rglob('*') if path.is_file()}, before)

    def test_v2_cross_generation_payload_projects_sealed_fields_without_writes(self):
        from tests.test_viewer_delivery_roundtrip import make_sealed_service
        from product_viewer_payloads import build_product_job_viewer_payload
        from product_workflow.artifacts import resolve_product_package
        service, job = make_sealed_service()
        self.assertEqual(job['schema_version'], 'latticegpt_design_job_v2')
        store = JobStore(service.runtime_root)
        store.reader_identity = {'generation': 'r4_lite_single_reference_workstation_v12'}
        before = {p: p.read_bytes() for p in service.runtime_root.rglob('*') if p.is_file()}
        payload = build_product_job_viewer_payload(job['job_id'], store=store, conformal_root=service.conformal_output_root)
        result = payload['result']
        package = resolve_product_package(store, job['job_id'], service.conformal_output_root)
        receipt = job['attempts'][0]['package_verification']['readonly_receipt']
        self.assert_public_deliveries(payload, package, receipt, job)
        self.assertEqual(result['compatibility']['package_format'], 'PRODUCT_SIX_FILES_V1')
        self.assertEqual(result['compatibility']['producer']['generation'], 'r4_lite_single_reference_workstation_v11')
        self.assertEqual(result['compatibility']['reader'], store.reader_identity)
        self.assertEqual(payload['target']['base_material'], job['target']['base_material'])
        self.assertEqual(payload['target']['source']['source_name'], job['target']['source']['name'])
        self.assertEqual(payload['target']['source']['source_sha256'], job['target']['source']['sha256'])
        self.assertFalse(payload['generation_classification']['runtime_permitted'])
        self.assertEqual({p: p.read_bytes() for p in service.runtime_root.rglob('*') if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
