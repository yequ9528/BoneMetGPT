import shutil
import unittest
import uuid
import os
from copy import deepcopy
from pathlib import Path

from llm_provider import resolve_model_runtime_profile
from product_workflow.contracts import (
    FORMAL_BUNDLE_ARTIFACT_NAMES,
    STIFFNESS_NAMES,
    build_adapter_run_result,
    formal_bundle_binding_from_verified_artifacts,
    target_from_physical_9d,
)
from product_workflow.service import ProductWorkflowService
from tests.test_r3_reproduction_profile import valid_profile
from product_workflow.reproducibility import build_comparison_contract_v1


VALID_INP = b"""*Heading
*Node
1,0,0,0
2,1,0,0
3,1,1,0
4,0,1,0
5,0,0,1
6,1,0,1
7,1,1,1
8,0,1,1
*Element, type=C3D8
1,1,2,3,4,5,6,7,8
"""


def target_values():
    return {
        "C11": 55.0, "C12": 5.0, "C13": 4.0,
        "C22": 50.0, "C23": 3.0, "C33": 45.0,
        "C44": 15.0, "C55": 12.0, "C66": 10.0,
    }


def explicitly_confirm_fixture_candidate(service, job):
    """Simulate the same versioned selection action as the product, using synthetic data."""
    return service.select_candidate(job["job_id"], job["selection"]["selected_candidate_id"],
        expected_revision=job["revision"], expected_candidate_set_sha256=job["candidate_set"]["candidate_set_sha256"],
        reason="Explicit synthetic user action")


class ProductWorkflowMeshRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["LATTICEGPT_TEST_RUNTIME_ROOT"]) / f"m-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.adapter_calls = []
        self.conformal_calls = []

        def inverse(target, **kwargs):
            candidates = []
            for index in range(1, 4):
                predicted = dict(target_values())
                predicted["C11"] += 0.1 * index
                candidates.append({
                    "candidate_id": f"candidate-{index}",
                    "cad_ready": True,
                    "predicted_stiffness": predicted,
                    "diameter": 0.1 * index,
                    "nodes": [[0.0, 0.0, 0.0]] * 15,
                    "connections": [[0, 1]] * 30,
                })
            return {"status": "success", "candidates": candidates, "model_variant": "150w", "cad_ready_count": 3, "cleanup": {"status": "complete", "raw_files_available": False, "work_dir_removed": True}, "work_dir_retained": False}

        def adapter(**request):
            self.adapter_calls.append(request)
            output_dir = Path(request["output_dir"])
            return build_adapter_run_result(
                status="SUCCESS",
                producer="fixture_adapter",
                producer_status="static_validated",
                producer_result_sha256="B" * 64,
                artifacts={
                    "custom_cell": {
                        "path": str(output_dir / "custom_cell.yml"),
                        "size_bytes": 10,
                        "sha256": "C" * 64,
                    },
                    "adapter_metadata": {
                        "path": str(output_dir / "adapter_metadata.json"),
                        "size_bytes": 20,
                        "sha256": "D" * 64,
                    },
                },
                candidate_id=request["candidate"]["candidate_id"],
                selection_manifest_sha256=request["traceability"]["selection_manifest_sha256"],
                mesh_binding_sha256=request["mesh_binding"]["mesh_binding_sha256"],
                errors=[],
                warnings=[],
            )

        def conformal(**request):
            self.conformal_calls.append(request)
            return {
                "status": "success",
                "package_id": request["package_id"],
                "package_dir": request["output_dir"],
            }

        self.bundle = formal_bundle_binding_from_verified_artifacts({
            name: {
                "path": f"C:/test/{name}",
                "size_bytes": index,
                "sha256": f"{index:X}" * 64,
                "status": "PASS",
            }
            for index, name in enumerate(FORMAL_BUNDLE_ARTIFACT_NAMES, start=1)
        })
        self.profile = resolve_model_runtime_profile(
            provider="api",
            model_name="gpt-4o-mini",
            selection_source="session_selection",
            session_bound=True,
        )

        self.service = ProductWorkflowService(
            self.root,
            inverse_runner=inverse,
            scaler_y_min={name: 0.0 for name in STIFFNESS_NAMES},
            scaler_y_max={name: 1.0 for name in STIFFNESS_NAMES},
            adapter_runner=adapter,
            conformal_runner=conformal,
            package_verifier=lambda **request: {"status": "passed", **request},
            formal_bundle_binding=self.bundle,
            formal_bundle_verifier=lambda: self.bundle,
            adapter_output_root=self.root / "adapter",
            conformal_output_root=self.root / "conformal",
        )
        proposal = target_from_physical_9d(
            target_values(),
            source_kind="csv_upload",
            source_sha256="A" * 64,
            source_name="target.csv",
        )
        created = self.service.create_job(
            proposal, model_runtime_profile=self.profile
        )
        self.ready = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )
        self.unconfirmed = deepcopy(self.ready)
        self.ready = explicitly_confirm_fixture_candidate(self.service, self.ready)

    def tearDown(self):
        shutil.rmtree(self.root)

    def _r3_service_and_ready_job(self, runtime_root=None):
        adapter_calls = []
        conformal_calls = []

        def inverse(target, **kwargs):
            candidates = []
            for index in range(1, 4):
                predicted = dict(target_values())
                predicted["C11"] += 0.1 * index
                candidates.append({
                    "candidate_id": f"candidate-{index}",
                    "cad_ready": True,
                    "predicted_stiffness": predicted,
                    "diameter": 0.1 * index,
                    "nodes": [[0.0, 0.0, 0.0]] * 15,
                    "connections": [[0, 1]] * 30,
                })
            return {"status": "success", "candidates": candidates, "model_variant": "150w", "cad_ready_count": 3, "cleanup": {"status": "complete", "raw_files_available": False, "work_dir_removed": True}, "work_dir_retained": False}

        def adapter(**request):
            adapter_calls.append(request)
            output_dir = Path(request["output_dir"])
            return build_adapter_run_result(
                status="SUCCESS",
                producer="fixture_adapter",
                producer_status="static_validated",
                producer_result_sha256="B" * 64,
                artifacts={
                    "custom_cell": {
                        "path": str(output_dir / "custom_cell.yml"),
                        "size_bytes": 10,
                        "sha256": "C" * 64,
                    },
                    "adapter_metadata": {
                        "path": str(output_dir / "adapter_metadata.json"),
                        "size_bytes": 20,
                        "sha256": "D" * 64,
                    },
                },
                candidate_id=request["candidate"]["candidate_id"],
                selection_manifest_sha256=request["traceability"]["selection_manifest_sha256"],
                mesh_binding_sha256=request["mesh_binding"]["digests"]["mesh_binding_sha256"],
                errors=[],
                warnings=[],
            )

        def conformal(**request):
            conformal_calls.append(request)
            return {
                "status": "success",
                "package_id": request["package_id"],
                "package_dir": request["output_dir"],
            }

        runtime_root = runtime_root or self.root / f"r3-{uuid.uuid4().hex[:8]}"
        reproduction = valid_profile()
        service = ProductWorkflowService(
            runtime_root,
            inverse_runner=inverse,
            scaler_y_min={name: 0.0 for name in STIFFNESS_NAMES},
            scaler_y_max={name: 1.0 for name in STIFFNESS_NAMES},
            adapter_runner=adapter,
            conformal_runner=conformal,
            package_verifier=lambda **request: {"status": "passed", **request},
            formal_bundle_binding=self.bundle,
            formal_bundle_verifier=lambda: self.bundle,
            reproduction_profile=reproduction,
            comparison_contract=build_comparison_contract_v1(
                tolerance_schema_path=reproduction["tolerance"]["schema_path"],
                tolerance_schema_sha256=reproduction["tolerance"]["schema_sha256"],
            ),
            source_manifest_sha256="F" * 64,
        )
        proposal = target_from_physical_9d(
            target_values(),
            source_kind="frozen_technical_reference",
            source_sha256="A" * 64,
            source_name="target.json",
        )
        created = service.create_job(proposal, model_runtime_profile=self.profile)
        ready = service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )
        ready = explicitly_confirm_fixture_candidate(service, ready)
        return service, ready, adapter_calls, conformal_calls

    def _bind_r3(self, service, ready):
        return service.bind_mesh(
            ready["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )

    def test_r3_mesh_binding_v2_runs_through_real_start_conformal_path(self):
        service, ready, adapter_calls, conformal_calls = self._r3_service_and_ready_job()
        bound = self._bind_r3(service, ready)

        done = service.start_conformal(
            bound["job_id"],
            expected_revision=bound["revision"],
            authorization={"authorized": True},
        )

        binding = bound["mesh_binding"]
        upload = bound["mesh_uploads"][-1]
        self.assertEqual(done["status"], "VIEWER_READY")
        self.assertEqual(adapter_calls[0]["mesh_binding"], binding)
        self.assertEqual(adapter_calls[0]["reference_length_mm"], 1.0)
        self.assertEqual(
            conformal_calls[0]["source_mesh_path"], upload["stored_path"]
        )
        self.assertEqual(
            conformal_calls[0]["source_mesh_contract"],
            {
                "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
                "element_type_original": "C3D8",
                "element_type_effective": "C3D8",
            },
        )
        context = conformal_calls[0]["product_context"]
        self.assertEqual(
            context["mesh_binding_sha256"], binding["digests"]["mesh_binding_sha256"]
        )
        self.assertEqual(context["mesh_original_filename"], "implant.inp")
        self.assertEqual(context["mesh_sha256"], upload["sha256"])
        self.assertNotIn("element_type_original", binding)
        self.assertNotIn("element_type_effective", binding)
        self.assertNotIn("stored_path", binding)
        self.assertNotIn("mesh_binding_sha256", binding)

    def test_r3_mesh_binding_v2_rejects_invalid_upload_relationship_before_attempt(self):
        mutations = {
            "missing": lambda job: job.__setitem__("mesh_uploads", []),
            "non_accepted": lambda job: job["mesh_uploads"][-1].__setitem__("status", "REJECTED"),
            "upload_id": lambda job: job["mesh_uploads"][-1].__setitem__("upload_id", "upload-other"),
            "filename": lambda job: job["mesh_uploads"][-1].__setitem__("original_filename", "other.inp"),
            "size": lambda job: job["mesh_uploads"][-1].__setitem__("size_bytes", len(VALID_INP) + 1),
            "sha": lambda job: job["mesh_uploads"][-1].__setitem__("sha256", "0" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                service, ready, adapter_calls, conformal_calls = self._r3_service_and_ready_job()
                bound = self._bind_r3(service, ready)
                corrupted = deepcopy(bound)
                mutate(corrupted)
                persisted = service.store.replace(
                    corrupted,
                    expected_revision=bound["revision"],
                    event=f"fixture_{label}",
                )
                with self.assertRaisesRegex(ValueError, "MeshBindingV2 upload evidence"):
                    service.start_conformal(
                        persisted["job_id"],
                        expected_revision=persisted["revision"],
                        authorization={"authorized": True},
                    )
                unchanged = service.get_job(persisted["job_id"])
                self.assertEqual(unchanged["attempts"], [])
                self.assertEqual(adapter_calls, [])
                self.assertEqual(conformal_calls, [])

    def test_r3_mesh_binding_v2_rejects_precheck_type_drift_before_attempt(self):
        service, ready, adapter_calls, conformal_calls = self._r3_service_and_ready_job()
        bound = self._bind_r3(service, ready)
        corrupted = deepcopy(bound)
        corrupted["mesh_uploads"][-1]["precheck"]["mesh"][
            "element_type_effective"
        ] = "C3D8_GEOMETRY_COMPATIBLE"
        persisted = service.store.replace(
            corrupted,
            expected_revision=bound["revision"],
            event="fixture_precheck_type_drift",
        )
        with self.assertRaisesRegex(ValueError, "runtime mesh precheck identity"):
            service.start_conformal(
                persisted["job_id"],
                expected_revision=persisted["revision"],
                authorization={"authorized": True},
            )
        self.assertEqual(service.get_job(persisted["job_id"])["attempts"], [])
        self.assertEqual(adapter_calls, [])
        self.assertEqual(conformal_calls, [])

    def test_explicit_stage_roots_outside_runtime_root_are_rejected_before_write(self):
        outside = self.root.parent / f"outside_{uuid.uuid4().hex}"
        with self.assertRaisesRegex(ValueError, "adapter output root"):
            ProductWorkflowService(
                self.root / "isolated-runtime",
                inverse_runner=lambda *_args, **_kwargs: {},
                scaler_y_min={name: 0.0 for name in STIFFNESS_NAMES},
                scaler_y_max={name: 1.0 for name in STIFFNESS_NAMES},
                formal_bundle_binding=self.bundle,
                formal_bundle_verifier=lambda: self.bundle,
                adapter_output_root=outside,
            )
        self.assertFalse(outside.exists())

    def test_bind_mesh_preserves_bytes_and_enters_authorization_wait(self):
        bound = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )
        self.assertEqual(bound["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        binding = bound["mesh_binding"]
        self.assertEqual(binding["element_type_original"], "C3D8")
        self.assertEqual(binding["element_type_effective"], "C3D8")
        self.assertEqual(binding["selected_element_count"], 1)
        self.assertEqual(Path(binding["stored_path"]).read_bytes(), VALID_INP)

    def test_bind_mesh_rejects_oversized_input_before_upload_write(self):
        oversized = b"x" * (50 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "50 MB"):
            self.service.bind_mesh(
                self.ready["job_id"],
                original_filename="oversized.inp",
                content=oversized,
                expected_revision=self.ready["revision"],
                confirmed_contract={
                    "units": "mm",
                    "frame": "source_native_cartesian_frame",
                    "design_domain": "whole_file_implant_domain",
                },
            )
        upload = self.root / "jobs" / self.ready["job_id"] / "uploads" / "source.inp"
        self.assertFalse(upload.exists())

    def test_bind_mesh_preserves_c3d8r_original_type(self):
        c3d8r_inp = VALID_INP.replace(b"type=C3D8", b"type=C3D8R")
        bound = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="implant_c3d8r.inp",
            content=c3d8r_inp,
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )
        binding = bound["mesh_binding"]
        self.assertEqual(binding["element_type_original"], "C3D8R")
        self.assertEqual(
            binding["element_type_effective"],
            "C3D8_GEOMETRY_COMPATIBLE",
        )

    def test_rejected_precheck_keeps_candidates_and_allows_numbered_reupload(self):
        candidate_identity = self.ready["candidate_set"]["candidate_set_sha256"]

        rejected = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="invalid.inp",
            content=b"*Heading\n",
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )

        self.assertEqual(rejected["status"], "CANDIDATES_READY")
        self.assertEqual(rejected["workflow_disposition"], "ACTIVE")
        self.assertIsNone(rejected["failure"])
        self.assertEqual(rejected["mesh_uploads"][0]["upload_id"], "upload-0001")
        self.assertEqual(rejected["mesh_uploads"][0]["status"], "REJECTED")
        self.assertEqual(
            rejected["candidate_set"]["candidate_set_sha256"],
            candidate_identity,
        )

        bound = self.service.bind_mesh(
            rejected["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=rejected["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )

        self.assertEqual(bound["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        self.assertEqual(bound["mesh_uploads"][1]["upload_id"], "upload-0002")
        self.assertEqual(bound["mesh_uploads"][1]["status"], "ACCEPTED")
        self.assertEqual(bound["mesh_binding"]["upload_id"], "upload-0002")
        self.assertIn("upload-0002", bound["mesh_binding"]["stored_path"])

    def test_bind_mesh_rejects_path_escape_and_unsupported_contract(self):
        with self.assertRaisesRegex(ValueError, "filename"):
            self.service.bind_mesh(
                self.ready["job_id"],
                original_filename="../implant.inp",
                content=VALID_INP,
                expected_revision=self.ready["revision"],
                confirmed_contract={"units": "mm"},
            )
        with self.assertRaisesRegex(ValueError, "millimetres"):
            self.service.bind_mesh(
                self.ready["job_id"],
                original_filename="implant.inp",
                content=VALID_INP,
                expected_revision=self.ready["revision"],
                confirmed_contract={
                    "units": "m",
                    "frame": "source_native_cartesian_frame",
                    "design_domain": "whole_file_implant_domain",
                },
            )

    def test_start_conformal_records_separate_authorizations_and_no_overwrite(self):
        bound = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )
        done = self.service.start_conformal(
            bound["job_id"],
            expected_revision=bound["revision"],
            authorization={"authorized": True, "authorized_by": "user"},
        )
        self.assertEqual(done["status"], "VIEWER_READY")
        actions = [item["action"] for item in done["runtime_authorizations"]]
        self.assertIn("adapter_controlled_write", actions)
        self.assertIn("conformal_runtime", actions)
        self.assertFalse(self.adapter_calls[0]["overwrite"])
        self.assertFalse(self.conformal_calls[0]["overwrite"])
        self.assertEqual(done["attempts"][0]["adapter_result"]["status"], "SUCCESS")
        self.assertEqual(
            done["attempts"][0]["adapter_result"]["producer_status"],
            "static_validated",
        )
        self.assertEqual(len(done["attempts"]), 1)
        self.assertEqual(done["attempts"][0]["status"], "VIEWER_READY")

    def test_failure_stops_without_auto_retry_and_same_job_retry_is_forbidden(self):
        bound = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )
        successful_adapter = self.service.adapter_runner
        self.service.adapter_runner = lambda **request: build_adapter_run_result(
            status="FAILED",
            producer="fixture_adapter",
            producer_status="invalid",
            producer_result_sha256="E" * 64,
            artifacts={"custom_cell": None, "adapter_metadata": None},
            candidate_id=request["candidate"]["candidate_id"],
            selection_manifest_sha256=request["traceability"]["selection_manifest_sha256"],
            mesh_binding_sha256=request["mesh_binding"]["mesh_binding_sha256"],
            errors=["fixture Adapter failure"],
            warnings=[],
        )
        failed = self.service.start_conformal(
            bound["job_id"],
            expected_revision=bound["revision"],
            authorization={"authorized": True},
        )
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(len(failed["attempts"]), 1)
        self.assertEqual(failed["attempts"][0]["adapter_result"]["status"], "FAILED")
        self.assertEqual(
            failed["attempts"][0]["adapter_result"]["producer_status"],
            "invalid",
        )
        self.assertEqual(self.conformal_calls, [])

        self.service.adapter_runner = successful_adapter
        with self.assertRaisesRegex(ValueError, "one runtime attempt"):
            self.service.start_conformal(
                bound["job_id"],
                expected_revision=failed["revision"],
                authorization={"authorized": True},
            )
        unchanged = self.service.get_job(bound["job_id"])
        self.assertEqual(unchanged["status"], "FAILED")
        self.assertEqual(len(unchanged["attempts"]), 1)

    def test_workflow_rejects_unversioned_adapter_status_alias(self):
        bound = self.service.bind_mesh(
            self.ready["job_id"],
            original_filename="implant.inp",
            content=VALID_INP,
            expected_revision=self.ready["revision"],
            confirmed_contract={
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
            },
        )
        self.service.adapter_runner = lambda **request: {
            "status": "static_validated",
            "custom_cell_path": str(Path(request["output_dir"]) / "custom_cell.yml"),
            "adapter_metadata_path": str(Path(request["output_dir"]) / "adapter_metadata.json"),
        }
        failed = self.service.start_conformal(
            bound["job_id"],
            expected_revision=bound["revision"],
            authorization={"authorized": True},
        )
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("AdapterRunResultV1", failed["failure"]["message"])
        self.assertEqual(self.conformal_calls, [])


if __name__ == "__main__":
    unittest.main()


class ProductSixFileChainTests(unittest.TestCase):
    """Synthetic compute only; real bridge, sealing, disk reload and HTTP readers."""
    def chain(self, *, reject_first=False, omit=None, verifier=True):
        import os, json
        from unittest.mock import patch
        from product_workflow.runtime import adapter_controlled_write, conformal_controlled_run, verify_product_package
        root = Path(os.environ["LATTICEGPT_TEST_RUNTIME_ROOT"]) / ("c-" + uuid.uuid4().hex[:8])
        root.mkdir(parents=True)  # Preserve every isolated chain, including failures.
        profile = valid_profile()
        producer = {"generation": "r4_lite_single_reference_workstation_v11", **{k: profile["identities"][k] for k in ("source_manifest_sha256", "config_sha256", "active_release_binding_sha256")}}
        bundle = formal_bundle_binding_from_verified_artifacts({name: {"path": f"C:/synthetic/{name}", "size_bytes": i, "sha256": f"{i:X}" * 64, "status": "PASS"} for i, name in enumerate(FORMAL_BUNDLE_ARTIFACT_NAMES, 1)})
        def inverse(*args, **kwargs):
            return {"status": "success", "model_variant": "150w", "runtime_mode": "formal_product", "cad_ready_count": 3,
                    "warnings": [], "errors": [], "steps": [], "cleanup": {"status": "complete", "raw_files_available": False, "work_dir_removed": True}, "work_dir_retained": False,
                    "candidates": [{"candidate_id": f"synthetic-{i}", "cad_ready": True, "predicted_stiffness": target_values(), "diameter": 0.1, "relative_density": 0.2, "nodes": [[0,0,0], [1,1,1]], "connections": [[0,1]]} for i in range(3)]}
        service = ProductWorkflowService(root, inverse_runner=inverse, scaler_y_min={k: 0 for k in STIFFNESS_NAMES}, scaler_y_max={k: 1 for k in STIFFNESS_NAMES},
            formal_bundle_binding=bundle, formal_bundle_verifier=lambda: bundle, adapter_runner=adapter_controlled_write, conformal_runner=conformal_controlled_run,
            package_verifier=verify_product_package if verifier else None, reproduction_profile=profile,
            comparison_contract=build_comparison_contract_v1(tolerance_schema_path=profile["tolerance"]["schema_path"], tolerance_schema_sha256=profile["tolerance"]["schema_sha256"]),
            source_manifest_sha256=profile["identities"]["source_manifest_sha256"], readonly_package_producer=producer)
        proposal = target_from_physical_9d(target_values(), source_kind="csv_upload", source_sha256="A"*64, source_name="synthetic.csv")
        model = resolve_model_runtime_profile(provider="api", model_name="gpt-4o-mini", selection_source="session_selection", session_bound=True)
        job = service.create_job(proposal, model_runtime_profile=model)
        job = service.confirm_target_and_run_inverse(job["job_id"], expected_revision=job["revision"], authorization={"authorized": True})
        self.assertEqual(job["status"], "CANDIDATES_READY", job.get("failure"))
        job = explicitly_confirm_fixture_candidate(service, job)
        contract = {"units": "mm", "frame": "source_native_cartesian_frame", "design_domain": "whole_file_implant_domain"}
        if reject_first:
            job = service.bind_mesh(job["job_id"], original_filename="rejected.inp", content=b"*Heading\n", expected_revision=job["revision"], confirmed_contract=contract)
        job = service.bind_mesh(job["job_id"], original_filename="synthetic.inp", content=VALID_INP, expected_revision=job["revision"], confirmed_contract=contract)
        def compute(request):
            from product_workflow.artifacts import PRODUCT_FILE_MAP, LEGACY_FILE_KEYS
            package = Path(request["output_root"]) / request["package_id"]
            adapter_metadata = json.loads(Path(request["adapter_metadata_path"]).read_text(encoding="utf-8"))
            trace = {**adapter_metadata["traceability"], "case_manifest_sha256": adapter_metadata["case_manifest_sha256"]}
            metadata = {"result_id": request["package_id"], "created_by": "cad_module.build_conformal_result_package", "stage": "6.11d-conformal-service-runtime-validation", "traceability": trace, "product_mesh_binding": request["product_mesh_binding"]}
            for key in LEGACY_FILE_KEYS:
                if key == omit: continue
                path = package / PRODUCT_FILE_MAP[key]; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(request["source_mesh_path"]).read_bytes() if key == "source_mesh" else json.dumps(metadata).encode() if key == "metadata" else b"synthetic isolated artifact\n")
            return {"status": "success", "package_id": request["package_id"], "package_dir": str(package), "metadata_path": str(package / "metadata.json")}
        with patch("product_workflow.runtime.run_conformal_service", side_effect=compute) as runner:
            done = service.start_conformal(job["job_id"], expected_revision=job["revision"], authorization={"authorized": True})
        self.assertEqual(runner.call_count, 1, done.get("failure"))
        return service, done

    def test_real_chain_seals_first_accept_and_reject_then_accept_without_whitelist(self):
        import hashlib
        from product_workflow.store import JobStore
        from product_workflow.artifacts import resolve_product_package, resolve_product_package_file, LEGACY_FILE_KEYS
        from product_viewer_payloads import build_product_job_viewer_payload
        from product_viewer_server import create_product_viewer_app
        from product_workflow.config import load_latticegpt_config
        from tests.test_product_viewer_server import viewer_preflight
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                service, job = self.chain(reject_first=rejected)
                self.assertEqual(job["status"], "VIEWER_READY", job.get("failure"))
                self.assertEqual(len(job["attempts"]), 1)
                receipt = job["attempts"][0]["package_verification"]["readonly_receipt"]
                self.assertEqual(receipt["package_format"], "PRODUCT_SIX_FILES_V1")
                store = JobStore(service.runtime_root)
                original = {p: p.read_bytes() for p in service.runtime_root.rglob('*') if p.is_file()}
                self.assertEqual(store.get_consistent(job["job_id"]), job)
                package = resolve_product_package(store, job["job_id"], service.conformal_output_root)
                self.assertEqual(package.compatibility["p13_status"], "NOT_EVALUATED")
                self.assertEqual(set(receipt["files"]), set(LEGACY_FILE_KEYS))
                self.assertEqual(package.file_identities, receipt["files"])
                for key, identity in receipt["files"].items():
                    path = resolve_product_package_file(package, key)
                    data = path.read_bytes()
                    self.assertEqual(identity["path"], path.relative_to(package.package_dir).as_posix())
                    self.assertEqual(identity["size_bytes"], len(data))
                    self.assertEqual(identity["sha256"], hashlib.sha256(data).hexdigest().upper())
                payload = build_product_job_viewer_payload(job["job_id"], store=store, conformal_root=service.conformal_output_root)
                self.assertEqual(payload["job_id"], job["job_id"])
                config = load_latticegpt_config(explicit={"release_root": service.runtime_root.parent, "runtime_root": service.runtime_root}, environ={})
                app = create_product_viewer_app(config, preflight=viewer_preflight())
                client = app.test_client()
                base = f"/api/viewer/product/jobs/{job['job_id']}"
                response = client.get(base)
                self.assertEqual(response.status_code, 200)
                result = response.get_json()["data"]["result"]
                self.assertEqual(result, payload["result"])
                # This producer stub seals placeholder B31 bytes. The two unit-cell
                # downloads are derived from its selected candidate's recorded graph.
                self.assertEqual(set(result["downloads"]), {"unit_png", "unit_stl"})
                self.assertEqual(result["unavailable_downloads"], ["conformal_inp"])
                self.assertEqual(set(result["download_identities"]), set(result["downloads"]))
                for role, url in result["downloads"].items():
                    self.assertTrue(url.startswith(f"{base}/deliver/{role}?snapshot="))
                    with client.get(url) as downloaded:
                        self.assertEqual(downloaded.status_code, 200)
                        identity = result["download_identities"][role]
                        self.assertEqual(identity["size_bytes"], len(downloaded.data))
                        self.assertEqual(identity["sha256"], hashlib.sha256(downloaded.data).hexdigest().upper())
                    stale = client.get(f"{base}/deliver/{role}?snapshot=outdated")
                    self.assertEqual(stale.status_code, 409)
                    self.assertEqual(stale.get_json()["code"], "FILE_IDENTITY_MISMATCH")
                for key in LEGACY_FILE_KEYS:
                    self.assertEqual(client.get(f"{base}/files/{key}").status_code, 404)
                    unknown_role = client.get(f"{base}/deliver/{key}")
                    self.assertEqual(unknown_role.status_code, 404)
                    self.assertEqual(unknown_role.get_json()["code"], "FILE_MISSING")
                self.assertEqual(original, {p: p.read_bytes() for p in service.runtime_root.rglob('*') if p.is_file()})

    def test_missing_final_file_fails_before_ready_and_preserves_one_attempt(self):
        service, job = self.chain(omit="stl")
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["failure"]["stage"], "PACKAGE_VERIFYING")
        self.assertEqual(len(job["attempts"]), 1)
        self.assertFalse(any(e["status"] == "VIEWER_READY" for e in service.store.read_events(job["job_id"])))

    def test_new_producer_without_real_verifier_never_reaches_ready(self):
        service, job = self.chain(verifier=False)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["failure"]["stage"], "PACKAGE_VERIFYING")
        self.assertIn("requires package verifier", job["failure"]["message"])

    def test_six_file_readability_does_not_grant_formal_package_verification(self):
        from product_workflow.job_evidence import _verify_package
        service, job = self.chain()
        self.assertEqual(job["status"], "VIEWER_READY")
        # Existing formal owner requires P13 metadata; this card does not change it.
        with self.assertRaises((ValueError, AttributeError)):
            _verify_package(service.store, job, service.runtime_root)
