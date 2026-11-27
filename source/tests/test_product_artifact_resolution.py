import hashlib
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path

from product_workflow.artifacts import (
    product_package_file_url,
    resolve_product_package,
    resolve_product_package_file,
)


class _Store:
    def __init__(self, job):
        self.job = job
        self.viewer_compatibility = {}

    def get_consistent(self, job_id):
        if job_id != self.job["job_id"]:
            raise FileNotFoundError(job_id)
        return self.job

    get_consistent_for_viewer = get_consistent


def write_p13_fixture(package_dir, package_id):
    """Small recorded package, no runtime producer or model execution."""
    files = {
        'input/source_c3d8.inp': b'*Heading\n',
        'core/conformal_b31.inp': b'*Heading\n',
        'visualization/conformal_preview.stl': b'solid fixture\nendsolid fixture\n',
        'visualization/conformal_preview.png': b'fixture-png',
        'metadata.json': json.dumps({'result_id': package_id}).encode(),
        'run_summary.txt': b'fixture only\n',
        'geometry_ledger.json': json.dumps({'schema_version': 'latticegpt_9x_geometry_ledger_v1', 'technical_disposition': 'PASS', 'formal_gate_disposition': 'NOT_CLAIMED'}).encode(),
        'source_element_map.csv': b'source_element_id,selected,selected_index,element_type_original,element_type_effective,node_ids\n1,true,1,C3D8,C3D8,1 2 3 4 5 6 7 8\n',
    }
    from product_workflow.artifacts import PRODUCT_FILE_MAP
    outputs = {}
    for relative, data in files.items():
        path = package_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        key = next(key for key, value in PRODUCT_FILE_MAP.items() if value.as_posix() == relative)
        outputs['legacy_metadata' if key == 'metadata' else key] = {'path': relative, 'sha256': hashlib.sha256(data).hexdigest().upper()}
    metadata = {
        'schema_version': 'latticegpt_9x_package_metadata_v1',
        'package': {'package_id': package_id, 'result_id': package_id},
        'outputs': {key: value for key, value in outputs.items() if key != 'source_mesh'},
        'source_mesh': {'package_copy_path': outputs['source_mesh']['path'], 'package_copy_sha256': outputs['source_mesh']['sha256']},
        'design_domain': {'selected_element_count': 1},
        'acceptance': {'technical_disposition': 'PASS', 'formal_gate_disposition': 'NOT_CLAIMED'},
    }
    (package_dir / 'package_metadata.json').write_text(json.dumps(metadata), encoding='utf-8')


class ProductArtifactResolutionTests(unittest.TestCase):
    def setUp(self):
        test_runtime_root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
        self.assertTrue(test_runtime_root)
        self.root = Path(test_runtime_root) / f"a-{uuid.uuid4().hex[:8]}"
        self.conformal_root = self.root / "conformal"
        self.job_id = "job-1234567890ABCDEF12345678"
        self.package_id = f"{self.job_id}-attempt-0001"
        self.package_dir = self.conformal_root / self.job_id / self.package_id
        self.package_dir.mkdir(parents=True)
        self.b31 = self.package_dir / "core" / "conformal_b31.inp"
        self.b31.parent.mkdir()
        self.b31.write_text("*Heading\n", encoding="utf-8")
        b31_sha = hashlib.sha256(self.b31.read_bytes()).hexdigest().upper()
        (self.package_dir / "metadata.json").write_text(
            json.dumps({"result_id": self.package_id}), encoding="utf-8"
        )
        (self.package_dir / "package_metadata.json").write_text(
            json.dumps(
                {
                    "package": {
                        "package_id": self.package_id,
                        "result_id": self.package_id,
                    },
                    "outputs": {
                        "b31": {
                            "path": "core/conformal_b31.inp",
                            "sha256": b31_sha,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.job = {
            "job_id": self.job_id,
            "status": "VIEWER_READY",
            "attempts": [
                {
                    "attempt_id": self.package_id,
                    "package_id": self.package_id,
                    "status": "VIEWER_READY",
                    "conformal_output_dir": str(self.package_dir),
                    "package_verification": {
                        "status": "passed",
                        "package_id": self.package_id,
                    },
                }
            ],
        }
        self.store = _Store(self.job)
        write_p13_fixture(self.package_dir, self.package_id)

    def test_partial_p13_package_is_never_read_as_legacy(self):
        (self.package_dir / 'package_metadata.json').unlink()
        with self.assertRaisesRegex(ValueError, 'P13'):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_complete_p13_package_validates_ledger_before_serving(self):
        (self.package_dir / 'geometry_ledger.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_unregistered_six_file_package_is_rejected(self):
        for name in ('package_metadata.json', 'geometry_ledger.json', 'source_element_map.csv'):
            (self.package_dir / name).unlink()
        with self.assertRaises(FileNotFoundError):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_resolves_exact_job_attempt_and_verifies_file_identity(self):
        package = resolve_product_package(self.store, self.job_id, self.conformal_root)
        self.assertEqual(package.package_dir, self.package_dir.resolve())
        self.assertEqual(package.package_id, self.package_id)
        self.assertEqual(resolve_product_package_file(package, "b31"), self.b31.resolve())
        self.assertEqual(
            product_package_file_url(self.job_id, "b31"),
            f"/api/viewer/product/jobs/{self.job_id}/files/b31",
        )

    def test_rejects_recorded_path_outside_current_conformal_root(self):
        self.job["attempts"][0]["conformal_output_dir"] = str(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "HOLD_PATH_OUTSIDE_CURRENT_RUNTIME_ROOT"):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_rejects_package_identity_mismatch_and_multiple_attempts(self):
        self.job["attempts"][0]["package_id"] = "wrong-package"
        with self.assertRaisesRegex(ValueError, "package identity"):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

        self.job["attempts"] = [dict(self.job["attempts"][0]), dict(self.job["attempts"][0])]
        with self.assertRaisesRegex(ValueError, "one unique attempt"):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_missing_current_package_does_not_fall_back_to_historical_root(self):
        historical = self.root / "conformal_runtime_69" / self.package_id
        historical.mkdir(parents=True)
        (historical / "metadata.json").write_text(
            json.dumps({"result_id": self.package_id}), encoding="utf-8"
        )
        shutil.rmtree(self.package_dir)
        with self.assertRaisesRegex(FileNotFoundError, "recorded package"):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_other_job_package_cannot_be_borrowed_with_consistent_file_hashes(self):
        foreign_id = 'job-' + 'F' * 24 + '-attempt-0001'
        foreign = self.conformal_root / foreign_id
        foreign.mkdir()
        write_p13_fixture(foreign, foreign_id)
        self.job['attempts'][0].update(attempt_id=foreign_id, package_id=foreign_id,
            conformal_output_dir=str(foreign), package_verification={'status': 'passed', 'package_id': foreign_id})
        with self.assertRaisesRegex(ValueError, 'package identity'):
            resolve_product_package(self.store, self.job_id, self.conformal_root)

    def test_file_sha_drift_and_unknown_key_fail_closed(self):
        package = resolve_product_package(self.store, self.job_id, self.conformal_root)
        self.b31.write_text("drift", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            resolve_product_package_file(package, "b31")
        with self.assertRaisesRegex(ValueError, "file_key"):
            resolve_product_package_file(package, "unknown")


if __name__ == "__main__":
    unittest.main()


class SixFileReceiptNegativeTests(unittest.TestCase):
    def test_receipt_relationship_shape_and_file_drift_fail_closed(self):
        from copy import deepcopy
        from tests.test_product_workflow_mesh_runtime import ProductSixFileChainTests
        from product_workflow.store import JobStore
        from product_workflow.manifest_io import canonical_json_object_v1
        fixture = ProductSixFileChainTests()
        service, job = fixture.chain()
        self.assertEqual(job["status"], "VIEWER_READY", job.get("failure"))
        path = service.store.job_directory(job["job_id"]) / "job.json"
        original = path.read_bytes()
        def read(): return resolve_product_package(JobStore(service.runtime_root), job["job_id"], service.conformal_output_root)
        mutations = {
            "extra receipt field": lambda r: r.update(extra=True),
            "wrong schema": lambda r: r.update(schema_version="unknown"),
            "wrong package": lambda r: r.update(package_id="foreign"),
            "wrong job": lambda r: r.update(job_id="job-"+"F"*24),
            "wrong target": lambda r: r.update(target_sha256="0"*64),
            "wrong selection": lambda r: r.update(selection_sha256="0"*64),
            "wrong mesh": lambda r: r.update(mesh_binding_sha256="0"*64),
            "producer drift": lambda r: r["producer"].update(config_sha256="0"*64),
            "extra producer": lambda r: r["producer"].update(extra=True),
            "missing role": lambda r: r["files"].pop("b31"),
            "extra role": lambda r: r["files"].update(extra={}),
            "traversal": lambda r: r["files"]["b31"].update(path="../foreign.inp"),
            "boolean size": lambda r: r["files"]["b31"].update(size_bytes=True),
            "hash drift": lambda r: r["files"]["b31"].update(sha256="0"*64),
        }
        for label, mutate in mutations.items():
            corrupted = deepcopy(job)
            mutate(corrupted["attempts"][0]["package_verification"]["readonly_receipt"])
            path.write_bytes(canonical_json_object_v1(corrupted))
            with self.subTest(label=label), self.assertRaises(ValueError): read()
            path.write_bytes(original)
        package = read()
        for key, identity in package.file_identities.items():
            artifact = package.package_dir / identity["path"]
            data = artifact.read_bytes()
            artifact.write_bytes(data + b"drift")
            with self.subTest(role=key), self.assertRaisesRegex(ValueError, "SHA256"): read()
            artifact.write_bytes(data)
        partial = package.package_dir / "geometry_ledger.json"
        partial.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, "P13"): read()
        partial.unlink()
        self.assertEqual(path.read_bytes(), original)
