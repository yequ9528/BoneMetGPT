from __future__ import annotations

import json
import os
import re
import threading
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .manifest_io import (
    canonical_json_object_v1, parse_closed_json_object_v1, sha256_bytes,
    validate_relative_path,
)


JOB_SCHEMA_VERSION = "latticegpt_design_job_v1"
R3_JOB_SCHEMA_VERSION = "latticegpt_design_job_v2"
R3_JOB_SCHEMA_VERSION_V3 = "latticegpt_design_job_v3"
JOB_ID_PATTERN = re.compile(r"^job-[0-9A-F]{24}$")
_JOB_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(
        self, runtime_root: str | Path, *,
        viewer_compatibility: Mapping[str, Any] | None = None,
        reader_identity: Mapping[str, Any] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.jobs_root = self.runtime_root / "jobs"
        self.viewer_compatibility = deepcopy(dict(viewer_compatibility or {}))
        self.reader_identity = deepcopy(dict(reader_identity or {}))

    def create(
        self,
        target: Mapping[str, Any],
        *,
        model_runtime_profile: Mapping[str, Any],
        formal_bundle_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        job_id = f"job-{uuid.uuid4().hex[:24].upper()}"
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(exist_ok=False)
        now = utc_now()
        job = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": job_id,
            "revision": 0,
            "status": "TARGET_PROPOSED",
            "workflow_disposition": "ACTIVE",
            "created_at": now,
            "updated_at": now,
            "target": dict(target),
            "model_runtime_profile": dict(model_runtime_profile),
            "formal_bundle_binding": dict(formal_bundle_binding),
            "runtime_authorizations": [],
            "candidate_set": None,
            "selection": None,
            "mesh_binding": None,
            "mesh_uploads": [],
            "attempts": [],
            "failure": None,
        }
        self._write_snapshot(job_dir, job, exclusive=True)
        self._append_event(job_dir, "job_created", job)
        return job

    def create_r3(
        self,
        target: Mapping[str, Any],
        *,
        model_runtime_profile: Mapping[str, Any],
        formal_bundle_binding: Mapping[str, Any],
        reproduction_profile: Mapping[str, Any] | None = None,
        comparison_contract: Mapping[str, Any] | None = None,
        comparison_contract_sha256: str | None = None,
        execution_context: Mapping[str, Any] | None = None,
        proposed_job_id: str | None = None,
    ) -> dict[str, Any]:
        if execution_context is not None:
            return self._create_r3_v3(
                target,
                model_runtime_profile=model_runtime_profile,
                formal_bundle_binding=formal_bundle_binding,
                execution_context=execution_context,
                proposed_job_id=proposed_job_id,
            )
        if proposed_job_id is not None:
            raise ValueError("proposed_job_id is supported only by R3 Job v3")
        from .reproducibility import validate_reproduction_profile_v1

        if reproduction_profile is None or comparison_contract is None or comparison_contract_sha256 is None:
            raise ValueError("historical R3 v2 creation requires profile and comparison contract")
        profile = validate_reproduction_profile_v1(reproduction_profile)
        expected_comparison_sha = sha256_bytes(
            canonical_json_object_v1(dict(comparison_contract))
        )
        if comparison_contract_sha256 != expected_comparison_sha:
            raise ValueError("comparison contract SHA256 mismatch")
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        job_id = f"job-{uuid.uuid4().hex[:24].upper()}"
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(exist_ok=False)
        now = utc_now()
        job = {
            "schema_version": R3_JOB_SCHEMA_VERSION,
            "generation": {
                "schema_version": "latticegpt_job_generation_v1",
                "stage": "R3",
                "contract": "R3_DETERMINISM_MESH_BINDING_V1",
            },
            "job_id": job_id,
            "revision": 0,
            "status": "TARGET_PROPOSED",
            "workflow_disposition": "ACTIVE",
            "created_at": now,
            "updated_at": now,
            "target": dict(target),
            "model_runtime_profile": dict(model_runtime_profile),
            "formal_bundle_binding": dict(formal_bundle_binding),
            "runtime_authorizations": [],
            "candidate_set": None,
            "selection": None,
            "mesh_binding": None,
            "mesh_uploads": [],
            "conformal_reservation": None,
            "attempts": [],
            "failure": None,
            "reproduction": {
                "profile": profile,
                "profile_sha256": profile["profile_sha256"],
                "status": "FROZEN_R3",
                "comparison_contract": dict(comparison_contract),
                "comparison_contract_sha256": comparison_contract_sha256,
            },
        }
        from .job_state import validate_r3_job_snapshot_v2

        job = validate_r3_job_snapshot_v2(job)
        self._write_snapshot(job_dir, job, exclusive=True)
        self._append_event(job_dir, "job_created", job)
        return job

    @staticmethod
    def propose_r3_job_id() -> str:
        return f"job-{uuid.uuid4().hex[:24].upper()}"

    def _create_r3_v3(
        self,
        target: Mapping[str, Any],
        *,
        model_runtime_profile: Mapping[str, Any],
        formal_bundle_binding: Mapping[str, Any],
        execution_context: Mapping[str, Any],
        proposed_job_id: str | None = None,
    ) -> dict[str, Any]:
        from .job_state import validate_r3_job_snapshot_v3
        from .r3_execution_context import validate_r3_execution_context_v1

        context = validate_r3_execution_context_v1(execution_context)
        job_id = self.propose_r3_job_id() if proposed_job_id is None else proposed_job_id
        if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError("proposed R3 Job ID is invalid")
        now = utc_now()
        job = {
            "schema_version": R3_JOB_SCHEMA_VERSION_V3,
            "generation": {
                "schema_version": "latticegpt_job_generation_v1",
                "stage": "R3",
                "contract": "R3_INTEGRATION_RSERIES_FOUNDATION_V1",
            },
            "job_id": job_id,
            "revision": 0,
            "status": "TARGET_PROPOSED",
            "workflow_disposition": "ACTIVE",
            "created_at": now,
            "updated_at": now,
            "target": dict(target),
            "model_runtime_profile": dict(model_runtime_profile),
            "formal_bundle_binding": dict(formal_bundle_binding),
            "runtime_authorizations": [],
            "candidate_set": None,
            "selection": None,
            "mesh_binding": None,
            "mesh_uploads": [],
            "conformal_reservation": None,
            "attempts": [],
            "failure": None,
            "reproduction": {
                "status": "FROZEN_R3_EXECUTION_CONTEXT_V1",
                "execution_context": context,
                "execution_context_sha256": context["execution_context_sha256"],
            },
        }
        job = validate_r3_job_snapshot_v3(job)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(exist_ok=False)
        self._write_snapshot(job_dir, job, exclusive=True)
        self._append_event(job_dir, "job_created", job)
        return job

    def reserve_conformal_identity(
        self,
        job_id: str,
        *,
        expected_revision: int,
        upload_id: str,
        candidate_id: str,
        reserved_at: str,
    ) -> dict[str, Any]:
        from .contracts import build_conformal_reservation_v1

        with self.mutation(job_id):
            job = self.get(job_id)
            if job.get("schema_version") not in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}:
                raise ValueError("conformal reservation requires an R3 Job")
            if job["revision"] != expected_revision:
                raise ValueError("stale revision")
            if job.get("conformal_reservation") is not None or job.get("attempts"):
                raise ValueError("RESERVATION_COLLISION")
            job["conformal_reservation"] = build_conformal_reservation_v1(
                job_id=job_id,
                upload_id=upload_id,
                candidate_id=candidate_id,
                reserved_at=reserved_at,
            )
            return self._replace_locked(
                job,
                expected_revision=expected_revision,
                event="conformal_identity_reserved",
            )

    def get(self, job_id: str) -> dict[str, Any]:
        job_dir = self._job_dir(job_id)
        path = job_dir / "job.json"
        if not path.is_file():
            raise FileNotFoundError(f"job does not exist: {job_id}")
        raw = path.read_bytes()
        value = _parse_viewer_json_object(raw)
        schema = value.get("schema_version")
        if schema in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}:
            if canonical_json_object_v1(value) != raw:
                raise ValueError("R3 Job bytes are not canonical")
        if value.get("job_id") != job_id or value.get("schema_version") not in {JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}:
            raise ValueError("job snapshot identity mismatch")
        return value

    def read_events(self, job_id: str) -> list[dict[str, Any]]:
        job_dir = self._job_dir(job_id)
        path = job_dir / "events.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"job event evidence does not exist: {job_id}")
        events: list[dict[str, Any]] = []
        r3 = self.get(job_id)["schema_version"] in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.strip():
                raise ValueError("job event evidence contains a blank record")
            value = _parse_viewer_json_object(line)
            if r3 and canonical_json_object_v1(value) != line:
                raise ValueError("R3 event bytes are not canonical")
            events.append(value)
        return events

    def get_consistent(self, job_id: str) -> dict[str, Any]:
        from .job_state import validate_job_snapshot_by_schema
        try:
            return validate_job_snapshot_by_schema(self.get(job_id), self.read_events(job_id))
        except FileNotFoundError as exc:
            raise JobNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise JobContractError(str(exc)) from exc

    def get_consistent_for_viewer(self, job_id: str) -> dict[str, Any]:
        try:
            return self._get_consistent_for_viewer(job_id)
        except FileNotFoundError as exc:
            raise JobNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise JobContractError(str(exc)) from exc

    def _get_consistent_for_viewer(self, job_id: str) -> dict[str, Any]:
        """Read original evidence through the separately bound Viewer-only contract."""
        from .job_state import validate_readonly_product_job_snapshot

        job_dir = self._job_dir(job_id)
        registry = self.viewer_compatibility
        if registry and (
            set(registry) != {"schema_version", "jobs"}
            or registry.get("schema_version") != "latticegpt_viewer_readonly_compatibility_v1"
            or not isinstance(registry.get("jobs"), Mapping)
        ):
            raise ValueError("invalid Viewer compatibility registry")
        entry = registry.get("jobs", {}).get(job_id)
        if entry is not None and not isinstance(entry, Mapping):
            raise ValueError("invalid Viewer compatibility entry")
        raw_objects = {}
        for role, filename in (("job", "job.json"), ("events", "events.jsonl")):
            path = (job_dir / filename).resolve()
            if path.parent != job_dir:
                raise ValueError("Viewer evidence path escapes Job directory")
            missing_message = (f"job does not exist: {job_id}" if role == "job"
                               else f"job event evidence does not exist: {job_id}")
            if not path.is_file():
                raise FileNotFoundError(missing_message)
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                raise FileNotFoundError(missing_message) from None
            if entry is not None:
                identity = entry.get(role)
                if not isinstance(identity, Mapping) or set(identity) != {"path", "sha256", "size_bytes"}:
                    raise ValueError("invalid Viewer evidence identity")
                relative = validate_relative_path(identity["path"], label="Viewer evidence path")
                if relative != f"jobs/{job_id}/{filename}":
                    raise ValueError("Viewer evidence relationship mismatch")
                if (
                    isinstance(identity["size_bytes"], bool)
                    or not isinstance(identity["size_bytes"], int)
                    or len(raw) != identity["size_bytes"]
                    or sha256_bytes(raw) != identity["sha256"]
                ):
                    raise ValueError("Viewer evidence byte identity mismatch")
            raw_objects[role] = raw
        value = _parse_viewer_json_object(raw_objects["job"])
        schema = value.get("schema_version")
        if value.get("job_id") != job_id:
            raise ValueError("Viewer Job identity mismatch")
        r3 = schema in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}
        if r3 and canonical_json_object_v1(value) != raw_objects["job"]:
            raise ValueError("Viewer R3 Job bytes are not canonical")
        events = []
        for line in raw_objects["events"].splitlines(keepends=True):
            if not line.strip():
                raise ValueError("Viewer event evidence contains a blank record")
            event = _parse_viewer_json_object(line)
            if r3 and canonical_json_object_v1(event) != line:
                raise ValueError("Viewer R3 event bytes are not canonical")
            if isinstance(event.get("revision"), bool) or not isinstance(event.get("revision"), int):
                raise ValueError("Viewer event revision is invalid")
            events.append(event)
        validate_readonly_product_job_snapshot(value, events, compatibility_entry=entry)
        return value

    def job_directory(self, job_id: str) -> Path:
        path = self._job_dir(job_id)
        if not path.is_dir():
            raise FileNotFoundError(f"job does not exist: {job_id}")
        return path

    def list_job_ids(self) -> list[str]:
        if not self.jobs_root.is_dir():
            return []
        return sorted(
            path.name
            for path in self.jobs_root.iterdir()
            if path.is_dir() and JOB_ID_PATTERN.fullmatch(path.name)
        )

    @contextmanager
    def mutation(self, job_id: str):
        self._job_dir(job_id)
        key = (str(self.jobs_root), job_id)
        with _JOB_LOCKS_GUARD:
            lock = _JOB_LOCKS.setdefault(key, threading.RLock())
        with lock:
            yield

    def replace(
        self,
        job: Mapping[str, Any],
        *,
        expected_revision: int,
        event: str,
    ) -> dict[str, Any]:
        job_id = str(job.get("job_id") or "")
        with self.mutation(job_id):
            return self._replace_locked(
                job,
                expected_revision=expected_revision,
                event=event,
            )

    def _replace_locked(
        self,
        job: Mapping[str, Any],
        *,
        expected_revision: int,
        event: str,
    ) -> dict[str, Any]:
        job_id = str(job.get("job_id") or "")
        current = self.get(job_id)
        if current["revision"] != expected_revision:
            raise ValueError(
                f"stale revision: expected {expected_revision}, current {current['revision']}"
            )
        replacement = dict(job)
        replacement["schema_version"] = current["schema_version"]
        replacement["revision"] = expected_revision + 1
        replacement["created_at"] = current["created_at"]
        replacement["updated_at"] = utc_now()
        job_dir = self._job_dir(job_id)
        self._write_snapshot(job_dir, replacement, exclusive=False)
        self._append_event(job_dir, event, replacement)
        return replacement

    def mark_runtime_interrupted(self, job_id: str) -> dict[str, Any]:
        from .job_state import RUNTIME_STATUSES, INVERSE_INTERRUPTION_STAGES

        with self.mutation(job_id):
            job = self.get_consistent(job_id)
            interrupted_stage = job["status"]
            if interrupted_stage in INVERSE_INTERRUPTION_STAGES:
                job["status"] = "FAILED"
            elif interrupted_stage in RUNTIME_STATUSES:
                if len(job.get("attempts") or []) != 1:
                    raise ValueError("running Job is missing its unique attempt")
                job["attempts"][0]["status"] = "HOLD_RUNTIME_INTERRUPTED"
                job["attempts"][0]["error"] = "runtime process stopped before a terminal result"
                job["status"] = "HOLD_RUNTIME_INTERRUPTED"
            else:
                return job
            job["workflow_disposition"] = "HOLD"
            job["failure"] = {
                "stage": interrupted_stage,
                "code": "RUNTIME_PROCESS_INTERRUPTED",
                "message": "runtime process stopped before a terminal result",
            }
            return self._replace_locked(
                job,
                expected_revision=job["revision"],
                event="runtime_process_interrupted",
            )

    def _job_dir(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(str(job_id)):
            raise ValueError("invalid job_id")
        path = (self.jobs_root / job_id).resolve()
        if path.parent != self.jobs_root:
            raise ValueError("job path escapes runtime root")
        return path

    @staticmethod
    def _write_snapshot(job_dir: Path, job: Mapping[str, Any], *, exclusive: bool) -> None:
        path = job_dir / "job.json"
        temporary = job_dir / f".job.{uuid.uuid4().hex}.tmp"
        try:
            if job.get("schema_version") in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3}:
                with temporary.open("xb") as stream:
                    stream.write(canonical_json_object_v1(dict(job)))
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(job, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            if exclusive and path.exists():
                raise FileExistsError(f"job snapshot already exists: {path}")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _append_event(job_dir: Path, event: str, job: Mapping[str, Any]) -> None:
        record = {
            "event_id": uuid.uuid4().hex.upper(),
            "recorded_at": utc_now(),
            "event": event,
            "job_id": job["job_id"],
            "revision": job["revision"],
            "status": job["status"],
        }
        with (job_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":") if job.get("schema_version") in {R3_JOB_SCHEMA_VERSION, R3_JOB_SCHEMA_VERSION_V3} else None, allow_nan=False))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


def _parse_viewer_json_object(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Viewer evidence contains a duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(token):
        raise ValueError("Viewer evidence contains a non-finite JSON number")

    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("Viewer evidence UTF-8 BOM is forbidden")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=unique_object, parse_constant=reject_constant)
        canonical_json_object_v1(value)  # Also rejects overflow, surrogates and non-object roots.
    except (TypeError, UnicodeError) as exc:
        raise ValueError("Viewer evidence must be a valid JSON object") from exc
    return value


class JobNotFoundError(FileNotFoundError):
    code = "JOB_NOT_FOUND"


class JobContractError(ValueError):
    code = "JOB_CONTRACT_INVALID"
