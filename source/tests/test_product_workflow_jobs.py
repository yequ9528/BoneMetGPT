import shutil
import threading
import time
import unittest
import uuid
import os
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_provider import resolve_model_runtime_profile
from product_workflow.contracts import (
    FORMAL_BUNDLE_ARTIFACT_NAMES,
    STIFFNESS_NAMES,
    formal_bundle_binding_from_verified_artifacts,
    target_from_physical_9d,
    canonical_sha256,
)
from product_workflow.service import ProductWorkflowService, CandidateConfirmationCommitError
from product_workflow.job_state import candidate_confirmation_status, build_product_job_projection


def nine():
    return {
        "C11": 55.0,
        "C12": 5.0,
        "C13": 4.0,
        "C22": 50.0,
        "C23": 3.0,
        "C33": 45.0,
        "C44": 15.0,
        "C55": 12.0,
        "C66": 10.0,
    }


def runtime_identities():
    binding = formal_bundle_binding_from_verified_artifacts({
        name: {
            "path": f"C:/test/{name}",
            "size_bytes": index,
            "sha256": f"{index:X}" * 64,
            "status": "PASS",
        }
        for index, name in enumerate(FORMAL_BUNDLE_ARTIFACT_NAMES, start=1)
    })
    profile = resolve_model_runtime_profile(
        provider="api",
        model_name="gpt-4o-mini",
        selection_source="session_selection",
        session_bound=True,
    )
    return binding, profile


class ProductWorkflowJobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["LATTICEGPT_TEST_RUNTIME_ROOT"]) / f".runtime_product_jobs_{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.runner_calls = []

        def runner(target, **kwargs):
            self.runner_calls.append((target, kwargs))
            candidates = []
            for index, error in enumerate((0.8, 0.2, 0.5), start=1):
                prediction = dict(nine())
                prediction["C11"] += error
                candidates.append({
                    "candidate_id": f"candidate-{index}",
                    "cad_ready": True,
                    "predicted_stiffness": prediction,
                    "diameter": 0.1 * index,
                    "nodes": [[0.0, 0.0, 0.0]] * 15,
                    "connections": [[0, 1]] * 30,
                })
            return {"status": "success", "candidates": candidates}

        self.bundle, self.profile = runtime_identities()

        self.service = ProductWorkflowService(
            self.root,
            inverse_runner=runner,
            scaler_y_min={name: 0.0 for name in STIFFNESS_NAMES},
            scaler_y_max={name: 1.0 for name in STIFFNESS_NAMES},
            formal_bundle_binding=self.bundle,
            formal_bundle_verifier=lambda: self.bundle,
        )
        self.target = target_from_physical_9d(
            nine(),
            source_kind="csv_upload",
            source_sha256="A" * 64,
            source_name="target.csv",
        )

    def test_default_job_and_stage_roots_are_derived_from_runtime_root(self):
        self.assertEqual(self.service.store.runtime_root, self.root.resolve())
        self.assertEqual(self.service.store.jobs_root, (self.root / "jobs").resolve())
        self.assertEqual(self.service.adapter_output_root, (self.root / "adapter").resolve())
        self.assertEqual(self.service.conformal_output_root, (self.root / "conformal").resolve())

    def test_bad_inverse_diagnostics_persist_a_readable_v2_failure(self):
        from tests.test_r3_reproduction_profile import valid_profile
        from product_workflow.reproducibility import build_comparison_contract_v1
        profile = valid_profile()
        self.service.reproduction_profile = profile
        self.service.comparison_contract = build_comparison_contract_v1(tolerance_schema_path=profile["tolerance"]["schema_path"], tolerance_schema_sha256=profile["tolerance"]["schema_sha256"])
        self.service.source_manifest_sha256 = profile["identities"]["source_manifest_sha256"]
        self.service.inverse_runner = lambda *a, **kw: {"status": "failed", "errors": [{"not": "text"}], "cleanup": {"bad": True}}
        created = self.service.create_job(self.target, model_runtime_profile=self.profile)
        failed = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(self.service.store.get_consistent(failed["job_id"]), failed)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_create_does_not_run_inverse_and_confirm_produces_three_ranked_candidates(self):
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )
        self.assertEqual(created["status"], "TARGET_PROPOSED")
        self.assertEqual(self.runner_calls, [])

        ready = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True, "authorized_by": "user"},
        )

        self.assertEqual(ready["status"], "CANDIDATES_READY")
        self.assertEqual(len(ready["candidate_set"]["candidates"]), 3)
        self.assertEqual(ready["candidate_set"]["recommended_candidate_id"], "candidate-2")
        self.assertEqual(ready["selection"]["selected_candidate_id"], "candidate-2")
        self.assertEqual(ready["selection"]["selection_source"], "default_rank1")
        self.assertFalse(ready["candidate_set"]["diameter_used_in_ranking"])
        self.assertEqual(
            ready["candidate_set"]["formal_bundle_binding_sha256"],
            self.bundle["binding_sha256"],
        )
        self.assertEqual(
            ready["inverse_result"]["formal_bundle_binding_sha256"],
            self.bundle["binding_sha256"],
        )
        self.assertEqual(self.runner_calls[0][1]["num_candidates"], 3)
        self.assertFalse(self.runner_calls[0][1]["run_cad"])
        self.assertEqual(self.runner_calls[0][1]["runtime_mode"], "formal_product")
        self.assertEqual(
            self.runner_calls[0][1]["formal_bundle_binding"]["binding_sha256"],
            self.bundle["binding_sha256"],
        )

    def test_user_can_override_with_any_eligible_candidate(self):
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )
        ready = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )
        selected = self.service.select_candidate(
            created["job_id"],
            "candidate-3",
            expected_revision=ready["revision"],
            expected_candidate_set_sha256=ready["candidate_set"]["candidate_set_sha256"],
            reason="user prefers candidate 3",
        )
        self.assertEqual(selected["selection"]["recommended_candidate_id"], "candidate-2")
        self.assertEqual(selected["selection"]["selected_candidate_id"], "candidate-3")
        self.assertEqual(selected["selection"]["selection_source"], "user_override")

    def test_same_job_commands_are_serialized_before_mutation_logic(self):
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )
        ready = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )
        entered = threading.Event()
        release = threading.Event()
        selection_calls = []
        original_selection = self.service._selection

        def blocking_selection(*args, **kwargs):
            selection_calls.append(args[1]["candidate_id"])
            if len(selection_calls) == 1:
                entered.set()
                release.wait(timeout=2)
            return original_selection(*args, **kwargs)

        self.service._selection = blocking_selection

        def select(candidate_id):
            return self.service.select_candidate(
                ready["job_id"],
                candidate_id,
                expected_revision=ready["revision"],
                expected_candidate_set_sha256=ready["candidate_set"]["candidate_set_sha256"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(select, "candidate-1")
            self.assertTrue(entered.wait(timeout=1))
            second = pool.submit(select, "candidate-3")
            time.sleep(0.05)
            self.assertEqual(selection_calls, ["candidate-1"])
            release.set()
            first.result(timeout=2)
            with self.assertRaisesRegex(ValueError, "stale revision"):
                second.result(timeout=2)

    def test_stale_revision_and_ineligible_candidate_are_rejected(self):
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )
        ready = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )
        with self.assertRaisesRegex(ValueError, "stale revision"):
            self.service.select_candidate(
                created["job_id"], "candidate-1", expected_revision=created["revision"],
                expected_candidate_set_sha256=ready["candidate_set"]["candidate_set_sha256"],
            )

        stored = self.service.store.get(created["job_id"])
        stored["candidate_set"]["candidates"][0]["eligible"] = False
        stored["candidate_set"]["candidate_set_sha256"] = canonical_sha256({k: v for k, v in stored["candidate_set"].items() if k != "candidate_set_sha256"})
        self.service.store.replace(stored, expected_revision=ready["revision"], event="test_fixture")
        current = self.service.get_job(created["job_id"])
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.service.select_candidate(
                created["job_id"], "candidate-1", expected_revision=current["revision"],
                expected_candidate_set_sha256=current["candidate_set"]["candidate_set_sha256"],
            )

    def _unconfirmed(self):
        created = self.service.create_job(self.target, model_runtime_profile=self.profile)
        return self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})

    def _explicit_select(self, ready):
        return self.service.select_candidate(ready["job_id"], ready["selection"]["selected_candidate_id"], expected_revision=ready["revision"], expected_candidate_set_sha256=ready["candidate_set"]["candidate_set_sha256"])

    def test_recommended_candidate_requires_explicit_versioned_confirmation(self):
        ready = self._unconfirmed()
        self.assertEqual(candidate_confirmation_status(ready), "UNCONFIRMED")
        self.assertFalse(build_product_job_projection(ready)["capabilities"]["upload_mesh"])
        before = self.service.store.read_events(ready["job_id"])
        with self.assertRaises(ValueError):
            self.service.select_candidate(ready["job_id"], ready["selection"]["selected_candidate_id"], expected_revision=ready["revision"], expected_candidate_set_sha256="0" * 64)
        self.assertEqual(self.service.store.read_events(ready["job_id"]), before)
        selected = self._explicit_select(ready)
        self.assertEqual(candidate_confirmation_status(selected), "CONFIRMED")
        self.assertEqual(selected["selection"]["selection_source"], "default_rank1")
        self.assertTrue(build_product_job_projection(selected)["capabilities"]["upload_mesh"])
        with self.assertRaisesRegex(ValueError, "stale revision"):
            self._explicit_select(ready)
        self.assertEqual(len(self.service.store.read_events(ready["job_id"])), len(before) + 1)

    def test_unconfirmed_backend_upload_and_start_are_refused_before_writes(self):
        ready = self._unconfirmed()
        directory = self.service.store.job_directory(ready["job_id"])
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        with self.assertRaisesRegex(ValueError, "explicit candidate confirmation"):
            self.service.bind_mesh(ready["job_id"], original_filename="fixture.inp", content=b"fixture", expected_revision=ready["revision"], confirmed_contract={})
        with self.assertRaises(ValueError):
            self.service.start_conformal(ready["job_id"], expected_revision=ready["revision"], authorization={"authorized": True})
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}, before)
        self.assertFalse((directory / "uploads").exists())

    def test_confirmation_partial_commit_is_classified_without_repair_or_retry(self):
        for phase, expected in (("snapshot", "NOT_COMMITTED"), ("event_before", "INCONSISTENT"), ("event_partial", "INCONSISTENT"), ("response", "COMMITTED")):
            with self.subTest(phase=phase):
                ready = self._unconfirmed()
                append = self.service.store._append_event
                def fail_event(directory, event, job):
                    if phase == "event_partial":
                        with (directory / "events.jsonl").open("ab") as stream:
                            stream.write(b'{"revision":')
                    if phase == "response":
                        append(directory, event, job)
                    raise OSError("injected write failure")
                method = "_write_snapshot" if phase == "snapshot" else "_append_event"
                with patch.object(self.service.store, method, side_effect=OSError("snapshot failure") if phase == "snapshot" else fail_event):
                    with self.assertRaises(CandidateConfirmationCommitError) as raised:
                        self._explicit_select(ready)
                self.assertEqual(raised.exception.outcome["status"], expected)
                job_dir = self.service.store.job_directory(ready["job_id"])
                before = {p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()}
                if expected == "INCONSISTENT":
                    with self.assertRaises(ValueError):
                        self.service.bind_mesh(ready["job_id"], expected_revision=ready["revision"] + 1, original_filename="x.inp", content=b"*Heading", confirmed_contract={})
                self.assertEqual(before, {p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()})
                self.assertFalse((job_dir / "uploads").exists())

    def test_inverse_restart_records_interruption_once_without_conformal_attempt(self):
        from product_workflow.runtime_manager import RuntimeExecutionManager, reconcile_interrupted_product_jobs
        from product_workflow.store import JobStore

        for stage in ("TARGET_CONFIRMED", "INVERSE_RUNNING"):
            with self.subTest(stage=stage):
                job = self.service.create_job(self.target, model_runtime_profile=self.profile)
                replace = self.service.store.replace
                def interrupt_after_save(value, **kwargs):
                    saved = replace(value, **kwargs)
                    if saved["status"] == stage:
                        raise KeyboardInterrupt
                    return saved

                with patch.object(self.service.store, "replace", side_effect=interrupt_after_save):
                    with self.assertRaises(KeyboardInterrupt):
                        self.service.confirm_target_and_run_inverse(job["job_id"],
                            expected_revision=job["revision"], authorization={"authorized": True})
                store = JobStore(self.root)
                manager = RuntimeExecutionManager()
                self.assertEqual(reconcile_interrupted_product_jobs(store, manager), [job["job_id"]])
                held = store.get_consistent(job["job_id"])
                self.assertEqual((held["status"], held["workflow_disposition"]), ("FAILED", "HOLD"))
                self.assertEqual(held["failure"]["stage"], stage)
                self.assertEqual(held["failure"]["code"], "RUNTIME_PROCESS_INTERRUPTED")
                self.assertEqual(held["attempts"], [])
                self.assertEqual(build_product_job_projection(held)["view_state"], "failed")
                self.assertEqual(reconcile_interrupted_product_jobs(store, manager), [])
                self.assertEqual(self.runner_calls, [])

    def test_inverse_requires_explicit_authorization(self):
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.service.confirm_target_and_run_inverse(
                created["job_id"],
                expected_revision=created["revision"],
                authorization={"authorized": False},
            )
        self.assertEqual(self.runner_calls, [])

    def test_float32_boundary_rounding_passes_entry_and_candidate_recheck_with_evidence(self):
        physical = nine()
        physical["C11"] = 110.00000327825546
        target = target_from_physical_9d(physical, source_kind="csv_upload", source_sha256="A" * 64, source_name="synthetic-boundary.csv")
        original_normalized = dict(target["normalized_9d"])
        created = self.service.create_job(target, model_runtime_profile=self.profile)
        ready = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
        self.assertEqual(ready["status"], "CANDIDATES_READY")
        self.assertEqual(ready["target"]["normalized_9d"], original_normalized)
        self.assertEqual(ready["target"]["physical_9d_gpa"], physical)
        check = ready["target"]["range_check"]
        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["fields"][0]["effective_value_C_over_Eb"], 1.0)
        self.assertEqual(check["scaler_y_identity"]["sha256"], self.bundle["artifacts"]["scaler_y"]["sha256"])
        self.assertEqual(ready["candidate_set"]["target_validation"]["range_check"], check)
        self.assertEqual(ready["target"]["proposal_sha256"], canonical_sha256({k: v for k, v in ready["target"].items() if k != "proposal_sha256"}))
        from product_workflow.chat_tools import ChatTools
        view = ChatTools._job_view(None, self.service.store.get_consistent(ready["job_id"]))
        self.assertEqual(view["target"]["range_check"], check)
        self.assertEqual(len(self.runner_calls), 1)

    def test_range_and_p09_failures_are_distinct_and_never_start_inverse(self):
        cases = []
        range_only = nine()
        range_only["C11"] = 110.00001311302185
        cases.append((range_only, "SCALER_RANGE", "HOLD", "PASS"))
        p09_only = nine()
        p09_only.update(C11=1.0, C22=1.0, C33=1.0, C12=2.0, C13=2.0, C23=2.0)
        cases.append((p09_only, "P09", "PASS", "FAIL"))
        both = dict(p09_only, C44=220.0)
        cases.append((both, "BOTH", "HOLD", "FAIL"))
        for values, kind, range_status, p09_status in cases:
            with self.subTest(kind=kind):
                target = target_from_physical_9d(values, source_kind="csv_upload", source_sha256="A" * 64, source_name="synthetic.csv")
                created = self.service.create_job(target, model_runtime_profile=self.profile)
                failed = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
                self.assertEqual(failed["status"], "FAILED")
                self.assertEqual(failed["failure"].get("code"), kind)
                self.assertEqual(failed["target"]["scaler_range_status"], range_status)
                self.assertEqual(failed["target"]["p09_validation"]["status"], p09_status)
                self.assertEqual(failed["failure"]["details"]["range_check"], failed["target"]["range_check"])
                from product_workflow.chat_tools import ChatTools
                view = ChatTools._job_view(None, self.service.store.get_consistent(failed["job_id"]))
                self.assertEqual(view["target"]["range_check"], failed["target"]["range_check"])
                self.assertEqual(view["failure"], failed["failure"])
                self.assertNotIn("inverse_started", [item["event"] for item in self.service.store.read_events(created["job_id"])])
        self.assertEqual(self.runner_calls, [])

    def test_inconsistent_raw_and_physical_units_are_recorded_as_invalid_conversion(self):
        target = target_from_physical_9d(nine(), source_kind="csv_upload", source_sha256="A" * 64, source_name="synthetic.csv")
        target["normalized_9d"]["C11"] = 55.0
        target["proposal_sha256"] = canonical_sha256({k: v for k, v in target.items() if k != "proposal_sha256"})
        created = self.service.create_job(target, model_runtime_profile=self.profile)
        failed = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
        self.assertEqual(failed["failure"]["code"], "INVALID_CONVERSION")
        self.assertEqual(failed["target"]["p09_validation"]["status"], "HOLD")
        self.assertFalse(failed["target"]["p09_validation"]["evaluated"])
        self.assertEqual(failed["target"]["range_check"]["fields"][0]["error_code"], "NORMALIZED_PHYSICAL_MISMATCH")
        self.assertEqual(failed["target"]["normalized_9d"]["C11"], 55.0)
        self.assertEqual(self.runner_calls, [])

    def test_extreme_finite_target_persists_json_safe_hold_without_p09_or_inverse(self):
        target = target_from_physical_9d({name: 1e308 for name in STIFFNESS_NAMES}, source_kind="csv_upload", source_sha256="A" * 64, source_name="synthetic-extreme.csv")
        created = self.service.create_job(target, model_runtime_profile=self.profile)
        with patch("latticegpt_9x_candidate_ranking.np.linalg.eigvalsh", side_effect=AssertionError("invalid float32 target must not enter P09 eigenanalysis")):
            failed = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
        self.assertEqual(failed["failure"]["code"], "INVALID_CONVERSION")
        self.assertEqual(failed["target"]["p09_validation"]["status"], "HOLD")
        self.assertFalse(failed["target"]["p09_validation"]["evaluated"])
        self.assertIn("INVALID_FLOAT32_CONVERSION", failed["target"]["p09_validation"]["reasons"])
        self.assertEqual(self.service.store.get_consistent(failed["job_id"]), failed)
        self.assertEqual(self.runner_calls, [])

    def test_inverse_failure_persists_structured_service_evidence(self):
        self.service.inverse_runner = lambda target, **kwargs: {
            "status": "warning",
            "model_variant": "150w",
            "warnings": ["target_stiffness_out_of_recommended_range"],
            "errors": [],
            "steps": [{
                "step": "inference",
                "status": "success",
                "returncode": 0,
                "elapsed_ms": 125,
                "stdout_tail": "generated three candidates",
                "stderr_tail": "",
                "error": None,
            }],
            "candidates": [{"candidate_id": f"candidate-{index}"} for index in range(1, 4)],
            "cad_ready_count": 3,
            "work_dir_retained": False,
            "cleanup": {
                "status": "complete",
                "work_dir_removed": True,
                "raw_files_available": False,
            },
        }
        created = self.service.create_job(
            self.target, model_runtime_profile=self.profile
        )

        failed = self.service.confirm_target_and_run_inverse(
            created["job_id"],
            expected_revision=created["revision"],
            authorization={"authorized": True},
        )

        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["failure"]["stage"], "INVERSE_RUNNING")
        self.assertEqual(
            failed["failure"]["message"],
            "150w inverse service status=warning",
        )
        self.assertEqual(failed["inverse_result"]["status"], "warning")
        self.assertEqual(failed["inverse_result"]["candidate_count"], 3)
        self.assertEqual(
            failed["inverse_result"]["warnings"],
            ["target_stiffness_out_of_recommended_range"],
        )
        self.assertEqual(
            failed["inverse_result"]["steps"][0]["step"],
            "inference",
        )


if __name__ == "__main__":
    unittest.main()
