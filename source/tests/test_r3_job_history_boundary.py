import copy
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from product_workflow.job_state import classify_job_generation
from product_workflow.reproducibility import (
    build_comparison_contract_v1,
    comparison_contract_sha256_v1,
)
from tests.test_r3_reproduction_profile import valid_profile


def valid_r3_job():
    profile = valid_profile()
    comparison_contract = build_comparison_contract_v1(
        tolerance_schema_path=profile["tolerance"]["schema_path"],
        tolerance_schema_sha256=profile["tolerance"]["schema_sha256"],
    )
    return {
        "schema_version": "latticegpt_design_job_v2",
        "generation": {
            "schema_version": "latticegpt_job_generation_v1",
            "stage": "R3",
            "contract": "R3_DETERMINISM_MESH_BINDING_V1",
        },
        "reproduction": {
            "profile": profile,
            "profile_sha256": profile["profile_sha256"],
            "status": "FROZEN_R3",
            "comparison_contract": comparison_contract,
            "comparison_contract_sha256": comparison_contract_sha256_v1(comparison_contract),
        },
    }


class R3JobHistoryBoundaryTests(unittest.TestCase):
    def test_valid_r3_job_is_distinct_from_verified_historical_v1(self):
        self.assertEqual(classify_job_generation(valid_r3_job())["classification"], "VALID_R3_JOB")
        historical = {
            "schema_version": "latticegpt_design_job_v1",
            "source_status": "PASS_R2_7_SOURCE_IDENTITY",
            "source_prefix": "release/v1/r2_7",
        }
        result = classify_job_generation(historical)
        self.assertEqual(result["classification"], "HISTORICAL_REPRODUCIBILITY_NOT_VERIFIED")
        self.assertFalse(result["runtime_permitted"])
        self.assertFalse(result["migration_performed"])

    def test_damaged_or_marked_r3_is_never_downgraded_to_historical(self):
        missing = valid_r3_job(); missing.pop("reproduction")
        marked_v1 = {
            "schema_version": "latticegpt_design_job_v1",
            "source_status": "PASS_R3_SOURCE_IDENTITY",
            "source_prefix": "release/v1/r3",
        }
        for value in (missing, marked_v1):
            with self.subTest(value=value):
                self.assertEqual(classify_job_generation(value)["classification"], "INVALID_R3_JOB")

    def test_unknown_generation_holds_without_migration(self):
        result = classify_job_generation({"schema_version": "other"})
        self.assertEqual(result["classification"], "HOLD_UNKNOWN_JOB_GENERATION")
        self.assertFalse(result["runtime_permitted"])
        self.assertFalse(result["migration_performed"])

    def test_r3_job_rejects_contract_tolerance_disagreement_and_contract_mutation(self):
        for mutate in (
            lambda job: job["reproduction"]["comparison_contract"].update(
                {"tolerance_schema_sha256": "F" * 64}
            ),
            lambda job: job["reproduction"]["comparison_contract"].update(
                {"unknown": True}
            ),
            lambda job: job["reproduction"].update(
                {"comparison_contract_sha256": "0" * 64}
            ),
        ):
            job = valid_r3_job()
            mutate(job)
            with self.subTest(job=job):
                self.assertEqual(
                    classify_job_generation(job)["classification"],
                    "INVALID_R3_JOB",
                )


if __name__ == "__main__":
    unittest.main()
