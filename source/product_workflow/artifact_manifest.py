"""Readonly artifact-manifest validation for the LatticeGPT V1 product lane."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .contracts import formal_bundle_binding_from_verified_artifacts
from .manifest_io import read_verified_json, sha256_file, validate_relative_path


ARTIFACT_MANIFEST_SCHEMA_VERSION = "latticegpt_artifact_manifest_v1"
ARTIFACT_MANIFEST_NAME = "artifact_manifest_v1.json"
ARTIFACT_MANIFEST_SHA_NAME = "artifact_manifest_v1.json.sha256"
REDISTRIBUTION_STATUS = "UNKNOWN_REQUIRES_OWNER_DECISION"
APPROVED_BUNDLE_ID = "latticegpt-v1-controlled-artifacts"
APPROVED_BUNDLE_VERSION = "1"
APPROVED_MANIFEST_STATUS = "VERIFIED_INTERNAL_CONTROLLED_ARTIFACTS"
APPROVED_PROVENANCE_STATUS = "PROJECT_CONFIRMED_CURRENT_BASELINE"
APPROVED_ARTIFACT_NON_CLAIMS = ("NOT_PUBLIC_RELEASE_AUTHORITY",)
APPROVED_MANIFEST_NON_CLAIMS = ("NOT_HISTORICAL_TRAINING_REPRODUCIBILITY",)

APPROVED_ARTIFACT_IDENTITIES: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        "formal_csv": MappingProxyType(
            {
                "size_bytes": 2_750_734_702,
                "sha256": "C5F13D1532FD94AACB8AF215F8653BBAD341AD719AEA4811282109B8648173DC",
            }
        ),
        "inverse_model": MappingProxyType(
            {
                "size_bytes": 4_389_357,
                "sha256": "A92DA57C43036BFAB4AB995ABD957C3D5AD725A80E015C237A429097027C5BB3",
            }
        ),
        "forward_model": MappingProxyType(
            {
                "size_bytes": 1_741_285,
                "sha256": "AB2EEBDFCC6669C04E04EF2EC38F8613A4FBEDDE14C4879BD74B6DEE2DA8197E",
            }
        ),
        "scaler_x": MappingProxyType(
            {
                "size_bytes": 1_837,
                "sha256": "E7CE01CB9AE2AD4C45415E681B912DF400F8C439835DC2D87FC30ECBF9E4DECE",
            }
        ),
        "scaler_y": MappingProxyType(
            {
                "size_bytes": 1_837,
                "sha256": "11BDD5087CFD88533800B9BAE5E834A7E385E14A620774B81AE5C9F77A6E9349",
            }
        ),
        "formal_query_sqlite": MappingProxyType(
            {
                "size_bytes": 187_719_680,
                "sha256": "F4BD9A524E5328F7A62533F02F521881F53213F4E7D67F21C2F5C371D5141EB6",
            }
        ),
        "formal_query_manifest": MappingProxyType(
            {
                "size_bytes": 1_568,
                "sha256": "8B361B2FD7085A9889ADC5AB43B4704BC2862FD3283F1FBB05BDDF40CCCAA05B",
            }
        ),
    }
)

DELIVERED_INVERSE_IDENTITY = MappingProxyType({
    'size_bytes': 5_488_781,
    'sha256': '20E375279AFB813CC43A74EB275D32236C24B809EA90729FE0659771E30B2C7C',
})
APPROVED_BUNDLE_IDENTITIES = MappingProxyType({
    '1': APPROVED_ARTIFACT_IDENTITIES,
    '2': MappingProxyType({**APPROVED_ARTIFACT_IDENTITIES, 'inverse_model': DELIVERED_INVERSE_IDENTITY}),
})

ARTIFACT_LAYOUT: dict[str, dict[str, str]] = {
    "formal_csv": {
        "role": "formal_data",
        "relative_path": "formal_data/formal_150w.csv",
        "loader_policy": "READ_ONLY_BYTES",
    },
    "inverse_model": {
        "role": "inverse_model",
        "relative_path": "models/inverse_model.pt",
        "loader_policy": "TRUSTED_TORCH_PICKLE_AFTER_SHA256",
    },
    "forward_model": {
        "role": "forward_model",
        "relative_path": "models/forward_model.pt",
        "loader_policy": "TRUSTED_TORCH_PICKLE_AFTER_SHA256",
    },
    "scaler_x": {
        "role": "scaler_x",
        "relative_path": "scalers/scaler_X.pt",
        "loader_policy": "TORCH_WEIGHTS_ONLY_TRUE",
    },
    "scaler_y": {
        "role": "scaler_y",
        "relative_path": "scalers/scaler_Y.pt",
        "loader_policy": "TORCH_WEIGHTS_ONLY_TRUE",
    },
    "formal_query_sqlite": {
        "role": "projection",
        "relative_path": "projections/formal_150w_query_v1.sqlite",
        "loader_policy": "READ_ONLY_BYTES",
    },
    "formal_query_manifest": {
        "role": "projection_metadata",
        "relative_path": "projections/formal_150w_query_v1.manifest.json",
        "loader_policy": "JSON_METADATA",
    },
}

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "bundle_id",
    "bundle_version",
    "status",
    "source_release_sha256",
    "artifacts",
    "trusted_source_boundary",
    "non_claims",
}
_ARTIFACT_FIELDS = {
    "logical_id",
    "role",
    "relative_path",
    "size_bytes",
    "sha256",
    "required",
    "loader_policy",
    "redistribution_status",
    "provenance_status",
    "non_claims",
}


@dataclass(frozen=True)
class VerifiedArtifact:
    logical_id: str
    role: str
    path: Path
    relative_path: str
    size_bytes: int
    sha256: str
    loader_policy: str
    redistribution_status: str


@dataclass(frozen=True)
class ArtifactBindingV1:
    bundle_id: str
    bundle_version: str
    manifest_path: Path
    manifest_sha256: str
    source_release_sha256: str
    artifacts: Mapping[str, VerifiedArtifact]

    def require(self, logical_id: str) -> VerifiedArtifact:
        try:
            return self.artifacts[logical_id]
        except KeyError as exc:
            raise ValueError(f"unknown artifact logical ID: {logical_id}") from exc

    def formal_bundle_binding(self) -> dict[str, object]:
        names = (
            "formal_csv",
            "inverse_model",
            "forward_model",
            "scaler_x",
            "scaler_y",
        )
        verified = {
            name: {
                "path": str(self.require(name).path),
                "size_bytes": self.require(name).size_bytes,
                "sha256": self.require(name).sha256,
                "status": "PASS",
            }
            for name in names
        }
        return formal_bundle_binding_from_verified_artifacts(verified)


def _require_text(value: object, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _require_upper_sha(value: object, *, label: str) -> str:
    normalized = _require_text(value, label=label)
    if (
        len(normalized) != 64
        or normalized != normalized.upper()
        or any(character not in "0123456789ABCDEF" for character in normalized)
    ):
        raise ValueError(f"{label} must be an uppercase SHA256")
    return normalized


def require_approved_artifact_identity(
    logical_id: str, size_bytes: int, sha256: str, *, bundle_version: str = APPROVED_BUNDLE_VERSION
) -> None:
    try:
        identities = APPROVED_ARTIFACT_IDENTITIES if bundle_version == '1' else APPROVED_BUNDLE_IDENTITIES[bundle_version]
        approved = identities[logical_id]
    except KeyError as exc:
        raise ValueError(f"unknown approved artifact logical ID: {logical_id}") from exc
    if size_bytes != approved["size_bytes"] or sha256 != approved["sha256"]:
        raise ValueError(f"{logical_id} does not match the project-approved baseline")


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _require_no_reparse(root: Path, relative_path: str) -> Path:
    current = root
    if _is_reparse_or_symlink(current):
        raise ValueError("artifact root cannot be a reparse point or symlink")
    for part in relative_path.split("/"):
        current = current / part
        if _is_reparse_or_symlink(current):
            raise ValueError("artifact path cannot contain a reparse point or symlink")
    return current.resolve()


def _require_single_link_file(path: Path, *, label: str) -> None:
    try:
        link_count = path.stat().st_nlink
    except (AttributeError, OSError) as exc:
        raise ValueError(f"unable to verify {label} hardlink status") from exc
    if link_count != 1:
        raise ValueError(f"{label} hardlink is forbidden")


def load_artifact_binding(
    artifact_root: str | Path,
    *,
    manifest_path: str | Path | None = None,
    manifest_sha_path: str | Path | None = None,
) -> ArtifactBindingV1:
    unresolved_root = Path(artifact_root).absolute()
    if _is_reparse_or_symlink(unresolved_root):
        raise ValueError("artifact root cannot be a reparse point or symlink")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise ValueError(f"artifact root is missing: {root}")
    selected_manifest = (
        Path(manifest_path) if manifest_path is not None else root / ARTIFACT_MANIFEST_NAME
    )
    selected_sha = (
        Path(manifest_sha_path)
        if manifest_sha_path is not None
        else root / ARTIFACT_MANIFEST_SHA_NAME
    )
    payload, manifest_sha = read_verified_json(selected_manifest, selected_sha)
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise ValueError("artifact manifest top-level fields are invalid")
    if payload.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        raise ValueError("artifact manifest schema_version is invalid")
    if payload.get("status") != APPROVED_MANIFEST_STATUS:
        raise ValueError("artifact manifest status is invalid")
    if payload.get("bundle_id") != APPROVED_BUNDLE_ID:
        raise ValueError("artifact manifest bundle_id is invalid")
    if payload.get("bundle_version") not in APPROVED_BUNDLE_IDENTITIES:
        raise ValueError("artifact manifest bundle_version is invalid")
    source_release_sha = _require_upper_sha(
        payload.get("source_release_sha256"), label="source_release_sha256"
    )
    entries = payload.get("artifacts")
    if not isinstance(entries, list):
        raise ValueError("artifact manifest artifacts must be a list")
    actual_order = tuple(
        entry.get("logical_id") if isinstance(entry, dict) else None for entry in entries
    )
    if actual_order != tuple(ARTIFACT_LAYOUT):
        raise ValueError("artifact order does not match the V1 contract")

    verified: dict[str, VerifiedArtifact] = {}
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ARTIFACT_FIELDS:
            raise ValueError("artifact record fields are invalid")
        logical_id = str(entry["logical_id"])
        expected = ARTIFACT_LAYOUT[logical_id]
        if entry["role"] != expected["role"]:
            raise ValueError(f"{logical_id} role mismatch")
        relative_path = validate_relative_path(
            str(entry["relative_path"]), label=f"{logical_id}.relative_path"
        )
        if relative_path != expected["relative_path"]:
            raise ValueError(f"{logical_id} relative path mismatch")
        folded = relative_path.casefold()
        if folded in seen_paths:
            raise ValueError("artifact relative paths must be casefold unique")
        seen_paths.add(folded)
        if entry["required"] is not True:
            raise ValueError(f"{logical_id} must be required")
        if entry["loader_policy"] != expected["loader_policy"]:
            raise ValueError(f"{logical_id} loader policy mismatch")
        if entry["redistribution_status"] != REDISTRIBUTION_STATUS:
            raise ValueError(f"{logical_id} redistribution status is not approved")
        if entry["provenance_status"] != APPROVED_PROVENANCE_STATUS:
            raise ValueError(f"{logical_id} provenance_status is not approved")
        if entry["non_claims"] != list(APPROVED_ARTIFACT_NON_CLAIMS):
            raise ValueError(f"{logical_id} non_claims are invalid")
        size_bytes = entry["size_bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(f"{logical_id} size_bytes is invalid")
        expected_sha = _require_upper_sha(entry["sha256"], label=f"{logical_id}.sha256")
        require_approved_artifact_identity(logical_id, size_bytes, expected_sha, bundle_version=payload['bundle_version'])
        path = _require_no_reparse(root, relative_path)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{logical_id} is outside artifact root") from exc
        if not path.is_file():
            raise ValueError(f"{logical_id} is missing")
        _require_single_link_file(path, label=logical_id)
        if path.stat().st_size != size_bytes:
            raise ValueError(f"{logical_id} size mismatch")
        if sha256_file(path) != expected_sha:
            raise ValueError(f"{logical_id} SHA256 mismatch")
        verified[logical_id] = VerifiedArtifact(
            logical_id=logical_id,
            role=expected["role"],
            path=path,
            relative_path=relative_path,
            size_bytes=size_bytes,
            sha256=expected_sha,
            loader_policy=expected["loader_policy"],
            redistribution_status=REDISTRIBUTION_STATUS,
        )

    trusted_boundary = payload.get("trusted_source_boundary")
    if trusted_boundary != {
        "status": APPROVED_PROVENANCE_STATUS,
        "public_release": "DENY_UNTIL_EXPLICIT_OWNER_DECISION",
    }:
        raise ValueError("trusted source boundary is invalid")
    if payload.get("non_claims") != list(APPROVED_MANIFEST_NON_CLAIMS):
        raise ValueError("artifact manifest non_claims are invalid")

    return ArtifactBindingV1(
        bundle_id=_require_text(payload.get("bundle_id"), label="bundle_id"),
        bundle_version=_require_text(
            payload.get("bundle_version"), label="bundle_version"
        ),
        manifest_path=selected_manifest.resolve(),
        manifest_sha256=manifest_sha,
        source_release_sha256=source_release_sha,
        artifacts=MappingProxyType(verified),
    )


def verify_trusted_loader_contract(binding: ArtifactBindingV1) -> dict[str, object]:
    if not isinstance(binding, ArtifactBindingV1):
        raise TypeError("artifact binding is required")
    for logical_id, expected in ARTIFACT_LAYOUT.items():
        artifact = binding.require(logical_id)
        if artifact.loader_policy != expected["loader_policy"]:
            raise ValueError(f"{logical_id} trusted loader policy mismatch")
        if artifact.redistribution_status != REDISTRIBUTION_STATUS:
            raise ValueError(f"{logical_id} redistribution status mismatch")
        require_approved_artifact_identity(
            logical_id, artifact.size_bytes, artifact.sha256, bundle_version=binding.bundle_version
        )
    return {"status": "PASS", "artifact_count": len(ARTIFACT_LAYOUT)}


__all__ = [
    "ARTIFACT_LAYOUT",
    "APPROVED_ARTIFACT_IDENTITIES",
    "ARTIFACT_MANIFEST_NAME",
    "ARTIFACT_MANIFEST_SCHEMA_VERSION",
    "ARTIFACT_MANIFEST_SHA_NAME",
    "ArtifactBindingV1",
    "VerifiedArtifact",
    "load_artifact_binding",
    "require_approved_artifact_identity",
    "verify_trusted_loader_contract",
]
