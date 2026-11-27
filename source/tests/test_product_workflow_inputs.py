import hashlib
import csv
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from product_workflow import inputs
from product_workflow.contracts import canonical_sha256
from product_workflow.inputs import (
    plan_formal_query,
    proposals_from_formal_query,
    target_from_csv_upload,
)


CSV = (
    "C11,C12,C13,C22,C23,C33,C44,C55,C66\n"
    "55,5,4,50,3,45,15,12,10\n"
).encode("utf-8")


class ProductWorkflowInputTests(unittest.TestCase):
    def parse_csv(self, content):
        parser = getattr(inputs, "parse_target_csv_upload", None)
        self.assertIsNotNone(parser, "CSV intake needs a complete record parser before row selection")
        return parser(content, filename="target.csv")

    def test_csv_preserves_invalid_record_positions_and_duplicate_value_identity(self):
        header, row, _ = CSV.split(b"\n")
        content = header + b"\n" + row + b"\n" + b"bad,5,4,50,3,45,15,12,10\n" + row + b"\n"
        parsed = self.parse_csv(content)
        self.assertEqual((parsed["record_count"], parsed["valid_count"], parsed["invalid_count"]), (3, 2, 1))
        self.assertEqual(parsed["sha256"], hashlib.sha256(content).hexdigest().upper())
        self.assertEqual([r["record_index"] for r in parsed["rows"]], [1, 2, 3])
        self.assertIsNone(parsed["rows"][1]["values"])
        self.assertTrue(parsed["rows"][1]["error"])
        self.assertEqual(parsed["rows"][0]["values"]["C11"], 55.0)
        first = inputs.target_from_csv_record(parsed, 1)
        third = inputs.target_from_csv_record(parsed, 3)
        self.assertEqual(first["physical_9d_gpa"], third["physical_9d_gpa"])
        self.assertNotEqual(first["proposal_sha256"], third["proposal_sha256"])
        self.assertEqual(third["source"]["record_index"], 3)
        self.assertEqual(third["source"]["record_count"], 3)
        self.assertEqual(third["proposal_sha256"], canonical_sha256({k: v for k, v in third.items() if k != "proposal_sha256"}))
        for index in (0, 2, 4, True):
            with self.subTest(index=index), self.assertRaises(ValueError):
                inputs.target_from_csv_record(parsed, index)

    def test_csv_parses_all_records_without_building_proposals_or_truncating_at_twenty(self):
        header, row, _ = CSV.split(b"\n")
        with patch.object(inputs, "target_from_physical_9d", side_effect=AssertionError("parse must not build proposals")):
            parsed = self.parse_csv(header + b"\n" + (row + b"\n") * 37)
        self.assertEqual(parsed["record_count"], 37)
        self.assertEqual(parsed["valid_count"], 37)
        self.assertEqual(parsed["rows"][-1]["record_index"], 37)

    def test_csv_rejects_header_encoding_geometry_and_size_errors(self):
        cases = [
            (b"C11,C12,C13,C22,C23,C33,C44,C55,C66,C11\n", "9D"),
            (b"C11,C12,C13,C22,C23,C33,C44,C55,other\n", "9D"),
            (b"node_id,x,y,z\n1,0,0,0\n", "geometry"),
            (b"\xff", "UTF-8"),
            (b"x" * (5 * 1024 * 1024 + 1), "5 MB"),
        ]
        for content, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.parse_csv(content)

    def test_csv_keeps_short_long_and_nonpositive_rows_invalid_without_shifting_indices(self):
        header, row, _ = CSV.split(b"\n")
        parsed = self.parse_csv(header + b"\n1,2\n" + row + b",extra\n0,5,4,50,3,45,15,12,10\n" + row)
        self.assertEqual((parsed["record_count"], parsed["invalid_count"]), (4, 3))
        self.assertTrue(all(r["error"] for r in parsed["rows"][:3]))
        self.assertEqual(inputs.target_from_csv_record(parsed, 4)["source"]["record_index"], 4)

    def test_csv_accepts_near_limit_upload_and_hashes_complete_bytes(self):
        # Bounded record count: the oversized quoted field remains one invalid record.
        padding = b" " * (5 * 1024 * 1024 - len(CSV) - 100)
        content = CSV + b'"' + padding + b'",5,4,50,3,45,15,12,10\n'
        parsed = self.parse_csv(content)
        self.assertEqual((parsed["record_count"], parsed["valid_count"], parsed["invalid_count"]), (2, 1, 1))
        self.assertEqual(parsed["sha256"], hashlib.sha256(content).hexdigest().upper())

    def test_concurrent_csv_parses_do_not_restore_another_parse_field_limit(self):
        parser = getattr(inputs, "parse_target_csv_upload", None)
        self.assertIsNotNone(parser)
        original_reader, original_limit = csv.reader, csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, original_limit)
        entered_first, entered_second = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        reader_calls = []
        def reader(*args, **kwargs):
            number = len(reader_calls)
            reader_calls.append(number)
            iterator = original_reader(*args, **kwargs)
            entered, release = ((entered_first, release_first) if number == 0
                                else (entered_second, release_second))
            entered.set()
            if not release.wait(2):
                raise AssertionError("synthetic reader was not released")
            return iterator
        large_field = CSV + b'"' + b" " * 140000 + b'",5,4,50,3,45,15,12,10\n'
        with patch.object(inputs.csv, "reader", side_effect=reader), ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(parser, CSV, filename="first.csv")
                self.assertTrue(entered_first.wait(1))
                second = pool.submit(parser, large_field, filename="second.csv")
                entered_second.wait(.1)
                release_first.set()
                self.assertEqual(first.result(2)["valid_count"], 1)
                release_second.set()
                self.assertIsNone(second.exception(2), "concurrent permitted upload lost its CSV field limit")
                self.assertEqual(second.result(2)["record_count"], 2)
            finally:
                release_first.set()
                release_second.set()
        self.assertEqual(csv.field_size_limit(), original_limit)

    def test_csv_upload_builds_single_gpa_target(self):
        proposal = target_from_csv_upload(CSV, filename="target.csv")
        self.assertEqual(proposal["source"]["kind"], "csv_upload")
        self.assertEqual(proposal["source"]["sha256"], hashlib.sha256(CSV).hexdigest().upper())
        self.assertEqual(proposal["physical_9d_gpa"]["C11"], 55.0)

    def test_csv_rejects_multiple_rows_and_large_upload(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            target_from_csv_upload(CSV + CSV.split(b"\n")[1], filename="target.csv")
        with self.assertRaisesRegex(ValueError, "5 MB"):
            target_from_csv_upload(b"x" * (5 * 1024 * 1024 + 1), filename="target.csv")

    def test_natural_language_planner_can_request_clarification_without_fabricating_target(self):
        result = plan_formal_query(
            "帮我找一个合适的",
            planner=lambda prompt: {"status": "needs_clarification", "question": "请说明刚度或密度条件"},
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("normalized_9d", result)

    def test_query_planner_normalizes_only_safe_operator_aliases(self):
        result = plan_formal_query(
            "sample id equals two",
            planner=lambda prompt: {
                "status": "query",
                "query_plan": {
                    "filters": [{"field": "sample_id", "operator": "=", "value": 2}],
                    "order_by": [],
                    "limit": 1,
                },
            },
        )

        self.assertEqual(result["status"], "query")
        self.assertEqual(
            result["query_plan"]["filters"],
            [{"field": "sample_id", "op": "eq", "value": 2.0}],
        )
        self.assertEqual(result["planner_evidence"]["normalizations"], [
            "filters[0].operator_to_op",
            "filters[0].operator_symbol_to_eq",
        ])

    def test_equivalent_dual_operator_keys_are_accepted_but_conflicts_are_rejected(self):
        result = plan_formal_query(
            "sample id equals two",
            planner=lambda prompt: {
                "status": "query",
                "query_plan": {
                    "filters": [{
                        "field": "sample_id",
                        "op": "eq",
                        "operator": "=",
                        "value": 2,
                    }],
                    "order_by": [],
                    "limit": 1,
                },
            },
        )
        self.assertEqual(result["query_plan"]["filters"][0]["op"], "eq")

        with self.assertRaisesRegex(ValueError, "conflicting op and operator"):
            plan_formal_query(
                "sample id mismatch",
                planner=lambda prompt: {
                    "status": "query",
                    "query_plan": {
                        "filters": [{
                            "field": "sample_id",
                            "op": "gt",
                            "operator": "=",
                            "value": 2,
                        }],
                        "order_by": [],
                        "limit": 1,
                    },
                },
            )

    def test_missing_operator_requests_clarification_without_guessing(self):
        result = plan_formal_query(
            "sample id two",
            planner=lambda prompt: {
                "status": "query",
                "query_plan": {
                    "filters": [{"field": "sample_id", "value": 2}],
                    "order_by": [],
                    "limit": 1,
                },
            },
        )

        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("query_plan", result)
        self.assertEqual(
            result["planner_evidence"]["failure_code"],
            "missing_filter_operator",
        )

    def test_structured_planner_failure_keeps_hash_without_excerpt(self):
        result = plan_formal_query(
            "find a target",
            planner=lambda prompt: {
                "schema_version": "latticegpt_structured_llm_response_v1",
                "status": "invalid_json",
                "parsed": None,
                "raw_sha256": "A" * 64,
                "raw_excerpt": "not-json",
                "error": "response is not valid JSON",
            },
        )

        self.assertEqual(result["status"], "needs_clarification")
        evidence = result["planner_evidence"]
        self.assertEqual(evidence["raw_response_sha256"], "A" * 64)
        self.assertIsNone(evidence["raw_response_excerpt"])
        self.assertEqual(evidence["failure_code"], "invalid_json")
        self.assertRegex(evidence["evidence_sha256"], r"^[0-9A-F]{64}$")

    def test_unknown_operator_and_raw_sql_remain_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported query operator"):
            plan_formal_query(
                "approximately two",
                planner=lambda prompt: {
                    "status": "query",
                    "query_plan": {
                        "filters": [{
                            "field": "sample_id",
                            "operator": "approximately",
                            "value": 2,
                        }],
                        "order_by": [],
                        "limit": 1,
                    },
                },
            )
        with self.assertRaisesRegex(ValueError, "raw SQL"):
            plan_formal_query(
                "run sql",
                planner=lambda prompt: {
                    "status": "query",
                    "query_plan": {"sql": "select * from targets"},
                },
            )

    def test_query_results_become_database_target_proposals(self):
        class QueryService:
            source_sha256 = "A" * 64

            def query(self, plan):
                return [{
                    "sample_id": 9,
                    "relative_density": 0.2,
                    "diameter": 0.1,
                    **{name: 0.1 for name in ("C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66")},
                }]

        proposals = proposals_from_formal_query(
            {"filters": [], "order_by": [], "limit": 5},
            QueryService(),
            planner_evidence={"evidence_sha256": "B" * 64},
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["source"]["sample_id"], 9)
        self.assertEqual(proposals[0]["source"]["planner_evidence_sha256"], "B" * 64)
        self.assertEqual(proposals[0]["physical_9d_gpa"]["C11"], 11.0)


if __name__ == "__main__":
    unittest.main()
