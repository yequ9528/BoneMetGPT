from __future__ import annotations

from pathlib import Path
from typing import Mapping

from latticegpt_9x_p10_manifest import _load_scaler_y_bounds
from services.truss_inverse_service import run_truss_inverse_service

from .artifact_manifest import ArtifactBindingV1
from .contracts import FORMAL_BUNDLE_ARTIFACT_NAMES, STIFFNESS_NAMES, require_sha256
from .config import LatticeGPTConfig, load_latticegpt_config, require_within
from .formal_150w_query import Formal150wQueryService
from .manifest_io import sha256_file
from .manifest_io import parse_closed_json_object_v1
from .release_generation import load_verified_canonical_object
from .reproducibility import validate_comparison_contract_v1
from TrussInvertibleDesign.src.inference_contract import load_reproduction_profile_from_environment
from .preflight import PreflightBindingV1, require_preflight
from .runtime import adapter_controlled_write, conformal_controlled_run, verify_product_package
from .service import ProductWorkflowService


def _selected_config(config: LatticeGPTConfig | None) -> LatticeGPTConfig:
    return load_latticegpt_config() if config is None else config


def verify_formal_bundle_binding(artifact_binding: ArtifactBindingV1) -> dict:
    """Recheck the five executable artifacts and return the frozen Job contract."""

    if not isinstance(artifact_binding, ArtifactBindingV1):
        raise TypeError("artifact_binding must be ArtifactBindingV1")
    for name in FORMAL_BUNDLE_ARTIFACT_NAMES:
        artifact = artifact_binding.require(name)
        if not artifact.path.is_file():
            raise ValueError(f"{name} is missing")
        if artifact.path.stat().st_size != artifact.size_bytes:
            raise ValueError(f"{name} size mismatch")
        if sha256_file(artifact.path) != artifact.sha256:
            raise ValueError(f"{name} SHA256 mismatch")
    return artifact_binding.formal_bundle_binding()


def _load_r4_lite_comparison_contract(
    config: LatticeGPTConfig, preflight: PreflightBindingV1
) -> dict:
    active = preflight.active_release_binding
    if not isinstance(active, Mapping):
        raise ValueError("R4-Lite ActiveReleaseBinding is required")
    path = require_within(
        config.paths.metadata_root / "comparison_contract.json",
        config.paths.release_root,
        label="R4-Lite comparison contract",
    )
    contract, observed_sha = load_verified_canonical_object(
        path,
        Path(str(path) + ".sha256"),
        validator=lambda value: validate_comparison_contract_v1(
            value,
            tolerance_schema_path="config/reproduction_tolerances_v1.json",
            tolerance_schema_sha256=preflight.reproduction_tolerances_sha256,
        ),
    )
    if observed_sha != active.get("comparison_contract_sha256"):
        raise ValueError("R4-Lite comparison contract identity mismatch")
    return contract


def create_formal_query_service(
    config: LatticeGPTConfig | None = None,
    *,
    preflight: PreflightBindingV1 | None = None,
) -> Formal150wQueryService:
    selected = _selected_config(config)
    active = preflight or require_preflight(selected, profile="full_product")
    if active.profile != "full_product" or active.artifact is None:
        raise ValueError("full_product preflight binding is required")
    artifact_binding = active.artifact
    database = artifact_binding.require("formal_query_sqlite")
    manifest = artifact_binding.require("formal_query_manifest")
    source = artifact_binding.require("formal_csv")
    return Formal150wQueryService(
        database.path,
        manifest.path,
        expected_source_sha256=source.sha256,
        expected_manifest_sha256=require_sha256(manifest.sha256, "formal_query_manifest.sha256"),
        expected_projection_sha256=require_sha256(database.sha256, "formal_query_sqlite.sha256"),
    )


def create_product_workflow_service(
    config: LatticeGPTConfig | None = None,
    *,
    preflight: PreflightBindingV1 | None = None,
    reproduction_profile=None,
    execution_context=None,
    compute_providers: Mapping[str, object] | None = None,
    runtime_root_override: str | Path | None = None,
) -> ProductWorkflowService:
    selected = _selected_config(config)
    active = preflight or require_preflight(selected, profile="full_product")
    if active.profile != "full_product" or active.artifact is None:
        raise ValueError("full_product preflight binding is required")
    artifact_binding = active.artifact
    binding = verify_formal_bundle_binding(artifact_binding)
    if execution_context is not None:
        from .r3_execution_context import validate_r3_execution_context_v1

        if reproduction_profile is not None:
            raise ValueError("execution_context and reproduction_profile are mutually exclusive")
        selected_context = validate_r3_execution_context_v1(execution_context)
        selected_profile = None
        comparison_contract = None
    else:
        selected_context = None
        selected_profile = (
            load_reproduction_profile_from_environment()
            if reproduction_profile is None
            else reproduction_profile
        )
        if selected.paths.active_generation.startswith("r4_lite_"):
            comparison_contract = _load_r4_lite_comparison_contract(selected, active)
        else:
            comparison_contract = parse_closed_json_object_v1(
                selected.paths.reproduction_tolerances.read_bytes(),
                "latticegpt_reproduction_tolerances_v1",
            )
    readonly_package_producer = None
    if selected_context is None and selected.paths.active_generation.startswith("r4_lite_"):
        from .artifacts import validate_readonly_package_producer
        readonly_package_producer = {
            "generation": selected.paths.active_generation,
            "source_manifest_sha256": active.source_manifest_sha256,
            "config_sha256": selected.paths.config_sha256,
            "active_release_binding_sha256": active.active_release_binding_sha256,
        }
        if not isinstance(selected_profile, Mapping) or not isinstance(selected_profile.get("identities"), Mapping):
            raise ValueError("R4-Lite producer requires frozen reproduction identities")
        validate_readonly_package_producer(readonly_package_producer, selected_profile["identities"])
        release_binding = active.active_release_binding
        if not isinstance(release_binding, Mapping) or any(release_binding.get(k) != readonly_package_producer[k] for k in ("generation", "source_manifest_sha256", "config_sha256")):
            raise ValueError("R4-Lite producer differs from active release binding")
    if compute_providers is None:
        inverse_runner = run_truss_inverse_service
        conformal_runner = conformal_controlled_run
        minimum, maximum = _load_scaler_y_bounds(artifact_binding.require("scaler_y").path)
        scaler_y_min = dict(zip(STIFFNESS_NAMES, minimum, strict=True))
        scaler_y_max = dict(zip(STIFFNESS_NAMES, maximum, strict=True))
    else:
        required_provider_keys = {
            "inverse_runner", "conformal_runner", "scaler_y_min", "scaler_y_max",
        }
        if not isinstance(compute_providers, Mapping) or set(compute_providers) != required_provider_keys:
            raise ValueError("synthetic compute providers must contain the exact four-key contract")
        inverse_runner = compute_providers["inverse_runner"]
        conformal_runner = compute_providers["conformal_runner"]
        if not callable(inverse_runner) or not callable(conformal_runner):
            raise TypeError("synthetic compute runners must be callable")
        scaler_y_min = dict(compute_providers["scaler_y_min"])
        scaler_y_max = dict(compute_providers["scaler_y_max"])
        if set(scaler_y_min) != set(STIFFNESS_NAMES) or set(scaler_y_max) != set(STIFFNESS_NAMES):
            raise ValueError("synthetic scaler bounds must contain the exact 9D stiffness keys")
    runtime_root = selected.paths.runtime_root
    if runtime_root_override is not None:
        runtime_root = require_within(
            runtime_root_override,
            selected.paths.runtime_root,
            label="runtime root override",
        )
        if runtime_root == selected.paths.runtime_root:
            raise ValueError("runtime root override must be a child of configured runtime root")
    return ProductWorkflowService(
        runtime_root,
        inverse_runner=inverse_runner,
        scaler_y_min=scaler_y_min,
        scaler_y_max=scaler_y_max,
        adapter_runner=adapter_controlled_write,
        conformal_runner=conformal_runner,
        package_verifier=verify_product_package,
        adapter_output_root=runtime_root / "adapter",
        conformal_output_root=runtime_root / "conformal",
        formal_bundle_binding=binding,
        formal_bundle_verifier=lambda: verify_formal_bundle_binding(artifact_binding),
        reproduction_profile=selected_profile,
        comparison_contract=comparison_contract,
        source_manifest_sha256=(
            None if selected_context is not None else active.source_manifest_sha256
        ),
        execution_context=selected_context,
        readonly_package_producer=readonly_package_producer,
    )
