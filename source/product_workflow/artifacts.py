"""Exact Job-bound product package and file resolution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import require_within
from .manifest_io import validate_relative_path
from .store import JOB_ID_PATTERN, JobStore, JobContractError, JobNotFoundError


PRODUCT_FILE_MAP = {
    "source_mesh": Path("input/source_c3d8.inp"),
    "b31": Path("core/conformal_b31.inp"),
    "stl": Path("visualization/conformal_preview.stl"),
    "png": Path("visualization/conformal_preview.png"),
    "metadata": Path("metadata.json"),
    "run_summary": Path("run_summary.txt"),
    "package_metadata": Path("package_metadata.json"),
    "geometry_ledger": Path("geometry_ledger.json"),
    "source_element_map": Path("source_element_map.csv"),
}
_OUTPUT_KEY = {
    "b31": "b31",
    "stl": "stl",
    "png": "png",
    "metadata": "legacy_metadata",
    "run_summary": "run_summary",
    "geometry_ledger": "geometry_ledger",
    "source_element_map": "source_element_map",
}
_PACKAGE_ID_PATTERN = re.compile(r"^job-[0-9A-F]{24}-attempt-[0-9]{4}$")
VIEWER_COMPATIBILITY_PATH = "config/viewer_readonly_compatibility_v1.json"
LEGACY_FILE_KEYS = ("source_mesh", "b31", "stl", "png", "metadata", "run_summary")
P13_FILE_KEYS = ("package_metadata", "geometry_ledger", "source_element_map")


def load_viewer_readonly_compatibility(preflight) -> dict:
    """Only a source-manifest member may grant the readonly evidence exception."""
    source = preflight.source
    member = source.files.get(VIEWER_COMPATIBILITY_PATH) if source is not None else None
    if member is None:
        return {}
    raw = member.path.read_bytes()
    if member.relative_path != VIEWER_COMPATIBILITY_PATH or len(raw) != member.size_bytes or hashlib.sha256(raw).hexdigest().upper() != member.sha256:
        raise ValueError("Viewer compatibility source identity mismatch")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate compatibility key")
            result[key] = value
        return result
    policy = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "jobs"} or policy["schema_version"] != "latticegpt_viewer_readonly_compatibility_v1" or not isinstance(policy["jobs"], dict):
        raise ValueError("Viewer compatibility closed schema mismatch")
    keys = {"job", "events", "producer", "allowed_job_extensions", "package_id", "package_format", "package_files", "supporting_files"}
    for job_id, entry in policy["jobs"].items():
        if not JOB_ID_PATTERN.fullmatch(job_id) or not isinstance(entry, dict) or set(entry) != keys:
            raise ValueError("Viewer compatibility entry is invalid")
        if entry["package_id"] != job_id + "-attempt-0001" or entry["package_format"] != "LEGACY_SIX_FILES_V1" or entry["allowed_job_extensions"] not in (["inverse_result"], ["inverse_result", "mesh_precheck"]):
            raise ValueError("Viewer compatibility scope mismatch")
        if not isinstance(entry["producer"], dict) or set(entry["producer"]) != {"generation", "source_manifest_sha256", "config_sha256", "active_release_binding_sha256"}:
            raise ValueError("Viewer compatibility producer is invalid")
        if not re.fullmatch(r"r4_lite_single_reference_workstation_v[0-9]+", entry["producer"]["generation"]):
            raise ValueError("Viewer compatibility producer generation is invalid")
        if any(not re.fullmatch(r"[0-9A-F]{64}", entry["producer"][key]) for key in entry["producer"] if key != "generation"):
            raise ValueError("Viewer compatibility producer digest is invalid")
        if not isinstance(entry["package_files"], dict) or set(entry["package_files"]) != set(LEGACY_FILE_KEYS) or not isinstance(entry["supporting_files"], list):
            raise ValueError("Viewer compatibility file roles are invalid")
        records = [entry["job"], entry["events"], *entry["package_files"].values(), *entry["supporting_files"]]
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes"}:
                raise ValueError("Viewer compatibility file identity is invalid")
            validate_relative_path(record["path"], label="Viewer compatibility path")
            if type(record["size_bytes"]) is not int or record["size_bytes"] <= 0 or not re.fullmatch(r"[0-9A-F]{64}", record["sha256"]):
                raise ValueError("Viewer compatibility file digest is invalid")
        if len({record["path"] for record in records}) != len(records):
            raise ValueError("Viewer compatibility duplicate file path")
    return policy


@dataclass(frozen=True)
class ProductPackage:
    job_id: str
    attempt_id: str
    package_id: str
    package_dir: Path
    metadata: dict
    package_metadata: dict | None
    file_identities: dict | None = None
    compatibility: dict | None = None
    mesh_view: dict | None = None


def _resolve_product_package(
    store: JobStore,
    job_id: str,
    conformal_root: str | Path,
) -> ProductPackage:
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("invalid job_id")
    job = store.get_consistent_for_viewer(job_id)
    if job.get("job_id") != job_id:
        raise ValueError("product Job identity mismatch")
    attempts = job.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1 or not isinstance(attempts[0], dict):
        raise ValueError("product result requires one unique attempt")
    attempt = attempts[0]
    attempt_id = str(attempt.get("attempt_id") or "")
    package_id = str(attempt.get("package_id") or "")
    verification = attempt.get("package_verification")
    if (
        job.get("status") != "VIEWER_READY"
        or attempt.get("status") != "VIEWER_READY"
        or not _PACKAGE_ID_PATTERN.fullmatch(package_id)
        or package_id != f"{job_id}-attempt-0001"
        or attempt_id != package_id
        or not isinstance(verification, dict)
        or verification.get("status") not in {"passed", "success", "complete"}
        or verification.get("package_id") != package_id
    ):
        raise ValueError("product package identity verification mismatch")

    recorded_dir = attempt.get("conformal_output_dir")
    if not isinstance(recorded_dir, str) or not recorded_dir.strip():
        raise ValueError("recorded conformal output directory is missing")
    try:
        package_dir = require_within(
            recorded_dir,
            conformal_root,
            label="product package directory",
        )
    except ValueError as exc:
        raise ValueError("HOLD_PATH_OUTSIDE_CURRENT_RUNTIME_ROOT") from exc
    if package_dir.name != package_id:
        raise ValueError("product package identity does not match recorded directory")
    if not package_dir.is_dir():
        raise FileNotFoundError("recorded package is missing from current conformal root")

    entry = store.viewer_compatibility.get("jobs", {}).get(job_id)
    p13_present = [(package_dir / PRODUCT_FILE_MAP[key]).is_file() for key in P13_FILE_KEYS]
    if entry is not None:
        if any(p13_present):
            raise ValueError("registered legacy package must not acquire P13 files")
        return _resolve_registered_legacy(store, job, entry, package_dir)
    receipt = verification.get("readonly_receipt")
    if receipt is not None:
        if any(p13_present) or attempt.get("conformal_result", {}).get("package_contract") is not None:
            raise ValueError("six-file receipt cannot be mixed with P13")
        from .service import normalize_job_mesh_binding
        mesh = normalize_job_mesh_binding(job, verify_stored_file=True)
        require_within(mesh["stored_path"], store.job_directory(job_id), label="product source mesh")
        if not isinstance(receipt, dict) or not isinstance(receipt.get("files"), dict):
            raise ValueError("readonly receipt is invalid")
        observed = validate_product_six_file_package(job_snapshot=job, package_dir=package_dir,
            normalized_mesh=mesh, producer=receipt.get("producer"), expected_file_identities=receipt["files"])
        if receipt != observed:
            raise ValueError("readonly receipt closed identity mismatch")
        metadata = _load_object(package_dir / "metadata.json", "metadata.json")
        return ProductPackage(job_id, attempt_id, package_id, package_dir, metadata, None, observed["files"],
            {"package_format": "PRODUCT_SIX_FILES_V1", "producer": receipt["producer"], "reader": store.reader_identity, "p13_status": "NOT_EVALUATED"}, mesh)
    if any(p13_present) and not all(p13_present):
        raise ValueError("incomplete P13 package cannot use legacy compatibility")
    metadata = _load_object(package_dir / "metadata.json", "metadata.json")
    if metadata.get("result_id") != package_id:
        raise ValueError("metadata result identity mismatch")
    package_metadata = _load_object(
        package_dir / "package_metadata.json", "package_metadata.json"
    )
    package_identity = package_metadata.get("package")
    if (
        not isinstance(package_identity, dict)
        or package_identity.get("package_id") != package_id
        or package_identity.get("result_id") != package_id
    ):
        raise ValueError("package_metadata identity mismatch")
    if package_metadata.get("schema_version") != "latticegpt_9x_package_metadata_v1":
        raise ValueError("P13 package schema mismatch")
    mesh_view = None
    if job.get("mesh_binding") is not None:
        from .service import normalize_job_mesh_binding
        mesh_view = normalize_job_mesh_binding(job, verify_stored_file=True)
        require_within(mesh_view["stored_path"], store.job_directory(job_id), label="product source mesh")
        _validate_product_metadata(job, metadata, mesh_view)
    package = ProductPackage(
        job_id=job_id,
        attempt_id=attempt_id,
        package_id=package_id,
        package_dir=package_dir,
        metadata=metadata,
        package_metadata=package_metadata,
        mesh_view=mesh_view,
    )
    for key in PRODUCT_FILE_MAP:
        resolve_product_package_file(package, key)
    if mesh_view is not None and _sha256(package_dir / PRODUCT_FILE_MAP["source_mesh"]) != mesh_view["raw_sha256"]:
        raise ValueError("package source mesh identity mismatch")
    from viewer_payloads import _build_p13_evidence
    ledger = _load_object(package_dir / PRODUCT_FILE_MAP["geometry_ledger"], "geometry_ledger")
    if ledger.get("schema_version") != "latticegpt_9x_geometry_ledger_v1":
        raise ValueError("P13 geometry ledger schema mismatch")
    _build_p13_evidence(
        package_id=package_id,
        p13_paths={key: package_dir / PRODUCT_FILE_MAP[key] for key in P13_FILE_KEYS},
        b31_path=package_dir / PRODUCT_FILE_MAP["b31"],
    )
    return package


def _verify_registered_file(runtime_root: Path, record: dict) -> Path:
    relative = validate_relative_path(record["path"], label="registered evidence path")
    path = require_within(runtime_root / relative, runtime_root, label="registered evidence")
    if not path.is_file():
        raise FileNotFoundError("registered evidence is missing")
    if path.stat().st_size != record["size_bytes"] or _sha256(path) != record["sha256"]:
        raise ProductFileIdentityError("registered evidence SHA256 or size mismatch")
    return path


def _resolve_registered_legacy(store, job, entry, package_dir):
    from .contracts import canonical_sha256
    from .service import normalize_job_mesh_binding

    attempt = job["attempts"][0]
    if entry["package_id"] != attempt["package_id"] or entry["package_format"] != "LEGACY_SIX_FILES_V1" or attempt.get("conformal_result", {}).get("package_contract") is not None:
        raise ValueError("registered legacy package contract mismatch")
    producer = entry["producer"]
    identities = job["reproduction"]["profile"]["identities"]
    if any(identities.get(key) != producer[key] for key in ("source_manifest_sha256", "config_sha256", "active_release_binding_sha256")):
        raise ValueError("registered legacy producer identity mismatch")
    files = {}
    for key in LEGACY_FILE_KEYS:
        record = entry["package_files"][key]
        path = _verify_registered_file(store.runtime_root, record)
        if path != require_within(package_dir / PRODUCT_FILE_MAP[key], package_dir, label="package file"):
            raise ValueError("registered package file path mismatch")
        files[key] = {**record, "path": PRODUCT_FILE_MAP[key].as_posix()}
    for record in entry["supporting_files"]:
        _verify_registered_file(store.runtime_root, record)
    mesh = normalize_job_mesh_binding(job, verify_stored_file=True)
    require_within(mesh["stored_path"], store.job_directory(job["job_id"]), label="product source mesh")
    validate_product_six_file_package(job_snapshot=job, package_dir=package_dir,
        normalized_mesh=mesh, producer=producer, expected_file_identities=files)
    metadata = _load_object(package_dir / "metadata.json", "metadata.json")
    return ProductPackage(job["job_id"], attempt["attempt_id"], attempt["package_id"], package_dir, metadata, None, files, {"package_format": "LEGACY_SIX_FILES_V1", "producer": producer, "reader": store.reader_identity, "p13_status": "NOT_EVALUATED"}, mesh)


def validate_readonly_package_producer(producer, identities):
    keys = {"generation", "source_manifest_sha256", "config_sha256", "active_release_binding_sha256"}
    if not isinstance(producer, dict) or set(producer) != keys:
        raise ValueError("readonly producer closed shape mismatch")
    if not isinstance(producer["generation"], str) or not re.fullmatch(r"r4_lite_single_reference_workstation_v[0-9]+", producer["generation"]):
        raise ValueError("readonly producer generation mismatch")
    for key in keys - {"generation"}:
        if not isinstance(producer[key], str) or not re.fullmatch(r"[0-9A-F]{64}", producer[key]) or identities.get(key) != producer[key]:
            raise ValueError("readonly producer frozen identity mismatch")
    return dict(producer)


def validate_product_six_file_package(*, job_snapshot: dict, package_dir: Path,
                                      normalized_mesh: dict, producer: dict,
                                      expected_file_identities: dict | None) -> dict:
    """Seal final bytes once; readers must supply frozen expected identities."""
    from .contracts import canonical_sha256
    job, mesh = job_snapshot, normalized_mesh
    producer = validate_readonly_package_producer(producer, job["reproduction"]["profile"]["identities"])
    attempts = job.get("attempts")
    package_id = f"{job['job_id']}-attempt-0001"
    if not isinstance(attempts, list) or len(attempts) != 1 or attempts[0].get("attempt_id") != package_id or attempts[0].get("package_id") != package_id:
        raise ValueError("six-file package must belong to unique attempt")
    attempt = attempts[0]
    package_dir = Path(package_dir).resolve()
    runtime_root = Path(mesh["stored_path"]).resolve().parents[3]
    # The upload is normalized by the existing binding owner; derive no alternate roots.
    while runtime_root.name != "jobs" and runtime_root.parent != runtime_root:
        runtime_root = runtime_root.parent
    if runtime_root.name != "jobs":
        raise ValueError("six-file mesh is not in controlled jobs root")
    runtime_root = runtime_root.parent
    expected_dir = runtime_root / "conformal" / job["job_id"] / package_id
    if package_dir != expected_dir or Path(attempt["conformal_output_dir"]).resolve() != expected_dir:
        raise ValueError("six-file package controlled path mismatch")
    require_within(package_dir, runtime_root / "conformal", label="six-file package")
    if attempt.get("conformal_result", {}).get("package_contract") is not None:
        raise ValueError("six-file package conflicts with package_contract")
    paths = {key: require_within(package_dir / PRODUCT_FILE_MAP[key], package_dir, label="six-file artifact") for key in LEGACY_FILE_KEYS}
    actual_paths = {p.relative_to(package_dir).as_posix() for p in package_dir.rglob('*') if p.is_file()}
    required_paths = {PRODUCT_FILE_MAP[k].as_posix() for k in LEGACY_FILE_KEYS}
    if actual_paths - required_paths:
        raise ValueError("six-file package contains extra or P13 files")
    if expected_file_identities is not None and (not isinstance(expected_file_identities, dict) or set(expected_file_identities) != set(LEGACY_FILE_KEYS)):
        raise ValueError("six-file roles mismatch")
    files = {}
    for key, path in paths.items():
        if not path.is_file():
            raise ProductFileMissingError("six-file artifact is missing")
        if path.stat().st_size <= 0:
            raise ValueError("six-file artifact is empty")
        observed = {"path": PRODUCT_FILE_MAP[key].as_posix(), "size_bytes": path.stat().st_size, "sha256": _sha256(path)}
        if expected_file_identities is not None:
            record = expected_file_identities[key]
            if (not isinstance(record, dict) or set(record) != {"path", "size_bytes", "sha256"}
                or record["path"] != observed["path"] or type(record["size_bytes"]) is not int or record["size_bytes"] <= 0
                or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9A-F]{64}", record["sha256"])):
                raise ValueError("six-file identity closed shape mismatch")
            if record != observed:
                raise ProductFileIdentityError("six-file size/SHA256 mismatch")
        files[key] = observed
    metadata = _load_object(paths["metadata"], "metadata")
    if metadata.get("result_id") != package_id or metadata.get("created_by") != "cad_module.build_conformal_result_package" or metadata.get("stage") != "6.11d-conformal-service-runtime-validation":
        raise ValueError("six-file producer metadata mismatch")
    _validate_product_metadata(job, metadata, mesh)
    source = Path(mesh["stored_path"])
    require_within(source, runtime_root / "jobs" / job["job_id"], label="six-file accepted upload")
    if files["source_mesh"]["sha256"] != mesh["raw_sha256"] or files["source_mesh"]["size_bytes"] != source.stat().st_size or _sha256(source) != mesh["raw_sha256"]:
        raise ProductFileIdentityError("six-file source mesh identity mismatch")
    target, candidates, selection = job["target"], job["candidate_set"], job["selection"]
    for obj, key in ((target, "proposal_sha256"), (candidates, "candidate_set_sha256"), (selection, "selection_sha256")):
        if canonical_sha256({k: v for k, v in obj.items() if k != key}) != obj[key]:
            raise ValueError("recorded mechanical identity mismatch")
    selected = [c for c in candidates["candidates"] if c["candidate_id"] == selection["selected_candidate_id"]]
    if len(selected) != 1 or canonical_sha256(selected[0]) != selection["selected_candidate_payload_sha256"] or selection["candidate_set_sha256"] != candidates["candidate_set_sha256"]:
        raise ValueError("selected candidate identity mismatch")
    return {"schema_version": "latticegpt_readonly_package_receipt_v1", "package_format": "PRODUCT_SIX_FILES_V1",
            "job_id": job["job_id"], "package_id": package_id, "producer": producer,
            "target_sha256": target["proposal_sha256"], "selection_sha256": selection["selection_sha256"],
            "mesh_binding_sha256": mesh["binding_sha256"], "source_mesh_sha256": mesh["raw_sha256"], "files": files}


def _validate_product_metadata(job: dict, metadata: dict, mesh: dict) -> None:
    """Validate original relationships before building any normalized display copy."""
    if metadata.get("product_mesh_binding") != job["mesh_binding"]:
        raise ValueError("metadata mesh binding mismatch")
    target, selection = job["target"], job["selection"]
    context, trace = metadata.get("product_context", {}), metadata.get("traceability", {})
    if not isinstance(context, dict) or not isinstance(trace, dict) or not isinstance(context.get("target"), dict):
        raise ValueError("metadata product context is missing or invalid")
    expected = {"job_id": job["job_id"], "selected_candidate_id": selection["selected_candidate_id"], "selection_sha256": selection["selection_sha256"], "mesh_binding_sha256": mesh["binding_sha256"], "mesh_sha256": mesh["raw_sha256"]}
    if context.get("schema_version") != "latticegpt_product_viewer_context_v1" or any(context.get(k) != v for k, v in expected.items()) or context.get("target", {}).get("target_sha256") != target["proposal_sha256"]:
        raise ValueError("metadata product context mismatch")
    expected_trace = {"job_id": job["job_id"], "candidate_id": selection["selected_candidate_id"], "target_sha256": target["proposal_sha256"], "selection_manifest_sha256": selection["selection_sha256"], "case_manifest_sha256": mesh["binding_sha256"]}
    if selection.get("selected_candidate_payload_sha256") is not None:
        expected_trace["candidate_manifest_sha256"] = selection["selected_candidate_payload_sha256"]
    if any(trace.get(k) != v for k, v in expected_trace.items()):
        raise ValueError("metadata traceability mismatch")


def resolve_product_package_file(package: ProductPackage, file_key: str) -> Path:
    if file_key not in PRODUCT_FILE_MAP:
        raise ValueError("invalid product file_key")
    if package.file_identities is not None and file_key not in package.file_identities:
        raise FileNotFoundError("file role is not available in registered legacy package")
    path = require_within(
        package.package_dir / PRODUCT_FILE_MAP[file_key],
        package.package_dir,
        label="product package file",
    )
    if not path.is_file():
        raise FileNotFoundError("product package file is missing")

    expected = _expected_file_identity(package, file_key)
    if expected is not None:
        if expected.get("path") != PRODUCT_FILE_MAP[file_key].as_posix():
            raise ValueError("product package file path identity mismatch")
        expected_sha = str(expected.get("sha256") or "").upper()
        if not re.fullmatch(r"[0-9A-F]{64}", expected_sha):
            raise ValueError("product package file SHA256 identity is invalid")
        if _sha256(path) != expected_sha or ("size_bytes" in expected and path.stat().st_size != expected["size_bytes"]):
            raise ProductFileIdentityError("product package file SHA256 mismatch")
    return path


def product_package_file_url(job_id: str, file_key: str) -> str:
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("invalid job_id")
    if file_key not in PRODUCT_FILE_MAP:
        raise ValueError("invalid product file_key")
    return f"/api/viewer/product/jobs/{job_id}/files/{file_key}"


def _expected_file_identity(package: ProductPackage, file_key: str) -> dict | None:
    if package.file_identities is not None:
        return package.file_identities.get(file_key)
    if file_key == "source_mesh":
        source = package.package_metadata.get("source_mesh")
        if not isinstance(source, dict):
            raise ValueError("package source mesh identity is missing")
        return {
            "path": source.get("package_copy_path"),
            "sha256": source.get("package_copy_sha256"),
        }
    output_key = _OUTPUT_KEY.get(file_key)
    if output_key is None:
        return None
    outputs = package.package_metadata.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("package_metadata outputs are invalid")
    value = outputs.get(output_key)
    if not isinstance(value, dict):
        raise ValueError(f"package_metadata output identity is missing: {output_key}")
    return value


def _load_object(path: Path, label: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing from product package")
    try:
        from .store import _parse_viewer_json_object
        value = _parse_viewer_json_object(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


__all__ = [
    "PRODUCT_FILE_MAP",
    "ProductPackage",
    "product_package_file_url",
    "resolve_product_package",
    "resolve_product_package_file",
]


class ProductPackageContractError(ValueError):
    code = "PACKAGE_CONTRACT_INVALID"


class ProductFileMissingError(FileNotFoundError):
    code = "FILE_MISSING"


class ProductFileIdentityError(ValueError):
    code = "FILE_IDENTITY_MISMATCH"


def resolve_product_package(store, job_id, conformal_root):
    try:
        return _resolve_product_package(store, job_id, conformal_root)
    except (JobNotFoundError, JobContractError, ProductFileIdentityError, ProductFileMissingError):
        raise
    except FileNotFoundError as exc:
        raise ProductFileMissingError(str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ProductPackageContractError(str(exc)) from exc
