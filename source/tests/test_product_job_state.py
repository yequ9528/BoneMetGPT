from __future__ import annotations

from copy import deepcopy
import os
import json
from pathlib import Path
import shutil
import unittest
import uuid

from product_workflow.job_state import (
    build_product_job_projection,
    validate_job_snapshot_by_schema,
    validate_product_job_snapshot,
)
from product_workflow.store import JobStore
from product_workflow.manifest_io import canonical_json_object_v1, sha256_bytes


def _readonly_v2_job(*, extended=False):
    from tests.test_r3_job_history_boundary import valid_r3_job

    job = _job()
    job.update(valid_r3_job())
    job["conformal_reservation"] = None
    if extended:
        job["status"] = "VIEWER_READY"
        package_id = f"{job['job_id']}-attempt-0001"
        job["mesh_binding"] = {"relationship": {"upload_id": "upload-0002"}}
        job["attempts"] = [{
            "attempt_id": package_id, "package_id": package_id,
            "status": "VIEWER_READY", "package_verification": {"status": "passed"},
        }]
        job["formal_bundle_binding"]["binding_sha256"] = "A" * 64
        job["candidate_set"]["candidates"][0]["cad_ready"] = True
        job["inverse_result"] = {
            "schema_version": "latticegpt_inverse_result_summary_v1",
            "status": "success", "model_variant": "150w", "runtime_mode": "formal_product",
            "candidate_count": 1, "cad_ready_count": 1, "warnings": [], "errors": [],
            "steps": [], "cleanup": {"status": "complete", "raw_files_available": False,
                                      "work_dir_removed": True},
            "work_dir_retained": False, "formal_bundle_binding_sha256": "A" * 64,
        }
        job["mesh_precheck"] = {
            "schema_version": "latticegpt_9x_mesh_precheck_v1", "status": "failed",
            "source": {"path": "fixture/rejected.inp", "sha256": "B" * 64, "size_bytes": 1},
            "mesh": {}, "design_domain": {}, "geometry_quality": {},
            "errors": ["unsupported elements"], "warnings": [], "non_claims": [],
            "writes_files": False, "runs_runtime": False,
        }
        job["mesh_uploads"] = [{
            "upload_id": "upload-0001", "status": "REJECTED",
            "stored_path": "fixture/rejected.inp", "sha256": "B" * 64, "size_bytes": 1,
            "precheck": deepcopy(job["mesh_precheck"]),
        }]
    return job


def _readonly_entry(job, events_raw):
    raw = canonical_json_object_v1(job)
    job_id = job["job_id"]
    return {
        "job": {"path": f"jobs/{job_id}/job.json", "sha256": sha256_bytes(raw), "size_bytes": len(raw)},
        "events": {"path": f"jobs/{job_id}/events.jsonl", "sha256": sha256_bytes(events_raw), "size_bytes": len(events_raw)},
        "allowed_job_extensions": ["inverse_result", "mesh_precheck"],
        "package_id": f"{job_id}-attempt-0001", "package_format": "LEGACY_SIX_FILES_V1",
        "producer": {"generation": "fixture-v8", "source_manifest_sha256": "A" * 64,
                     "config_sha256": "B" * 64, "active_release_binding_sha256": "C" * 64},
        "package_files": {}, "supporting_files": [],
    }


def _job(status: str = "CANDIDATES_READY") -> dict:
    job_id = "job-ABCDEF0123456789ABCDEF01"
    job = {
        "schema_version": "latticegpt_design_job_v1",
        "job_id": job_id,
        "revision": 4,
        "status": status,
        "workflow_disposition": "ACTIVE",
        "created_at": "2026-08-02T00:00:00+00:00",
        "updated_at": "2026-08-02T00:04:00+00:00",
        "target": {"schema_version": "latticegpt_target_proposal_v1"},
        "model_runtime_profile": {"provider": "api", "model": "fixture"},
        "formal_bundle_binding": {"status": "PASS_5_OF_5"},
        "runtime_authorizations": [],
        "candidate_set": {
            "recommended_candidate_id": "candidate-1",
            "candidates": [{"candidate_id": "candidate-1", "eligible": True}],
        },
        "selection": {
            "selected_candidate_id": "candidate-1",
            "recommended_candidate_id": "candidate-1",
        },
        "mesh_binding": None,
        "mesh_uploads": [],
        "attempts": [],
        "failure": None,
    }
    return job


def _events(job: dict) -> list[dict]:
    return [
        {
            "event": "job_created" if revision == 0 else "fixture_transition",
            "job_id": job["job_id"],
            "revision": revision,
            "status": job["status"] if revision == job["revision"] else "TARGET_PROPOSED",
        }
        for revision in range(job["revision"] + 1)
    ]


class ProductJobStateTests(unittest.TestCase):
    def test_store_returns_only_snapshot_with_matching_event_tail(self):
        test_runtime_root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
        self.assertTrue(test_runtime_root)
        directory = Path(test_runtime_root) / f"job-state-{uuid.uuid4().hex}"
        try:
            store = JobStore(directory)
            created = store.create(
                {"schema_version": "latticegpt_target_proposal_v1"},
                model_runtime_profile={"provider": "api", "model": "fixture"},
                formal_bundle_binding={"status": "PASS_5_OF_5"},
            )

            restored = store.get_consistent(created["job_id"])

            self.assertEqual(restored["job_id"], created["job_id"])
            self.assertEqual(store.read_events(created["job_id"])[-1]["revision"], 0)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_schema_dispatch_validates_v1_v2_and_v3_event_continuity(self):
        from unittest.mock import patch

        base = {
            "job_id": "job-ABCDEF0123456789ABCDEF01",
            "revision": 0,
            "status": "TARGET_PROPOSED",
        }
        events = [{"event": "job_created", **base}]
        for schema, validator_name in (
            ("latticegpt_design_job_v1", "validate_product_job_snapshot"),
            ("latticegpt_design_job_v2", "validate_r3_job_snapshot_v2"),
            ("latticegpt_design_job_v3", "validate_r3_job_snapshot_v3"),
        ):
            job = {"schema_version": schema, **base}
            with self.subTest(schema=schema), patch(
                f"product_workflow.job_state.{validator_name}", return_value=job
            ) as validator:
                self.assertEqual(validate_job_snapshot_by_schema(job, events), job)
                validator.assert_called_once()

    def test_schema_dispatch_rejects_r3_event_gap_without_repair(self):
        from unittest.mock import patch

        job = {
            "schema_version": "latticegpt_design_job_v3",
            "job_id": "job-ABCDEF0123456789ABCDEF01",
            "revision": 2,
            "status": "TARGET_CONFIRMED",
        }
        events = [
            {"event": "job_created", "job_id": job["job_id"], "revision": 0, "status": "TARGET_PROPOSED"},
            {"event": "fixture", "job_id": job["job_id"], "revision": 2, "status": job["status"]},
        ]
        with patch(
            "product_workflow.job_state.validate_r3_job_snapshot_v3",
            return_value=job,
        ), self.assertRaisesRegex(ValueError, "event revisions are not contiguous"):
            validate_job_snapshot_by_schema(job, events)

    def test_store_get_consistent_reads_real_r3_v2_without_migration(self):
        from product_workflow.reproducibility import (
            build_comparison_contract_v1,
            comparison_contract_sha256_v1,
        )
        from tests.test_r3_reproduction_profile import valid_profile

        test_runtime_root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
        self.assertTrue(test_runtime_root)
        directory = Path(test_runtime_root) / f"job-state-r3-v2-{uuid.uuid4().hex}"
        try:
            store = JobStore(directory)
            profile = valid_profile()
            contract = build_comparison_contract_v1(
                tolerance_schema_path=profile["tolerance"]["schema_path"],
                tolerance_schema_sha256=profile["tolerance"]["schema_sha256"],
            )
            created = store.create_r3(
                {"schema_version": "target-v1"},
                model_runtime_profile={"schema_version": "model-profile-v1"},
                formal_bundle_binding={"schema_version": "bundle-v1"},
                reproduction_profile=profile,
                comparison_contract=contract,
                comparison_contract_sha256=comparison_contract_sha256_v1(contract),
            )
            restored = store.get_consistent(created["job_id"])
            self.assertEqual(restored, created)
            self.assertEqual(restored["schema_version"], "latticegpt_design_job_v2")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_candidate_projection_exposes_exact_identity_and_safe_capabilities(self):
        job = _job()

        validated = validate_product_job_snapshot(job, _events(job))
        projection = build_product_job_projection(validated)

        self.assertEqual(projection["job_id"], job["job_id"])
        self.assertEqual(projection["revision"], 4)
        self.assertEqual(projection["workflow_disposition"], "ACTIVE")
        self.assertEqual(projection["view_state"], "candidate")
        self.assertTrue(projection["capabilities"]["resume"])
        self.assertTrue(projection["capabilities"]["select_candidate"])
        self.assertFalse(projection["capabilities"]["upload_mesh"])
        self.assertTrue(projection["capabilities"]["view_in_viewer"])
        self.assertFalse(projection["capabilities"]["start_conformal"])

    def test_confirmation_is_closed_and_bound_to_job_collection_payload_revision_and_utc(self):
        from tests.test_product_chainlit_mesh_upload import _confirm_fixture
        from product_workflow.job_state import candidate_confirmation_status
        from product_workflow.contracts import canonical_sha256
        job = _confirm_fixture(_job())
        self.assertEqual(candidate_confirmation_status(job), "CONFIRMED")
        for field, value in (("schema_version", "unknown"), ("method", "inferred"), ("job_id", "job-" + "F"*24),
                             ("candidate_set_sha256", "F"*64), ("selected_candidate_payload_sha256", "F"*64),
                             ("action_revision", True), ("confirmed_revision", 99), ("confirmed_at", "2026-09-18T10:00:00"), ("unexpected", 1)):
            corrupted = deepcopy(job)
            confirmation = corrupted["selection"]["confirmation"]
            confirmation[field] = value
            confirmation["confirmation_sha256"] = canonical_sha256({k:v for k,v in confirmation.items() if k != "confirmation_sha256"})
            corrupted["selection"]["selection_sha256"] = canonical_sha256({k:v for k,v in corrupted["selection"].items() if k != "selection_sha256"})
            self.assertEqual(candidate_confirmation_status(corrupted), "INVALID", field)
            with self.assertRaises(ValueError):
                validate_product_job_snapshot(corrupted, _events(corrupted))

    def test_authorization_projection_allows_one_runtime_start_only(self):
        job = _job("AWAITING_CONFORMAL_AUTHORIZATION")
        from tests.test_product_chainlit_mesh_upload import _confirm_fixture
        _confirm_fixture(job)
        job["mesh_binding"] = {"mesh_binding_sha256": "A" * 64}

        projection = build_product_job_projection(
            validate_product_job_snapshot(job, _events(job))
        )

        self.assertEqual(projection["view_state"], "mesh")
        self.assertTrue(projection["capabilities"]["start_conformal"])

        job["attempts"] = [{"attempt_id": f"{job['job_id']}-attempt-0001"}]
        with self.assertRaisesRegex(ValueError, "must not contain a runtime attempt"):
            validate_product_job_snapshot(job, _events(job))

    def test_event_revision_gap_is_rejected_instead_of_repaired(self):
        job = _job()
        events = _events(job)[:-1]

        with self.assertRaisesRegex(ValueError, "event revisions are not contiguous"):
            validate_product_job_snapshot(job, events)

    def test_viewer_ready_requires_one_verified_attempt(self):
        job = _job("VIEWER_READY")
        job["mesh_binding"] = {"mesh_binding_sha256": "A" * 64}
        job["attempts"] = [
            {
                "attempt_id": f"{job['job_id']}-attempt-0001",
                "status": "VIEWER_READY",
                "package_id": f"{job['job_id']}-attempt-0001",
                "package_verification": {"status": "passed"},
            }
        ]

        projection = build_product_job_projection(
            validate_product_job_snapshot(job, _events(job))
        )

        self.assertEqual(projection["view_state"], "result")
        self.assertFalse(projection["capabilities"]["start_conformal"])

        corrupt = deepcopy(job)
        corrupt["attempts"].append(deepcopy(corrupt["attempts"][0]))
        with self.assertRaisesRegex(ValueError, "at most one runtime attempt"):
            validate_product_job_snapshot(corrupt, _events(corrupt))

    def test_interrupted_runtime_is_terminal_hold(self):
        job = _job("HOLD_RUNTIME_INTERRUPTED")
        job["workflow_disposition"] = "HOLD"
        job["mesh_binding"] = {"mesh_binding_sha256": "A" * 64}
        job["attempts"] = [
            {
                "attempt_id": f"{job['job_id']}-attempt-0001",
                "status": "HOLD_RUNTIME_INTERRUPTED",
            }
        ]
        job["failure"] = {
            "stage": "CONFORMAL_RUNNING",
            "code": "RUNTIME_PROCESS_INTERRUPTED",
            "message": "runtime process stopped before a terminal result",
        }

        projection = build_product_job_projection(
            validate_product_job_snapshot(job, _events(job))
        )

        self.assertEqual(projection["view_state"], "failed")
        self.assertEqual(projection["workflow_disposition"], "HOLD")
        self.assertFalse(projection["capabilities"]["start_conformal"])


class ReadonlyProductJobStateTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
        self.assertTrue(root)
        self.directory = Path(root) / f"viewer-job-reader-{uuid.uuid4().hex}"

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def fixture(self, job, *, registered=False, events=None, raw=None):
        job_dir = self.directory / "jobs" / job["job_id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        events_raw = b"".join(canonical_json_object_v1(item) for item in (_events(job) if events is None else events))
        (job_dir / "job.json").write_bytes(canonical_json_object_v1(job) if raw is None else raw)
        (job_dir / "events.jsonl").write_bytes(events_raw)
        registry = {"schema_version": "latticegpt_viewer_readonly_compatibility_v1",
                    "jobs": {job["job_id"]: _readonly_entry(job, events_raw)} if registered else {}}
        return JobStore(self.directory, viewer_compatibility=registry), registry

    def projection(self, job, entry=None):
        from product_workflow.job_state import build_readonly_product_job_projection
        return build_readonly_product_job_projection(job, compatibility_entry=entry)

    def test_viewer_reads_v1_and_canonical_v2_from_root_schema_with_only_readonly_capabilities(self):
        historical = _job()
        historical.update(source_status="PASS_R2_SOURCE_IDENTITY", source_prefix="release/v1/r2")
        historical["target"]["schema_version"] = "latticegpt_design_job_v2"
        historical["inverse_result"] = {"status": "success"}
        for job in (_job(), historical, _readonly_v2_job()):
            with self.subTest(schema=job["schema_version"]):
                store, _ = self.fixture(job)
                before = {p.name: p.read_bytes() for p in store.job_directory(job["job_id"]).iterdir()}
                actual = store.get_consistent_for_viewer(job["job_id"])
                self.assertEqual(actual, job)
                projection = self.projection(actual)
                self.assertTrue(projection["capabilities"]["refresh"])
                self.assertTrue(projection["capabilities"]["view_in_viewer"])
                self.assertFalse(projection["generation_classification"]["runtime_permitted"])
                if "source_status" in job:
                    self.assertEqual(projection["generation_classification"]["classification"],
                                     "HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED")
                self.assertTrue(all(not enabled for role, enabled in projection["capabilities"].items()
                                    if role not in {"refresh", "view_in_viewer"}))
                self.assertEqual(before, {p.name: p.read_bytes() for p in store.job_directory(job["job_id"]).iterdir()})

    def test_known_v2_extensions_use_one_contract_and_preserve_registered_bytes(self):
        job = _readonly_v2_job(extended=True)
        store, registry = self.fixture(job, registered=True)
        entry = registry["jobs"][job["job_id"]]
        original = canonical_json_object_v1(job)
        self.assertEqual(store.get_consistent_for_viewer(job["job_id"]), job)
        self.assertEqual(self.projection(job, entry)["view_state"], "result")
        self.assertEqual((store.job_directory(job["job_id"]) / "job.json").read_bytes(), original)
        self.assertEqual(validate_job_snapshot_by_schema(job), job)
        self.assertEqual(store.get_consistent(job["job_id"]), job)
        self.assertEqual(self.projection(job)["view_state"], "result")
        registry["jobs"][job["job_id"]]["job"]["sha256"] = "0" * 64
        self.assertEqual(store.get_consistent_for_viewer(job["job_id"]), job)

    def test_first_accepted_mesh_inverse_only_v2_needs_no_job_whitelist(self):
        job = _readonly_v2_job(extended=True)
        del job["mesh_precheck"]
        job["mesh_uploads"] = []
        store, _ = self.fixture(job)
        self.assertEqual(store.get_consistent(job["job_id"]), job)
        self.assertEqual(store.get_consistent_for_viewer(job["job_id"]), job)
        self.assertEqual(build_product_job_projection(job)["view_state"], "result")

    def test_repeated_compact_rejected_mesh_diagnostics_remain_readable(self):
        job = _readonly_v2_job(extended=True)
        job["mesh_precheck"] = {"status": "failed", "errors": ["precheck exception"], "warnings": []}
        job["mesh_uploads"] = [{"status": "REJECTED", "precheck": deepcopy(job["mesh_precheck"])} for _ in range(2)]
        store, _ = self.fixture(job)
        self.assertEqual(store.get_consistent(job["job_id"]), job)

    def test_ordinary_reader_uses_root_schema_and_rejects_duplicate_events(self):
        job = _job()
        job["target"]["schema_version"] = "latticegpt_design_job_v2"
        store, _ = self.fixture(job, raw=json.dumps(job, indent=2).encode())
        self.assertEqual(store.get_consistent(job["job_id"]), job)
        path = store.job_directory(job["job_id"]) / "events.jsonl"
        path.write_bytes(path.read_bytes().replace(b'"revision":0', b'"revision":0,"revision":0'))
        with self.assertRaises(ValueError):
            store.get_consistent(job["job_id"])

    def test_synthetic_inflight_v2_reconciles_once_and_terminal_is_untouched(self):
        from product_workflow.runtime_manager import RuntimeExecutionManager, reconcile_interrupted_product_jobs
        job = _readonly_v2_job(extended=True)
        job["status"] = job["attempts"][0]["status"] = "CONFORMAL_RUNNING"
        store, _ = self.fixture(job)
        self.assertEqual(reconcile_interrupted_product_jobs(store, RuntimeExecutionManager()), [job["job_id"]])
        held = store.get_consistent(job["job_id"])
        self.assertEqual(held["status"], "HOLD_RUNTIME_INTERRUPTED")
        self.assertEqual(len(held["attempts"]), 1)
        self.assertEqual(reconcile_interrupted_product_jobs(store, RuntimeExecutionManager()), [])
        self.assertEqual(store.get_consistent(job["job_id"]), held)

    def test_inverse_failure_shapes_are_readable_without_success_cleanup_claim(self):
        base = _readonly_v2_job(extended=True)
        base.update(status="FAILED", workflow_disposition="HOLD", candidate_set=None, selection=None,
                    failure={"stage": "INVERSE_RUNNING", "message": "synthetic failure"}, attempts=[], mesh_binding=None, mesh_uploads=[])
        del base["mesh_precheck"]
        from product_workflow.service import ProductWorkflowService
        results = [None, {"status": "failed", "errors": ["producer failure"]},
                   {"status": "success", "candidates": [{"bad": "candidate"}], "errors": [], "steps": []}]
        for result in results:
            job = deepcopy(base)
            job["inverse_result"] = ProductWorkflowService._inverse_result_summary(result, formal_bundle_binding_sha256="A"*64)
            store, _ = self.fixture(job)
            self.assertEqual(store.get_consistent(job["job_id"]), job)
            self.assertEqual(store.get_consistent_for_viewer(job["job_id"]), job)
        bad = deepcopy(job)
        bad["inverse_result"]["cleanup"] = {"status": "failed", "raw_files_available": 1, "work_dir_removed": False}
        with self.assertRaises(ValueError): validate_job_snapshot_by_schema(bad)

    def test_extended_projection_rejects_identity_and_extension_semantic_drift(self):
        source = _readonly_v2_job(extended=True)
        for index, change in enumerate((lambda j: j.update(unreviewed=True),
                       lambda j: j["inverse_result"].update(candidate_count=2),
                       lambda j: j["inverse_result"].update(candidate_count=True),
                       lambda j: j["inverse_result"].update(cad_ready_count=0),
                       lambda j: j["inverse_result"].update(status="failed"),
                       lambda j: j["inverse_result"].update(formal_bundle_binding_sha256="D" * 64),
                       lambda j: j["mesh_uploads"][0].update(status="ACCEPTED"),
                       lambda j: j["attempts"].append(deepcopy(j["attempts"][0])))):
            job = deepcopy(source)
            change(job)
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.projection(job, _readonly_entry(job, b""))
        altered = deepcopy(source)
        altered["updated_at"] = "changed"
        with self.assertRaises(ValueError):
            self.projection(altered, _readonly_entry(source, b""))

    def test_viewer_rejects_unknown_root_duplicate_nonfinite_and_noncanonical_r3(self):
        job = _readonly_v2_job()
        raw = canonical_json_object_v1(job)
        variants = [raw.replace(b'"schema_version":"latticegpt_design_job_v2"', b'"schema_version":"unknown"'),
                    raw.replace(b'"revision":4', b'"revision":4,"revision":4'),
                    raw.replace(b'"revision":4', b'"revision":NaN'),
                    json.dumps(job, indent=2).encode() + b"\n", b"\xef\xbb\xbf" + raw]
        for malformed in variants:
            with self.subTest(raw=malformed[:80]):
                store, _ = self.fixture(job, raw=malformed)
                with self.assertRaises(ValueError):
                    store.get_consistent_for_viewer(job["job_id"])

    def test_viewer_rejects_event_gap_tail_mismatch_duplicate_and_byte_drift(self):
        job = _readonly_v2_job(extended=True)
        for events in (_events(job)[:-1], [*_events(job)[:-1], {**_events(job)[-1], "status": "FAILED"}]):
            store, _ = self.fixture(job, registered=True, events=events)
            with self.assertRaises(ValueError):
                store.get_consistent_for_viewer(job["job_id"])
        store, _ = self.fixture(job, registered=True)
        events_path = store.job_directory(job["job_id"]) / "events.jsonl"
        events_path.write_bytes(events_path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            store.get_consistent_for_viewer(job["job_id"])
        store, _ = self.fixture(_readonly_v2_job())
        raw = events_path.read_bytes().replace(b'"revision":0', b'"revision":0,"revision":0')
        events_path.write_bytes(raw)
        with self.assertRaises(ValueError):
            store.get_consistent_for_viewer(job["job_id"])

    def test_viewer_entry_paths_cannot_escape_or_bind_another_job(self):
        job = _readonly_v2_job(extended=True)
        _, registry = self.fixture(job, registered=True)
        for path in ("../job.json", "jobs/job-000000000000000000000000/job.json"):
            registry["jobs"][job["job_id"]]["job"]["path"] = path
            store = JobStore(self.directory, viewer_compatibility=registry)
            with self.assertRaises(ValueError):
                store.get_consistent_for_viewer(job["job_id"])

    def test_viewer_nonexistent_job_keeps_safe_missing_evidence_message(self):
        store = JobStore(self.directory)
        with self.assertRaisesRegex(FileNotFoundError, "job does not exist: job-"):
            store.get_consistent_for_viewer(_job()["job_id"])


if __name__ == "__main__":
    unittest.main()
