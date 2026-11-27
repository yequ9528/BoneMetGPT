import csv
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import patch, MagicMock
from contextlib import contextmanager
from pathlib import Path


STIFFNESS = ("C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66")


@contextmanager
def local_test_directory():
    test_root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
    if not test_root:
        raise AssertionError(
            "LATTICEGPT_TEST_RUNTIME_ROOT must name the explicit test root"
        )
    root = Path(test_root) / f"product-targets-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root)


class ProductWorkflowTargetTests(unittest.TestCase):
    def test_database_record_builds_bound_physical_target(self):
        from product_workflow.contracts import target_from_database_record

        record = {"sample_id": 7, **{name: 0.01 * (i + 1) for i, name in enumerate(STIFFNESS)}}
        proposal = target_from_database_record(
            record,
            source_sha256="a" * 64,
            query_identity="query-1",
        )

        self.assertEqual(proposal["schema_version"], "latticegpt_target_proposal_v1")
        self.assertEqual(proposal["status"], "TARGET_PROPOSED")
        self.assertEqual(proposal["source"]["kind"], "formal_150w_database")
        self.assertEqual(proposal["normalized_9d"]["C11"], 0.01)
        self.assertAlmostEqual(proposal["physical_9d_gpa"]["C11"], 1.1)
        self.assertEqual(proposal["base_material"], {"name": "TC4", "elastic_modulus_gpa": 110.0})
        self.assertRegex(proposal["proposal_sha256"], r"^[0-9A-F]{64}$")

    def test_csv_physical_target_uses_same_contract(self):
        from product_workflow.contracts import target_from_physical_9d

        target = {name: 11.0 * (i + 1) for i, name in enumerate(STIFFNESS)}
        proposal = target_from_physical_9d(
            target,
            source_kind="csv_upload",
            source_sha256="b" * 64,
            source_name="target.csv",
        )

        self.assertEqual(proposal["source"]["kind"], "csv_upload")
        self.assertEqual(proposal["physical_9d_gpa"], target)
        self.assertAlmostEqual(proposal["normalized_9d"]["C11"], 0.1)
        self.assertFalse(proposal["confirmed"])

    def test_query_plan_rejects_raw_sql_unknown_fields_and_large_limits(self):
        from product_workflow.formal_150w_query import validate_query_plan

        with self.assertRaisesRegex(ValueError, "raw SQL"):
            validate_query_plan({"sql": "select * from targets"})
        with self.assertRaisesRegex(ValueError, "unsupported query field"):
            validate_query_plan({"filters": [{"field": "nodes", "op": "eq", "value": 1}]})
        with self.assertRaisesRegex(ValueError, "limit"):
            validate_query_plan({"filters": [], "order_by": [], "limit": 6})

    def test_projection_accepts_authoritative_boolean_flag_encoding(self):
        from product_workflow.formal_150w_query import _eligible

        row = {
            "is_singular": "False",
            "is_pd_full": "True",
            "has_periodic_mismatch": "0",
            "is_degenerate": "0",
            "relative_density": "0.2",
            "diameter": "0.1",
            **{name: "0.01" for name in STIFFNESS},
        }
        self.assertTrue(_eligible(row))

    def test_projection_filters_ineligible_rows_and_queries_top_five(self):
        from product_workflow.formal_150w_query import (
            Formal150wQueryService,
            build_projection,
        )

        with local_test_directory() as root:
            source = root / "formal.csv"
            db_path = root / "formal.sqlite"
            manifest_path = root / "formal.manifest.json"
            fieldnames = [
                "sample_id", "relative_density", "diameter", "is_singular",
                "is_pd_full", "has_periodic_mismatch", "is_degenerate", *STIFFNESS,
            ]
            rows = []
            for sample_id in range(1, 8):
                rows.append({
                    "sample_id": sample_id,
                    "relative_density": 0.05 * sample_id,
                    "diameter": 0.01 * sample_id,
                    "is_singular": 0,
                    "is_pd_full": 1,
                    "has_periodic_mismatch": 1 if sample_id == 6 else 0,
                    "is_degenerate": 1 if sample_id == 7 else 0,
                    **{name: 0.001 * sample_id * (i + 1) for i, name in enumerate(STIFFNESS)},
                })
            with source.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            source_sha = hashlib.sha256(source.read_bytes()).hexdigest().upper()
            result = build_projection(
                source,
                db_path,
                manifest_path,
                expected_source_sha256=source_sha,
                overwrite=False,
            )

            self.assertEqual(result["eligible_rows"], 5)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"]["sha256"], source_sha)
            self.assertEqual(
                manifest["generator"]["identity"],
                "product_workflow.formal_150w_query.build_projection",
            )
            self.assertFalse(manifest["query_contract"]["raw_sql_allowed"])
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("select count(*) from targets").fetchone()[0], 5)
            finally:
                connection.close()

            service = Formal150wQueryService(
                db_path,
                manifest_path,
                expected_source_sha256=source_sha,
            )
            matches = service.query({
                "filters": [{"field": "C11", "op": "gte", "value": 0.001}],
                "order_by": [{"field": "C11", "direction": "desc"}],
                "limit": 5,
            })
            self.assertEqual([item["sample_id"] for item in matches], [5, 4, 3, 2, 1])

    def test_query_service_rejects_manifest_source_identity_mismatch(self):
        from product_workflow.formal_150w_query import Formal150wQueryService

        with local_test_directory() as root:
            db_path = root / "formal.sqlite"
            sqlite3.connect(db_path).close()
            manifest_path = root / "formal.manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": "latticegpt_formal_150w_query_projection_v1",
                "source": {"sha256": "A" * 64},
                "projection": {"sha256": hashlib.sha256(db_path.read_bytes()).hexdigest().upper()},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "数据来源身份不匹配"):
                Formal150wQueryService(
                    db_path,
                    manifest_path,
                    expected_source_sha256="B" * 64,
                )


class DatabaseReadContractTests(unittest.TestCase):
    def setUp(self):
        from product_workflow import formal_150w_query as db
        self.db = db
        self.directory = local_test_directory()
        self.root = self.directory.__enter__()
        self.addCleanup(self.directory.__exit__, None, None, None)
        source = self.root / "source.csv"
        columns = [*db.PROJECTION_COLUMNS, "is_singular", "is_pd_full", "has_periodic_mismatch", "is_degenerate"]
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for number in range(1, 9):
                writer.writerow({"sample_id": number, "relative_density": 0.2, "diameter": 0.1,
                    **{name: number * 0.01 for name in STIFFNESS}, "is_singular": 0, "is_pd_full": 1,
                    "has_periodic_mismatch": 0, "is_degenerate": 0})
        self.source_sha = db._sha256_file(source)
        self.sqlite = self.root / "data.sqlite"
        self.manifest = self.root / "manifest.json"
        db.build_projection(source, self.sqlite, self.manifest, expected_source_sha256=self.source_sha)
        self.service = self.make_service()
        self.plan = {"filters": [{"field": "C11", "op": "gte", "value": 0.02}], "order_by": [], "limit": 5}
        self.before = {p.name: p.read_bytes() for p in self.root.iterdir()}

    def make_service(self):
        return self.db.Formal150wQueryService(self.sqlite, self.manifest, expected_source_sha256=self.source_sha,
            expected_manifest_sha256=self.db._sha256_file(self.manifest), expected_projection_sha256=self.db._sha256_file(self.sqlite))

    def request(self, kind, **values):
        return {"schema_version": self.db.DATABASE_REQUEST_SCHEMA, "kind": kind, **values}

    def test_new_and_legacy_queries_and_frozen_plan_share_sql_and_keep_readonly_bytes(self):
        from product_workflow.inputs import replay_frozen_query_plan, proposals_from_formal_query
        with patch.object(self.db.sqlite3, "connect", wraps=self.db.sqlite3.connect) as connect:
            overview = self.service.execute_request(self.request("overview"))
            self.assertEqual(overview["data"]["eligible_rows"], 8)
            connect.assert_not_called()
            old = self.service.query(self.plan)
            new = self.service.execute_request(self.request("records", **self.plan))
            count = self.service.execute_request(self.request("count", filters=self.plan["filters"]))
            frozen = replay_frozen_query_plan(self.plan)
            proposals = proposals_from_formal_query(frozen["query_plan"], self.service)
            self.assertEqual(new["data"]["rows"], old)
            self.assertEqual(len(proposals), 5)
            self.assertEqual(count["data"]["matched_count"], 7)
            self.assertEqual(self.service.count([]), 8)
            for call in connect.call_args_list:
                self.assertTrue(call.args[0].endswith("?mode=ro"))
                self.assertEqual(call.kwargs["timeout"], 1.0)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, self.before)
        empty = self.service.execute_request(self.request("count", filters=[{"field": "sample_id", "op": "eq", "value": 999}]))
        self.assertEqual((empty["status"], empty["data"]["matched_count"]), ("ok", 0))

    def test_strict_new_request_does_not_reinterpret_legacy_plan(self):
        base = self.request("records", **self.plan)
        invalid = [dict(base, limit=True), dict(base, limit=1.0), dict(base, sql="SELECT 1"),
                   self.request("count", filters=[], limit=5), self.request("overview", filters=[]),
                   dict(base, filters=self.plan["filters"] * 9)]
        for value in (True, float("nan"), float("inf"), "1 -- comment", 1.5):
            invalid.append(dict(base, filters=[{"field": "sample_id", "op": "eq", "value": value}]))
        invalid.extend([dict(base, filters=[{"field": "C11", "op": "between", "value": [2, 1]}]),
                        '{"schema_version":"latticegpt_database_read_request_v1","kind":"count","kind":"records","filters":[]}'])
        for request in invalid:
            with self.subTest(request=str(request)):
                result = self.service.execute_request(request)
                self.assertEqual(result["error"]["code"], "INVALID_QUERY")
                self.assertIsNone(result["data"])
                self.assertFalse(result["error"]["automatic_retry"])
        self.assertEqual(self.service.execute_request(self.request("sum"))["error"]["code"], "UNSUPPORTED_QUERY_KIND")
        self.assertEqual(len(self.service.query(dict(self.plan, filters=self.plan["filters"] * 9))), 5)

    def test_all_actual_sql_entrypoints_share_time_and_instruction_budgets_and_release_slot(self):
        from product_workflow.inputs import replay_frozen_query_plan, proposals_from_formal_query
        calls = [lambda: self.service.query(self.plan),
                 lambda: self.service.execute_request(self.request("records", **self.plan)),
                 lambda: self.service.execute_request(self.request("count", filters=[])),
                 lambda: proposals_from_formal_query(replay_frozen_query_plan(self.plan)["query_plan"], self.service)]
        for name, budget, reason in (("SQL_TIME_BUDGET_SECONDS", -1, "TIME_BUDGET"), ("SQL_INSTRUCTION_BUDGET", 1, "INSTRUCTION_BUDGET")):
            for call in calls:
                with self.subTest(name=name, call=call), patch.object(self.db, name, budget), patch.object(self.db, "SQL_PROGRESS_INTERVAL", 1):
                    try:
                        result = call()
                        error = result["error"]
                    except self.db.ReadonlyQueryError as exc:
                        error = exc.error
                    self.assertEqual(error["code"], "QUERY_LIMIT_EXCEEDED")
                    self.assertEqual(error["limit_reason"], reason)
                self.assertEqual(self.make_service().count([]), 8)

    def test_global_slot_database_lock_connect_execute_fetch_failure_cleanup(self):
        other = self.make_service()
        self.db._SQL_SLOT.acquire()
        try:
            self.assertEqual(other.execute_request(self.request("count", filters=[]))["error"]["code"], "QUERY_BUSY")
        finally:
            self.db._SQL_SLOT.release()
        lock = sqlite3.connect(self.sqlite)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            self.assertEqual(other.execute_request(self.request("count", filters=[]))["error"]["code"], "DATABASE_BUSY")
        finally:
            lock.rollback()
            lock.close()
        with patch.object(self.db.sqlite3, "connect", side_effect=sqlite3.OperationalError("unavailable")):
            self.assertEqual(other.execute_request(self.request("count", filters=[]))["error"]["code"], "DATABASE_UNAVAILABLE")
        for stage in ("execute", "fetch"):
            connection = MagicMock()
            if stage == "execute":
                connection.execute.side_effect = [None, sqlite3.OperationalError("execute fault")]
            else:
                connection.execute.return_value.fetchall.side_effect = sqlite3.OperationalError("fetch fault")
            with patch.object(self.db.sqlite3, "connect", return_value=connection):
                self.assertEqual(other.execute_request(self.request("count", filters=[]))["error"]["code"], "QUERY_EXECUTION_FAILED")
            connection.set_progress_handler.assert_called_with(None, 0)
            connection.close.assert_called_once()
            self.assertEqual(other.count([]), 8)

    def test_identity_drift_invalidates_metadata_and_discards_results(self):
        self.manifest.write_text(self.manifest.read_text() + " ", encoding="utf-8")
        result = self.service.execute_request(self.request("overview"))
        self.assertEqual(result["error"]["code"], "BINDING_MISMATCH")
        self.assertIsNone(result["provenance"]["manifest_sha256"])
        self.manifest.unlink()
        self.assertEqual(self.service.execute_request(self.request("count", filters=[]))["error"]["code"], "DATABASE_UNAVAILABLE")

    def test_units_deterministic_count_and_untrusted_planner_shapes(self):
        from product_workflow.inputs import plan_database_read
        self.assertEqual(plan_database_read("C11大于11 GPa的数量")["request"]["filters"][0]["value"], 0.1)
        self.assertEqual(plan_database_read("sample_id=2")["request"]["limit"], 1)
        self.assertEqual(plan_database_read("数据库共多少条")["request"]["kind"], "overview")
        self.assertEqual(plan_database_read("C11大于0.1的数量")["status"], "needs_clarification")
        for response in ("not JSON", {"kind": "design"}, self.request("count", filters=[], sql="DROP")):
            with self.assertRaises((ValueError, self.db.ReadonlyQueryError)):
                plan_database_read("C11较高 C/Eb", planner=lambda _: response)


class Float32TargetRangeTests(unittest.TestCase):
    """Synthetic values only; never load a released scaler or model."""

    def test_effective_values_match_production_csv_reader_and_torch_at_ties(self):
        import numpy as np
        import pandas as pd
        import torch
        from services.truss_inverse_service import _write_target_csv

        targets = []
        for value in (0.11651468508929674, 0.11651468276977539, 0.11651469022035599, 0.1, 1.0):
            lower = np.float32(value)
            upper = np.nextafter(lower, np.float32(np.inf))
            midpoint = (float(lower) + float(upper)) / 2.0
            for raw in (value, midpoint, np.nextafter(midpoint, -np.inf), np.nextafter(midpoint, np.inf)):
                targets.append({name: float(raw) * 110.0 for name in STIFFNESS})
        with local_test_directory() as root:
            path = root / "synthetic_target.csv"
            _write_target_csv(path, targets)
            before = path.read_bytes()
            actual = torch.tensor(pd.read_csv(path)[list(STIFFNESS)].values / 110.0, dtype=torch.float32)
            for index, target in enumerate(targets):
                with self.subTest(index=index):
                    check = self.check(target, maximum=2.0)
                    self.assertEqual([item["effective_value_C_over_Eb"] for item in check["fields"]],
                                     actual[index].tolist())
            self.assertEqual(path.read_bytes(), before)

    def check(self, values, *, minimum=0.0, maximum=1.0, raw=None):
        import latticegpt_9x_candidate_ranking as module

        checker = getattr(module, "check_target_scaler_range", None)
        self.assertTrue(callable(checker), "the shared float32 range predicate is required")
        return checker(
            values,
            scaler_y_min={name: minimum for name in STIFFNESS},
            scaler_y_max={name: maximum for name in STIFFNESS},
            raw_normalized_9d=raw,
            scaler_y_identity={"path": "synthetic/scaler_Y.pt", "sha256": "A" * 64},
        )

    def test_half_ulp_overshoot_rounds_to_boundary_without_changing_input(self):
        values = {name: 55.0 for name in STIFFNESS}
        values["C11"] = 110.00000327825546
        before = dict(values)
        result = self.check(values)
        self.assertEqual(result["status"], "PASS")
        c11 = result["fields"][0]
        self.assertGreater(c11["raw_delta_from_maximum"], 0.0)
        self.assertEqual(c11["effective_value_C_over_Eb"], 1.0)
        self.assertEqual(c11["effective_delta_from_maximum"], 0.0)
        self.assertEqual(c11["rounding_classification"], "ROUNDED_TO_BOUNDARY")
        self.assertEqual(result["scaler_y_identity"]["sha256"], "A" * 64)
        self.assertEqual(values, before)
        json.dumps(result, allow_nan=False)

    def test_exact_endpoints_and_inner_float32_neighbors_pass_outer_neighbors_hold(self):
        cases = (
            (0.5, "PASS"),
            (1.0, "PASS"),
            (0.5000000596046448, "PASS"),
            (0.9999999403953552, "PASS"),
            (0.4999999701976776, "HOLD"),
            (1.0000001192092896, "HOLD"),
            (1.1, "HOLD"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                result = self.check({name: value * 110.0 for name in STIFFNESS}, minimum=0.5)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["fields"][0]["effective_in_range"], expected == "PASS")

    def test_lower_rounding_and_float64_boundaries_are_not_widened(self):
        lower_rounding = self.check({name: 54.999999180436134 for name in STIFFNESS}, minimum=0.5)
        self.assertEqual(lower_rounding["status"], "PASS")
        self.assertEqual(lower_rounding["fields"][0]["effective_value_C_over_Eb"], 0.5)
        strict = self.check({name: 110.0 for name in STIFFNESS}, maximum=0.99999999)
        self.assertEqual(strict["status"], "HOLD")
        self.assertEqual(strict["fields"][0]["maximum"], 0.99999999)

    def test_nonfinite_overflow_missing_units_or_component_mismatch_fail_closed(self):
        for value in (float("nan"), float("inf"), 1e300, True):
            with self.subTest(value=value):
                result = self.check({name: value for name in STIFFNESS})
                self.assertEqual(result["status"], "HOLD")
                self.assertEqual(result["failure_kind"], "INVALID_CONVERSION")
                json.dumps(result, allow_nan=False)
        missing = {name: 55.0 for name in STIFFNESS if name != "C66"}
        self.assertEqual(self.check(missing)["failure_kind"], "INVALID_CONVERSION")
        raw = {name: 0.5 for name in STIFFNESS}
        raw["C11"] = 55.0  # GPa supplied as C/Eb must never pass silently.
        mismatch = self.check({name: 55.0 for name in STIFFNESS}, raw=raw)
        self.assertEqual(mismatch["failure_kind"], "INVALID_CONVERSION")
        self.assertEqual(mismatch["fields"][0]["error_code"], "NORMALIZED_PHYSICAL_MISMATCH")
        invalid_bounds = self.check({name: 55.0 for name in STIFFNESS}, maximum=float("nan"))
        self.assertEqual(invalid_bounds["failure_kind"], "INVALID_CONVERSION")
        json.dumps(invalid_bounds, allow_nan=False)
        extreme = self.check({name: 1e308 for name in STIFFNESS}, minimum=-1e308, maximum=1e308,
                             raw={name: 1e308 for name in STIFFNESS})
        self.assertEqual(extreme["failure_kind"], "INVALID_CONVERSION")
        json.dumps(extreme, allow_nan=False)

    def test_p10_wrapper_uses_the_same_float32_evidence(self):
        from latticegpt_9x_p10_manifest import _check_target_range

        target = {name: 55.0 for name in STIFFNESS}
        target["C11"] = 110.00000327825546
        result = _check_target_range(target, {name: 0.0 for name in STIFFNESS}, {name: 1.0 for name in STIFFNESS})
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["fields"][0]["effective_value_C_over_Eb"], 1.0)
        target["C11"] = 110.00001311302185
        result = _check_target_range(target, {name: 0.0 for name in STIFFNESS}, {name: 1.0 for name in STIFFNESS})
        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(result["failure_kind"], "SCALER_RANGE")

    def test_reported_c11_rounding_case_uses_exact_synthetic_literals(self):
        import latticegpt_9x_candidate_ranking as module

        raw = {name: 0.01 for name in STIFFNESS}
        raw["C11"] = 0.11651468508929674
        physical = {name: value * 110.0 for name, value in raw.items()}
        maximum = {name: 1.0 for name in STIFFNESS}
        maximum["C11"] = 0.11651468276977539
        result = module.check_target_scaler_range(physical, scaler_y_min={name: 0.0 for name in STIFFNESS}, scaler_y_max=maximum, raw_normalized_9d=raw)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["fields"][0]["raw_value_C_over_Eb"], 0.11651468508929674)
        self.assertEqual(result["fields"][0]["effective_value_C_over_Eb"], 0.11651468276977539)
        self.assertEqual(result["fields"][0]["rounding_classification"], "ROUNDED_TO_BOUNDARY")
        raw["C11"] = 0.11651469022035599
        physical["C11"] = raw["C11"] * 110.0
        outside = module.check_target_scaler_range(physical, scaler_y_min={name: 0.0 for name in STIFFNESS}, scaler_y_max=maximum, raw_normalized_9d=raw)
        self.assertEqual(outside["status"], "HOLD")
        self.assertEqual(outside["fields"][0]["effective_value_C_over_Eb"], 0.11651469022035599)

    def test_p09_call_guard_rejects_nonfinite_numeric_evidence(self):
        import latticegpt_9x_candidate_ranking as module

        validator = getattr(module, "validate_target_p09", None)
        self.assertTrue(callable(validator), "target P09 needs a JSON-safe calling guard")
        invalid_p09 = {"status": "PASS", "lambda_min": 1.0, "lambda_max": float("inf"), "lambda_min_over_lambda_max": 0.0}
        with patch.object(module, "validate_restricted_9d_p09", return_value=invalid_p09):
            result = validator({name: 55.0 for name in STIFFNESS}, {"status": "PASS", "failure_kind": None})
        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["evaluated"])
        self.assertIn("NONFINITE_P09_RESULT", result["reasons"])
        self.assertEqual(module.target_validation_failure_kind({"status": "PASS"}, result), "INVALID_CONVERSION")
        json.dumps(result, allow_nan=False)

    def test_p10_producer_records_verified_synthetic_scaler_and_rounded_boundary(self):
        import torch
        from latticegpt_9x_p10_manifest import produce_p10_ranking_manifest

        with local_test_directory() as root:
            target = dict(C11=110.00000327825546, C12=5.0, C13=4.0, C22=50.0, C23=3.0, C33=45.0, C44=15.0, C55=12.0, C66=10.0)
            run_path = root / "run.json"
            run_path.write_text(json.dumps({"run_id": "synthetic-run", "task_id": "synthetic-task", "candidate_count": 1,
                "source_context": {"target_sha256": "A" * 64, "physical_target_9d": {"units": "GPa", "values": target}}}), encoding="utf-8")
            candidate_path = root / "candidate.json"
            candidate_path.write_text(json.dumps({"run_id": "synthetic-run", "task_id": "synthetic-task", "target_sha256": "A" * 64,
                "candidate_id": "candidate-1", "source_candidate_id": "source-1", "candidate": {"cad_ready": True,
                "target_stiffness": target, "predicted_stiffness": target}}), encoding="utf-8")
            scaler = root / "scaler_Y.pt"
            torch.save({"min": torch.zeros(9, dtype=torch.float32), "max": torch.ones(9, dtype=torch.float32)}, scaler)
            before = {p.name: p.read_bytes() for p in (run_path, candidate_path, scaler)}
            scaler_sha = hashlib.sha256(scaler.read_bytes()).hexdigest().upper()
            result = produce_p10_ranking_manifest(run_manifest_path=run_path, candidate_manifest_paths=[candidate_path],
                scaler_y_path=scaler, expected_scaler_y_sha256=scaler_sha, output_dir=root / "output")
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "RANKED")
            check = manifest["target"]["range_check"]
            self.assertEqual(check["status"], "PASS")
            self.assertEqual(check["fields"][0]["effective_value_C_over_Eb"], 1.0)
            self.assertEqual(check["scaler_y_identity"]["sha256"], scaler_sha)
            self.assertIsNone(manifest["target"]["validation_failure_kind"])
            self.assertEqual({p.name: p.read_bytes() for p in (run_path, candidate_path, scaler)}, before)


if __name__ == "__main__":
    unittest.main()
