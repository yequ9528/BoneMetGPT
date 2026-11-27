import asyncio
import json
import unittest
from product_workflow.chat_tools import ChatCase
import csv
import hashlib
import os
import uuid
from pathlib import Path

from product_workflow.chat_agent import ChatAgent
from tests.test_product_chat_tools import SyntheticQuery
from product_workflow.chat_tools import ChatTools
from product_workflow.formal_150w_query import Formal150wQueryService, build_projection
from product_workflow.contracts import STIFFNESS_NAMES


class AutomaticQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(os.environ['LATTICEGPT_TEST_RUNTIME_ROOT']) / ('auto-query-' + uuid.uuid4().hex)
        root.mkdir(parents=True)
        source = root / 'synthetic.csv'
        rows = [dict(sample_id=i, relative_density=.2, diameter=.1, is_singular=0, is_pd_full=1,
                     has_periodic_mismatch=0, is_degenerate=0, **{n: v for n in STIFFNESS_NAMES})
                for i, v in ((17, .1), (42, .3), (93, .2))]
        with source.open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        sha = hashlib.sha256(source.read_bytes()).hexdigest().upper()
        build_projection(source, root / 'synthetic.sqlite', root / 'projection.json', expected_source_sha256=sha)
        self.query = Formal150wQueryService(root / 'synthetic.sqlite', root / 'projection.json', expected_source_sha256=sha)
        self.reads = []
        execute = self.query.execute_request
        def recording(request):
            self.reads.append(request)
            return execute(request)
        self.query.execute_request = recording
        self.tools = ChatTools(query_service=lambda: self.query, read_job=lambda _: None, read_result=lambda _: None,
                               current_job_id=lambda: None, session_id='auto')

    async def test_maximum_plan_executes_real_synthetic_sort_once_and_binds_evidence(self):
        calls, shown = [], []
        def model(messages):
            calls.append(messages)
            if len(calls) == 1:
                self.assertEqual(json.loads(messages[-1]['content']).get('phase'), 'planning')
                return {'kind': 'plan', 'task': 'records', 'arguments': {
                    'conditions': [], 'order_by': [{'field': 'C11', 'direction': 'desc'}], 'limit': 1}}
            self.assertIn('0.3', messages[-1]['content'])
            return {'kind': 'answer', 'text': '已读取最大C11对应记录。'}
        async def show(name, result): shown.append(result)
        result = await ChatAgent(model, self.tools, on_tool=show).run('找C11最大的数据')
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result.get('answer_basis'), 'database')
        self.assertEqual(self.reads, [{'schema_version': 'latticegpt_database_read_request_v1', 'kind': 'records',
            'filters': [], 'order_by': [{'field': 'C11', 'direction': 'desc'}, {'field': 'sample_id', 'direction': 'asc'}], 'limit': 1}])
        self.assertEqual(shown[0]['data']['rows'][0]['sample_id'], 42)
        self.assertEqual(self.tools.case.used, {'runs': 1, 'model_calls': 1, 'tool_calls': 1})

    async def test_real_local_plan_with_empty_arguments_continues_to_answer(self):
        # Exact first response observed from local Qwen in the v41 UI acceptance.
        observed_plan = {"kind": "plan", "task": "conversation", "arguments": {}}
        calls = []
        def model(messages):
            calls.append(messages)
            if len(calls) == 1:
                return dict(observed_plan)
            context = json.loads(messages[-1]["content"])
            self.assertEqual(context["task_plan"], {"kind": "plan", "task": "conversation"})
            return {"kind": "answer", "text": "Synthetic explanation for offline regression."}
        result = await ChatAgent(model, self.tools).run("Explain a unit cell only.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer_basis"], "conversation")
        self.assertEqual(result["query_state"], {"status": "not_queried"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.tools.case.used["tool_calls"], 0)

    async def test_non_read_plan_does_not_discard_meaningful_or_unknown_fields(self):
        plans = [
            {"kind": "plan", "task": "conversation", "arguments": {"job_id": "job-other"}},
            {"kind": "plan", "task": "conversation", "arguments": []},
            {"kind": "plan", "task": "conversation", "arguments": None},
            {"kind": "plan", "task": "workflow", "arguments": {"confirm": True}},
            {"kind": "plan", "task": "workflow", "arguments": {}, "authorized": True},
        ]
        for plan in plans:
            with self.subTest(plan=plan):
                calls = []
                def model(_):
                    calls.append(None)
                    return plan
                result = await ChatAgent(model, self.tools).run("Explain only.")
                self.assertEqual(result["status"], "error")
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.reads, [])
                self.assertEqual(self.tools.pending, {})

    async def test_free_answer_or_tool_without_planning_stops_before_io(self):
        for response in ({'kind': 'answer', 'text': '总数999'},
                         {'kind': 'tool', 'name': 'count_records', 'arguments': {}},
                         {'kind': 'plan', 'task': 'records', 'arguments': {}, 'authorized': True}):
            result = await ChatAgent(lambda _: response, self.tools).run('C11最大的数据')
            self.assertEqual(result['status'], 'error')
            self.assertEqual(self.reads, [])
            self.assertIsNone(self.tools.records)

    async def test_explanation_cannot_invoke_database_or_claim_query_evidence(self):
        for response in ({'kind': 'answer', 'text': '数据库总共999条'},
                         {'kind': 'tool', 'name': 'count_records', 'arguments': {}}):
            calls = []
            def model(messages):
                calls.append(messages)
                return {'kind': 'plan', 'task': 'conversation'} if len(calls) == 1 else response
            result = await ChatAgent(model, self.tools).run('解释数据库的用途')
            self.assertEqual(self.reads, [])
            if response['kind'] == 'answer':
                self.assertEqual(result.get('answer_basis'), 'conversation')
                self.assertEqual(result.get('query_state'), {'status': 'not_queried'})
            else:
                self.assertEqual(result['status'], 'error')

    async def test_planned_read_cannot_requery_count_or_prepare_computation(self):
        for name in ('count_records', 'query_records', 'prepare_workflow_action'):
            before = len(self.reads)
            calls = []
            def model(_):
                calls.append(None)
                return ({'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}, 'explain_result': True}
                        if len(calls) == 1 else {'kind': 'tool', 'name': name, 'arguments': {}})
            result = await ChatAgent(model, self.tools).run('第一条')
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(self.reads), before + 1)
            self.assertEqual(self.tools.pending, {})

    async def test_context_changed_during_planning_stops_before_read(self):
        for mutation in ('invalidate', 'query', 'records'):
            def model(_):
                if mutation == 'invalidate': self.tools.invalidate()
                elif mutation == 'query': self.tools.last_query = {'conditions': [], 'order_by': [], 'limit': 2}
                else: self.tools.records = {'result_ref': 'changed', 'result': {}}
                return {'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}}
            result = await ChatAgent(model, self.tools).run('按刚才的条件查询')
            self.assertEqual(result['status'], 'error')
            self.assertEqual(self.reads, [])
            self.tools.invalidate()

    async def test_mixed_units_and_inheritance_are_normalized_per_condition_once(self):
        plans = [
            {'kind': 'plan', 'task': 'records', 'arguments': {'conditions': [
                {'field': 'C11', 'op': 'gte', 'value': 22, 'unit': 'GPa'},
                {'field': 'C22', 'op': 'lte', 'value': .3, 'unit': 'C/Eb'},
                {'field': 'relative_density', 'op': 'lt', 'value': 25, 'unit': '%'}], 'limit': 2}},
            {'kind': 'plan', 'task': 'count', 'arguments': {'inherit': True, 'conditions': [
                {'field': 'C11', 'op': 'gte', 'value': 33, 'unit': 'GPa'}]}},
            {'kind': 'plan', 'task': 'records', 'arguments': {'inherit': True}},
        ]
        def model(messages):
            return plans.pop(0) if json.loads(messages[-1]['content']).get('phase') == 'planning' else {'kind': 'answer', 'text': '实际结果'}
        agent = ChatAgent(model, self.tools)
        for text in ('找满足条件的两条', '其中把C11下限改成33GPa，多少条', '显示这些记录'):
            self.assertEqual((await agent.run(text))['status'], 'answered')
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.reads[0]['filters'], [
            {'field': 'C11', 'op': 'gte', 'value': .2}, {'field': 'C22', 'op': 'lte', 'value': .3},
            {'field': 'relative_density', 'op': 'lt', 'value': .25}])
        self.assertEqual(self.reads[1]['filters'][-1], {'field': 'C11', 'op': 'gte', 'value': .3})
        self.assertEqual(self.reads[2]['filters'], self.reads[1]['filters'])
        self.assertEqual(self.reads[2]['limit'], 2)

    async def test_missing_threshold_unit_and_unsupported_plan_stop_without_fallback(self):
        for args in ({'conditions': [{'field': 'C11', 'op': 'gt', 'value': 10}]},
                     {'conditions': [], 'sql': 'select * from samples'}, {'limit': 100}):
            result = await ChatAgent(lambda _: {'kind': 'plan', 'task': 'records', 'arguments': args}, self.tools).run('条件查询')
            self.assertEqual(result['status'], 'needs_clarification')
            self.assertEqual(self.reads, [])

    async def test_auto_empty_and_error_stop_before_explanation_without_retry(self):
        for status in ('empty', 'error'):
            calls = []
            execute = self.query.execute_request
            def terminal(request):
                result = execute(request)
                result.update(status=status, data={'rows': [], 'returned_count': 0} if status == 'empty' else None)
                return result
            self.query.execute_request = terminal
            def model(_):
                calls.append(None)
                return {'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}}
            result = await ChatAgent(model, self.tools).run('第一条')
            self.assertEqual(result['status'], status)
            self.assertEqual(len(calls), 1)
            self.query.execute_request = execute


class ChatAgentTests(unittest.IsolatedAsyncioTestCase):
    def csv_summary(self, index=1):
        return {"filename": "target.csv", "sha256": "D" * 64, "record_count": 2,
                "valid_count": 2, "invalid_count": 0, "record_index": index,
                "physical_9d_gpa": {name: float(index) for name in STIFFNESS_NAMES}, "state": "PROPOSED"}

    def set_csv(self, tools, summary):
        setter = getattr(tools, "set_pending_csv_target", None)
        self.assertIsNotNone(setter, "CSV row context must be independent of Job invalidation")
        setter(summary)

    async def test_csv_context_reaches_both_model_phases_without_extra_calls(self):
        tools, calls = self.tools(), []
        self.set_csv(tools, self.csv_summary(2))
        active_job_id = "job-" + "A" * 24
        tools.current_job_id = lambda: active_job_id
        tools.job_context = {"job_id": active_job_id, "status": "TARGET_PROPOSED", "revision": 7}
        def model(messages):
            context = json.loads(messages[-1]["content"])
            calls.append(context)
            self.assertEqual(context["context"]["pending_csv_target"]["record_index"], 2)
            self.assertEqual(context["context"]["job"]["job_id"], active_job_id)
            return ({"kind": "plan", "task": "conversation"} if len(calls) == 1
                    else {"kind": "answer", "text": "The selected CSV record is still a proposal."})
        result = await ChatAgent(model, tools).run("Explain the selected CSV record")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 2, "tool_calls": 0})

    async def test_csv_change_during_planning_or_execution_rejects_old_answer_and_history(self):
        for phase in ("planning", "execution"):
            tools, calls = self.tools(), []
            self.set_csv(tools, self.csv_summary())
            entered, release = asyncio.Event(), asyncio.Event()
            async def model(messages):
                context = json.loads(messages[-1]["content"])
                calls.append(context["phase"])
                if context["phase"] == phase:
                    entered.set()
                    await release.wait()
                return ({"kind": "plan", "task": "conversation"} if context["phase"] == "planning"
                        else {"kind": "answer", "text": "Old CSV row answer"})
            agent = ChatAgent(model, tools)
            running = asyncio.create_task(agent.run("Explain this target"))
            await entered.wait()
            self.set_csv(tools, self.csv_summary(2))
            release.set()
            result = await running
            self.assertEqual(result["status"], "error")
            self.assertEqual(agent.history, [])
            self.assertEqual(len(calls), 1 if phase == "planning" else 2)
            self.assertEqual(tools.case.used["tool_calls"], 0)
            self.assertNotIn("Old CSV row answer", result["text"])

    async def test_csv_change_during_tool_read_rejects_publication_and_further_model_calls(self):
        tools, query, calls, shown = self.tools(), SyntheticQuery(), [], []
        self.set_csv(tools, self.csv_summary())
        entered, release = asyncio.Event(), asyncio.Event()
        original = query.execute_request
        async def read(request):
            entered.set()
            await release.wait()
            return original(request)
        query.execute_request = read
        tools.query_service = lambda: query
        def model(messages):
            calls.append(messages)
            return {"kind": "tool", "name": "query_records", "arguments": {"limit": 1}}
        async def show(name, result):
            shown.append(result)
        agent = ChatAgent(model, tools, on_tool=show)
        running = asyncio.create_task(agent.run("Read first record", read_requirement={"kind": "records"}))
        await entered.wait()
        self.set_csv(tools, self.csv_summary(2))
        release.set()
        result = await running
        self.assertEqual(result["status"], "error")
        self.assertEqual((len(calls), len(query.requests), len(shown)), (1, 1, 0))
        self.assertEqual(agent.history, [])


    def planned(self, model):
        # Legacy fixed actions at the model boundary; new automatic tests above
        # supply the full planning protocol directly and check real SQLite I/O.
        saved = []
        async def callback(messages):
            from product_workflow.chat_tools import call_read
            context = json.loads(messages[-1]['content']) if messages[-1]['role'] == 'tool' else {}
            if context.get('phase') == 'planning':
                response = await call_read(model, messages)
                if response.get('kind') == 'tool':
                    kind = {'query_records': 'records', 'count_records': 'count', 'describe_database': 'overview'}.get(response.get('name'))
                    if kind:
                        return {'kind': 'plan', 'task': kind, 'arguments': response['arguments']}
                    saved[:] = [response]
                    return {'kind': 'plan', 'task': 'workflow'}
                if response.get('kind') == 'answer' and set(response) == {'kind', 'text'}:
                    saved[:] = [response]
                    return {'kind': 'plan', 'task': 'conversation'}
                return response
            return saved.pop() if saved else await call_read(model, messages)
        return callback

    def agent(self, model, tools, **kwargs):
        return ChatAgent(self.planned(model), tools, **kwargs)

    async def run_read(self, agent, text, requirement):
        try:
            return await agent.run(text, read_requirement=requirement)
        except TypeError as exc:
            if "read_requirement" in str(exc):
                self.fail("production Agent does not accept trusted read requirements")
            raise

    async def test_query_answer_without_this_turn_evidence_is_incomplete_and_not_history(self):
        tools = self.tools()
        agent = self.agent(lambda _: {"kind": "answer", "text": "查询失败或没有记录，已停止。"}, tools)
        with self.assertLogs("latticegpt.chat", level="INFO") as captured:
            result = await self.run_read(agent, "读取第一条", {"kind": "records"})
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("没有记录", result["text"])
        self.assertEqual(agent.history, [])
        self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 1, "tool_calls": 0})
        events = [json.loads(row.getMessage()) for row in captured.records]
        self.assertTrue(any(e["event"] == "answer_rejected" for e in events))

    async def test_required_read_ignores_old_cache_and_skips_automatic_job_read(self):
        tools = self.tools()
        await tools.execute("query_records", {"conditions": []})
        tools.current_job_id = lambda: "job-current"
        tools.read_job = lambda _: self.fail("readonly turn must not read a Job")
        before = tools.case.used["tool_calls"]
        agent = self.agent(lambda _: {"kind": "answer", "text": "旧记录可以当新查询"}, tools)
        result = await self.run_read(agent, "重新查询", {"kind": "records"})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(tools.case.used["tool_calls"], before)

    async def test_required_records_accepts_only_matching_real_result_without_explanation(self):
        query = SyntheticQuery()
        tools = self.tools()
        tools.query_service = lambda: query
        calls = []
        request = {"schema_version": "latticegpt_database_read_request_v1", "kind": "records", "filters": [],
                   "order_by": [{"field": "sample_id", "direction": "asc"}], "limit": 1}
        def model(messages):
            calls.append(messages)
            if len(calls) == 1:
                context = json.loads(messages[-1]["content"])
                self.assertEqual(context["query_state"]["status"], "not_queried")
                return {"kind": "tool", "name": "query_records", "arguments": {"conditions": [], "limit": 1}}
            self.assertIn('"returned_count": 1', messages[-1]["content"])
            return {"kind": "answer", "text": "已读取实际记录"}
        result = await self.run_read(self.agent(model, tools), "第一条", {"kind": "records", "request": request})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(calls), 1)
        self.assertEqual(query.requests, [request])

    async def test_wrong_kind_and_request_stop_before_query_service(self):
        for response in ({"kind": "tool", "name": "count_records", "arguments": {"conditions": []}},
                         {"kind": "tool", "name": "query_records", "arguments": {"conditions": [], "limit": 2}},
                         {"kind": "tool", "name": "prepare_design_target", "arguments": {}}):
            tools = self.tools()
            tools.query_service = lambda: self.fail("mismatched tool/request must not acquire database")
            request = {"schema_version": "latticegpt_database_read_request_v1", "kind": "records", "filters": [],
                       "order_by": [{"field": "sample_id", "direction": "asc"}], "limit": 1}
            result = await self.run_read(self.agent(lambda _: response, tools), "第一条", {"kind": "records", "request": request})
            self.assertEqual(result["status"], "needs_clarification")

    async def test_read_requirement_is_closed_and_clarification_is_not_success(self):
        for requirement in ({"kind": "records", "authorized": True}, {"kind": "execute"},
                            {"kind": "count", "request": {"schema_version": "latticegpt_database_read_request_v1", "kind": "overview"}}):
            tools = self.tools()
            result = await self.run_read(self.agent(lambda _: self.fail("invalid trusted requirement must stop before model"), tools), "查", requirement)
            self.assertEqual(result["status"], "error")
            self.assertEqual(tools.case.used["model_calls"], 0)
        result = await self.run_read(self.agent(lambda _: {"kind": "clarify", "text": "请明确单位"}, self.tools()), "范围？", {"kind": "records"})
        self.assertEqual(result["status"], "needs_clarification")

    async def test_required_read_real_empty_or_error_stops_without_another_model_call(self):
        for status in ("empty", "error"):
            tools, query, calls = self.tools(), SyntheticQuery(), []
            original = query.execute_request
            def terminal(request):
                result = original(request)
                result.update(status=status, data={"rows": [], "returned_count": 0} if status == "empty" else None)
                return result
            query.execute_request = terminal
            tools.query_service = lambda: query
            def model(_):
                calls.append(None)
                self.assertEqual(len(calls), 1, "terminal query must stop before another model call")
                return {"kind": "tool", "name": "query_records", "arguments": {"limit": 1}}
            result = await self.run_read(self.agent(model, tools), "读取第一条", {"kind": "records"})
            self.assertEqual(result["status"], status)
            self.assertEqual(len(query.requests), 1)
            self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 1, "tool_calls": 1})

    def tools(self):
        return ChatTools(query_service=SyntheticQuery, read_job=lambda _: None, read_result=lambda _: None,
                         current_job_id=lambda: None, session_id="test")

    async def test_case_run_budget_includes_receipt_resumption_and_stops_before_model(self):
        calls = []
        def model(messages):
            calls.append(messages)
            return {"kind": "answer", "text": "已读取"}
        tools = self.tools()
        tools.case = ChatCase(max_runs=10)
        agent = self.agent(model, tools)
        for index in range(10):
            result = await agent.run("解释", receipt={"action": "stage", "revision": index})
            self.assertEqual(result["status"], "answered")
        self.assertEqual((await agent.run("继续"))["status"], "budget_exhausted")
        self.assertEqual(len(calls), 10)

    async def test_case_model_budget_is_shared_between_runs(self):
        calls = []
        def model(messages):
            calls.append(messages)
            return ({"kind": "plan", "task": "conversation"} if json.loads(messages[-1]['content']).get('phase') == 'planning'
                    else {"kind": "answer", "text": "流程说明"})
        agent = ChatAgent(model, self.tools())
        agent.tools.case.limits['model_calls'] = 3
        self.assertEqual((await agent.run("解释"))["status"], "answered")
        self.assertEqual((await agent.run("继续查"))["status"], "budget_exhausted")
        self.assertEqual(len(calls), 3)
        self.assertEqual((await agent.run("再查"))["status"], "budget_exhausted")
        self.assertEqual(len(calls), 3)

    async def test_trace_links_dispatch_actual_results_and_final_status(self):
        calls = []
        def model(_):
            calls.append(None)
            return ({"kind": "tool", "name": "count_records", "arguments": {"conditions": []}}
                    if len(calls) == 1 else {"kind": "answer", "text": "有三条"})
        with self.assertLogs("latticegpt.chat", level="INFO") as captured:
            await self.agent(model, self.tools()).run("多少条")
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(len({event["case_id"] for event in events}), 1)
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertTrue({"run_started", "model_request", "model_response", "tool_request", "tool_result", "run_finished"}
                        <= {event["event"] for event in events})
        result = next(event for event in events if event["event"] == "tool_result")
        self.assertEqual(result["result"]["data"]["matched_count"], 3)
        self.assertEqual(events[-1]["status"], "answered")

    async def test_failed_model_attempt_consumes_case_quota_without_retry(self):
        calls = []
        def model(_):
            calls.append(None)
            raise RuntimeError("synthetic model failure")
        agent = self.agent(model, self.tools())
        agent.tools.case.limits["model_calls"] = 1
        self.assertEqual((await agent.run("解释"))["status"], "error")
        self.assertEqual((await agent.run("继续"))["status"], "budget_exhausted")
        self.assertEqual(len(calls), 1)

    async def test_tool_case_quota_stops_before_external_read_and_survives_new_agent(self):
        tools = self.tools()
        tools.case.limits["tool_calls"] = 1
        def model(messages):
            return ({"kind": "plan", "task": "overview", "arguments": {}}
                    if json.loads(messages[-1]['content']).get('phase') == 'planning'
                    else {"kind": "answer", "text": "实际说明"})
        first = await ChatAgent(model, tools, max_tool_calls=1).run("说明")
        self.assertEqual(first["status"], "answered")
        second = await ChatAgent(model, tools).run("再次说明")
        self.assertEqual(second["status"], "budget_exhausted")
        self.assertEqual(tools.case.used["tool_calls"], 1)

    async def test_confirmation_admission_rechecks_budget_after_async_identity_read(self):
        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        proposal = await tools.execute("prepare_design_target", {"result_ref": records["result_ref"], "position": 1})
        entered, release = asyncio.Event(), asyncio.Event()
        original = tools._check_source
        async def identity_read(cache):
            entered.set()
            await release.wait()
            await original(cache)
        tools._check_source = identity_read
        pending = asyncio.create_task(tools.consume_action(proposal["confirmation_ref"]))
        await entered.wait()
        agent = self.agent(lambda _: {"kind": "answer", "text": "解释"}, tools)
        tools.case.limits["runs"] = 1
        try:
            self.assertEqual((await agent.run("解释"))["status"], "answered")
            self.assertEqual((await agent.run("继续"))["status"], "budget_exhausted")
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await pending

    async def test_confirmation_trace_uses_issuing_run_not_concurrent_chat(self):
        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        agent = self.agent(lambda _: {"kind": "tool", "name": "prepare_design_target", "arguments": {
            "result_ref": records["result_ref"], "position": 1}}, tools)
        await agent.run("用第一条")
        ref, issued = next(iter(tools.pending.items()))
        origin = issued["issued_run_id"]
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting_model(_):
            entered.set()
            await release.wait()
            return {"kind": "answer", "text": "解释"}
        agent.model = waiting_model
        with self.assertLogs("latticegpt.chat", level="INFO") as captured:
            running = asyncio.create_task(agent.run("解释其他问题"))
            await entered.wait()
            try:
                await tools.consume_action(ref)
                tools.case.trace("external_ui_event")
            finally:
                release.set()
                await running
        events = [json.loads(record.getMessage()) for record in captured.records]
        consumed = next(event for event in events if event["event"] == "confirmation_consumed")
        self.assertEqual(consumed["run_id"], origin)
        self.assertIsNone(next(event for event in events if event["event"] == "other_event")["run_id"])

    async def test_program_delivers_actual_count_without_model_paraphrase(self):
        calls = []
        def model(messages):
            calls.append(messages)
            if len(calls) == 1:
                return {"kind": "tool", "name": "count_records", "arguments": {"conditions": []}}
            self.assertIn("matched_count", messages[-1]["content"])
            return {"kind": "answer", "text": "这些条件下有三条合成记录。"}
        result = await self.agent(model, self.tools()).run("一共有多少？")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["query_state"]["status"], "ok")

    async def test_halts_at_confirmation_and_never_invents_authorization(self):
        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        def model(_):
            return {"kind": "tool", "name": "prepare_design_target", "arguments": {"result_ref": records["result_ref"], "position": 2}}
        result = await self.agent(model, tools).run("用第二条")
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertNotIn("authorized", str(result))

    async def test_illegal_protocol_and_budget_fail_closed(self):
        for response in ({"kind": "execute", "authorized": True}, {"kind": "answer", "text": "x", "extra": 1}):
            result = await self.agent(lambda _: response, self.tools()).run("问题")
            self.assertEqual(result["status"], "error")
        result = await self.agent(lambda _: {"kind": "tool", "name": "describe_database", "arguments": {}},
                                 self.tools(), max_tool_calls=0).run("一直查")
        self.assertEqual(result["status"], "limit_reached")

    async def test_timeout_rejects_late_model_and_releases_busy(self):
        async def slow(_):
            await asyncio.sleep(.1)
            return {"kind": "tool", "name": "query_records", "arguments": {"conditions": []}}
        tools = self.tools()
        agent = self.agent(slow, tools, timeout_seconds=.01)
        self.assertEqual((await agent.run("查"))["status"], "timeout")
        await asyncio.sleep(.11)
        self.assertIsNone(tools.snapshot()["records"])
        agent.model = self.planned(lambda _: {"kind": "answer", "text": "已结束等待"})
        agent.timeout_seconds = 1
        self.assertEqual((await agent.run("继续"))["status"], "answered")

    async def test_timeout_during_confirmation_delivery_revokes_action(self):
        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        issued = []
        async def delivery(name, result):
            issued.append(result["confirmation_ref"])
            await asyncio.sleep(.2)
        model = lambda _: {"kind": "tool", "name": "prepare_design_target", "arguments": {"result_ref": records["result_ref"], "position": 1}}
        result = await self.agent(model, tools, on_tool=delivery, timeout_seconds=.1).run("用第一条")
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(issued)
        with self.assertRaises(ValueError):
            await tools.consume_action(issued[0])

    async def test_waiting_for_confirmation_retains_original_user_intent(self):
        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        agent = self.agent(lambda _: {"kind": "tool", "name": "prepare_design_target", "arguments": {
            "result_ref": records["result_ref"], "position": 2}}, tools)
        await agent.run("用第二条，之后比较候选的C11")
        self.assertIn("之后比较候选的C11", str(agent.history))

    async def test_failure_receipt_cannot_prepare_another_execution(self):
        tools = self.tools()
        response = {"kind": "tool", "name": "prepare_workflow_action", "arguments": {"action": "start_conformal", "job_ref": "invented"}}
        result = await self.agent(lambda _: response, tools).run("解释失败", receipt={"failed": True})
        self.assertEqual(result["status"], "error")
        self.assertEqual(tools.pending, {})

    async def test_prompts_keep_units_non_claims_and_internal_protocol_out_of_user_instructions(self):
        prompts = []
        def model(messages):
            prompts.append(messages[0]["content"])
            return ({"kind": "plan", "task": "conversation"} if len(prompts) == 1
                    else {"kind": "answer", "text": "固定流程说明"})
        tools = self.tools()
        result = await ChatAgent(model, tools).run("解释流程")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["text"], "固定流程说明")
        self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 2, "tool_calls": 0})
        for prompt in prompts:
            with self.subTest(prompt=prompt[:25]):
                self.assertRegex(prompt, r"数据库刚度[^。]*C/Eb[^。]*无量纲")
                self.assertRegex(prompt, r"目标[^。]*候选[^。]*GPa")
                self.assertRegex(prompt, r"归一化杆径[^。]*无量纲")
                self.assertRegex(prompt, r"NOT_\*[^。]*未验证[^。]*不[^。]*通过")
                self.assertIn("不要要求用户手写工具名或JSON", prompt)
                self.assertIn("所有面向用户的回答和澄清都使用English", prompt)

    async def test_empty_and_error_have_distinct_user_outcomes_without_extra_work(self):
        for status in ("empty", "error"):
            with self.subTest(status=status):
                tools, query = self.tools(), SyntheticQuery()
                original = query.execute_request
                def terminal(request):
                    result = original(request)
                    result.update(status=status, data={"rows": [], "returned_count": 0} if status == "empty" else None)
                    return result
                query.execute_request = terminal
                tools.query_service = lambda: query
                model_calls = []
                def model(_):
                    model_calls.append(None)
                    return {"kind": "plan", "task": "records", "arguments": {"limit": 1}}
                result = await ChatAgent(model, tools).run("查询第一条")
                self.assertEqual(result["status"], status)
                self.assertEqual(len(model_calls), 1)
                self.assertEqual(len(query.requests), 1)
                self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 1, "tool_calls": 1})
                self.assertEqual(tools.pending, {})
                self.assertIn("will not", result["text"])
                self.assertNotRegex(result["text"], r"[\u4e00-\u9fff]")
                if status == "empty":
                    self.assertIn("No records matched", result["text"])
                    self.assertNotIn("failed", result["text"])
                else:
                    self.assertIn("read failed", result["text"])
                    self.assertNotIn("No records matched", result["text"])

    async def test_timeout_text_only_stops_chat_waiting_not_calculation(self):
        tools = self.tools()
        async def blocked_model(_):
            await asyncio.Event().wait()
        result = await ChatAgent(blocked_model, tools, timeout_seconds=.01).run("解释")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 1, "tool_calls": 0})
        self.assertEqual(tools.pending, {})
        self.assertIn("assistant response timed out", result["text"])
        self.assertIn("computation status", result["text"])
        self.assertIn("Job record", result["text"])
        self.assertNotRegex(result["text"], r"[\u4e00-\u9fff]")
        self.assertNotIn("computation stopped", result["text"])

    async def test_budget_text_keeps_calculation_state_separate_and_does_not_call_again(self):
        tools = self.tools()
        tools.case.limits["runs"] = 1
        calls = []
        def model(_):
            calls.append(None)
            return {"kind": "answer", "text": "固定解释"}
        agent = self.agent(model, tools)
        self.assertEqual((await agent.run("解释"))["status"], "answered")
        before = dict(tools.case.used)
        result = await agent.run("继续")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(tools.case.used, before)
        self.assertEqual(len(calls), 1)
        self.assertIn("assistant explanation has stopped", result["text"])
        self.assertIn("computation status", result["text"])
        self.assertIn("Job record", result["text"])
        self.assertNotIn("computation stopped", result["text"])

    async def test_tool_limit_text_keeps_calculation_state_separate_without_tool_execution(self):
        tools = self.tools()
        result = await ChatAgent(lambda _: {"kind": "plan", "task": "records", "arguments": {"limit": 1}},
                                 tools, max_tool_calls=0).run("查询第一条")
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(tools.case.used, {"runs": 1, "model_calls": 1, "tool_calls": 0})
        self.assertEqual(tools.pending, {})
        self.assertIn("assistant explanation has stopped", result["text"])
        self.assertIn("computation status", result["text"])
        self.assertIn("Job record", result["text"])
        self.assertNotRegex(result["text"], r"[\u4e00-\u9fff]")
        self.assertNotIn("computation stopped", result["text"])

    async def test_fixed_busy_failure_and_confirmation_messages_are_english(self):
        tools = self.tools()
        agent = self.agent(lambda _: {"kind": "execute", "authorized": True}, tools)
        agent.busy = True
        busy = await agent.run("Explain")
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(tools.case.used, {"runs": 0, "model_calls": 0, "tool_calls": 0})
        agent.busy = False
        invalid = await agent.run("Explain")
        self.assertEqual(invalid["status"], "error")
        self.assertEqual(tools.pending, {})

        def failing_model(_):
            raise RuntimeError("synthetic external failure")
        failed = await self.agent(failing_model, self.tools()).run("Explain")
        self.assertEqual(failed["status"], "error")
        self.assertIn("will not retry automatically", failed["text"])
        self.assertIn("Job record", failed["text"])

        tools = self.tools()
        records = await tools.execute("query_records", {"conditions": []})
        waiting = await self.agent(lambda _: {"kind": "tool", "name": "prepare_design_target", "arguments": {
            "result_ref": records["result_ref"], "position": 1}}, tools).run("Use the first record")
        self.assertEqual(waiting["status"], "waiting_for_user")
        self.assertEqual(len(tools.pending), 1)
        self.assertIn("confirmation card", waiting["text"])
        for outcome in (busy, invalid, failed, waiting):
            self.assertNotRegex(outcome["text"], r"[\u4e00-\u9fff]")
