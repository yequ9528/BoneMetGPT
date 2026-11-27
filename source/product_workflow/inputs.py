from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from threading import Lock
from typing import Any, Callable, Mapping

from .contracts import (
    STIFFNESS_NAMES,
    BASE_MATERIAL_MODULUS_GPA,
    build_query_planner_evidence,
    canonical_sha256,
    require_nine,
    require_sha256,
    target_from_database_record,
    target_from_physical_9d,
)
from .formal_150w_query import (Formal150wQueryService, validate_query_plan,
    DATABASE_REQUEST_SCHEMA, validate_database_request, _unique_json_pairs, ReadonlyQueryError)


def classify_readonly_intent(text: str) -> str:
    value = str(text or "").strip().lower()
    if re.search(r"数据库|数据集|sample_id|c(?:11|12|13|22|23|33|44|55|66)|relative_density|diameter|相对密度", value):
        return "database"
    if re.search(r"这个结果|当前(?:任务|job)|任务状态|结果怎么样|此任务", value):
        return "current_job"
    if re.search(r"功能|你能|帮助|help|流程|是什么|解释|介绍|b31|stl|csv|单胞", value):
        return "help"
    if re.search(r"不要|别|不想|无需|不开始|不设计|不用", value):
        return "help"
    if re.search(r"开始.*设计|设计.*(?:晶格|植入|骨)|design|生成.*候选|推荐.*单胞", value):
        return "design"
    return "unknown"


def plan_database_read(question: str, *, planner=None) -> dict:
    """A query plan is read-only; it never represents a design authorization."""
    text = question.strip()
    count = bool(re.search(r"多少|数量|计数|count", text, re.I))
    fields = re.findall(r"sample_id|relative_density|diameter|C(?:11|12|13|22|23|33|44|55|66)", text, re.I)
    if not fields:
        if re.search(r"条件|其中|满足", text):
            return {"status": "needs_clarification", "question": "请明确需要计数的字段、比较关系、数值和单位。"}
        return {"status": "query", "request": {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": "overview"}, "unit_conversion": None}
    has_stiffness = any(field.upper() in STIFFNESS_NAMES for field in fields)
    gpa = bool(re.search(r"GPa", text, re.I))
    if has_stiffness and not gpa and not re.search(r"C\s*/\s*Eb|无量纲|归一化", text, re.I):
        return {"status": "needs_clarification", "question": "刚度条件使用 C/Eb（无量纲）还是 GPa？请注明单位。"}
    matches = re.findall(r"(sample_id|relative_density|diameter|C(?:11|12|13|22|23|33|44|55|66))\s*(>=|<=|>|<|=|大于等于|小于等于|大于|小于|等于|为)\s*(-?\d+(?:\.\d+)?)", text, re.I)
    aliases = {">=": "gte", "<=": "lte", ">": "gt", "<": "lt", "=": "eq", "大于等于": "gte", "小于等于": "lte", "大于": "gt", "小于": "lt", "等于": "eq", "为": "eq"}
    if matches and len(matches) == len(fields):
        filters = []
        for field, op, raw in matches:
            field = field.upper() if field.upper() in STIFFNESS_NAMES else field.lower()
            value = int(raw) if field == "sample_id" and "." not in raw else float(raw)
            filters.append({"field": field, "op": aliases[op], "value": value})
        request = {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": "count" if count else "records", "filters": filters}
        if not count:
            request.update(order_by=[], limit=1 if any(f["field"] == "sample_id" and f["op"] == "eq" for f in filters) else 5)
    elif planner is not None:
        prompt = ("只返回封闭 JSON 只读请求，不输出 SQL、路径、设计动作或额外字段。schema_version=latticegpt_database_read_request_v1；kind 仅 records/count。"
                  "records 必须有 filters、order_by、limit(1-5)；count 仅 filters。filters 元素严格为 field/op/value；op 为 eq/lt/lte/gt/gte/between。"
                  "字段为 sample_id, relative_density, diameter, C11,C12,C13,C22,C23,C33,C44,C55,C66。保留用户输入单位的数值，不擅自转换。无法确定时返回空对象。用户：" + question)
        request = planner(prompt)
        if isinstance(request, Mapping) and request.get("schema_version") == STRUCTURED_LLM_RESPONSE_SCHEMA_VERSION:
            if request.get("status") != "parsed":
                raise ReadonlyQueryError("INVALID_QUERY")
            request = request.get("parsed")
        if isinstance(request, str):
            request = json.loads(request, object_pairs_hook=_unique_json_pairs)
    else:
        return {"status": "needs_clarification", "question": "请明确查询字段、比较关系、数值和单位。"}
    normalized = validate_database_request(request)
    if normalized["kind"] not in ("records", "count"):
        raise ReadonlyQueryError("INVALID_QUERY")
    conversion = None
    if gpa:
        for item in normalized["filters"]:
            if item["field"] in STIFFNESS_NAMES:
                item["value"] = [v / BASE_MATERIAL_MODULUS_GPA for v in item["value"]] if isinstance(item["value"], list) else item["value"] / BASE_MATERIAL_MODULUS_GPA
        conversion = f"输入 GPa / 绑定 Eb={BASE_MATERIAL_MODULUS_GPA:g} GPa → 执行 C/Eb（无量纲）"
    return {"status": "query", "request": normalized, "input_text": text,
            "input_units": "GPa" if gpa else "C/Eb 或字段自身单位",
            "execution_units": "C/Eb（无量纲）；杆径为无量纲比例", "unit_conversion": conversion}


MAX_CSV_BYTES = 5 * 1024 * 1024
_CSV_FIELD_LIMIT_LOCK = Lock()
STRUCTURED_LLM_RESPONSE_SCHEMA_VERSION = "latticegpt_structured_llm_response_v1"
OPERATOR_ALIASES = {
    "=": "eq",
    "==": "eq",
    "eq": "eq",
    ">": "gt",
    "gt": "gt",
    ">=": "gte",
    "gte": "gte",
    "<": "lt",
    "lt": "lt",
    "<=": "lte",
    "lte": "lte",
    "between": "between",
}


def parse_target_csv_upload(content: bytes, *, filename: str) -> dict[str, Any]:
    """Parse every CSV record; a row becomes a proposal only after UI selection."""
    if not isinstance(content, bytes) or len(content) > MAX_CSV_BYTES:
        raise ValueError("CSV upload must not exceed 5 MB")
    if not isinstance(filename, str) or not filename.lower().endswith(".csv"):
        raise ValueError("target upload must be a CSV file")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("target CSV must use UTF-8 encoding") from exc
    # csv's default field limit is smaller than an otherwise permitted upload.
    _CSV_FIELD_LIMIT_LOCK.acquire()
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(max(previous_limit, MAX_CSV_BYTES))
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        fields = next(reader, [])
        if len(fields) != len(STIFFNESS_NAMES) or set(fields) != set(STIFFNESS_NAMES):
            names = {name.strip().lower() for name in fields}
            if {"x", "y", "z"} <= names or names & {"nodes", "edges", "connectivity", "node_id", "element_id"}:
                raise ValueError("geometry CSV is not a stiffness target; upload the supported 9D GPa columns")
            raise ValueError("target CSV must contain exactly the supported 9D columns without repeated headers")
        rows = []
        for index, row in enumerate(reader, 1):
            values, error = None, None
            if len(row) != len(fields):
                error = "Record must contain exactly nine values."
            else:
                try:
                    values = require_nine(dict(zip(fields, row)), "physical_9d_gpa")
                except (ValueError, TypeError, OverflowError) as exc:
                    message = str(exc)
                    error = (message if message.startswith("physical_9d_gpa.")
                             else "Record must contain finite positive numbers in all 9D columns.")
            rows.append({"record_index": index, "values": values, "error": error})
    except csv.Error as exc:
        raise ValueError("target CSV is malformed") from exc
    finally:
        csv.field_size_limit(previous_limit)
        _CSV_FIELD_LIMIT_LOCK.release()
    if not rows:
        raise ValueError("target CSV must contain at least one data row")
    valid_count = sum(row["error"] is None for row in rows)
    return {"filename": filename, "sha256": hashlib.sha256(content).hexdigest().upper(),
            "record_count": len(rows), "valid_count": valid_count,
            "invalid_count": len(rows) - valid_count, "rows": rows}


def target_from_csv_record(parsed: Mapping[str, Any], record_index: int) -> dict[str, Any]:
    if type(record_index) is not int or not 1 <= record_index <= parsed["record_count"]:
        raise ValueError("CSV record index must identify an existing data record")
    row = parsed["rows"][record_index - 1]
    if row["record_index"] != record_index or row["error"] is not None or row["values"] is None:
        raise ValueError("CSV record is invalid and cannot become a design target")
    return target_from_physical_9d(
        row["values"], source_kind="csv_upload", source_sha256=parsed["sha256"],
        source_name=parsed["filename"], source_record_index=record_index,
        source_record_count=parsed["record_count"],
    )


def target_from_csv_upload(content: bytes, *, filename: str) -> dict[str, Any]:
    parsed = parse_target_csv_upload(content, filename=filename)
    if parsed["record_count"] != 1:
        raise ValueError("target CSV must contain exactly one data row")
    return target_from_csv_record(parsed, 1)


def plan_formal_query(
    question: str,
    *,
    planner: Callable[[str], Mapping[str, Any] | str],
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("natural-language query must not be empty")
    request_text = question.strip()
    request_sha = hashlib.sha256(request_text.encode("utf-8")).hexdigest().upper()
    prompt = (
        "Convert the user request into one JSON object only. Do not invent fields, values, "
        "units, or stiffness targets. Return exactly one of these shapes: "
        "{\"status\":\"needs_clarification\",\"question\":\"...\"} or "
        "{\"status\":\"query\",\"query_plan\":{" 
        "\"filters\":[{\"field\":\"sample_id\",\"op\":\"eq\",\"value\":2}],"
        "\"order_by\":[],\"limit\":1}}. "
        "Every filter must contain field, op, and value. "
        "Allowed fields: sample_id, relative_density, diameter, C11, C12, C13, C22, "
        "C23, C33, C44, C55, C66. Allowed op values: eq, lt, lte, gt, gte, between. "
        "Never return SQL. If a required field, operator, or numeric value is unclear, "
        "return needs_clarification. "
        f"User request: {question}"
    )
    response = planner(prompt)
    raw_sha = None
    model_profile_sha = None
    if isinstance(response, str):
        raw_sha = hashlib.sha256(response.encode("utf-8")).hexdigest().upper()
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            evidence = build_query_planner_evidence(
                request_sha256=request_sha,
                status="invalid_response",
                raw_response_sha256=raw_sha,
                failure_code="invalid_json",
                error="query planner did not return valid JSON",
            )
            return {
                "status": "needs_clarification",
                "question": "查询条件未能形成有效结构，请明确字段、比较关系和数值。",
                "planner_evidence": evidence,
            }
    if not isinstance(response, Mapping):
        raise ValueError("query planner response must be a mapping")
    if response.get("schema_version") == STRUCTURED_LLM_RESPONSE_SCHEMA_VERSION:
        raw_sha = response.get("raw_sha256")
        model_profile_sha = response.get("model_runtime_profile_sha256")
        if response.get("status") != "parsed" or not isinstance(response.get("parsed"), Mapping):
            failure_code = response.get("status")
            if failure_code not in ("invalid_json", "invalid_shape"):
                failure_code = "invalid_response"
            evidence = build_query_planner_evidence(
                request_sha256=request_sha,
                status="invalid_response",
                raw_response_sha256=raw_sha,
                model_runtime_profile_sha256=model_profile_sha,
                failure_code=failure_code,
                error="structured planner response is invalid",
            )
            return {
                "status": "needs_clarification",
                "question": "查询条件未能形成有效结构，请明确字段、比较关系和数值。",
                "planner_evidence": evidence,
            }
        response = response["parsed"]

    status = response.get("status")
    if status == "needs_clarification":
        clarification = response.get("question")
        if not isinstance(clarification, str) or not clarification.strip():
            raise ValueError("query planner clarification question is required")
        clarification = clarification.strip()
        evidence = build_query_planner_evidence(
            request_sha256=request_sha,
            status="needs_clarification",
            raw_response_sha256=raw_sha,
            parsed_response=response,
            model_runtime_profile_sha256=model_profile_sha,
        )
        return {"status": status, "question": clarification, "planner_evidence": evidence}
    if status != "query":
        raise ValueError("query planner status is not supported")
    normalized, normalizations, failure_code = _normalize_query_plan(
        response.get("query_plan") or {}
    )
    if failure_code:
        evidence = build_query_planner_evidence(
            request_sha256=request_sha,
            status="needs_clarification",
            raw_response_sha256=raw_sha,
            parsed_response=response,
            model_runtime_profile_sha256=model_profile_sha,
            normalizations=normalizations,
            failure_code=failure_code,
            error="a filter operator is missing and cannot be inferred safely",
        )
        return {
            "status": "needs_clarification",
            "question": "请明确每个筛选条件的比较关系，例如等于、大于或介于。",
            "planner_evidence": evidence,
        }
    plan = validate_query_plan(normalized)
    evidence = build_query_planner_evidence(
        request_sha256=request_sha,
        status="query",
        raw_response_sha256=raw_sha,
        parsed_response=response,
        model_runtime_profile_sha256=model_profile_sha,
        normalizations=normalizations,
    )
    return {
        "status": "query",
        "query_plan": plan,
        "query_identity": canonical_sha256(plan),
        "planner_evidence": evidence,
    }


def replay_frozen_query_plan(
    query_plan: Mapping[str, Any],
    *,
    planner: Callable[[str], Mapping[str, Any] | str] | None = None,
) -> dict[str, Any]:
    """Validate an already frozen query plan without invoking an LLM."""

    del planner
    normalized = validate_query_plan(query_plan)
    return {
        "status": "replay",
        "replay_start": "FROZEN_QUERY_PLAN_OR_TARGET",
        "query_plan": normalized,
        "query_identity": canonical_sha256(normalized),
    }


def _normalize_query_plan(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], str | None]:
    if not isinstance(plan, Mapping):
        raise ValueError("query plan must be a mapping")
    normalized = dict(plan)
    raw_filters = plan.get("filters") or []
    if not isinstance(raw_filters, list):
        raise ValueError("query filters must be a list")
    filters = []
    normalizations: list[str] = []
    for index, raw in enumerate(raw_filters):
        if not isinstance(raw, Mapping):
            raise ValueError("query filters must be mappings")
        item = dict(raw)
        if "operator" in item:
            operator_alias = str(item["operator"]).strip().lower()
            if "op" in item:
                op_alias = str(item["op"]).strip().lower()
                if OPERATOR_ALIASES.get(op_alias, op_alias) != OPERATOR_ALIASES.get(
                    operator_alias,
                    operator_alias,
                ):
                    raise ValueError("query filter contains conflicting op and operator")
                item.pop("operator")
                normalizations.append(f"filters[{index}].redundant_operator_removed")
            else:
                item["op"] = item.pop("operator")
                normalizations.append(f"filters[{index}].operator_to_op")
        operator = str(item.get("op") or "").strip().lower()
        if not operator:
            normalized["filters"] = filters + [item, *raw_filters[index + 1:]]
            return normalized, normalizations, "missing_filter_operator"
        mapped = OPERATOR_ALIASES.get(operator, operator)
        if mapped != operator:
            item["op"] = mapped
            normalizations.append(f"filters[{index}].operator_symbol_to_{mapped}")
        filters.append(item)
    normalized["filters"] = filters
    return normalized, normalizations, None


def proposals_from_formal_query(
    query_plan: Mapping[str, Any],
    query_service: Formal150wQueryService,
    *,
    query_text: str | None = None,
    planner_evidence: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = validate_query_plan(query_plan)
    identity = canonical_sha256(normalized)
    proposals = [
        target_from_database_record(
            record,
            source_sha256=query_service.source_sha256,
            query_identity=identity,
        )
        for record in query_service.query(normalized)
    ]
    if planner_evidence is not None:
        evidence_sha = require_sha256(
            planner_evidence.get("evidence_sha256"),
            "planner_evidence.evidence_sha256",
        )
        for proposal in proposals:
            proposal["source"]["planner_evidence_sha256"] = evidence_sha
            proposal["proposal_sha256"] = canonical_sha256(
                {key: value for key, value in proposal.items() if key != "proposal_sha256"}
            )
    if query_text is not None:
        encoded = query_text.strip().encode("utf-8")
        for proposal in proposals:
            proposal["source"]["request_kind"] = "natural_language_query"
            proposal["source"]["request_sha256"] = hashlib.sha256(encoded).hexdigest().upper()
            proposal["proposal_sha256"] = canonical_sha256(
                {key: value for key, value in proposal.items() if key != "proposal_sha256"}
            )
    return proposals
