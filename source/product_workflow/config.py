"""Machine-independent path and service configuration for the V1 product lane."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


CONFIG_SCHEMA_VERSION = "latticegpt_paths_config_v1"
CONFIG_SCHEMA_VERSION_V2 = "latticegpt_paths_config_v2"

_ARTIFACT_PATHS = {
    "formal_csv": Path("formal_data/formal_150w.csv"),
    "formal_query_sqlite": Path("projections/formal_150w_query_v1.sqlite"),
    "formal_query_manifest": Path("projections/formal_150w_query_v1.manifest.json"),
    "inverse_model": Path("models/inverse_model.pt"),
    "forward_model": Path("models/forward_model.pt"),
    "scaler_x": Path("scalers/scaler_X.pt"),
    "scaler_y": Path("scalers/scaler_Y.pt"),
}


def _contains_parent_traversal(path: str | Path) -> bool:
    return ".." in Path(path).parts


def _resolved_path(value: str | Path, *, base: Path, label: str) -> Path:
    path = Path(value)
    if _contains_parent_traversal(path):
        raise ValueError(f"{label} contains parent traversal")
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def require_within(path: str | Path, root: str | Path, *, label: str) -> Path:
    """Resolve *path* and require it to remain below *root*."""

    if _contains_parent_traversal(path):
        raise ValueError(f"{label} contains parent traversal")
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    if not _is_within(resolved_path, resolved_root):
        raise ValueError(f"{label} is outside its allowed root")
    return resolved_path


@dataclass(frozen=True)
class LatticeGPTPaths:
    source_root: Path
    release_root: Path
    artifact_root: Path
    runtime_root: Path
    formal_csv: Path
    formal_query_sqlite: Path
    formal_query_manifest: Path
    inverse_model: Path
    forward_model: Path
    scaler_x: Path
    scaler_y: Path
    active_generation: str = "r3"
    source_release_prefix: Path = Path("release/v1/r3")
    metadata_prefix: Path = Path("release_metadata/r3")
    environment_control_root: Path | None = None
    generation_contract_path: Path | None = None
    generation_contract_sha256_value: str = ""
    active_release_binding_path: Path | None = None
    config_path: Path | None = None
    config_sha256: str = ""

    @property
    def source_manifest(self) -> Path:
        return require_within(
            self.source_root / self.source_release_prefix / "source_manifest_v1.json",
            self.source_root,
            label="R3 source manifest",
        )

    @property
    def source_manifest_sha256(self) -> Path:
        return require_within(
            self.source_root
            / self.source_release_prefix
            / "source_manifest_v1.json.sha256",
            self.source_root,
            label="R3 source manifest SHA256",
        )

    @property
    def r2_7_source_manifest(self) -> Path:
        return require_within(
            self.source_root / "release" / "v1" / "r2_7" / "source_manifest_v1.json",
            self.source_root,
            label="historical R2.7 source manifest",
        )

    @property
    def r2_7_source_manifest_sha256(self) -> Path:
        return require_within(
            self.source_root / "release" / "v1" / "r2_7" / "source_manifest_v1.json.sha256",
            self.source_root,
            label="historical R2.7 source manifest SHA256",
        )

    @property
    def artifact_manifest(self) -> Path:
        return require_within(
            self.metadata_root / "artifact_manifest_v1.json",
            self.release_root,
            label="R3 artifact manifest",
        )

    @property
    def artifact_manifest_sha256(self) -> Path:
        return require_within(
            self.metadata_root / "artifact_manifest_v1.json.sha256",
            self.release_root,
            label="R3 artifact manifest SHA256",
        )

    @property
    def environment_root(self) -> Path:
        return self.environment_control_root or self.release_root

    @property
    def metadata_root(self) -> Path:
        return require_within(self.release_root / self.metadata_prefix, self.release_root, label="metadata root")

    @property
    def environment_manifest(self) -> Path:
        return require_within(
            self.metadata_root / "environment_manifest_v1.json",
            self.release_root,
            label="environment manifest",
        )

    @property
    def environment_manifest_sha256(self) -> Path:
        return require_within(
            self.metadata_root / "environment_manifest_v1.json.sha256",
            self.release_root,
            label="environment manifest SHA256",
        )

    @property
    def reproduction_tolerances(self) -> Path:
        return require_within(
            self.source_root / "config" / "reproduction_tolerances_v1.json",
            self.source_root,
            label="R3 reproduction tolerances",
        )

    @property
    def generation_contract(self) -> Path:
        if self.generation_contract_path is None:
            raise ValueError("generation contract is unavailable for config v1")
        return self.generation_contract_path

    @property
    def active_release_binding(self) -> Path:
        if self.active_release_binding_path is None:
            raise ValueError("active release binding is unavailable for config v1")
        return self.active_release_binding_path

    @property
    def active_release_binding_sha256(self) -> Path:
        return Path(str(self.active_release_binding) + ".sha256")

    @property
    def jobs_root(self) -> Path:
        return require_within(self.runtime_root / "jobs", self.runtime_root, label="jobs root")

    @property
    def adapter_root(self) -> Path:
        return require_within(self.runtime_root / "adapter", self.runtime_root, label="adapter root")

    @property
    def conformal_root(self) -> Path:
        return require_within(
            self.runtime_root / "conformal", self.runtime_root, label="conformal root"
        )


@dataclass(frozen=True)
class LatticeGPTServiceConfig:
    chainlit_host: str = "127.0.0.1"
    chainlit_port: int = 8000
    viewer_host: str = "127.0.0.1"
    viewer_port: int = 8001
    frontend_host: str = "127.0.0.1"
    frontend_port: int = 5173

    def __post_init__(self) -> None:
        for name in ("chainlit_host", "viewer_host", "frontend_host"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("chainlit_port", "viewer_port", "frontend_port"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError(f"{name} must be a valid TCP port")

    @property
    def viewer_backend_origin(self) -> str:
        return f"http://{self.viewer_host}:{self.viewer_port}"

    @property
    def viewer_frontend_origin(self) -> str:
        return f"http://{self.frontend_host}:{self.frontend_port}"


@dataclass(frozen=True)
class LatticeGPTConfig:
    paths: LatticeGPTPaths
    services: LatticeGPTServiceConfig


def _read_json_config(config_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read BoneMetGPT config: {config_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("BoneMetGPT config must be a JSON object")
    if payload.get("schema_version") not in {CONFIG_SCHEMA_VERSION, CONFIG_SCHEMA_VERSION_V2}:
        raise ValueError(f"schema_version must be {CONFIG_SCHEMA_VERSION} or {CONFIG_SCHEMA_VERSION_V2}")
    services = payload.get("services", {})
    if not isinstance(services, dict):
        raise ValueError("services must be a JSON object")
    return payload


def _first_value(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _service_value(
    name: str,
    *,
    explicit_services: Mapping[str, object],
    environ: Mapping[str, str],
    json_services: Mapping[str, object],
    default: object,
):
    env_name = f"LATTICEGPT_{name.upper()}"
    value = _first_value(
        explicit_services.get(name),
        environ.get(env_name),
        json_services.get(name),
        default,
    )
    if name.endswith("_port"):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a valid TCP port") from exc
    return value


def load_latticegpt_config(
    *,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    explicit: Mapping[str, object] | None = None,
) -> LatticeGPTConfig:
    """Load the V1 configuration without guessing a workspace or sibling root."""

    env = os.environ if environ is None else environ
    explicit_values = {} if explicit is None else dict(explicit)
    selected_config_path = _first_value(config_path, env.get("LATTICEGPT_CONFIG"))

    payload: dict[str, object] = {}
    if selected_config_path is not None:
        raw_config_path = Path(selected_config_path)
        if _contains_parent_traversal(raw_config_path):
            raise ValueError("config path contains parent traversal")
        resolved_config_path = raw_config_path.resolve()
        probed_payload = _read_json_config(resolved_config_path)
        if probed_payload.get("schema_version") == CONFIG_SCHEMA_VERSION_V2:
            from .release_generation import load_verified_canonical_object

            if str(probed_payload.get("active_generation", "")).startswith("r4_lite_"):
                from .r4_lite_release_generation import validate_config_v2
            else:
                from .release_generation import validate_config_v2

            payload, config_sha = load_verified_canonical_object(
                resolved_config_path,
                Path(str(resolved_config_path) + ".sha256"),
                validator=validate_config_v2,
            )
        else:
            payload = probed_payload
            config_sha = ""
        config_parent = resolved_config_path.parent
    else:
        declared_release_root = explicit_values.get("release_root")
        if declared_release_root is None:
            raise ValueError("a config file or explicit release root is required")
        config_parent = Path(declared_release_root).resolve()

    release_value = explicit_values.get("release_root")
    release_root = (
        _resolved_path(release_value, base=config_parent, label="release root")
        if release_value is not None
        else config_parent.resolve()
    )

    artifact_value = _first_value(
        explicit_values.get("artifact_root"),
        env.get("LATTICEGPT_ARTIFACT_ROOT"),
        payload.get("artifact_root"),
        "artifacts_v1",
    )
    runtime_value = _first_value(
        explicit_values.get("runtime_root"),
        env.get("LATTICEGPT_RUNTIME_ROOT"),
        payload.get("runtime_root"),
        "runtime",
    )
    artifact_root = _resolved_path(artifact_value, base=release_root, label="artifact root")
    runtime_root = _resolved_path(runtime_value, base=release_root, label="runtime root")

    if (
        artifact_root == runtime_root
        or _is_within(artifact_root, runtime_root)
        or _is_within(runtime_root, artifact_root)
    ):
        raise ValueError("artifact root and runtime root overlap")

    artifact_files = {
        name: require_within(artifact_root / relative, artifact_root, label=name)
        for name, relative in _ARTIFACT_PATHS.items()
    }
    is_v2 = payload.get("schema_version") == CONFIG_SCHEMA_VERSION_V2
    source_prefix = Path(str(payload.get("source_release_prefix", "release/v1/r3")))
    metadata_prefix = Path(str(payload.get("metadata_prefix", "release_metadata/r3")))
    environment_control_root = (
        _resolved_path(payload["environment_control_root"], base=release_root, label="environment control root")
        if is_v2 else release_root
    )
    generation_contract_path = (
        _resolved_path(payload["generation_contract"], base=release_root, label="generation contract")
        if is_v2 else None
    )
    active_release_binding_path = (
        _resolved_path(payload["active_release_binding"], base=release_root, label="active release binding")
        if is_v2 else None
    )
    if is_v2:
        from .release_generation import load_verified_canonical_object

        if str(payload.get("active_generation", "")).startswith("r4_lite_"):
            from .r4_lite_release_generation import validate_generation_contract_v1
        else:
            from .release_generation import validate_generation_contract_v1

        _, observed_contract_sha = load_verified_canonical_object(
            generation_contract_path,
            Path(str(generation_contract_path) + ".sha256"),
            validator=validate_generation_contract_v1,
        )
        if observed_contract_sha != payload["generation_contract_sha256"]:
            raise ValueError("generation contract SHA does not match config")
    paths = LatticeGPTPaths(
        source_root=Path(__file__).resolve().parents[1],
        release_root=release_root,
        artifact_root=artifact_root,
        runtime_root=runtime_root,
        active_generation=str(payload.get("active_generation", "r3")),
        source_release_prefix=source_prefix,
        metadata_prefix=metadata_prefix,
        environment_control_root=environment_control_root,
        generation_contract_path=generation_contract_path,
        generation_contract_sha256_value=str(payload.get("generation_contract_sha256", "")),
        active_release_binding_path=active_release_binding_path,
        config_path=resolved_config_path if selected_config_path is not None else None,
        config_sha256=config_sha if selected_config_path is not None else "",
        **artifact_files,
    )

    json_services = payload.get("services", {})
    explicit_services = explicit_values.get("services", {})
    if not isinstance(explicit_services, Mapping):
        raise ValueError("explicit services must be a mapping")
    defaults = LatticeGPTServiceConfig()
    services = LatticeGPTServiceConfig(
        chainlit_host=_service_value(
            "chainlit_host",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.chainlit_host,
        ),
        chainlit_port=_service_value(
            "chainlit_port",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.chainlit_port,
        ),
        viewer_host=_service_value(
            "viewer_host",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.viewer_host,
        ),
        viewer_port=_service_value(
            "viewer_port",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.viewer_port,
        ),
        frontend_host=_service_value(
            "frontend_host",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.frontend_host,
        ),
        frontend_port=_service_value(
            "frontend_port",
            explicit_services=explicit_services,
            environ=env,
            json_services=json_services,
            default=defaults.frontend_port,
        ),
    )
    return LatticeGPTConfig(paths=paths, services=services)


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "CONFIG_SCHEMA_VERSION_V2",
    "LatticeGPTConfig",
    "LatticeGPTPaths",
    "LatticeGPTServiceConfig",
    "load_latticegpt_config",
    "require_within",
]
