"""Session-local, closed tools. Runtime execution belongs exclusively to trusted UI callbacks."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import math
import secrets
import time
import threading
from contextvars import ContextVar

from .contracts import BASE_MATERIAL_MODULUS_GPA, CANDIDATE_VERSIONS, STIFFNESS_NAMES, canonical_sha256, target_from_database_record, candidate_ranking_view
from .formal_150w_query import DATABASE_REQUEST_SCHEMA, DATABASE_RESULT_SCHEMA, QUERY_FIELDS, validate_database_request
from .job_state import build_product_job_projection, classify_job_generation, candidate_confirmation_status
from . import model_context as facts
from .service import normalize_job_mesh_binding


TOOL_SPECS = {
    "describe_database": "无参数。正式数据库字段、单位、资格政策、清单统计。",
    "query_records": "conditions:[{field,op,value,unit?}]；inherit:bool默认false；order_by:[{field,direction}]；limit:1..5。"
                     "字段sample_id,relative_density,diameter,C11,C12,C13,C22,C23,C33,C44,C55,C66。"
                     "op:eq,lt,lte,gt,gte,between；仅AND，最多8条件；排序最多2业务字段，末尾sample_id稳定排序。"
                     "刚度每条件unit为GPa或C/Eb；比例unit为ratio或%；杆径仅归一化比例，不能mm。"
                     "数值保留用户原值，不自行换算。inherit继承上次成功条件；相同field/op替换，其他追加。",
    "count_records": "conditions和inherit同query_records；计数全部匹配行，无排序和limit。",
    "prepare_design_target": "result_ref加position(从1开始)或sample_id，二选一。仅用有效明细准备目标并展示新建确认，零Job零计算。",
    "get_job_context": "可选job_id，必须是当前已打开完整Job ID。读取目标、已有候选预测(GPa)、排名、网格、状态、job_ref和有效动作；不重算。",
    "prepare_workflow_action": "action:create_job(需proposal_ref)、confirm_target_and_generate、select_candidate(需candidate_id)、"
                               "request_mesh_upload、start_conformal；所有非create_job动作需当前job_ref。仅准备确认卡。不能传authorized、路径或计算参数。",
    "get_design_result": "可选job_id，仅当前Job；核验已有结果包和文件，返回摘要及链接；不生成、不修复。",
}
TTL_SECONDS = 900
CHAT_DISPATCH = ContextVar("latticegpt_chat_dispatch", default=None)


def _candidate_comparison_facts(candidates, recommended_id, *, metric="score"):
    """Read-only comparisons of recorded eligible designs; never rank or select."""
    eligible = [c for c in candidates if c.get('eligible') is True]
    metrics = ('relative_density', 'nmse') if metric == 'nmse' else ('score', 'relative_density', 'nmse')

    def value(candidate, key):
        number = candidate.get(key)
        return number if type(number) in (int, float) and math.isfinite(number) and number >= 0 else None

    recommended = next((c for c in eligible if c['candidate_id'] == recommended_id), None)
    counts = {key: sum(value(c, key) is not None for c in eligible) for key in metrics}

    def lowest(key):
        recorded = [c for c in eligible if value(c, key) is not None]
        minimum = min((value(c, key) for c in recorded), default=None)
        return [c['candidate_id'] for c in recorded if value(c, key) == minimum]

    deltas = {}
    if recommended is not None:
        for candidate in eligible:
            deltas[candidate['candidate_id']] = {
                key: value(candidate, key) - value(recommended, key)
                if value(candidate, key) is not None and value(recommended, key) is not None else None
                for key in metrics}
    result = {
        'scope': 'recorded_eligible_candidates_only', 'eligible_count': len(eligible),
        'recommended_candidate_id': recommended['candidate_id'] if recommended else None,
        'lowest_recorded_density_candidate_ids': lowest('relative_density'),
        'lowest_recorded_nmse_candidate_ids': lowest('nmse'),
        'recorded_counts': counts, 'deltas_from_recommended': deltas,
        'delta_definition': 'candidate minus recorded recommendation; positive matching-error delta means worse target match, negative density means less material fraction. No significance threshold or new ranking is applied.',
    }

    if metric == "score": result["p10_recommended_candidate_id"] = result["recommended_candidate_id"]
    return result


class ChatToolError(ValueError):
    pass


class ChatWorkflowStateError(ChatToolError):
    def __init__(self, code, message, job_status=None):
        super().__init__(message)
        self.code, self.job_status = code, job_status

    def result(self):
        return {'status':'error', 'text':str(self), 'job_status': self.job_status,
                'failure':{'code':self.code, 'stage':'action_prepare', 'message':str(self),
                           'next_step':'Read the current Job and use its available action. No operation was executed or retried.'}}


class ChatActionArgumentError(ChatToolError):
    """An internal action request is malformed, not an incomplete user target."""


class ChatBudgetExceeded(ChatToolError):
    pass


class ChatReadTurn:
    """Application-owned requirement and observations for one admitted run."""
    def __init__(self, requirement=None):
        self.requirement = copy.deepcopy(requirement)
        self.context_identity = None
        self.planning_rows_ref = None
        self.planning_job = None
        self.planning_rows_deadline = None
        self.result_rows_ref = None
        self.result_provenance = None
        self.state = {"status": "not_queried"}
        self.attempted = False
        self.display = {}
        if requirement is not None:
            _closed(self.requirement, {"kind", "request"}, {"kind"})
            if self.requirement["kind"] not in {"overview", "records", "count"}:
                raise ChatToolError("本轮查询要求的类型无效。")
            if "request" in self.requirement:
                try:
                    request = validate_database_request(self.requirement["request"])
                except (ValueError, TypeError, KeyError) as exc:
                    raise ChatToolError("本轮可信查询要求无效。") from exc
                if request["kind"] != self.requirement["kind"]:
                    raise ChatToolError("本轮查询要求与请求类型不一致。")
                self.requirement["request"] = copy.deepcopy(request)

    def check_tool(self, name):
        if self.requirement is not None:
            expected = {"overview": "describe_database", "records": "query_records", "count": "count_records"}[self.requirement["kind"]]
            if name != expected or self.attempted:
                raise ChatToolError("本轮仅允许一次指定类型的只读查询；未执行其他操作或重试。")

    def prepare(self, request):
        if self.requirement is not None:
            expected = self.requirement.get("request")
            if expected is not None and request != expected:
                raise ChatToolError("有效查询条件与本轮可信要求不一致；尚未读取数据库。")
        self.attempted = True
        self.state = {"status": "pending", "kind": request["kind"], "query_identity": canonical_sha256(request)}

    def observe(self, request, result, query):
        status = result["status"]
        if self.requirement is not None:
            if (result.get("schema_version") != DATABASE_RESULT_SCHEMA or result.get("kind") != request["kind"]
                    or result.get("normalized_request") != request):
                raise ChatToolError("查询返回与本轮实际请求不一致，不能建立执行证据。")
            # Binding failures can legitimately carry no provenance. Preserve
            # their actual error instead of validating them as successful reads.
            if status != "error":
                origin = result.get("provenance")
                if not isinstance(origin, dict) or origin.get("query_identity") != canonical_sha256(request):
                    raise ChatToolError("查询返回缺少有效请求身份。")
                for key in ("source_sha256", "manifest_sha256", "projection_sha256"):
                    expected = getattr(query, key, None)
                    if not isinstance(expected, str) or len(expected) != 64 or origin.get(key) != expected:
                        raise ChatToolError("查询返回的正式来源身份不一致。")
                data = result.get("data")
                if not isinstance(data, dict):
                    raise ChatToolError("查询返回缺少数据。")
                if status == "empty" and request["kind"] != "records":
                    raise ChatToolError("只有记录明细可以返回空集状态。")
                if request["kind"] == "records":
                    rows, count = data.get("rows"), data.get("returned_count")
                    if (not isinstance(rows, list) or type(count) is not int or count != len(rows)
                            or not 0 <= count <= request["limit"] or (status == "empty") != (count == 0)):
                        raise ChatToolError("查询返回的记录数量无效。")
                    for row in rows:
                        if (not isinstance(row, dict) or set(row) != {"sample_id", "relative_density", "diameter", *STIFFNESS_NAMES}
                                or type(row["sample_id"]) is not int
                                or any(type(v) not in (int, float) or not math.isfinite(v) for k, v in row.items() if k != "sample_id")):
                            raise ChatToolError("查询返回的记录字段无效。")
                elif request["kind"] == "count" and (type(data.get("matched_count")) is not int or data["matched_count"] < 0):
                    raise ChatToolError("查询返回的计数无效。")
                elif request["kind"] == "overview":
                    fields = data.get("fields")
                    if (type(data.get("scanned_rows")) is not int or type(data.get("eligible_rows")) is not int
                            or not 0 <= data["eligible_rows"] <= data["scanned_rows"]
                            or not isinstance(data.get("statistics_time"), str) or not data["statistics_time"]
                            or not isinstance(data.get("eligibility_policy"), dict)
                            or not isinstance(fields, list) or len(fields) != len(QUERY_FIELDS)):
                        raise ChatToolError("查询返回的数据库说明或统计无效。")
                    expected_units = {name: "dimensionless_C_over_Eb" if name in STIFFNESS_NAMES else
                                      "integer_identifier" if name == "sample_id" else "dimensionless" for name in QUERY_FIELDS}
                    if (any(not isinstance(field, dict) or not isinstance(field.get("name"), str)
                            or field.get("name") not in expected_units
                            or field.get("unit") != expected_units[field["name"]] for field in fields)
                            or {field["name"] for field in fields} != QUERY_FIELDS):
                        raise ChatToolError("查询返回的数据库字段或单位无效。")
        self.state = {"status": status, "kind": request["kind"], "query_identity": canonical_sha256(request)}

    @property
    def satisfied(self):
        return self.requirement is None or (self.state["status"] == "ok" and self.state["kind"] == self.requirement["kind"])


_TRACE_EVENTS = (
    "budget_exhausted", "context_invalidated", "target_proposal_retired", "confirmation_issued",
    "confirmation_requested", "confirmation_rejected", "confirmation_consumed", "confirmation_revoked",
    "tool_request", "tool_error", "tool_result", "run_started", "read_requirement", "execution_receipt",
    "run_timeout", "run_error", "run_finished", "model_request", "model_error", "model_response",
    "answer_rejected", "program_job_answer", "query_plan", "model_profile", "receipt_rejected",
    "receipt_received", "ui_confirmation", "ui_callback_returned", "ui_confirmation_rejected",
    "mesh_domain_confirmation", "mesh_domain_returned", "mesh_contract_confirmation",
    "model_call_started", "model_raw_response", "model_json_parsed", "model_invocation_error",
)
_TURN_STATUSES = ("answered", "needs_clarification", "waiting_for_user", "stale", "busy", "timeout", "cancelled",
                  "incomplete", "budget_exhausted", "limit_reached", "error", "empty")


def _dispatch_id(value):
    return value if isinstance(value, str) and len(value) == 24 and all(c in "0123456789abcdef" for c in value) else None


def _trace_fields(event, fields):
    """Select diagnostic scalars; never serialize arbitrary values or caller-supplied map keys."""
    enum, project = facts.enum, facts.project
    common = {"call_id": _dispatch_id, "issued_run_id": _dispatch_id, "job_id": facts.job_id,
              "revision": facts.count, "phase": enum("planning", "execution", "receipt"),
              "stage": enum("model_call", "response_text", "json_parse"), "name": enum(*TOOL_SPECS),
              "action": enum(*facts.ACTIONS, "mesh_confirmed"), "resource": enum("runs", "model_calls", "tool_calls"),
              "status": enum(*_TURN_STATUSES, *facts.KNOWN_STATUSES), "duration_seconds": facts.number,
              "timeout_seconds": facts.number, "max_selected_tool_calls": facts.count, "pending_count": facts.count,
              "raw_length": facts.count, "message_count": facts.count, "truncated": facts.boolean,
              "automatic": facts.boolean, "confirmed": facts.boolean, "timed_out": facts.boolean, "accepted": facts.boolean,
              "underlying_completion": enum("unknown_after_cancellation", "returned_error"),
              "authorization_source": enum("trusted_ui"),
              "reason": enum("MISSING_OR_STALE_JOB_FACT_EVIDENCE", "MISSING_REQUIRED_READ_EVIDENCE", "STALE_JOB_EVIDENCE",
                             "stale_session_or_job", "changed_during_read", "duplicate"),
              "origin": enum("program_job_facts", "llm_semantic_plan")}
    result = project(fields, common)
    if "error_type" in fields:
        result["error_type"] = facts.choice(fields["error_type"], (
            "ValueError", "KeyError", "TypeError", "OSError", "FileNotFoundError", "RuntimeError", "TimeoutError",
            "CancelledError", "JSONDecodeError", "ChatToolError", "ChatActionArgumentError", "ChatWorkflowStateError",
            "ChatBudgetExceeded", "APIConnectionError", "APITimeoutError", "APIStatusError", "AuthenticationError",
            "RateLimitError", "ConnectionError")) or "OtherError"
    if event == "model_request" and isinstance(fields.get("messages"), list):
        result["message_count"] = len(fields["messages"])
    if event == "query_plan":
        result["plan"] = project(fields.get("plan"), {"kind": enum("plan"),
                                 "task": enum("conversation", "workflow", *facts.QUERY_KINDS)})
    if event == "model_response":
        result["response_kind"] = facts.choice(fields.get("response_kind"),
                                               ("plan", "tool", "clarify", "answer"))
    if event == "model_profile":
        result["profile"] = project(fields.get("profile"), {"prompt_template_version": enum("chat-agent-auto-query-v2", "chat-agent-auto-query-v3")})
    if event in ("execution_receipt", "receipt_received", "receipt_rejected"):
        result["receipt"] = project(fields.get("receipt"), {"job_id": facts.job_id, "revision": facts.count,
                                    "action": enum(*facts.ACTIONS, "mesh_confirmed"), "failed": facts.boolean,
                                    "status": enum(*facts.KNOWN_STATUSES)})
    if event == "tool_result":
        raw = facts.mapping(fields.get("result"))
        summary = project(raw, {"status": enum(*facts.READ_STATES, *facts.KNOWN_STATUSES, "verified", "awaiting_confirmation"),
                                "kind": enum(*facts.QUERY_KINDS), "revision": facts.count})
        counts = project(raw.get("data"), {"matched_count": facts.count, "returned_count": facts.count})
        if counts:
            summary["data"] = counts
        if raw.get("error"):
            summary["error_code"] = facts.recorded_error_code(facts.mapping(raw["error"]))
        if isinstance(raw.get("files"), dict):
            summary["file_role_count"] = len(facts.result_file_roles(raw["files"]))
        result["result"] = summary
    return result


class ChatCase:
    """One UI session's admission counters; Job/context changes never replenish them."""
    def __init__(self, *, max_runs=20, max_model_calls=40, max_tool_calls=70):
        self.case_id = secrets.token_hex(16)
        self.limits = {"runs": max_runs, "model_calls": max_model_calls, "tool_calls": max_tool_calls}
        if any(type(value) is not int or value < 1 for value in self.limits.values()):
            raise ValueError("case limits must be positive integers")
        self.used = dict.fromkeys(self.limits, 0)
        self.exhausted = False
        self.seq = 0
        self.started_at = time.monotonic()
        self.trace_lock = threading.Lock()
        self.logger = logging.getLogger("latticegpt.chat")
        self.logger.setLevel(logging.INFO)

    def trace(self, event, *, run_id=None, **fields):
        with self.trace_lock:
            self.seq += 1
            payload = {"schema": "latticegpt_chat_trace_v1", "case_id": self.case_id,
                       "run_id": _dispatch_id(run_id), "seq": self.seq,
                       "event": facts.choice(event, _TRACE_EVENTS) or "other_event",
                       "time_unix": time.time(), "elapsed_seconds": time.monotonic() - self.started_at,
                       "budget_used": dict(self.used), "budget_limits": dict(self.limits), **_trace_fields(event, fields)}
            self.logger.info(json.dumps(payload, ensure_ascii=False, allow_nan=False), extra={"product_event": payload})

    def reserve(self, kind, *, run_id=None):
        if self.exhausted or self.used[kind] >= self.limits[kind]:
            self.exhausted = True
            self.trace("budget_exhausted", run_id=run_id, resource=kind)
            raise ChatBudgetExceeded("已达到本次会话的整例调度预算；不会继续调用模型、工具或自动重试。")
        self.used[kind] += 1


async def call_read(function, *args):
    if inspect.iscoroutinefunction(function):
        return await function(*args)
    return await asyncio.to_thread(function, *args)


def _closed(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ChatToolError("工具参数不完整或包含不支持的字段。")


class ChatTools:
    def __init__(self, *, query_service, read_job, read_result, current_job_id, session_id, clock=time.monotonic, case=None,
                 current_job_revision=None):
        self.query_service = query_service
        self.read_job = read_job
        self.read_result = read_result
        self.current_job_id = current_job_id
        self.current_job_revision = current_job_revision
        self.session_id = session_id
        self.clock = clock
        self.case = case if case is not None else ChatCase()
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.last_query = None
        self.records = None
        self.proposal = None
        self.job_context = None
        self.pending = {}
        self.last_database_read = {"status": "not_queried"}
        self.pending_csv_target = None
        self.csv_context_version = 0
        self.job_evidence_by_run = {}

    def set_pending_csv_target(self, summary: dict | None):
        """Update selected CSV evidence independently of active Job actions/locks."""
        if summary is not None and not isinstance(summary, dict):
            raise ChatToolError("Pending CSV context must be a summary object or null.")
        if summary != self.pending_csv_target:
            self.pending_csv_target = copy.deepcopy(summary)
            self.csv_context_version += 1

    def invalidate(self):
        self.case.trace("context_invalidated", pending_count=len(self.pending))
        self.epoch += 1
        self.records = self.proposal = self.job_context = self.last_query = None
        self.pending.clear()
        self.last_database_read = {"status": "not_queried"}
        self.job_evidence_by_run.clear()

    def _remember_job_evidence(self, job):
        run_id = (CHAT_DISPATCH.get() or {}).get('run_id')
        if run_id is not None:
            self.job_evidence_by_run[run_id] = {
                'job_id': job['job_id'], 'revision': job['revision'], 'sha256': canonical_sha256(job)}

    async def job_evidence_current(self, evidence):
        if evidence is None:
            return True
        if self.current_job_id() != evidence['job_id']:
            return False
        epoch = self.epoch
        current = await self._job({'job_id': evidence['job_id']})
        self._check_epoch(epoch)
        return current['revision'] == evidence['revision'] and canonical_sha256(current) == evidence['sha256']

    def retire_target_proposals(self, *, keep_ref=None):
        """Revoke only unconsumed target creation; other current Job actions survive."""
        for ref, record in list(self.pending.items()):
            if ref != keep_ref and record['action'] == 'create_job' and record['state'] == 'issued':
                record['state'] = 'retired'
                self.case.trace('target_proposal_retired')

    def snapshot(self):
        records = None if self.records is None else {k: copy.deepcopy(self.records[k]) for k in ("result_ref", "result")}
        return {"last_query": copy.deepcopy(self.last_query), "records": records,
                "active_job_id": self.current_job_id(), "job": copy.deepcopy(self.job_context),
                "proposal_ref": self.proposal["proposal_ref"] if self.proposal else None,
                "last_database_read": copy.deepcopy(self.last_database_read),
                "pending_csv_target": copy.deepcopy(self.pending_csv_target),
                "csv_context_version": self.csv_context_version}

    def planning_identity(self):
        return canonical_sha256({"epoch": self.epoch, "job_id": self.current_job_id(), "context": self.snapshot()})

    def model_snapshot(self):
        """Hide expired/cross-context cached rows in a display copy. Admission still checks the original cache."""
        view = self.snapshot()
        if self.records is not None:
            try:
                self._record_cache(self.records.get("result_ref"))
            except ChatToolError:
                view.update(records=None, proposal_ref=None)
        return view

    async def planning_snapshot(self):
        """Bind only currently available facts. Local handles are never part of the model projection."""
        view = self.model_snapshot()
        rows_ref = None
        if view["records"] and view["records"]["result"].get("status") == "ok":
            try:
                cache = self._record_cache(view["records"]["result_ref"])
                await self._check_bound_database(cache)
                rows_ref = cache["result_ref"]
            except (ValueError, OSError, RuntimeError):
                view.update(records=None, proposal_ref=None)

        cached = view["job"] or {}
        active = facts.job_id(view["active_job_id"])
        revision = self.current_job_revision() if active and self.current_job_revision is not None else None
        if not active or (cached and cached.get("job_id") != active):
            view["job"] = None
            return view, rows_ref, None
        # A cold UI session supplies the revision already bound when the Job was opened.
        # A cache with scientific values always needs the identity issued by get_job_context.
        if not cached.get("job_sha256") and type(revision) is not int:
            if cached.get("target") or cached.get("candidates"):
                view["job"] = None
            return view, rows_ref, None
        try:
            current = await self._job({"job_id": active})
            digest = canonical_sha256(current)
            current_revision = current["revision"]
            if ((self.current_job_revision is not None and self.current_job_revision() != revision)
                    or (type(revision) is int and current_revision < revision)
                    or (cached and (cached.get("revision") != current_revision or cached.get("job_sha256") != digest))):
                view["job"] = None
                return view, rows_ref, None
            if not cached:
                # Only the fields consumed by the bounded planning projection are needed here.
                collection = current.get("candidate_set") or {}
                candidates = collection.get("candidates") or []
                view["job"] = {key: current[key] for key in ("job_id", "revision", "status", "target", "selection") if key in current}
                if collection.get("schema_version") in CANDIDATE_VERSIONS:
                    view["job"]["ranking"] = candidate_ranking_view(collection)
                view["job"].update(candidate_confirmation=candidate_confirmation_status(current), prediction_units="GPa",
                    candidates=[{**row, "origin": "inverse_generated_structure"} for row in candidates[:3]])
            return view, rows_ref, {"job_id": active, "revision": current_revision, "sha256": digest}
        except (ValueError, OSError, RuntimeError):
            view["job"] = None
            return view, rows_ref, None

    async def _check_planned_read(self, turn, query):
        try:
            if not await self.job_evidence_current(turn.planning_job):
                raise ChatToolError("The current Job changed during this query. Read its current target before continuing.")
            if turn.planning_rows_ref is not None:
                cache = self._record_cache(turn.planning_rows_ref)
                self._check_database_identity(cache, query)
            if turn.context_identity is not None and turn.context_identity != self.planning_identity():
                raise ChatToolError("The session or selected inputs changed during this query.")
        except (ValueError, OSError, RuntimeError):
            turn.display["context_stale"] = True
            raise

    async def response_evidence_current(self, turn):
        """Check the sources actually shown to this turn before accepting its response."""
        try:
            if not await self.job_evidence_current(turn.planning_job):
                return False
            ref = turn.result_rows_ref or turn.planning_rows_ref
            if ref is not None:
                await self._check_bound_database(self._record_cache(ref))
                self._record_cache(ref)
            elif turn.result_provenance is not None:
                await self._check_bound_database({"result": {"provenance": turn.result_provenance}})
            return turn.planning_rows_deadline is None or self.clock() < turn.planning_rows_deadline
        except (ValueError, OSError, RuntimeError):
            return False

    async def bind_workflow_plan(self, args, *, run_id):
        """Resolve selectors using this session. The resulting tool still enforces its own guards."""
        intent = args.get("intent")
        if intent == "reference_target":
            _closed(args, {"intent", "position", "sample_id"}, {"intent"})
            if ("position" in args) == ("sample_id" in args):
                raise ChatToolError("Specify one reference record by its position or sample ID.")
            if self.records is None:
                raise ChatToolError("Query reference records first, then choose a record from the result.")
            selector = {key: args[key] for key in ("position", "sample_id") if key in args}
            return "prepare_design_target", {"result_ref": self.records["result_ref"], **selector}

        if intent in ('prepare_generation', 'prepare_conformal'):
            _closed(args, {'intent', 'job_id'}, {'intent'})
            job_args = {'job_id': args['job_id']} if 'job_id' in args else {}
            job = await self.execute('get_job_context', job_args, run_id=run_id, automatic=True)
            action = 'confirm_target_and_generate' if intent == 'prepare_generation' else 'start_conformal'
            return 'prepare_workflow_action', {'action': action, 'job_ref': job['job_ref']}

        if intent in ("compare_candidates", "read_result"):
            allowed = {'intent', 'job_id'}
            if intent == 'compare_candidates':
                allowed |= {'focus', 'ranks', 'candidate_ids', 'component'}
            _closed(args, allowed, {"intent"})
            job_args = {"job_id": args["job_id"]} if "job_id" in args else {}
            if intent == "compare_candidates":
                focus = args.get('focus', 'overall')
                if not isinstance(focus, str) or focus not in {'overall', 'density', 'matching', 'selection', 'metrics', 'component_delta'}:
                    raise ChatToolError('The comparison focus is unsupported.')
                if 'component' in args and (focus != 'component_delta' or args['component'] not in STIFFNESS_NAMES):
                    raise ChatToolError('Specify a supported stiffness component only for component_delta.')
                selectors = [key for key in ('ranks', 'candidate_ids') if key in args]
                if len(selectors) > 1 or (focus == 'selection' and selectors):
                    raise ChatToolError('Specify one comparison selector; confirmation status has no selector.')
                if selectors:
                    key = selectors[0]
                    values = args[key]
                    valid = (lambda v: type(v) is int and v > 0) if key == 'ranks' else (lambda v: isinstance(v, str) and bool(v))
                    if not isinstance(values, list) or not 1 <= len(values) <= 3 or not all(valid(v) for v in values) or len(set(values)) != len(values):
                        raise ChatToolError('Specify one to three unique current candidate ranks or IDs.')
                job = await self.execute("get_job_context", job_args, run_id=run_id, automatic=True)
                if selectors:
                    field = 'rank' if selectors[0] == 'ranks' else 'candidate_id'
                    if any(sum(c.get(field) == value for c in job['candidates']) != 1 for value in args[selectors[0]]):
                        raise ChatToolError('The comparison must identify recorded candidates in this Job; no fallback scope was used.')
                return None
            job = await self.execute('get_job_context', job_args, run_id=run_id, automatic=True)
            return ('get_design_result', job_args) if job['status'] == 'VIEWER_READY' else None

        if intent == "choose_candidate":
            _closed(args, {"intent", "job_id", "rank", "candidate_id"}, {"intent"})
            if ("rank" in args) == ("candidate_id" in args):
                raise ChatToolError("Specify one candidate by its rank or candidate ID.")
            key = "rank" if "rank" in args else "candidate_id"
            value = args[key]
            if (key == "rank" and (type(value) is not int or value < 1)) or (key == "candidate_id" and not isinstance(value, str)):
                raise ChatToolError("The candidate selector is invalid.")
            job_args = {"job_id": args["job_id"]} if "job_id" in args else {}
            job = await self.execute("get_job_context", job_args, run_id=run_id, automatic=True)
            matches = [c for c in job["candidates"] if c[key] == value]
            if len(matches) != 1:
                raise ChatToolError("The selector must identify one recorded candidate in the current Job.")
            if not matches[0]["eligible"]:
                raise ChatWorkflowStateError("ACTION_STATE_NOT_ALLOWED", "The selected candidate is not eligible in the current Job.", job["status"])
            return "prepare_workflow_action", {"action": "select_candidate", "job_ref": job["job_ref"],
                                               "candidate_id": matches[0]["candidate_id"]}

        raise ChatToolError("The assistant plan contains an unsupported workflow intent.")

    def bind_read_plan(self, kind, args, context_identity, *, turn=None):
        """Freeze the semantic planner's request before I/O, without granting semantic truth."""
        if self.planning_identity() != context_identity:
            raise ChatToolError("规划期间会话、记录或继承条件已变化，请重新表达需求。")
        if kind == "overview":
            _closed(args, set())
            request = {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": kind}
        else:
            request, _ = self._query_request(kind, args)
        if turn is None:
            turn = ChatReadTurn()
        turn.requirement = {"kind": kind, "request": request}
        turn.context_identity = context_identity
        return turn

    def _check_epoch(self, epoch):
        if epoch != self.epoch:
            raise ChatToolError("会话或动作上下文已失效，请重新读取。")

    def _query_request(self, kind, args):
        allowed = {"conditions", "inherit"} | ({"order_by", "limit"} if kind == "records" else set())
        _closed(args, allowed)
        inherit = args.get("inherit", False)
        if type(inherit) is not bool or (inherit and self.last_query is None):
            raise ChatToolError("没有可继承的成功查询条件。")
        previous = self.last_query if inherit else None
        conditions = copy.deepcopy(previous["conditions"]) if previous else []
        added = []
        additions = args.get("conditions", [])
        if not isinstance(additions, list) or len(additions) > 8:
            raise ChatToolError("最多接受8个AND条件。")
        for condition in additions:
            _closed(condition, {"field", "op", "value", "unit"}, {"field", "op", "value"})
            item = copy.deepcopy(condition)
            field = item["field"]
            if not isinstance(field, str) or field not in QUERY_FIELDS:
                raise ChatToolError("不支持的数据库字段。")
            if not isinstance(item["op"], str) or ("unit" in item and not isinstance(item["unit"], str)):
                raise ChatToolError("条件运算符和单位必须为字符串。")
            if "unit" not in item:
                units = {c["unit"] for c in conditions if c["field"] == field and c.get("unit") is not None}
                if len(units) == 1:
                    item["unit"] = units.pop()
            if field in STIFFNESS_NAMES and item.get("unit") not in {"GPa", "C/Eb"}:
                raise ChatToolError(f"请明确{field}使用GPa还是C/Eb。")
            if field in {"relative_density", "diameter"} and item.get("unit", "ratio") not in {"ratio", "%"}:
                raise ChatToolError("相对密度和归一化杆径只接受ratio或%，不接受mm。")
            if field == "sample_id" and item.get("unit", "id") != "id":
                raise ChatToolError("sample_id只能是整数编号。")
            added.append(item)
        replaced = {(c["field"], c["op"]) for c in added}
        conditions = [c for c in conditions if (c["field"], c["op"]) not in replaced] + added
        filters = []
        for item in conditions:
            values = item["value"] if item["op"] == "between" else [item["value"]]
            if not isinstance(values, list) or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                raise ChatToolError("条件必须是有限数值，between使用两项数组。")
            divisor = BASE_MATERIAL_MODULUS_GPA if item.get("unit") == "GPa" else 100 if item.get("unit") == "%" else 1
            converted = [v / divisor if divisor != 1 else v for v in values]
            filters.append({"field": item["field"], "op": item["op"],
                            "value": converted if item["op"] == "between" else converted[0]})
        request = {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": kind, "filters": filters}
        order = copy.deepcopy(args.get("order_by", previous.get("order_by", []) if previous else []))
        limit = args.get("limit", previous.get("limit", 5) if previous else 5)
        if kind == "records":
            request.update(order_by=order, limit=limit)
        try:
            request = validate_database_request(request)
        except (ValueError, TypeError, KeyError) as exc:
            raise ChatToolError("查询条件、排序或数量超出现有只读合同。") from exc
        state = {"conditions": conditions, "order_by": order, "limit": limit}
        return request, state

    def register_records(self, result):
        """Share exactly the same result identity between deterministic tables and chat."""
        if result.get("kind") != "records" or result.get("status") not in {"ok", "empty"}:
            raise ChatToolError("只有成功读取的明细才能建立记录引用。")
        result = copy.deepcopy(result)
        result.pop("result_ref", None)
        ref = secrets.token_hex(24)
        self.records = {"result_ref": ref, "result": result, "issued_at": self.clock(),
                        "job_id": self.current_job_id(), "session_id": self.session_id}
        self.proposal = None
        self.pending.clear()
        return ref

    def _record_cache(self, ref):
        cache = self.records
        if (cache is None or cache["result_ref"] != ref or cache["session_id"] != self.session_id
                or cache["job_id"] != self.current_job_id() or self.clock() - cache["issued_at"] >= TTL_SECONDS):
            raise ChatToolError("明细引用已失效或过期，请重新查询；不会改选其他记录。")
        return cache

    async def _check_source(self, cache):
        await self._check_bound_database(cache)
        self._record_cache(cache["result_ref"])

    async def _check_bound_database(self, cache):
        query = await call_read(self.query_service)
        await call_read(query.overview)
        self._check_database_identity(cache, query)

    @staticmethod
    def _check_database_identity(cache, query):
        origin = facts.mapping(cache["result"].get("provenance"))
        if any(origin.get(k) != getattr(query, k, None) or origin.get(k) is None
               for k in ("source_sha256", "manifest_sha256", "projection_sha256")):
            raise ChatToolError("数据库来源身份已变化，请重新查询。")

    def _job_id(self, args):
        _closed(args, {"job_id"})
        current = self.current_job_id()
        if not current or args.get("job_id", current) != current:
            raise ChatWorkflowStateError('JOB_NOT_OPEN', 'Open the current Job by its complete ID before preparing an action.')
        return current

    async def _job(self, args):
        job_id = self._job_id(args)
        job = await call_read(self.read_job, job_id)
        if self.current_job_id() != job_id or not isinstance(job, dict) or job.get("job_id") != job_id:
            raise ChatWorkflowStateError('JOB_CONTEXT_STALE', 'The current Job changed or cannot be read consistently.')
        return job

    def _job_view(self, job):
        projection = build_product_job_projection(job)
        permitted = classify_job_generation(job)["runtime_permitted"] and job["workflow_disposition"] == "ACTIVE"
        actions = []
        if permitted and not job.get("failure"):
            if job["status"] == "TARGET_PROPOSED":
                actions.append("confirm_target_and_generate")
            for capability, action in (("select_candidate", "select_candidate"), ("upload_mesh", "request_mesh_upload"),
                                       ("start_conformal", "start_conformal")):
                if projection["capabilities"][capability]:
                    actions.append(action)
        ranking = candidate_ranking_view(job.get('candidate_set') or {})
        candidates = [{k: copy.deepcopy(c.get(k)) for k in
                       ("candidate_id", "rank", "score", "eligible", "p09_validation", "predicted_stiffness", "normalized_diameter",
                        "relative_density", "nmse", "exclusion_reasons", "source_order", "geometry_contract") if k != "score" or ranking["metric"] == "score"}
                      for c in (job.get("candidate_set") or {}).get("candidates", [])]
        target_values = (job.get('target') or {}).get('physical_9d_gpa') or {}
        for candidate in candidates:
            candidate['origin'] = 'inverse_generated_structure'
            prediction = candidate.get('predicted_stiffness') or {}
            candidate['stiffness_delta_gpa'] = {key: prediction[key] - target_values[key]
                for key in STIFFNESS_NAMES if key in prediction and key in target_values}
        inverse = job.get('inverse_result') or {}
        mesh = normalize_job_mesh_binding(job, verify_stored_file=False) if job.get("mesh_binding") else {}
        return {"job_id": job["job_id"], "revision": job["revision"], "status": job["status"],
                "target": copy.deepcopy(job.get("target")), "ranking": ranking, "candidates": candidates, "prediction_units": "GPa",
                "selection": copy.deepcopy(job.get("selection")), "allowed_actions": actions,
                "candidate_confirmation": candidate_confirmation_status(job),
                "comparison_facts": _candidate_comparison_facts(candidates,
                    (job.get('candidate_set') or {}).get('recommended_candidate_id'), metric=ranking['metric']),
                "inverse_summary": {key: copy.deepcopy(inverse.get(key)) for key in
                    ('status', 'algorithm', 'diagnostics', 'elapsed_ms', 'failure')},
                "candidate_interpretation": {
                    'origin': 'These candidates are newly generated structures. The DATABASE/CSV source belongs to the TARGET reference, not to the generated candidates.',
                    'prediction': 'Stiffness is predicted by the forward surrogate, not measured or FEA-validated.',
                    'nmse': 'sum((predicted stiffness - target stiffness)^2) / sum(target stiffness^2). This is target-matching error, NOT accuracy against physical truth.',
                    'nmse_example': 'NMSE ratio 0.0025 = NMSE 0.25% = relative L2 norm error 5%; it does NOT mean accuracy 99.75%.',
                    'ranking': ('Use raw NMSE rank as recorded; ties preserve source output order. Recommend only eligible candidates.' if ranking['metric'] == 'nmse' else 'Use historical P-10 rank/score as recorded. NMSE is auxiliary for this Job.'),
                    'delta': 'predicted minus target in GPa. A negative delta means below target, not negative stiffness.',
                    'density': 'Lower relative density means lower material fraction, not proven strength or fatigue safety. Density is not a hard generation constraint.',
                },
                "mesh": {k: copy.deepcopy(mesh.get(k)) for k in ("original_filename", "precheck_status", "design_domain", "binding_sha256", "original_upload")},
                "failure": copy.deepcopy(job.get("failure")), "runtime_permitted": bool(permitted),
                "non_claims": ["NOT_MECHANICAL_PASS", "NOT_FEA_VALIDATION", "NOT_FULL_DUAL_SPACE_FUSION"]}

    async def _prepare(self, args):
        epoch = self.epoch
        try:
            _closed(args, {"action", "job_ref", "candidate_id", "proposal_ref"}, {"action"})
        except ChatToolError as exc:
            raise ChatActionArgumentError('The assistant action arguments are incomplete or unsupported. No confirmation was prepared.') from exc
        action = args["action"]
        job = None
        target = None
        source_ref = None
        if action == "create_job":
            _closed(args, {"action", "proposal_ref"}, {"action", "proposal_ref"})
            proposal = self.proposal
            if proposal is None or args["proposal_ref"] != proposal["proposal_ref"]:
                raise ChatToolError("目标提案引用无效。")
            source_ref = proposal["result_ref"]
            cache = self._record_cache(source_ref)
            await self._check_source(cache)
            target = proposal["target"]
            if self.current_job_id():
                job = await self._job({})
                if job["status"] in {"INVERSE_RUNNING", "ADAPTER_RUNNING", "CONFORMAL_RUNNING", "PACKAGE_VERIFYING"}:
                    raise ChatWorkflowStateError("ACTION_STATE_NOT_ALLOWED", "The current Job is running; its target has not been replaced.", job["status"])
        else:
            try:
                _closed(args, {"action", "job_ref"} | ({"candidate_id"} if action == "select_candidate" else set()), {"action", "job_ref"})
            except ChatToolError as exc:
                raise ChatActionArgumentError('The assistant did not supply valid internal action arguments. No confirmation was prepared; the target does not need to be changed.') from exc
            job = await self._job({})
            context = self.job_context
            if context is None or args["job_ref"] != context["job_ref"] or context["job_sha256"] != canonical_sha256(job):
                raise ChatWorkflowStateError('JOB_CONTEXT_STALE', 'The Job reference or revision has changed.', job['status'])
            if action not in self._job_view(job)["allowed_actions"]:
                raise ChatWorkflowStateError('ACTION_STATE_NOT_ALLOWED', 'The current Job does not permit this action.', job['status'])
            if action == "select_candidate" and not any(c["candidate_id"] == args.get("candidate_id") and c["eligible"]
                    for c in job["candidate_set"]["candidates"]):
                raise ChatWorkflowStateError('ACTION_STATE_NOT_ALLOWED', 'This candidate is not eligible in the current Job.', job['status'])
        signature = canonical_sha256({"args": args, "job": job, "target": target, "source_ref": source_ref})
        self._check_epoch(epoch)
        for ref, record in self.pending.items():
            if record["signature"] == signature and record["state"] == "issued" and self.clock() - record["issued_at"] < TTL_SECONDS:
                return self._confirmation(ref, record)
        ref = secrets.token_hex(24)
        record = {"action": action, "job": copy.deepcopy(job), "target": copy.deepcopy(target),
                  "candidate_id": args.get("candidate_id"), "result_ref": source_ref,
                  "current_job_id": self.current_job_id(), "issued_at": self.clock(), "state": "issued", "signature": signature}
        self.pending[ref] = record
        record["issued_run_id"] = (CHAT_DISPATCH.get() or {}).get("run_id")
        self.case.trace("confirmation_issued", run_id=record["issued_run_id"], action=action,
                        job_id=record["current_job_id"], revision=job.get("revision") if job else None)
        return self._confirmation(ref, record)

    def _confirmation(self, ref, record):
        return {"status": "awaiting_confirmation", "confirmation_ref": ref, "action": record["action"],
                "target": copy.deepcopy(record["target"]), "proposal_ref": self.proposal["proposal_ref"] if record["target"] else None,
                "job_id": record["current_job_id"], "candidate_id": record["candidate_id"],
                "message": "请通过界面确认；本工具未创建Job、未选择候选、未运行算法。"}

    async def consume_action(self, ref):
        """Trusted UI only. There is deliberately no execution tool or authorization argument."""
        pending = self.pending.get(ref) if isinstance(ref, str) else None
        origin = pending.get("issued_run_id") if pending else None
        self.case.trace("confirmation_requested", run_id=origin, authorization_source="trusted_ui")
        try:
            if self.case.exhausted:
                raise ChatBudgetExceeded("整例调度预算已耗尽，未执行本次确认。")
            record = await self._consume_action(ref)
        except BaseException as exc:
            self.case.trace("confirmation_rejected", run_id=origin, error_type=type(exc).__name__)
            raise
        self.case.trace("confirmation_consumed", run_id=origin, action=record["action"],
                        issued_run_id=record["issued_run_id"], job_id=record["current_job_id"],
                        revision=record["job"].get("revision") if record["job"] else None)
        return record

    async def _consume_action(self, ref):
        async with self.lock:
            epoch = self.epoch
            record = self.pending.get(ref) if isinstance(ref, str) else None
            if (record is None or record["state"] != "issued" or self.clock() - record["issued_at"] >= TTL_SECONDS
                    or record["current_job_id"] != self.current_job_id()):
                raise ChatToolError("确认已过期、已消费或当前任务已变化。")
            record["state"] = "consumed"  # Any uncertain confirmation must not be retried.
            if record["result_ref"]:
                cache = self._record_cache(record["result_ref"])
                await self._check_source(cache)
            if record["job"] is not None:
                current = await self._job({})
                if canonical_sha256(current) != canonical_sha256(record["job"]):
                    raise ChatToolError("确认所绑定的任务revision或输入身份已变化。")
            self._check_epoch(epoch)
            if self.case.exhausted or self.pending.get(ref) is not record:
                raise ChatToolError("确认在身份复核期间失效或整例预算已耗尽；未执行该动作。")
            return copy.deepcopy(record)

    async def execute(self, name, args, *, run_id=None, automatic=False, read_turn=None):
        self.case.reserve("tool_calls", run_id=run_id)
        call_id = secrets.token_hex(12)
        self.case.trace("tool_request", run_id=run_id, call_id=call_id, name=name, automatic=automatic)
        context_token = CHAT_DISPATCH.set({"run_id": run_id, "call_id": call_id})
        epoch = self.epoch
        try:
            if read_turn is not None and not isinstance(read_turn, ChatReadTurn):
                raise ChatToolError("本轮查询证据上下文无效。")
            result = await self._execute(name, args, read_turn=read_turn)
        except BaseException as exc:
            if isinstance(read_turn, ChatReadTurn) and read_turn.state["status"] == "pending":
                read_turn.state = {**read_turn.state, "status": "error"}
                if epoch == self.epoch and (read_turn.context_identity is None or read_turn.context_identity == self.planning_identity()):
                    self.last_database_read = copy.deepcopy(read_turn.state)
            self.case.trace("tool_error", run_id=run_id, call_id=call_id, name=name, error_type=type(exc).__name__)
            raise
        finally:
            CHAT_DISPATCH.reset(context_token)
        self.case.trace("tool_result", run_id=run_id, call_id=call_id, name=name, result=result)
        return result

    async def _execute(self, name, args, *, read_turn=None):
        if not isinstance(name, str) or name not in TOOL_SPECS:
            raise ChatToolError("不支持的工具；不能调用任意代码或运行函数。")
        async with self.lock:
            epoch = self.epoch
            if read_turn is not None:
                read_turn.check_tool(name)
                if read_turn.context_identity is not None and read_turn.context_identity != self.planning_identity():
                    raise ChatToolError("执行前规划上下文已变化；尚未读取数据库。")
            if name in {"describe_database", "query_records", "count_records"}:
                if name == "describe_database":
                    _closed(args, set())
                    request, state = {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": "overview"}, None
                else:
                    request, state = self._query_request("records" if name == "query_records" else "count", args)
                turn = read_turn if read_turn is not None else ChatReadTurn()
                turn.prepare(request)
                units = {c.get("unit") for c in (state or {}).get("conditions", [])}
                turn.display.update(request=copy.deepcopy(request), inherited=args.get("inherit") is True,
                                    from_gpa="GPa" in units, from_percent="%" in units)
                query = await call_read(self.query_service)
                self._check_epoch(epoch)
                await self._check_planned_read(turn, query)
                turn.display["submitted"] = True
                try:
                    result = await call_read(query.execute_request, copy.deepcopy(request))
                except Exception:
                    turn.display["outcome"] = "read_failed"
                    raise
                self._check_epoch(epoch)
                await self._check_planned_read(turn, query)
                turn.display["outcome"] = "unverified_return"
                if not isinstance(result, dict) or result.get("status") not in {"ok", "empty", "error"}:
                    raise ChatToolError("数据库返回的结果合同无效。")
                result = copy.deepcopy(result)
                turn.observe(request, result, query)
                turn.display["outcome"] = result["status"]
                self.last_database_read = copy.deepcopy(turn.state)
                if result["status"] != "error":
                    if turn.requirement is not None:
                        turn.result_provenance = {key: result["provenance"][key] for key in
                                                  ("source_sha256", "manifest_sha256", "projection_sha256")}
                    if state is not None:
                        self.last_query = state
                    if name == "query_records":
                        result["result_ref"] = self.register_records(result)
                        turn.result_rows_ref = result["result_ref"]
                return result
            if name == "prepare_design_target":
                _closed(args, {"result_ref", "position", "sample_id"}, {"result_ref"})
                if ("position" in args) == ("sample_id" in args):
                    raise ChatToolError("请选择展示序号或sample_id之一。")
                cache = self._record_cache(args["result_ref"])
                await self._check_source(cache)
                self._check_epoch(epoch)
                rows = cache["result"]["data"]["rows"]
                selector = args.get("position", args.get("sample_id"))
                if type(selector) is not int:
                    raise ChatToolError("记录序号或sample_id必须是整数。")
                row = rows[selector - 1] if "position" in args and 1 <= selector <= len(rows) else next(
                    (r for r in rows if "sample_id" in args and r["sample_id"] == selector), None)
                if row is None:
                    raise ChatToolError("该记录不在这组实际结果中。")
                origin = cache["result"]["provenance"]
                target = target_from_database_record(row, source_sha256=origin["source_sha256"], query_identity=origin["query_identity"])
                self.retire_target_proposals()
                self.proposal = {"proposal_ref": secrets.token_hex(24), "result_ref": args["result_ref"], "target": target}
                result = await self._prepare({"action": "create_job", "proposal_ref": self.proposal["proposal_ref"]})
            elif name == "get_job_context":
                job = await self._job(args)
                self._check_epoch(epoch)
                result = self._job_view(job)
                result.update(job_ref=secrets.token_hex(24), job_sha256=canonical_sha256(job))
                self.job_context = copy.deepcopy(result)
                self._remember_job_evidence(job)
            elif name == "prepare_workflow_action":
                result = await self._prepare(args)
            else:
                job = await self._job(args)
                self._remember_job_evidence(job)
                if job['status'] != 'VIEWER_READY':
                    return {'status': 'error', 'job_id': job['job_id'], 'job_status': job['status'],
                            'error': {'code': 'RESULT_NOT_READY', 'stage': 'result_read',
                                      'message': 'The final geometry package is not recorded as ready.',
                                      'next_step': 'Read the current Job and its candidate or computation stage.'}}
                try:
                    result = await call_read(self.read_result, job["job_id"])
                except Exception as exc:
                    from .artifacts import ProductFileMissingError, ProductFileIdentityError
                    if isinstance(exc, (ProductFileMissingError, FileNotFoundError)):
                        code, message = 'RESULT_FILE_MISSING', 'A recorded result file is missing.'
                    elif isinstance(exc, ProductFileIdentityError):
                        code, message = 'RESULT_IDENTITY_MISMATCH', 'The result files do not match their recorded identities.'
                    else:
                        code, message = 'RESULT_READ_FAILED', 'The saved result package could not be verified or read.'
                    result = {'status': 'error', 'job_id': job['job_id'], 'job_status': job['status'],
                              'error': {'code': code, 'stage': 'result_read', 'message': message,
                                        'error_type': type(exc).__name__,
                                        'next_step': 'Inspect the recorded Job and result files. Do not regenerate or repair them automatically.'}}
                current = await self._job(args)
                if canonical_sha256(job) != canonical_sha256(current):
                    raise ChatToolError("结果读取期间任务已变化。")
            self._check_epoch(epoch)
            return result
