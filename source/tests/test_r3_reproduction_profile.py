import copy
import hashlib
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from product_workflow.manifest_io import canonical_json_object_v1, parse_closed_json_object_v1
from product_workflow.reproducibility import (
    VOLATILE_JSON_POINTERS,
    build_comparison_contract_v1,
    build_reproduction_profile_v1,
    comparison_contract_sha256_v1,
    reproduction_child_environment,
    validate_comparison_contract_v1,
    validate_reproduction_profile_v1,
)


def valid_profile():
    return build_reproduction_profile_v1(
        random_seed=20260811,
        versions={
            "windows_build": "10.0.26100",
            "architecture": "AMD64",
            "python_implementation": "CPython",
            "python": "3.10.17",
            "numpy": "2.2.6",
            "torch": "2.7.1",
        },
        identities={
            "config_sha256": "1" * 64,
            "release_generation_contract_sha256": "2" * 64,
            "active_release_binding_sha256": "3" * 64,
            "source_manifest_sha256": "A" * 64,
            "artifact_manifest_sha256": "B" * 64,
            "environment_manifest_sha256": "C" * 64,
            "formal_bundle_binding_sha256": "D" * 64,
        },
        llm={
            "provider": "replay",
            "model": "frozen-target",
            "interface_version": "gate7-frozen-target-v1",
            "prompt_template_version": "not-used-frozen-target-v1",
            "temperature": 0.0,
            "top_p": None,
            "provider_seed": None,
            "online_text_determinism": "NOT_CLAIMED",
            "replay_start": "FROZEN_QUERY_PLAN_OR_TARGET",
        },
        tolerance_schema_sha256="E" * 64,
    )


class R3ReproductionProfileTests(unittest.TestCase):
    def test_comparison_contract_is_closed_and_distinct_from_tolerance_identity(self):
        contract = build_comparison_contract_v1(
            tolerance_schema_path="config/reproduction_tolerances_v1.json",
            tolerance_schema_sha256="E" * 64,
        )

        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "volatile_field_policy",
                "volatile_json_pointers",
                "stable_projection_schema_version",
                "projection_construction",
                "relationship_integrity_policy",
                "required_layers",
                "visual_acceptance",
                "tolerance_schema_path",
                "tolerance_schema_sha256",
                "aggregation_precedence",
            },
        )
        self.assertEqual(contract["volatile_json_pointers"], list(VOLATILE_JSON_POINTERS))
        self.assertEqual(
            validate_comparison_contract_v1(
                contract,
                tolerance_schema_path="config/reproduction_tolerances_v1.json",
                tolerance_schema_sha256="E" * 64,
            ),
            contract,
        )
        self.assertNotEqual(comparison_contract_sha256_v1(contract), "E" * 64)

    def test_comparison_contract_rejects_missing_extra_type_order_and_tolerance_alias(self):
        valid = build_comparison_contract_v1(
            tolerance_schema_path="config/reproduction_tolerances_v1.json",
            tolerance_schema_sha256="E" * 64,
        )
        mutations = []
        missing = copy.deepcopy(valid); missing.pop("required_layers"); mutations.append(missing)
        extra = copy.deepcopy(valid); extra["unknown"] = True; mutations.append(extra)
        wrong_type = copy.deepcopy(valid); wrong_type["volatile_json_pointers"] = "not-an-array"; mutations.append(wrong_type)
        wrong_order = copy.deepcopy(valid); wrong_order["volatile_json_pointers"].reverse(); mutations.append(wrong_order)
        tolerance_alias = copy.deepcopy(valid); tolerance_alias["tolerance_schema_sha256"] = "F" * 64; mutations.append(tolerance_alias)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_comparison_contract_v1(
                        value,
                        tolerance_schema_path="config/reproduction_tolerances_v1.json",
                        tolerance_schema_sha256="E" * 64,
                    )

    def test_valid_profile_round_trips_and_hashes_without_self_leaf(self):
        profile = valid_profile()
        expected = hashlib.sha256(
            canonical_json_object_v1(profile, self_hash_pointer="/profile_sha256")
        ).hexdigest().upper()
        self.assertEqual(profile["profile_sha256"], expected)
        raw = canonical_json_object_v1(profile)
        self.assertEqual(
            parse_closed_json_object_v1(raw, "latticegpt_reproduction_profile_v1"),
            profile,
        )

    def test_four_seeds_must_be_equal_uint32_and_bool_is_rejected(self):
        for path, value in (
            (("rng", "python", "seed"), 1),
            (("rng", "numpy", "seed"), True),
            (("random_seed",), 4294967296),
        ):
            profile = valid_profile()
            target = profile
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            with self.subTest(path=path, value=value):
                with self.assertRaises(ValueError):
                    validate_reproduction_profile_v1(profile)

    def test_missing_unknown_and_non_cpu_fields_fail_closed(self):
        mutations = []
        missing = valid_profile()
        missing["threads"].pop("mkl")
        mutations.append(missing)
        extra = valid_profile()
        extra["device"]["gpu"] = "forbidden"
        mutations.append(extra)
        cuda = valid_profile()
        cuda["device"]["cuda_allowed"] = True
        mutations.append(cuda)
        for profile in mutations:
            with self.assertRaises(ValueError):
                validate_reproduction_profile_v1(profile)

    def test_child_environment_is_complete_and_preserves_canonical_profile_lf(self):
        profile = valid_profile()
        env = reproduction_child_environment(profile, base_environment={"KEEP": "yes"})
        self.assertEqual(env["KEEP"], "yes")
        self.assertEqual(env["PYTHONHASHSEED"], "20260811")
        self.assertEqual(env["OMP_NUM_THREADS"], "1")
        self.assertEqual(env["MKL_NUM_THREADS"], "1")
        self.assertEqual(env["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(env["NUMEXPR_NUM_THREADS"], "1")
        self.assertEqual(env["LATTICEGPT_SCALER_POLICY"], "require_existing")
        self.assertTrue(env["LATTICEGPT_REPRODUCTION_PROFILE_JSON"].endswith("\n"))

    def test_profile_hash_rejects_post_hash_mutation(self):
        profile = valid_profile()
        mutated = copy.deepcopy(profile)
        mutated["llm"]["model"] = "changed"
        with self.assertRaisesRegex(ValueError, "profile_sha256"):
            validate_reproduction_profile_v1(mutated)

    def test_recovery_identity_triple_is_required_and_closed(self):
        for key in (
            "config_sha256",
            "release_generation_contract_sha256",
            "active_release_binding_sha256",
        ):
            profile = valid_profile()
            profile["identities"].pop(key)
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validate_reproduction_profile_v1(profile)


if __name__ == "__main__":
    unittest.main()
