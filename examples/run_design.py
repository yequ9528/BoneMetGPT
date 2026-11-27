"""Reproduce the V1 design example with four verified model artifacts."""

import argparse
from contextlib import redirect_stdout
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import random
import shutil
import sys
from tempfile import gettempdir
from uuid import uuid4

EXAMPLES = Path(__file__).resolve().parent
SOURCE = EXAMPLES.parent / "source"
RESOURCE_MANIFEST = EXAMPLES.parent / "resources" / "manifest.json"
REQUIRED = ("inverse_model", "forward_model", "scaler_x", "scaler_y")
SEED = 0


def verify_resources(root):
    manifest = json.loads(RESOURCE_MANIFEST.read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["files"]}
    missing = [entries[name]["path"] for name in REQUIRED
               if not (root / entries[name]["path"]).is_file()]
    if missing:
        raise ValueError("Missing design resources: " + ", ".join(missing))

    payloads, hashes = {}, {}
    for name in REQUIRED:
        entry = entries[name]
        with (root / entry["path"]).open("rb") as stream:
            payload = stream.read(entry["size_bytes"] + 1)
        digest = sha256(payload).hexdigest().upper()
        if len(payload) != entry["size_bytes"] or digest != entry["sha256"]:
            raise ValueError(f"Resource size or SHA-256 mismatch: {entry['path']}")
        payloads[name], hashes[name] = payload, digest
    return payloads, hashes


def run_design(args, work):
    # The distributed checkpoints use the original src.* module names.
    sys.path[:0] = [str(SOURCE), str(SOURCE / "TrussInvertibleDesign")]
    os.environ["MPLCONFIGDIR"] = str(work / "matplotlib")

    import numpy as np
    import torch
    from TrussInvertibleDesign.main_export_test import run_optimized
    from TrussInvertibleDesign.src.latent_optimization import VERSION
    from TrussInvertibleDesign.src.normalization import MinMaxScaler
    from latticegpt_9x_candidate_ranking import (
        STIFFNESS_NAMES,
        check_target_scaler_range,
        rank_candidates_nmse,
        validate_restricted_9d_p09,
    )
    from product_workflow.inputs import target_from_csv_upload
    from services.truss_inverse_service import build_delivered_candidates

    target_bytes = args.target.read_bytes()
    target = target_from_csv_upload(target_bytes, filename=args.target.name)["physical_9d_gpa"]
    payloads, resource_hashes = verify_resources(args.resources)
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    generator = torch.Generator(device="cpu").manual_seed(SEED)

    scaler_x, scaler_y = MinMaxScaler(), MinMaxScaler()
    scaler_x.load(BytesIO(payloads["scaler_x"]))
    scaler_y.load(BytesIO(payloads["scaler_y"]))
    bounds = check_target_scaler_range(
        target,
        scaler_y_min=dict(zip(STIFFNESS_NAMES, scaler_y.min.tolist(), strict=True)),
        scaler_y_max=dict(zip(STIFFNESS_NAMES, scaler_y.max.tolist(), strict=True)),
    )
    if bounds["status"] != "PASS" or validate_restricted_9d_p09(target)["status"] != "PASS":
        raise ValueError("Target is outside the supported scaler range or fails the V1 stiffness check.")

    # The locked hashes above must match before loading the full-model checkpoints.
    inverse, forward = [
        torch.load(BytesIO(payloads[name]), map_location="cpu", weights_only=False).eval()
        for name in ("inverse_model", "forward_model")
    ]
    data = work / "datatest"
    data.mkdir()
    (data / "target_stiffness.csv").write_bytes(target_bytes)
    previous = Path.cwd()
    try:
        os.chdir(work)
        run_optimized(inverse, forward, scaler_x, scaler_y, generator)
    finally:
        os.chdir(previous)

    prediction = data / "prediction_target_test.csv"
    candidates = build_delivered_candidates(prediction, target)
    ranking = rank_candidates_nmse(
        [{**candidate, "source_candidate_id": candidate["candidate_id"],
          "b14_status": validate_restricted_9d_p09(candidate["predicted_stiffness"])["status"]}
         for candidate in candidates],
        target_stiffness_gpa=target,
        target_range_status=bounds["status"],
    )
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    files = {"candidates.csv": prediction}
    result = {
        "algorithm": VERSION,
        "ranking": ranking["contract"],
        "rng_seed": SEED,
        "torch_version": torch.__version__,
        "target_sha256": sha256(target_bytes).hexdigest().upper(),
        "target_gpa": target,
        "resources_sha256": resource_hashes,
        "recommended_candidate": ranking["selected_source_candidate_id"],
        "ranks": sorted(ranking["candidates"], key=lambda candidate: candidate["rank"]),
        "mechanical_validation": "not_run",
    }
    if args.select_rank is not None:
        chosen = result["ranks"][args.select_rank - 1]
        if not chosen["eligible"]:
            raise ValueError("Selected rank is ineligible under the V1 ranking contract.")
        result["selected_rank"] = args.select_rank
        result["selected_candidate"] = chosen["source_candidate_id"]
        files.update(conformal_export(args.mesh, by_id[chosen["source_candidate_id"]], work, result))
    return files, result


def conformal_export(mesh, candidate, work, report):
    from candidate_cell_adapter import build_custom_cell_from_candidate
    from conformal_service import run_conformal_service
    from latticegpt_9x_mesh_precheck import (
        WHOLE_FILE_PRODUCT_DOMAIN,
        WHOLE_FILE_PRODUCT_PURPOSE,
        WHOLE_FILE_SELECTOR_VALUE,
        precheck_inp_mesh,
    )

    # Use an isolated byte copy so the inspected mesh is exactly the generated mesh input.
    if mesh.suffix.lower() != ".inp":
        raise ValueError("Mesh must use the .inp extension.")
    used_mesh = work / "selected.inp"
    used_mesh.write_bytes(mesh.read_bytes())
    check = precheck_inp_mesh(
        used_mesh, selector_type=WHOLE_FILE_PRODUCT_DOMAIN,
        selector_value=WHOLE_FILE_SELECTOR_VALUE, selector_explicit_declaration=True,
        selector_purpose=WHOLE_FILE_PRODUCT_PURPOSE,
    )
    if check["status"] != "passed":
        raise ValueError("Mesh precheck failed: " + "; ".join(check["errors"]))

    adapter = build_custom_cell_from_candidate(
        candidate, work / "adapter", dry_run=False, allowed_output_root=work,
    )
    if adapter["status"] != "static_validated":
        raise ValueError("Candidate adaptation failed: " + "; ".join(adapter["errors"]))

    length = check["geometry_quality"]["reference_length"]
    diameter = candidate["diameter"] * length
    types = check["mesh"]
    output = run_conformal_service({
        "source_mesh_path": used_mesh,
        "source_mesh_contract": {
            "schema_version": "latticegpt_runtime_source_mesh_contract_v1",
            "element_type_original": types["element_type_original"],
            "element_type_effective": types["element_type_effective"],
        },
        "cell_type": "custom",
        "cell_config_path": adapter["custom_cell_path"],
        "output_root": work,
        "allowed_output_roots": [work],
        "package_id": "design",
        "diameter": diameter,
        "diameter_source": "normalized_candidate_diameter_times_reference_length",
        "runtime_enabled": True,
    })
    if output["status"] != "success":
        raise ValueError("Conformal geometry failed: " + output["message"])

    report["mesh_sha256"] = check["source"]["sha256"]
    report["geometry"] = {
        "domain": "whole_file",
        "units": "mm",
        "source_elements": output["source_element_count"],
        "source_element_type_original": output["source_element_type_original"],
        "source_element_type_effective": output["source_element_type_effective"],
        "reference_length_mm": length,
        "normalized_diameter": candidate["diameter"],
        "strut_diameter_mm": diameter,
        "b31_nodes": output["b31_node_count"],
        "b31_beams": output["b31_beam_count"],
    }
    return {
        "conformal_b31.inp": Path(output["core_b31_path"]),
        "conformal_preview.stl": Path(output["stl_path"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", required=True, type=Path,
                        help="Directory with the verified models/ and scalers/ files.")
    parser.add_argument("--output", required=True, type=Path, help="New directory for results.")
    parser.add_argument("--target", type=Path, default=EXAMPLES / "target_9d_gpa.csv")
    parser.add_argument("--mesh", type=Path)
    parser.add_argument("--select-rank", type=int, choices=(1, 2, 3))
    parser.add_argument("--whole-mesh-mm", action="store_true",
                        help="Confirm that the entire input mesh is the design domain and uses mm.")
    args = parser.parse_args()
    if len({args.mesh is not None, args.select_rank is not None, args.whole_mesh_mm}) != 1:
        parser.error("Supply --mesh, --select-rank and --whole-mesh-mm together for geometry generation.")
    if args.output.exists():
        parser.error("Output directory already exists.")

    work = Path(gettempdir()).resolve() / f"bonemetgpt-design-{uuid4().hex}"
    work.mkdir()
    try:
        with redirect_stdout(sys.stderr):
            files, result = run_design(args, work)
        result["output_sha256"] = {name: sha256(path.read_bytes()).hexdigest().upper()
                                   for name, path in files.items()}
        args.output.mkdir()
        for name, path in files.items():
            shutil.copyfile(path, args.output / name)
        (args.output / "design.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8",
        )
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        parser.error(str(error))
    finally:
        shutil.rmtree(work)
    print("Design results written.")


if __name__ == "__main__":
    main()

