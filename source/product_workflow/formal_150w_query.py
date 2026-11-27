from __future__ import annotations

import csv
import hashlib
import json
import math
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import STIFFNESS_NAMES, require_sha256, canonical_sha256


PROJECTION_SCHEMA_VERSION = "latticegpt_formal_150w_query_projection_v1"
PROJECTION_GENERATOR = "product_workflow.formal_150w_query.build_projection"
QUERY_FIELDS = frozenset({"sample_id", "relative_density", "diameter", *STIFFNESS_NAMES})
FILTER_OPERATORS = {
    "eq": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=",
}
PROJECTION_COLUMNS = ("sample_id", "relative_density", "diameter", *STIFFNESS_NAMES)
PROJECTION_MANIFEST_FIELDS = {
    "schema_version",
    "created_at",
    "generator",
    "source",
    "projection",
    "eligibility_policy",
    "query_contract",
    "overwrite",
}

DATABASE_REQUEST_SCHEMA = "latticegpt_database_read_request_v1"
DATABASE_RESULT_SCHEMA = "latticegpt_database_read_result_v1"
SQL_TIME_BUDGET_SECONDS = 5.0
SQL_INSTRUCTION_BUDGET = 50_000_000
SQL_PROGRESS_INTERVAL = 1_000
_SQL_SLOT = threading.BoundedSemaphore(1)
logger = logging.getLogger(__name__)
_QUERY_ERRORS = {
    "INVALID_QUERY": ("查询条件无效，请按支持的字段和单位修改。", True, "CHANGE_REQUEST"),
    "UNSUPPORTED_QUERY_KIND": ("当前不支持这类统计，请选择概览、记录或计数。", True, "CHANGE_REQUEST"),
    "QUERY_BUSY": ("当前有查询正在执行，请稍后手动查询。", True, "AFTER_IDLE"),
    "DATABASE_BUSY": ("数据库暂时忙碌，请稍后手动查询。", True, "AFTER_IDLE"),
    "QUERY_LIMIT_EXCEEDED": ("查询超过资源限制，未返回部分结果；请缩小条件后手动查询。", True, "NARROW_REQUEST"),
    "BINDING_MISMATCH": ("数据来源身份不匹配，已停止查询，请核对发布配置。", False, "REPAIR_BINDING"),
    "DATABASE_UNAVAILABLE": ("绑定的数据库或清单无法读取，请核对文件。", False, "RESTORE_SOURCE"),
    "QUERY_EXECUTION_FAILED": ("查询未完成，请保留诊断编号供核对。", False, "INSPECT_DIAGNOSTIC"),
}


class ReadonlyQueryError(ValueError):
    def __init__(self, code: str, *, limit_reason: str | None = None):
        message, again, condition = _QUERY_ERRORS[code]
        super().__init__(message)
        self.code = code
        self.error = {"code": code, "message_zh": message, "can_query_again": again,
                      "requery_condition": condition, "automatic_retry": False,
                      "diagnostic_id": uuid.uuid4().hex, "limit_reason": limit_reason}


def _unique_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def validate_database_request(request: Mapping[str, Any] | str) -> dict[str, Any]:
    try:
        if isinstance(request, str):
            request = json.loads(request, object_pairs_hook=_unique_json_pairs)
        if not isinstance(request, Mapping) or request.get("schema_version") != DATABASE_REQUEST_SCHEMA:
            raise ValueError("request schema")
        kind = request.get("kind")
        if kind not in ("overview", "records", "count"):
            raise ReadonlyQueryError("UNSUPPORTED_QUERY_KIND")
        fields = {"schema_version", "kind"} | ({"filters", "order_by", "limit"} if kind == "records" else {"filters"} if kind == "count" else set())
        if set(request) != fields:
            raise ValueError("request fields")
        if kind == "overview":
            return dict(request)
        filters = request["filters"]
        if not isinstance(filters, list) or len(filters) > 8:
            raise ValueError("filters")
        for item in filters:
            if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
                raise ValueError("filter fields")
            if item["field"] not in QUERY_FIELDS or item["op"] not in {*FILTER_OPERATORS, "between"}:
                raise ValueError("filter enums")
            values = item["value"] if item["op"] == "between" else [item["value"]]
            if not isinstance(values, list) or len(values) != (2 if item["op"] == "between" else 1):
                raise ValueError("filter values")
            for value in values:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("numeric value")
                if item["field"] == "sample_id" and type(value) is not int:
                    raise ValueError("sample_id integer")
            if len(values) == 2 and values[0] > values[1]:
                raise ValueError("reversed interval")
        orders = request.get("order_by", [])
        if not isinstance(orders, list) or len(orders) > 3:
            raise ValueError("orders")
        for item in orders:
            if not isinstance(item, Mapping) or set(item) != {"field", "direction"} or item["field"] not in QUERY_FIELDS or item["direction"] not in ("asc", "desc"):
                raise ValueError("order fields")
        if len({item["field"] for item in orders}) != len(orders):
            raise ValueError("duplicate order field")
        limit = request.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("limit")
        normalized = validate_query_plan({"filters": filters, "order_by": orders, "limit": limit})
        for item in normalized["filters"]:
            if item["field"] == "sample_id":
                item["value"] = [int(v) for v in item["value"]] if isinstance(item["value"], list) else int(item["value"])
        return {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": kind, "filters": normalized["filters"], **({"order_by": normalized["order_by"], "limit": limit} if kind == "records" else {})}
    except ReadonlyQueryError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ReadonlyQueryError("INVALID_QUERY") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_query_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise ValueError("query plan must be a mapping")
    if "sql" in plan:
        raise ValueError("raw SQL is not accepted")
    unexpected = set(plan) - {"filters", "order_by", "limit"}
    if unexpected:
        raise ValueError(f"unsupported query plan fields: {sorted(unexpected)}")

    filters: list[dict[str, Any]] = []
    for item in plan.get("filters") or []:
        if not isinstance(item, Mapping):
            raise ValueError("query filters must be mappings")
        field = str(item.get("field") or "")
        if field not in QUERY_FIELDS:
            raise ValueError(f"unsupported query field: {field}")
        operator = str(item.get("op") or "").lower()
        if operator not in {*FILTER_OPERATORS, "between"}:
            raise ValueError(f"unsupported query operator: {operator}")
        if operator == "between":
            raw = item.get("value")
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise ValueError("between requires two values")
            value: Any = [_finite(raw[0]), _finite(raw[1])]
        else:
            value = _finite(item.get("value"))
        filters.append({"field": field, "op": operator, "value": value})

    order_by: list[dict[str, str]] = []
    for item in plan.get("order_by") or []:
        if not isinstance(item, Mapping):
            raise ValueError("order_by entries must be mappings")
        field = str(item.get("field") or "")
        if field not in QUERY_FIELDS:
            raise ValueError(f"unsupported query field: {field}")
        direction = str(item.get("direction") or "asc").lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("order direction must be asc or desc")
        order_by.append({"field": field, "direction": direction})
    sample_positions = [
        index for index, item in enumerate(order_by) if item["field"] == "sample_id"
    ]
    if len(sample_positions) > 1:
        raise ValueError("sample_id order field must be unique")
    if sample_positions and sample_positions[0] != len(order_by) - 1:
        raise ValueError("sample_id order field must be final")
    if not sample_positions:
        order_by.append({"field": "sample_id", "direction": "asc"})
    if len(order_by) > 3:
        raise ValueError("at most three order fields are supported")

    limit = int(plan.get("limit", 5))
    if isinstance(plan.get("limit"), bool) or limit < 1 or limit > 5:
        raise ValueError("limit must be between 1 and 5")
    return {"filters": filters, "order_by": order_by, "limit": limit}


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("query values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("query values must be finite numbers")
    return number


def build_projection(
    source_csv: str | Path,
    database_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_source_sha256: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    if overwrite is not False:
        raise ValueError("overwrite must be false")
    source = Path(source_csv).resolve()
    database = Path(database_path).resolve()
    manifest = Path(manifest_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source CSV does not exist: {source}")
    if database.exists() or manifest.exists():
        raise FileExistsError("projection database or manifest already exists")
    expected_sha = require_sha256(expected_source_sha256, "expected_source_sha256")
    actual_sha = _sha256_file(source)
    if actual_sha != expected_sha:
        raise ValueError("source CSV SHA256 mismatch")

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    scanned = 0
    eligible = 0
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE targets ("
            "sample_id INTEGER PRIMARY KEY, relative_density REAL NOT NULL, diameter REAL NOT NULL, "
            + ", ".join(f"{name} REAL NOT NULL" for name in STIFFNESS_NAMES)
            + ")"
        )
        insert_sql = (
            f"INSERT INTO targets ({', '.join(PROJECTION_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in PROJECTION_COLUMNS)})"
        )
        batch: list[tuple[Any, ...]] = []
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {
                *PROJECTION_COLUMNS, "is_singular", "is_pd_full",
                "has_periodic_mismatch", "is_degenerate",
            }
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"source CSV missing required columns: {sorted(missing)}")
            for row in reader:
                scanned += 1
                if not _eligible(row):
                    continue
                batch.append((
                    int(row["sample_id"]),
                    _finite(row["relative_density"]),
                    _finite(row["diameter"]),
                    *(_positive_finite(row[name]) for name in STIFFNESS_NAMES),
                ))
                eligible += 1
                if len(batch) >= 5000:
                    connection.executemany(insert_sql, batch)
                    connection.commit()
                    batch.clear()
        if batch:
            connection.executemany(insert_sql, batch)
        connection.execute("CREATE INDEX idx_targets_relative_density ON targets(relative_density)")
        connection.commit()
    except Exception:
        connection.close()
        if database.exists():
            database.unlink()
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass

    projection_sha = _sha256_file(database)
    record = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": {
            "identity": PROJECTION_GENERATOR,
            "policy_version": PROJECTION_SCHEMA_VERSION,
        },
        "source": {
            "path": str(source),
            "size_bytes": source.stat().st_size,
            "sha256": actual_sha,
        },
        "projection": {
            "path": str(database),
            "sha256": projection_sha,
            "scanned_rows": scanned,
            "eligible_rows": eligible,
            "columns": list(PROJECTION_COLUMNS),
        },
        "eligibility_policy": {
            "is_singular": 0,
            "is_pd_full": 1,
            "has_periodic_mismatch": 0,
            "is_degenerate": 0,
            "selected_9d": "finite_positive",
        },
        "query_contract": {
            "allowed_fields": sorted(QUERY_FIELDS),
            "allowed_operators": [*FILTER_OPERATORS, "between"],
            "maximum_results": 5,
            "raw_sql_allowed": False,
        },
        "overwrite": False,
    }
    _atomic_json(manifest, record)
    return {"status": "complete", "scanned_rows": scanned, "eligible_rows": eligible, **record}


def _eligible(row: Mapping[str, str]) -> bool:
    try:
        if _flag(row["is_singular"]):
            return False
        if not _flag(row["is_pd_full"]):
            return False
        if _flag(row["has_periodic_mismatch"]):
            return False
        if _flag(row["is_degenerate"]):
            return False
        for name in STIFFNESS_NAMES:
            _positive_finite(row[name])
        _finite(row["relative_density"])
        _finite(row["diameter"])
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError("quality flag must be boolean or 0/1")


def _positive_finite(value: Any) -> float:
    number = _finite(value)
    if number <= 0:
        raise ValueError("value must be positive")
    return number


def verify_projection_manifest(
    manifest_path: str | Path,
    *,
    expected_source_sha256: str,
    expected_projection_sha256: str,
) -> Mapping[str, Any]:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError("formal 150w projection manifest is required")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("projection manifest is not valid JSON") from exc
    if not isinstance(metadata, dict) or set(metadata) != PROJECTION_MANIFEST_FIELDS:
        raise ValueError("projection manifest fields are invalid")
    if metadata.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise ValueError("projection manifest schema mismatch")
    if metadata.get("generator") != {
        "identity": PROJECTION_GENERATOR,
        "policy_version": PROJECTION_SCHEMA_VERSION,
    }:
        raise ValueError("projection manifest generator policy mismatch")
    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ValueError("projection manifest source record is invalid")
    source_sha = require_sha256(source.get("sha256"), "manifest source SHA256")
    if source_sha != require_sha256(expected_source_sha256, "expected_source_sha256"):
        raise ValueError("projection manifest source SHA256 mismatch")
    projection = metadata.get("projection")
    if not isinstance(projection, dict):
        raise ValueError("projection manifest projection record is invalid")
    projection_sha = require_sha256(projection.get("sha256"), "projection SHA256")
    if projection_sha != require_sha256(
        expected_projection_sha256, "expected_projection_sha256"
    ):
        raise ValueError("projection manifest projection SHA256 mismatch")
    if projection.get("columns") != list(PROJECTION_COLUMNS):
        raise ValueError("projection manifest columns mismatch")
    scanned = projection.get("scanned_rows")
    eligible = projection.get("eligible_rows")
    if (
        isinstance(scanned, bool)
        or not isinstance(scanned, int)
        or scanned < 0
        or isinstance(eligible, bool)
        or not isinstance(eligible, int)
        or eligible < 0
        or eligible > scanned
    ):
        raise ValueError("projection manifest row counts are invalid")
    if metadata.get("eligibility_policy") != {
        "is_singular": 0,
        "is_pd_full": 1,
        "has_periodic_mismatch": 0,
        "is_degenerate": 0,
        "selected_9d": "finite_positive",
    }:
        raise ValueError("projection manifest eligibility policy mismatch")
    if metadata.get("query_contract") != {
        "allowed_fields": sorted(QUERY_FIELDS),
        "allowed_operators": [*FILTER_OPERATORS, "between"],
        "maximum_results": 5,
        "raw_sql_allowed": False,
    }:
        raise ValueError("projection manifest query contract mismatch")
    if metadata.get("overwrite") is not False:
        raise ValueError("projection manifest overwrite contract mismatch")
    return metadata


class Formal150wQueryService:
    def __init__(self, database_path: str | Path, manifest_path: str | Path, *,
                 expected_source_sha256: str, expected_manifest_sha256: str | None = None,
                 expected_projection_sha256: str | None = None) -> None:
        self.database_path = Path(database_path).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.source_sha256 = require_sha256(expected_source_sha256, "expected_source_sha256")
        self.manifest_sha256 = expected_manifest_sha256
        self.projection_sha256 = expected_projection_sha256
        self._verified_state = None
        self._metadata = None
        self._check_binding()

    def _file_state(self):
        return tuple((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_ino)
                     for p in (self.manifest_path, self.database_path))

    def _check_binding(self):
        try:
            state = self._file_state()
            if self._verified_state == state and self._metadata is not None:
                return state
            self._metadata = None
            manifest_sha = _sha256_file(self.manifest_path)
            if self.manifest_sha256 is not None and manifest_sha != self.manifest_sha256.upper():
                raise ValueError("manifest SHA256 mismatch")
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_pairs)
            projection_sha = self.projection_sha256 or require_sha256(raw["projection"]["sha256"], "projection SHA256")
            metadata = verify_projection_manifest(self.manifest_path, expected_source_sha256=self.source_sha256,
                                                  expected_projection_sha256=projection_sha)
            if _sha256_file(self.database_path) != projection_sha.upper() or self._file_state() != state:
                raise ValueError("projection SHA256 or file state mismatch")
            self.manifest_sha256 = manifest_sha
            self.projection_sha256 = projection_sha.upper()
            self._metadata = metadata
            self._verified_state = state
            return state
        except OSError as exc:
            self._metadata = None
            self._raise_logged("DATABASE_UNAVAILABLE", "binding", exc)
        except (ValueError, KeyError, TypeError) as exc:
            self._metadata = None
            self._raise_logged("BINDING_MISMATCH", "binding", exc)

    @staticmethod
    def _raise_logged(code, stage, cause, *, limit_reason=None):
        error = ReadonlyQueryError(code, limit_reason=limit_reason)
        logger.error("readonly_query diagnostic_id=%s stage=%s code=%s", error.error["diagnostic_id"], stage, code,
                     extra={"product_event": {"event": "readonly_query_failed", "code": code,
                                              "diagnostic_id": error.error["diagnostic_id"]}})
        raise error from cause

    def _unchanged(self, state):
        try:
            if self._file_state() == state:
                return
        except OSError as exc:
            self._metadata = None
            self._raise_logged("DATABASE_UNAVAILABLE", "post_read", exc)
        self._metadata = None
        raise ReadonlyQueryError("BINDING_MISMATCH")

    def _execute_read(self, sql, parameters):
        # This is the only SQL execution path, including historical v1 plans.
        if not _SQL_SLOT.acquire(blocking=False):
            raise ReadonlyQueryError("QUERY_BUSY")
        connection = None
        reason = None
        stage = "binding"
        try:
            state = self._check_binding()
            stage = "connect"
            connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + SQL_TIME_BUDGET_SECONDS
            instructions = 0

            def progress():
                nonlocal instructions, reason
                instructions += SQL_PROGRESS_INTERVAL
                if time.monotonic() >= deadline:
                    reason = "TIME_BUDGET"
                elif instructions >= SQL_INSTRUCTION_BUDGET:
                    reason = "INSTRUCTION_BUDGET"
                return int(reason is not None)

            connection.set_progress_handler(progress, SQL_PROGRESS_INTERVAL)
            stage = "execute_fetch"
            rows = [dict(row) for row in connection.execute(sql, parameters).fetchall()]
            if time.monotonic() >= deadline:
                reason = "TIME_BUDGET"
                raise ReadonlyQueryError("QUERY_LIMIT_EXCEEDED", limit_reason=reason)
            self._unchanged(state)
            return rows
        except ReadonlyQueryError:
            raise
        except sqlite3.Error as exc:
            if reason:
                self._raise_logged("QUERY_LIMIT_EXCEEDED", stage, exc, limit_reason=reason)
            code = getattr(exc, "sqlite_errorcode", 0) or 0
            # Primary SQLite codes are stable; Python 3.10 does not export these names.
            if (code & 255) in (5, 6) or "locked" in str(exc).lower():
                self._raise_logged("DATABASE_BUSY", stage, exc)
            if stage == "connect":
                self._raise_logged("DATABASE_UNAVAILABLE", stage, exc)
            self._raise_logged("QUERY_EXECUTION_FAILED", stage, exc)
        except OSError as exc:
            self._raise_logged("DATABASE_UNAVAILABLE", stage, exc)
        except Exception as exc:
            self._raise_logged("QUERY_EXECUTION_FAILED", stage, exc)
        finally:
            try:
                if connection is not None:
                    try:
                        connection.set_progress_handler(None, 0)
                    finally:
                        connection.close()
            finally:
                _SQL_SLOT.release()

    @staticmethod
    def _where(filters):
        clauses, parameters = [], []
        for item in filters:
            if item["op"] == "between":
                clauses.append(f"{item['field']} BETWEEN ? AND ?")
                parameters.extend(item["value"])
            else:
                clauses.append(f"{item['field']} {FILTER_OPERATORS[item['op']]} ?")
                parameters.append(item["value"])
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def query(self, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            normalized = validate_query_plan(plan)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ReadonlyQueryError("INVALID_QUERY") from exc
        where, parameters = self._where(normalized["filters"])
        sql = f"SELECT {', '.join(PROJECTION_COLUMNS)} FROM targets" + where
        sql += " ORDER BY " + ", ".join(f"{item['field']} {item['direction'].upper()}" for item in normalized["order_by"])
        return self._execute_read(sql + " LIMIT ?", [*parameters, normalized["limit"]])

    def count(self, filters) -> int:
        normalized = validate_database_request({"schema_version": DATABASE_REQUEST_SCHEMA, "kind": "count", "filters": filters})
        where, parameters = self._where(normalized["filters"])
        return self._execute_read("SELECT COUNT(*) AS matched_count FROM targets" + where, parameters)[0]["matched_count"]

    def overview(self) -> dict[str, Any]:
        state = self._check_binding()
        metadata = self._metadata
        value = {"purpose": "正式数据只读查询与目标选择；不代表工程验证通过。",
                 "fields": [{"name": name, "unit": "dimensionless_C_over_Eb" if name in STIFFNESS_NAMES else "dimensionless" if name in ("diameter", "relative_density") else "integer_identifier"} for name in PROJECTION_COLUMNS],
                 "scanned_rows": metadata["projection"]["scanned_rows"],
                 "eligible_rows": metadata["projection"]["eligible_rows"],
                 "statistics_time": metadata["created_at"], "eligibility_policy": metadata["eligibility_policy"]}
        self._unchanged(state)
        return value

    def execute_request(self, request) -> dict[str, Any]:
        normalized = None
        kind = request.get("kind") if isinstance(request, Mapping) and request.get("kind") in ("overview", "records", "count") else None
        data, error, status = None, None, "ok"
        try:
            normalized = validate_database_request(request)
            kind = normalized["kind"]
            if kind == "overview":
                data = self.overview()
            elif kind == "records":
                rows = self.query({k: v for k, v in normalized.items() if k not in ("schema_version", "kind")})
                data = {"rows": rows, "returned_count": len(rows)}
                status = "ok" if rows else "empty"
            else:
                where, parameters = self._where(normalized["filters"])
                data = {"matched_count": self._execute_read("SELECT COUNT(*) AS matched_count FROM targets" + where, parameters)[0]["matched_count"]}
        except ReadonlyQueryError as exc:
            status, error = "error", exc.error
        metadata = self._metadata
        provenance = {"manifest_sha256": self.manifest_sha256 if metadata else None,
                      "projection_sha256": self.projection_sha256 if metadata else None,
                      "source_sha256": self.source_sha256 if metadata else None,
                      "query_identity": canonical_sha256(normalized) if normalized else None,
                      "contract_version": DATABASE_REQUEST_SCHEMA,
                      "read_at": datetime.now(timezone.utc).isoformat(),
                      "manifest_created_at": metadata["created_at"] if metadata else None,
                      "statistics_basis": "RELEASE_MANIFEST" if kind == "overview" else "BOUND_READONLY_PROJECTION" if kind else None}
        return {"schema_version": DATABASE_RESULT_SCHEMA, "kind": kind, "status": status,
                "normalized_request": normalized, "data": data, "error": error, "provenance": provenance,
                "non_claims": ["NOT_MECHANICAL_PASS", "NOT_OOD_PASS", "NOT_FEA_VALIDATION", "NOT_MANUFACTURING_VALIDATION", "NOT_CLINICAL_VALIDATION"]}
