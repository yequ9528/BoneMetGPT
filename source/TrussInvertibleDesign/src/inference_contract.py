"""Apply the R3 CPU determinism contract before any model is loaded."""

from __future__ import annotations

import os
import random
from typing import Any, Mapping

from product_workflow.manifest_io import parse_closed_json_object_v1
from product_workflow.reproducibility import validate_reproduction_profile_v1


def load_reproduction_profile_from_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    raw = env.get("LATTICEGPT_REPRODUCTION_PROFILE_JSON")
    if not isinstance(raw, str):
        raise ValueError("LATTICEGPT_REPRODUCTION_PROFILE_JSON is required")
    return parse_closed_json_object_v1(
        raw.encode("utf-8"),
        "latticegpt_reproduction_profile_v1",
    )


def apply_inference_contract(
    profile: Mapping[str, Any],
    *,
    numpy_module=None,
    torch_module=None,
):
    value = validate_reproduction_profile_v1(profile)
    if numpy_module is None:
        import numpy as numpy_module
    if torch_module is None:
        import torch as torch_module
    random.seed(value["rng"]["python"]["seed"])
    numpy_module.random.seed(value["rng"]["numpy"]["seed"])
    torch_module.manual_seed(value["rng"]["torch_cpu"]["seed"])
    torch_module.use_deterministic_algorithms(True, warn_only=False)
    torch_module.set_num_threads(value["threads"]["torch_intraop"])
    torch_module.set_num_interop_threads(value["threads"]["torch_interop"])
    return torch_module.Generator(device="cpu").manual_seed(
        value["rng"]["torch_cpu"]["seed"]
    )


__all__ = ["apply_inference_contract", "load_reproduction_profile_from_environment"]
