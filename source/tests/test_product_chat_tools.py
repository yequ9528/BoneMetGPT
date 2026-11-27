import asyncio
import copy
import unittest

from product_workflow.chat_tools import ChatTools, ChatToolError, TOOL_SPECS
from product_workflow.contracts import STIFFNESS_NAMES, canonical_sha256
from product_workflow import chat_tools as tools_module
from product_workflow.formal_150w_query import validate_database_request


class SyntheticQuery:
    source_sha256 = "A" * 64
    manifest_sha256 = "B" * 64
    projection_sha256 = "C" * 64

    def __init__(self):
        self.requests = []
        self.rows = [dict(sample_id=i, relative_density=.2, diameter=.1,
                          **{n: .01 for n in STIFFNESS_NAMES}) for i in (17, 42, 93)]

    def overview(self):
        return {"fields": [{"name": name, "unit": "dimensionless_C_over_Eb" if name in STIFFNESS_NAMES else
                            "integer_identifier" if name == "sample_id" else "dimensionless"}
                           for name in ("sample_id", "relative_density", "diameter", *STIFFNESS_NAMES)],
                "scanned_rows": 3, "eligible_rows": 3, "statistics_time": "synthetic", "eligibility_policy": {}}

    def execute_request(self, request):
        request = validate_database_request(request)
        self.requests.append(request)
        kind = request["kind"]
        return {"schema_version": "latticegpt_database_read_result_v1", "kind": kind, "status": "ok", "normalized_request": request,
                "data": self.overview() if kind == "overview" else {"matched_count": 3} if kind == "count"
                else {"rows": copy.deepcopy(self.rows[:request["limit"]]), "returned_count": min(3, request["limit"])},
                "provenance": {"source_sha256": self.source_sha256, "manifest_sha256": self.manifest_sha256,
                               "projection_sha256": self.projection_sha256, "query_identity": canonical_sha256(request),
                               "read_at": "2026-10-18T00:00:00+00:00"},
                "non_claims": ["NOT_FEA_VALIDATION"]}


class ChatToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_csv_context_changes_preserve_existing_job_actions_records_and_lock(self):
        setter = getattr(self.tools, "set_pending_csv_target", None)
        self.assertIsNotNone(setter, "CSV selection needs an independent read-only context setter")
        self.tools.job_context = {"job_id": "job-existing", "status": "TARGET_PROPOSED", "revision": 4}
        records = await self.records()
        self.tools.pending["existing-ref"] = {"state": "issued", "action": "confirm_target_and_generate"}
        job_before, pending_before = copy.deepcopy(self.tools.job_context), copy.deepcopy(self.tools.pending)
        epoch, lock = self.tools.epoch, self.tools.lock
        summary = {"filename": "target.csv", "sha256": "D" * 64, "record_count": 3,
                   "valid_count": 2, "invalid_count": 1, "record_index": 3,
                   "physical_9d_gpa": {name: 11. for name in STIFFNESS_NAMES}, "state": "PROPOSED"}
        await lock.acquire()
        try:
            setter(summary)
            self.assertEqual(self.tools.snapshot()["csv_context_version"], 1)
            setter(copy.deepcopy(summary))
            self.assertEqual(self.tools.snapshot()["csv_context_version"], 1)
            summary["record_index"] = 1
            self.assertEqual(self.tools.snapshot()["pending_csv_target"]["record_index"], 3)
            setter(summary)
            self.assertEqual(self.tools.snapshot()["csv_context_version"], 2)
            snapshot = self.tools.snapshot()
            snapshot["pending_csv_target"]["physical_9d_gpa"]["C11"] = 99
            self.assertEqual(self.tools.snapshot()["pending_csv_target"]["physical_9d_gpa"]["C11"], 11.)
            self.assertIs(self.tools.lock, lock)
            self.assertTrue(lock.locked())
        finally:
            lock.release()
        self.assertEqual(self.tools.epoch, epoch)
        self.assertEqual(self.tools.job_context, job_before)
        self.assertEqual(self.tools.pending, pending_before)
        self.assertEqual(self.tools.snapshot()["records"]["result_ref"], records["result_ref"])
        setter(None)
        self.assertEqual(self.tools.snapshot()["csv_context_version"], 3)
        self.assertIsNone(self.tools.snapshot()["pending_csv_target"])
        setter(None)
        self.assertEqual(self.tools.snapshot()["csv_context_version"], 3)

    async def test_frozen_context_is_rechecked_after_async_factory_and_read(self):
        for stage in ('factory', 'read'):
            self.tools.invalidate()
            self.query.requests.clear()
            turn = self.tools.bind_read_plan('records', {'limit': 1}, self.tools.planning_identity())
            entered, release = asyncio.Event(), asyncio.Event()
            original = self.query.execute_request
            async def factory():
                if stage == 'factory':
                    entered.set(); await release.wait()
                return self.query
            async def read(request):
                entered.set(); await release.wait()
                return original(request)
            self.tools.query_service = factory
            if stage == 'read': self.query.execute_request = read
            running = asyncio.create_task(self.tools.execute('query_records', {'limit': 1}, read_turn=turn))
            await entered.wait()
            self.tools.last_query = {'conditions': [], 'order_by': [], 'limit': 2}
            self.tools.last_database_read = {'status': 'not_queried', 'context': 'new'}
            release.set()
            with self.assertRaises(ChatToolError): await running
            self.assertEqual(len(self.query.requests), 0 if stage == 'factory' else 1)
            self.assertIsNone(self.tools.records)
            self.assertEqual(self.tools.last_database_read, {'status': 'not_queried', 'context': 'new'})
            self.query.execute_request = original

    async def test_frozen_plan_rejects_context_change_while_waiting_for_tool_lock(self):
        binder = getattr(self.tools, 'bind_read_plan', None)
        self.assertIsNotNone(binder, 'tools must bind the planning snapshot before database admission')
        turn = binder('records', {'inherit': False, 'limit': 1}, self.tools.planning_identity())
        await self.tools.lock.acquire()
        try:
            task = asyncio.create_task(self.tools.execute('query_records', {'limit': 1}, read_turn=turn))
            await asyncio.sleep(0)
            self.tools.last_query = {'conditions': [], 'order_by': [], 'limit': 5}
        finally:
            self.tools.lock.release()
        with self.assertRaises(ChatToolError):
            await task
        self.assertEqual(self.query.requests, [])
        self.assertIsNone(self.tools.records)

    def read_turn(self, requirement):
        turn_type = getattr(tools_module, "ChatReadTurn", None)
        self.assertIsNotNone(turn_type, "production tools lack a per-turn trusted read contract")
        return turn_type(requirement)

    async def test_read_turn_normalizes_request_and_blocks_repeat_before_io(self):
        requirement = {"kind": "records", "request": {"schema_version": "latticegpt_database_read_request_v1",
                       "kind": "records", "filters": [], "order_by": [], "limit": 1}}
        turn = self.read_turn(requirement)
        requirement["request"]["limit"] = 5
        result = await self.tools.execute("query_records", {"conditions": [], "limit": 1}, read_turn=turn)
        self.assertEqual(result["data"]["returned_count"], 1)
        self.assertEqual(turn.state["status"], "ok")
        self.assertTrue(turn.satisfied)
        with self.assertRaises(ChatToolError):
            await self.tools.execute("query_records", {"conditions": [], "limit": 1}, read_turn=turn)
        self.assertEqual(len(self.query.requests), 1)

    async def test_forged_result_identity_is_rejected_before_cache(self):
        original = self.query.execute_request
        for mutation in ("kind", "request", "query_identity", "source_sha256", "schema_version", "row_count"):
            def altered(request, mutation=mutation):
                result = original(request)
                if mutation == "kind": result["kind"] = "count"
                elif mutation == "request": result["normalized_request"]["limit"] = 2
                elif mutation in {"query_identity", "source_sha256"}: result["provenance"][mutation] = "F" * 64
                elif mutation == "schema_version": result["schema_version"] = "invented"
                else: result["data"]["returned_count"] = 99
                return result
            self.query.execute_request = altered
            turn = self.read_turn({"kind": "records"})
            with self.assertRaises(ChatToolError):
                await self.tools.execute("query_records", {"conditions": [], "limit": 1}, read_turn=turn)
            self.assertFalse(turn.satisfied)
            self.assertIsNone(self.tools.snapshot()["records"])

    async def test_empty_and_error_remain_real_terminal_results_without_retry(self):
        original = self.query.execute_request
        for status in ("empty", "error"):
            def terminal(request, status=status):
                result = original(request)
                result.update(status=status, data={"rows": [], "returned_count": 0} if status == "empty" else None)
                if status == "error":
                    result["provenance"] = {"source_sha256": None, "manifest_sha256": None, "projection_sha256": None}
                    result["error"] = {"code": "BINDING_MISMATCH"}
                return result
            self.query.execute_request = terminal
            turn = self.read_turn({"kind": "records"})
            result = await self.tools.execute("query_records", {"conditions": []}, read_turn=turn)
            self.assertEqual(result["status"], status)
            self.assertEqual(turn.state["status"], status)
            self.assertFalse(turn.satisfied)
            with self.assertRaises(ChatToolError):
                await self.tools.execute("query_records", {"conditions": []}, read_turn=turn)
        self.assertEqual(len(self.query.requests), 2)

    async def test_last_database_state_tracks_overview_and_resets_on_invalidation(self):
        self.assertEqual(self.tools.snapshot()["last_database_read"]["status"], "not_queried")
        await self.tools.execute("describe_database", {})
        snapshot = self.tools.snapshot()
        self.assertEqual(snapshot["last_database_read"]["kind"], "overview")
        self.assertEqual(snapshot["last_database_read"]["status"], "ok")
        self.assertIsNone(snapshot["records"])
        self.tools.invalidate()
        self.assertEqual(self.tools.snapshot()["last_database_read"]["status"], "not_queried")

    async def test_invalidation_during_required_query_does_not_restore_old_read_state(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def factory():
            entered.set()
            await release.wait()
            return self.query
        self.tools.query_service = factory
        turn = self.read_turn({"kind": "records"})
        task = asyncio.create_task(self.tools.execute("query_records", {"limit": 1}, read_turn=turn))
        await entered.wait()
        self.tools.invalidate()
        release.set()
        with self.assertRaises(ChatToolError):
            await task
        self.assertEqual(self.query.requests, [])
        self.assertEqual(self.tools.snapshot()["last_database_read"], {"status": "not_queried"})
        self.assertFalse(turn.satisfied)

    async def test_incomplete_overview_is_not_successful_query_evidence(self):
        original = self.query.execute_request
        for data in ({}, {**self.query.overview(), "eligible_rows": 4},
                     {**self.query.overview(), "scanned_rows": True},
                     {**self.query.overview(), "fields": []},
                     {**self.query.overview(), "statistics_time": None},
                     {**self.query.overview(), "eligibility_policy": None}):
            def altered(request, data=data):
                result = original(request)
                result["data"] = data
                return result
            self.query.execute_request = altered
            turn = self.read_turn({"kind": "overview"})
            with self.assertRaises(ChatToolError):
                await self.tools.execute("describe_database", {}, read_turn=turn)
            self.assertFalse(turn.satisfied)

    def setUp(self):
        self.query = SyntheticQuery()
        self.current = None
        self.now = 10.
        self.tools = ChatTools(query_service=lambda: self.query, read_job=lambda _: self.fail("unexpected Job read"),
                               read_result=lambda _: self.fail("unexpected result read"),
                               current_job_id=lambda: self.current, session_id="session-A", clock=lambda: self.now)

    async def records(self, **kwargs):
        return await self.tools.execute("query_records", {"conditions": [], **kwargs})

    async def test_seven_closed_tools_and_no_execution_tool(self):
        self.assertEqual(set(TOOL_SPECS), {"describe_database", "query_records", "count_records", "prepare_design_target",
                                         "get_job_context", "prepare_workflow_action", "get_design_result"})
        for name, args in [("execute", {}), ("query_records", {"sql": "SELECT *"}),
                           ("prepare_workflow_action", {"authorized": True})]:
            with self.assertRaises(ChatToolError):
                await self.tools.execute(name, args)

    async def test_each_condition_keeps_its_unit_and_records_count_semantics(self):
        await self.records(conditions=[{"field": "relative_density", "op": "lt", "value": 30, "unit": "%"},
                                       {"field": "C11", "op": "gt", "value": 11, "unit": "GPa"},
                                       {"field": "C22", "op": "lt", "value": .2, "unit": "C/Eb"}],
                           order_by=[{"field": "C11", "direction": "desc"}], limit=2)
        request = self.query.requests[-1]
        self.assertEqual([f["value"] for f in request["filters"]], [.3, .1, .2])
        self.assertEqual(request["limit"], 2)
        result = await self.tools.execute("count_records", {"inherit": True})
        self.assertEqual(result["data"]["matched_count"], 3)
        self.assertNotIn("limit", self.query.requests[-1])
        self.assertEqual(self.query.requests[-1]["filters"], request["filters"])

    async def test_followup_changes_matching_condition_once_and_keeps_order_limit(self):
        await self.records(conditions=[{"field": "C11", "op": "gt", "value": 11, "unit": "GPa"}], limit=2)
        await self.tools.execute("query_records", {"inherit": True, "conditions": [{"field": "C11", "op": "gt", "value": 22}]})
        self.assertEqual(self.query.requests[-1]["filters"], [{"field": "C11", "op": "gt", "value": .2}])
        self.assertEqual(self.query.requests[-1]["limit"], 2)

    async def test_second_record_is_not_sample_id_two_and_count_keeps_reference(self):
        result = await self.records()
        await self.tools.execute("count_records", {"inherit": True})
        proposal = await self.tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 2})
        self.assertEqual(proposal["target"]["source"]["sample_id"], 42)
        self.assertEqual(proposal["target"]["physical_9d_gpa"]["C11"], 1.1)
        self.assertIsNone(self.current)
        action = await self.tools.consume_action(proposal["confirmation_ref"])
        self.assertEqual(action["action"], "create_job")
        with self.assertRaises(ChatToolError):
            await self.tools.consume_action(proposal["confirmation_ref"])

    async def test_new_records_invalidate_old_reference_and_proposal(self):
        first = await self.records()
        proposal = await self.tools.execute("prepare_design_target", {"result_ref": first["result_ref"], "sample_id": 17})
        await self.records()
        with self.assertRaises(ChatToolError):
            await self.tools.execute("prepare_design_target", {"result_ref": first["result_ref"], "position": 1})
        with self.assertRaises(ChatToolError):
            await self.tools.consume_action(proposal["confirmation_ref"])

    async def test_new_proposal_retires_previous_confirmation(self):
        result = await self.records()
        old = await self.tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 1})
        new = await self.tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 2})
        with self.assertRaises(ChatToolError):
            await self.tools.consume_action(old["confirmation_ref"])
        self.assertEqual((await self.tools.consume_action(new["confirmation_ref"]))["target"]["source"]["sample_id"], 42)

    async def test_expiry_source_drift_and_session_invalidation_reject(self):
        for reason in ("expiry", "source", "session"):
            result = await self.records()
            if reason == "expiry":
                self.now += 901
            elif reason == "source":
                self.query.source_sha256 = "F" * 64
            else:
                self.tools.invalidate()
            with self.assertRaises(ChatToolError):
                await self.tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 1})

    async def test_invalid_units_values_limits_and_unknown_parameters_fail_before_query(self):
        for kwargs in ({"conditions": [{"field": "C11", "op": "gt", "value": 1}]},
                       {"conditions": [{"field": "diameter", "op": "gt", "value": 1, "unit": "mm"}]},
                       {"limit": 6}, {"limit": True},
                       {"conditions": [{"field": "sample_id", "op": "eq", "value": True}]},
                       {"conditions": [{"field": "relative_density", "op": "gt", "value": float("nan")}]},
                       {"conditions": [], "or": True}):
            with self.assertRaises(ChatToolError):
                await self.records(**kwargs)
        self.assertEqual(self.query.requests, [])

    async def test_same_turn_and_conditions_are_never_silently_replaced(self):
        await self.records(conditions=[{"field": "sample_id", "op": "eq", "value": 17},
                                       {"field": "sample_id", "op": "eq", "value": 42}])
        self.assertEqual(self.query.requests[-1]["filters"], [{"field": "sample_id", "op": "eq", "value": 17},
                                                           {"field": "sample_id", "op": "eq", "value": 42}])

    async def test_cancellation_does_not_install_late_query_result(self):
        async def slow(_):
            await asyncio.sleep(.1)
            return self.query.execute_request({"schema_version": "latticegpt_database_read_request_v1", "kind": "records",
                                               "filters": [], "order_by": [], "limit": 1})
        self.query.execute_request = slow
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.records(), .01)
        self.assertIsNone(self.tools.snapshot()["records"])
