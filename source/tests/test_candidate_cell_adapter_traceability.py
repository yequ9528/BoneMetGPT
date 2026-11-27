import inspect
import unittest
from pathlib import Path

from candidate_cell_adapter import build_custom_cell_from_candidate


VALID_SHA256 = "a" * 64
RUN_SHA256 = "b" * 64
SELECTION_POLICY = "cad_ready_max_diameter_then_source_id_v1"
P10_SELECTION_POLICY = "scaler_range_weighted_RMSE_v1"
PRODUCT_SELECTION_POLICY = "product_candidate_selection_v1"
P10_SHA256 = "c" * 64
CASE_MANIFEST_SHA256 = "d" * 64


def make_candidate():
    return {
        "schema_version": "truss_candidate_manifest_v1",
        "candidate_id": "truss150w-run-test-candidate-0001-0003",
        "source_candidate_id": "inverse_150w_0003",
        "target_sha256": VALID_SHA256,
        "nodes": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        "connections": [[0, 1], [0, 2], [0, 3]],
        "diameter": 0.043796,
        "relative_density": 0.043156788,
        "cad_ready": True,
    }


def make_traceability():
    return {
        "task_id": "truss150w-task-test",
        "run_id": "truss150w-run-test",
        "candidate_id": "truss150w-run-test-candidate-0001-0003",
        "source_candidate_id": "inverse_150w_0003",
        "target_sha256": VALID_SHA256,
        "evidence_level": "real_150w_candidate",
        "selection_policy": SELECTION_POLICY,
        "candidate_manifest_sha256": VALID_SHA256,
        "run_manifest_sha256": RUN_SHA256,
        "gate1_review": "passed",
        "gate1_review_basis": "independent_readonly_review",
        "gate1_review_artifact": None,
    }


def make_p10_traceability():
    traceability = make_traceability()
    traceability.update(
        {
            "candidate_id": "truss150w-run-test-candidate-0001-0003",
            "source_candidate_id": "inverse_150w_0003",
            "selection_policy": P10_SELECTION_POLICY,
            "selection_authority": "p10_ranking_manifest",
            "p10_ranking_manifest_sha256": P10_SHA256,
            "p10_schema_version": "latticegpt_9x_p10_ranking_manifest_v1",
            "p10_evidence_level": "TECHNICAL_FIXTURE",
            "p10_ranking_status": "RANKED",
            "p10_selected_status": "SURROGATE_RANKED_GEOMETRY_CANDIDATE",
            "p09_status": "PASS",
            "mechanical_match_status": "NOT_VALIDATED",
            "ood_status": "not_available",
            "diameter_used_in_ranking": False,
            "non_claims": [
                "TECHNICAL_VALIDATION_ONLY",
                "NOT_FORMAL_ENGINEERING_CASE",
                "NOT_GATE_A1A_PASS",
                "NOT_MECHANICAL_PASS",
                "NOT_OOD_PASS",
            ],
        }
    )
    traceability.pop("gate1_review")
    traceability.pop("gate1_review_basis")
    traceability.pop("gate1_review_artifact")
    return traceability


def make_formal_case_binding():
    return {
        "schema_version": "latticegpt_9x_adapter_case_binding_v1",
        "case_manifest_sha256": CASE_MANIFEST_SHA256,
        "physical_scale_contract": "average_equivalent_selected_hex_scale_v1",
        "reference_length_mm": 4.310178999208903,
        "assignment_scope": "global_uniform",
    }


def make_product_traceability():
    return {
        "job_id": "job-1234567890ABCDEF12345678",
        "candidate_id": "truss150w-run-test-candidate-0001-0003",
        "source_candidate_id": "inverse_150w_0003",
        "target_sha256": VALID_SHA256,
        "evidence_level": "real_150w_candidate",
        "selection_policy": PRODUCT_SELECTION_POLICY,
        "candidate_manifest_sha256": VALID_SHA256,
        "run_manifest_sha256": RUN_SHA256,
        "selection_manifest_sha256": P10_SHA256,
        "recommended_candidate_id": "candidate-rank-1",
        "selected_candidate_id": "truss150w-run-test-candidate-0001-0003",
        "selected_rank": 2,
        "selection_source": "user_override",
        "p09_status": "PASS",
        "mechanical_match_status": "NOT_VALIDATED",
        "diameter_used_in_ranking": False,
        "non_claims": ["NOT_MECHANICAL_PASS", "NOT_OOD_PASS", "NOT_FEA_VALIDATION"],
    }


class CandidateCellAdapterTraceabilityTests(unittest.TestCase):
    def test_write_without_allowed_output_root_is_blocked_before_directory_creation(self):
        requested = Path(__file__).resolve().parent / "adapter-write-must-not-exist"
        result = build_custom_cell_from_candidate(
            make_candidate(),
            output_path=requested,
            dry_run=False,
            traceability=make_product_traceability(),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("allowed_output_root" in item for item in result["errors"]))
        self.assertFalse(requested.exists())

    def test_product_selection_accepts_eligible_user_override_not_rank_one(self):
        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=make_product_traceability(),
        )
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["adapter_metadata"]["traceability"]["selected_rank"], 2)

    def test_product_selection_rejects_selected_candidate_identity_drift(self):
        traceability = make_product_traceability()
        traceability["selected_candidate_id"] = "different-candidate"
        result = build_custom_cell_from_candidate(make_candidate(), traceability=traceability)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("selected_candidate_id" in item for item in result["errors"]))

    def test_legacy_preview_without_traceability_preserves_metadata_shape(self):
        result = build_custom_cell_from_candidate(make_candidate())

        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["errors"])
        self.assertNotIn("traceability", result["adapter_metadata"])

    def test_preview_metadata_embeds_valid_traceability(self):
        traceability = make_traceability()

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["errors"])
        self.assertEqual(result["adapter_metadata"]["traceability"], traceability)

    def test_traceability_candidate_identity_mismatch_is_invalid(self):
        traceability = make_traceability()
        traceability["candidate_id"] = "wrong-candidate"

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(
            any("traceability.candidate_id" in error for error in result["errors"])
        )

    def test_traceability_rejects_non_real_evidence_level(self):
        traceability = make_traceability()
        traceability["evidence_level"] = "fixture"

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(
            any("traceability.evidence_level" in error for error in result["errors"])
        )

    def test_preview_metadata_accepts_p10_ranking_traceability(self):
        traceability = make_p10_traceability()

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["errors"])
        self.assertEqual(result["adapter_metadata"]["traceability"], traceability)

    def test_p10_traceability_rejects_missing_ranking_manifest_sha256(self):
        traceability = make_p10_traceability()
        traceability.pop("p10_ranking_manifest_sha256")

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(
            any("p10_ranking_manifest_sha256" in error for error in result["errors"])
        )

    def test_p10_traceability_rejects_mechanical_pass_claim(self):
        traceability = make_p10_traceability()
        traceability["mechanical_match_status"] = "PASS"

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=traceability,
        )

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(
            any("mechanical_match_status" in error for error in result["errors"])
        )

    def test_formal_case_binding_adds_case_and_physical_scale_metadata(self):
        self.assertIn(
            "formal_case_binding",
            inspect.signature(build_custom_cell_from_candidate).parameters,
        )

        result = build_custom_cell_from_candidate(
            make_candidate(),
            traceability=make_p10_traceability(),
            formal_case_binding=make_formal_case_binding(),
        )

        metadata = result["adapter_metadata"]
        physical_scale = metadata["physical_scale"]
        self.assertEqual(result["status"], "preview")
        self.assertEqual(metadata["case_manifest_sha256"], CASE_MANIFEST_SHA256)
        self.assertEqual(
            physical_scale["contract"],
            "average_equivalent_selected_hex_scale_v1",
        )
        self.assertEqual(physical_scale["normalized_diameter"], 0.043796)
        self.assertEqual(physical_scale["reference_length_mm"], 4.310178999208903)
        self.assertAlmostEqual(
            physical_scale["physical_diameter_mm"],
            0.043796 * 4.310178999208903,
        )
        self.assertEqual(physical_scale["assignment_scope"], "global_uniform")

    def test_formal_case_binding_rejects_invalid_case_manifest_sha256(self):
        binding = make_formal_case_binding()
        binding["case_manifest_sha256"] = "not-sha256"

        self.assertIn(
            "formal_case_binding",
            inspect.signature(build_custom_cell_from_candidate).parameters,
        )
        result = build_custom_cell_from_candidate(
            make_candidate(),
            formal_case_binding=binding,
        )

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("case_manifest_sha256" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
