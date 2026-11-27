"""Fail-closed source/artifact/environment preflight bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .artifact_manifest import ArtifactBindingV1, load_artifact_binding
from .config import LatticeGPTConfig
from .environment_manifest import EnvironmentBindingV1, load_environment_binding
from .formal_150w_query import verify_projection_manifest
from .manifest_io import read_verified_json, sha256_file, validate_relative_path
from .release_generation import load_verified_canonical_object


class PreflightStageError(ValueError):
    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


@dataclass(frozen=True)
class SourceFileBinding:
    path: Path
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SourceBindingV1:
    manifest_path: Path
    manifest_sha256: str
    files: Mapping[str, SourceFileBinding]


@dataclass(frozen=True)
class PreflightBindingV1:
    profile: str
    source: SourceBindingV1
    artifact: ArtifactBindingV1 | None
    environment: EnvironmentBindingV1
    source_manifest_sha256: str
    artifact_manifest_sha256: str
    environment_manifest_sha256: str
    reproduction_tolerances_sha256: str = ""
    active_release_binding: Mapping[str, object] | None = None
    active_release_binding_sha256: str = ""


def _at_stage(stage: str, code: str, callback):
    try:
        return callback()
    except PreflightStageError:
        raise
    except Exception as exc:
        raise PreflightStageError(stage, code) from exc


def load_source_binding(
    source_root: str | Path,
    manifest_path: str | Path,
    detached_sha_path: str | Path,
) -> SourceBindingV1:
    root = Path(source_root).resolve()
    payload, manifest_sha = read_verified_json(manifest_path, detached_sha_path)
    if payload.get("schema_version") != "latticegpt_v1_source_manifest_v1":
        raise ValueError("source manifest schema is invalid")
    status = payload.get("status")
    if not isinstance(status, str) or status not in {
        "PASS_R3_SOURCE_IDENTITY",
        "PASS_R4_LITE_SOURCE_IDENTITY",
    }:
        raise ValueError(
            "active source manifest status is invalid; expected "
            "PASS_R3_SOURCE_IDENTITY or PASS_R4_LITE_SOURCE_IDENTITY"
        )
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("source manifest files are required")
    verified: dict[str, SourceFileBinding] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("source manifest file record is invalid")
        relative = validate_relative_path(str(entry.get("path") or ""), label="source path")
        if relative.casefold() in {key.casefold() for key in verified}:
            raise ValueError("source manifest contains duplicate paths")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("source file is outside source root") from exc
        size = entry.get("size_bytes")
        expected_sha = str(entry.get("sha256") or "").strip().upper()
        if not path.is_file():
            raise ValueError(f"source file is missing: {relative}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"source file size is invalid: {relative}")
        if len(expected_sha) != 64 or any(ch not in "0123456789ABCDEF" for ch in expected_sha):
            raise ValueError(f"source file SHA256 is invalid: {relative}")
        if path.stat().st_size != size or sha256_file(path) != expected_sha:
            raise ValueError(f"source file identity mismatch: {relative}")
        verified[relative] = SourceFileBinding(path, relative, size, expected_sha)
    return SourceBindingV1(
        manifest_path=Path(manifest_path).resolve(),
        manifest_sha256=manifest_sha,
        files=MappingProxyType(verified),
    )


def _load_viewer_environment(config: LatticeGPTConfig, source_sha: str) -> EnvironmentBindingV1:
    payload, _ = read_verified_json(
        config.paths.environment_manifest,
        config.paths.environment_manifest_sha256,
    )
    artifact_sha = str(payload.get("artifact_manifest_sha256") or "")
    return load_environment_binding(
        config.paths.environment_root,
        source_manifest_sha256=source_sha,
        artifact_manifest_sha256=artifact_sha,
        manifest_path=config.paths.environment_manifest,
        manifest_sha_path=config.paths.environment_manifest_sha256,
    )


def require_preflight(
    config: LatticeGPTConfig,
    *,
    profile: str,
    source_binding: SourceBindingV1 | None = None,
    artifact_binding: ArtifactBindingV1 | None = None,
    environment_binding: EnvironmentBindingV1 | None = None,
) -> PreflightBindingV1:
    if not isinstance(config, LatticeGPTConfig):
        raise TypeError("config must be LatticeGPTConfig")
    if profile not in {"full_product", "viewer_only"}:
        raise ValueError("preflight profile must be full_product or viewer_only")
    active = None
    active_sha = ""
    if getattr(config.paths, "active_release_binding_path", None) is not None:
        if str(config.paths.active_generation).startswith("r4_lite_"):
            from .r4_lite_release_generation import validate_active_release_binding_v1
        else:
            from .release_generation import validate_active_release_binding_v1
        active, active_sha = _at_stage(
            "active_release",
            "ACTIVE_RELEASE_BINDING_FAILURE",
            lambda: load_verified_canonical_object(
                config.paths.active_release_binding,
                config.paths.active_release_binding_sha256,
                validator=validate_active_release_binding_v1,
            ),
        )
        if active["config_sha256"] != config.paths.config_sha256 or active["generation_contract_sha256"] != config.paths.generation_contract_sha256_value:
            raise PreflightStageError("active_release", "ACTIVE_RELEASE_CONFIG_MISMATCH")
        if str(config.paths.active_generation).startswith("r4_lite_"):
            from .release_identity import validate_final_active_identity_v1

            final_path = config.paths.release_root / active["final_active_identity_relative"]
            final_identity, final_sha = _at_stage(
                "active_release",
                "FINAL_ACTIVE_IDENTITY_FAILURE",
                lambda: load_verified_canonical_object(
                    final_path,
                    Path(str(final_path) + ".sha256"),
                    validator=validate_final_active_identity_v1,
                ),
            )
            final_expected = {
                "generation": active["generation"],
                "config_sha256": active["config_sha256"],
                "generation_contract_sha256": active["generation_contract_sha256"],
                "active_release_binding_sha256": active_sha,
                "source_manifest_sha256": active["source_manifest_sha256"],
                "artifact_manifest_sha256": active["artifact_manifest_sha256"],
                "environment_manifest_sha256": active["environment_manifest_sha256"],
                "formal_bundle_binding_sha256": active["formal_bundle_binding_sha256"],
                "comparison_contract_sha256": active["comparison_contract_sha256"],
                "tolerance_schema_sha256": active["tolerance_schema_sha256"],
            }
            if any(final_identity[key] != expected for key, expected in final_expected.items()):
                raise PreflightStageError("active_release", "FINAL_ACTIVE_IDENTITY_CHAIN_MISMATCH")
    source = source_binding or _at_stage(
        "source",
        "SOURCE_BINDING_FAILURE",
        lambda: load_source_binding(
            config.paths.source_root,
            config.paths.source_manifest,
            config.paths.source_manifest_sha256,
        ),
    )
    artifact = artifact_binding
    if profile == "full_product":
        artifact = artifact or _at_stage(
            "artifact",
            "ARTIFACT_BINDING_FAILURE",
            lambda: load_artifact_binding(
                config.paths.artifact_root,
                manifest_path=config.paths.artifact_manifest,
                manifest_sha_path=config.paths.artifact_manifest_sha256,
            ),
        )
        if artifact.source_release_sha256 != source.manifest_sha256:
            raise ValueError("source-artifact manifest identity mismatch")
        environment = environment_binding or _at_stage(
            "environment",
            "ENVIRONMENT_BINDING_FAILURE",
            lambda: load_environment_binding(
                config.paths.environment_root,
                source_manifest_sha256=source.manifest_sha256,
                artifact_manifest_sha256=artifact.manifest_sha256,
                manifest_path=config.paths.environment_manifest,
                manifest_sha_path=config.paths.environment_manifest_sha256,
            ),
        )
        if environment.artifact_manifest_sha256 != artifact.manifest_sha256:
            raise ValueError("artifact-environment manifest identity mismatch")
        _at_stage(
            "projection",
            "PROJECTION_BINDING_FAILURE",
            lambda: verify_projection_manifest(
                artifact.require("formal_query_manifest").path,
                expected_source_sha256=artifact.require("formal_csv").sha256,
                expected_projection_sha256=artifact.require("formal_query_sqlite").sha256,
            ),
        )
        artifact_sha = artifact.manifest_sha256
    else:
        if artifact_binding is not None:
            raise ValueError("viewer_only preflight cannot accept artifact bytes")
        environment = environment_binding or _load_viewer_environment(
            config, source.manifest_sha256
        )
        artifact_sha = environment.artifact_manifest_sha256
    if environment.source_manifest_sha256 != source.manifest_sha256:
        raise ValueError("source-environment manifest identity mismatch")
    tolerance_sha = _at_stage(
        "tolerances",
        "TOLERANCE_IDENTITY_FAILURE",
        lambda: sha256_file(config.paths.reproduction_tolerances),
    )
    if active is not None:
        expected = {
            "source_manifest_sha256": source.manifest_sha256,
            "artifact_manifest_sha256": artifact_sha,
            "environment_manifest_sha256": environment.manifest_sha256,
            "tolerance_schema_sha256": tolerance_sha,
        }
        if any(active[key] != value for key, value in expected.items()):
            raise PreflightStageError("active_release", "ACTIVE_RELEASE_CHAIN_MISMATCH")
        if profile == "full_product":
            formal_sha = str(artifact.formal_bundle_binding()["binding_sha256"])
            if active["formal_bundle_binding_sha256"] != formal_sha:
                raise PreflightStageError("active_release", "ACTIVE_RELEASE_FORMAL_BUNDLE_MISMATCH")
    return PreflightBindingV1(
        profile=profile,
        source=source,
        artifact=artifact,
        environment=environment,
        source_manifest_sha256=source.manifest_sha256,
        artifact_manifest_sha256=artifact_sha,
        environment_manifest_sha256=environment.manifest_sha256,
        reproduction_tolerances_sha256=tolerance_sha,
        active_release_binding=active,
        active_release_binding_sha256=active_sha,
    )


__all__ = [
    "PreflightBindingV1",
    "SourceBindingV1",
    "SourceFileBinding",
    "PreflightStageError",
    "load_source_binding",
    "require_preflight",
]
