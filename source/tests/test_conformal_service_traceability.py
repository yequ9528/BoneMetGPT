import copy
import hashlib
import json
import shutil
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import conformal_service
from conformal_service import _load_verified_adapter_traceability, _validate_product_mesh_binding


NORMALIZED_DIAMETER = 0.043796
REFERENCE_LENGTH = 4.310178999208903
PHYSICAL_DIAMETER = NORMALIZED_DIAMETER * REFERENCE_LENGTH
PHYSICAL_SCALE_FORMULA = "P11_AVERAGE_EQUIVALENT_SCALE_V1"
PHYSICAL_DIAMETER_SOURCE = (
    "latticegpt_9x_technical_case_preview:physical_diameter"
)


TRACEABILITY = {
    "task_id": "truss150w-task-test",
    "run_id": "truss150w-run-test",
    "candidate_id": "truss150w-run-test-candidate-0001",
    "source_candidate_id": "inverse_150w_0001",
    "target_sha256": "a" * 64,
    "evidence_level": "real_150w_candidate",
    "selection_policy": "cad_ready_max_diameter_then_source_id_v1",
    "candidate_manifest_sha256": "b" * 64,
    "run_manifest_sha256": "c" * 64,
    "gate1_review": "passed",
    "gate1_review_basis": "independent_readonly_review",
    "gate1_review_artifact": None,
}
P10_TRACEABILITY = {
    **{
        key: value
        for key, value in TRACEABILITY.items()
        if key not in {"gate1_review", "gate1_review_basis", "gate1_review_artifact"}
    },
    "selection_policy": "scaler_range_weighted_RMSE_v1",
    "selection_authority": "p10_ranking_manifest",
    "p10_ranking_manifest_sha256": "d" * 64,
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
        "NOT_CURRENT_FORMAL_150W_PROVENANCE",
        "NOT_ORIGINAL_FULL_6X6",
        "NOT_DOWNSTREAM_ADAPTER_REBOUND",
        "NOT_FULL_DUAL_SPACE_FUSION_COMPLETION",
    ],
}
PRODUCT_TRACEABILITY = {
    "job_id": "job-1234567890ABCDEF12345678",
    "candidate_id": TRACEABILITY["candidate_id"],
    "source_candidate_id": TRACEABILITY["source_candidate_id"],
    "target_sha256": "a" * 64,
    "evidence_level": "real_150w_candidate",
    "selection_policy": "product_candidate_selection_v1",
    "candidate_manifest_sha256": "b" * 64,
    "run_manifest_sha256": "c" * 64,
    "selection_manifest_sha256": "d" * 64,
    "recommended_candidate_id": "candidate-rank-1",
    "selected_candidate_id": TRACEABILITY["candidate_id"],
    "selected_rank": 2,
    "selection_source": "user_override",
    "p09_status": "PASS",
    "mechanical_match_status": "NOT_VALIDATED",
    "diameter_used_in_ranking": False,
    "non_claims": ["NOT_MECHANICAL_PASS", "NOT_OOD_PASS", "NOT_FEA_VALIDATION"],
}
CASE_MANIFEST_SHA256 = "E" * 64


@contextmanager
def local_test_directory():
    root = PROJECT_ROOT / ".tmp" / "r3_gate2_synthetic" / f".runtime_conformal_traceability_{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield str(root)
    finally:
        shutil.rmtree(root)


class ConformalServiceTraceabilityTests(unittest.TestCase):
    def test_runtime_requires_explicit_allowed_roots_before_path_or_dependency_loading(self):
        with patch.object(conformal_service, "_resolve_source_mesh") as resolve_source:
            result = conformal_service.run_conformal_service({"runtime_enabled": True})
        self.assertEqual(result["status"], "error")
        self.assertIn("allowed_output_roots", result["message"])
        resolve_source.assert_not_called()

    def test_validates_product_mesh_binding_against_source_bytes_and_type(self):
        with local_test_directory() as temp_dir:
            source = Path(temp_dir) / "source.inp"
            source.write_text("*Heading\n", encoding="utf-8")
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest().upper()
            binding = {
                "schema_version": "latticegpt_mesh_binding_v1",
                "stored_path": str(source),
                "sha256": source_sha,
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "design_domain": "whole_file_implant_domain",
                "element_type_original": "C3D8R",
                "element_type_effective": "C3D8_GEOMETRY_COMPATIBLE",
                "mesh_binding_sha256": "F" * 64,
            }
            result = _validate_product_mesh_binding(binding, source_mesh_path=source.resolve())
            self.assertEqual(result["sha256"], source_sha)

    def test_selects_c3d8r_for_geometry_runtime_without_relabeling_original(self):
        c3d8r_elements = SimpleNamespace(shape=(1832, 8))

        original_type, effective_type, selected = (
            conformal_service._select_runtime_hex_elements(
                {"C3D8R": c3d8r_elements}
            )
        )

        self.assertEqual(original_type, "C3D8R")
        self.assertEqual(effective_type, "C3D8_GEOMETRY_COMPATIBLE")
        self.assertIs(selected, c3d8r_elements)

    def test_rejects_mixed_c3d8_and_c3d8r_runtime_source(self):
        elements = SimpleNamespace(shape=(1, 8))

        with self.assertRaisesRegex(ValueError, "exactly one"):
            conformal_service._select_runtime_hex_elements(
                {"C3D8": elements, "C3D8R": elements}
            )

    def test_rejects_runtime_parser_identity_drift_against_formal_contract(self):
        elements = SimpleNamespace(shape=(1, 8))

        with self.assertRaisesRegex(ValueError, "formal mesh contract"):
            conformal_service._select_runtime_hex_elements(
                {"C3D8": elements},
                source_mesh_contract={
                    "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
                    "element_type_original": "C3D8R",
                    "element_type_effective": "C3D8_GEOMETRY_COMPATIBLE",
                },
            )

    def test_runtime_source_mesh_contract_is_closed_and_preserves_c3d8r(self):
        valid = {
            "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
            "element_type_original": "C3D8R",
            "element_type_effective": "C3D8_GEOMETRY_COMPATIBLE",
        }
        self.assertEqual(
            conformal_service._validate_runtime_source_mesh_contract(valid, required=True),
            valid,
        )
        for mutation in (
            {key: value for key, value in valid.items() if key != "element_type_effective"},
            {**valid, "legacy_element_type": "C3D8R"},
            {**valid, "element_type_effective": "C3D8"},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    conformal_service._validate_runtime_source_mesh_contract(
                        mutation, required=True
                    )

    def _write_adapter_fixture(
        self,
        root: Path,
        *,
        diameter: float = NORMALIZED_DIAMETER,
        traceability: dict | None = None,
        formal_case_binding: bool = False,
    ):
        selected_traceability = TRACEABILITY if traceability is None else traceability
        cell_path = root / "custom_cell.yml"
        metadata_path = root / "adapter_metadata.json"
        cell_path.write_text("nodes: []\nedges: []\n", encoding="utf-8")
        metadata = {
            "candidate_id": selected_traceability["candidate_id"],
            "diameter": diameter,
            "adapter_status": "static_validated",
            "custom_cell_path": "custom_cell.yml",
            "traceability": selected_traceability,
        }
        if formal_case_binding:
            metadata.update(
                {
                    "case_manifest_sha256": CASE_MANIFEST_SHA256,
                    "physical_scale": {
                        "contract": "average_equivalent_selected_hex_scale_v1",
                        "normalized_diameter": NORMALIZED_DIAMETER,
                        "reference_length_mm": REFERENCE_LENGTH,
                        "physical_diameter_mm": PHYSICAL_DIAMETER,
                        "units": "mm",
                        "assignment_scope": "global_uniform",
                    },
                }
            )
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return cell_path, metadata_path

    def test_loads_verified_adapter_traceability_with_file_hashes(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(Path(temp_dir))

            result = _load_verified_adapter_traceability(
                adapter_metadata_path=metadata_path,
                cell_config_path=cell_path,
                normalized_diameter=NORMALIZED_DIAMETER,
                physical_diameter=PHYSICAL_DIAMETER,
                reference_length=REFERENCE_LENGTH,
                physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                physical_diameter_units="mm",
                diameter_source=PHYSICAL_DIAMETER_SOURCE,
            )

            self.assertEqual(result["candidate_id"], TRACEABILITY["candidate_id"])
            self.assertEqual(result["task_id"], TRACEABILITY["task_id"])
            self.assertEqual(result["adapter_status"], "static_validated")
            self.assertEqual(result["normalized_diameter"], NORMALIZED_DIAMETER)
            self.assertEqual(result["reference_length"], REFERENCE_LENGTH)
            self.assertEqual(result["physical_diameter"], PHYSICAL_DIAMETER)
            self.assertEqual(result["physical_diameter_units"], "mm")
            self.assertEqual(result["physical_scale_formula"], PHYSICAL_SCALE_FORMULA)
            self.assertEqual(
                result["adapter_metadata_sha256"],
                hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result["custom_cell_sha256"],
                hashlib.sha256(cell_path.read_bytes()).hexdigest(),
            )

    def test_loads_p10_traceability_without_legacy_gate_fields(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(
                Path(temp_dir),
                traceability=P10_TRACEABILITY,
            )

            result = _load_verified_adapter_traceability(
                adapter_metadata_path=metadata_path,
                cell_config_path=cell_path,
                normalized_diameter=NORMALIZED_DIAMETER,
                physical_diameter=PHYSICAL_DIAMETER,
                reference_length=REFERENCE_LENGTH,
                physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                physical_diameter_units="mm",
                diameter_source=PHYSICAL_DIAMETER_SOURCE,
            )

        self.assertEqual(result["selection_authority"], "p10_ranking_manifest")
        self.assertEqual(result["p09_status"], "PASS")
        self.assertEqual(result["p10_ranking_status"], "RANKED")
        self.assertEqual(
            result["p10_selected_status"],
            "SURROGATE_RANKED_GEOMETRY_CANDIDATE",
        )
        self.assertEqual(result["mechanical_match_status"], "NOT_VALIDATED")
        self.assertEqual(result["ood_status"], "not_available")
        self.assertFalse(result["diameter_used_in_ranking"])
        self.assertNotIn("gate1_review", result)

    def test_loads_product_user_override_traceability(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(
                Path(temp_dir), traceability=PRODUCT_TRACEABILITY
            )
            result = _load_verified_adapter_traceability(
                adapter_metadata_path=metadata_path,
                cell_config_path=cell_path,
                normalized_diameter=NORMALIZED_DIAMETER,
                physical_diameter=PHYSICAL_DIAMETER,
                reference_length=REFERENCE_LENGTH,
                physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                physical_diameter_units="mm",
                diameter_source=PHYSICAL_DIAMETER_SOURCE,
            )
        self.assertEqual(result["job_id"], PRODUCT_TRACEABILITY["job_id"])
        self.assertEqual(result["selected_rank"], 2)
        self.assertEqual(result["selection_source"], "user_override")

    def test_carries_verified_formal_case_binding_into_runtime_traceability(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(
                Path(temp_dir),
                traceability=P10_TRACEABILITY,
                formal_case_binding=True,
            )

            result = _load_verified_adapter_traceability(
                adapter_metadata_path=metadata_path,
                cell_config_path=cell_path,
                normalized_diameter=NORMALIZED_DIAMETER,
                physical_diameter=PHYSICAL_DIAMETER,
                reference_length=REFERENCE_LENGTH,
                physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                physical_diameter_units="mm",
                diameter_source=PHYSICAL_DIAMETER_SOURCE,
            )

        self.assertEqual(result["case_manifest_sha256"], CASE_MANIFEST_SHA256)
        self.assertEqual(
            result["physical_scale_contract"],
            "average_equivalent_selected_hex_scale_v1",
        )
        self.assertEqual(result["assignment_scope"], "global_uniform")

    def test_rejects_missing_or_forged_p10_traceability_fields(self):
        invalid_cases = []
        missing_status = copy.deepcopy(P10_TRACEABILITY)
        missing_status.pop("p10_ranking_status")
        invalid_cases.append((missing_status, "p10_ranking_status"))
        forged_authority = copy.deepcopy(P10_TRACEABILITY)
        forged_authority["selection_authority"] = "forged_manifest"
        invalid_cases.append((forged_authority, "selection_authority"))

        for traceability, expected_error in invalid_cases:
            with self.subTest(expected_error=expected_error):
                with local_test_directory() as temp_dir:
                    cell_path, metadata_path = self._write_adapter_fixture(
                        Path(temp_dir),
                        traceability=traceability,
                    )
                    with self.assertRaisesRegex(ValueError, expected_error):
                        _load_verified_adapter_traceability(
                            adapter_metadata_path=metadata_path,
                            cell_config_path=cell_path,
                            normalized_diameter=NORMALIZED_DIAMETER,
                            physical_diameter=PHYSICAL_DIAMETER,
                            reference_length=REFERENCE_LENGTH,
                            physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                            physical_diameter_units="mm",
                            diameter_source=PHYSICAL_DIAMETER_SOURCE,
                        )

    def test_rejects_adapter_metadata_with_diameter_mismatch(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(
                Path(temp_dir),
                diameter=0.05,
            )

            with self.assertRaisesRegex(ValueError, "diameter"):
                _load_verified_adapter_traceability(
                    adapter_metadata_path=metadata_path,
                    cell_config_path=cell_path,
                    normalized_diameter=NORMALIZED_DIAMETER,
                    physical_diameter=PHYSICAL_DIAMETER,
                    reference_length=REFERENCE_LENGTH,
                    physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                    physical_diameter_units="mm",
                    diameter_source=PHYSICAL_DIAMETER_SOURCE,
                )

    def test_rejects_physical_diameter_that_violates_p11_formula(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(Path(temp_dir))

            with self.assertRaisesRegex(ValueError, "physical diameter"):
                _load_verified_adapter_traceability(
                    adapter_metadata_path=metadata_path,
                    cell_config_path=cell_path,
                    normalized_diameter=NORMALIZED_DIAMETER,
                    physical_diameter=PHYSICAL_DIAMETER + 0.01,
                    reference_length=REFERENCE_LENGTH,
                    physical_scale_formula=PHYSICAL_SCALE_FORMULA,
                    physical_diameter_units="mm",
                    diameter_source=PHYSICAL_DIAMETER_SOURCE,
                )

    def test_rejects_ambiguous_or_incomplete_physical_scale_contract(self):
        with local_test_directory() as temp_dir:
            cell_path, metadata_path = self._write_adapter_fixture(Path(temp_dir))

            invalid_cases = (
                {"physical_scale_formula": "UNKNOWN"},
                {"physical_diameter_units": None},
                {"diameter_source": "adapter_metadata.json:diameter"},
                {"reference_length": None},
            )
            for override in invalid_cases:
                with self.subTest(override=override):
                    arguments = {
                        "adapter_metadata_path": metadata_path,
                        "cell_config_path": cell_path,
                        "normalized_diameter": NORMALIZED_DIAMETER,
                        "physical_diameter": PHYSICAL_DIAMETER,
                        "reference_length": REFERENCE_LENGTH,
                        "physical_scale_formula": PHYSICAL_SCALE_FORMULA,
                        "physical_diameter_units": "mm",
                        "diameter_source": PHYSICAL_DIAMETER_SOURCE,
                    }
                    arguments.update(override)
                    with self.assertRaises(ValueError):
                        _load_verified_adapter_traceability(**arguments)


if __name__ == "__main__":
    unittest.main()
