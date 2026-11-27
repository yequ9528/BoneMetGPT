import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from product_workflow import chainlit_ui
from llm_provider import MODEL_DEFAULTS
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_chat_tools import SyntheticQuery


def _message_recorder(records):
    def factory(*, content, actions=None, **kwargs):
        message = SimpleNamespace(
            content=content,
            actions=list(actions or []),
            kwargs=kwargs,
        )

        async def send():
            records.append(message)
            return message

        message.send = send
        return message

    return factory


class _Session:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value




class ProductMessageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_product_job_allows_ordinary_question(self):
        initial_state = {
            "product_mode": True,
            "product_job_id": "job-AAAAAAAAAAAAAAAAAAAAAAAA",
            "product_job_revision": 7,
        }
        session = _Session(initial_state)
        sent = []

        with (
            patch.object(chainlit_ui.cl, "user_session", session),
            patch.object(
                chainlit_ui.cl,
                "Message",
                side_effect=_message_recorder(sent),
            ),
            patch.object(
                chainlit_ui,
                "_run_product_chat", new=AsyncMock(),
            ) as chat,
        ):
            handled = await chainlit_ui.handle_product_message(
                SimpleNamespace(content="单胞是什么？", elements=[])
            )

        self.assertTrue(handled)
        chat.assert_awaited_once_with("单胞是什么？")
        self.assertEqual(session.values, initial_state)
        self.assertEqual(sent, [])

    async def test_invalid_resume_and_attachments_do_not_enter_agent(self):
        session = _Session()
        sent = []
        with patch.object(chainlit_ui.cl, "user_session", session), \
                patch.object(chainlit_ui.cl, "Message", side_effect=_message_recorder(sent)), \
                patch.object(chainlit_ui, "_run_product_chat", new=AsyncMock()) as chat:
            await chainlit_ui.handle_product_message(SimpleNamespace(content="/resume bad", elements=[]))
            await chainlit_ui.handle_product_message(SimpleNamespace(content="", elements=[SimpleNamespace(path="x.stl", name="x.stl")]))
        chat.assert_not_awaited()


class ProductAutomaticQueryUiTests(unittest.IsolatedAsyncioTestCase):

    async def test_reused_agent_preserves_api_deadline_and_cumulative_budget(self):
        def invoke(messages):
            history = json.loads(messages[-1].content)
            context = json.loads(history[-1]["content"])
            action = ({"kind": "plan", "task": "conversation"} if context.get("phase") == "planning"
                      else {"kind": "answer", "text": "Synthetic ordinary explanation."})
            return SimpleNamespace(content=json.dumps(action))

        with _UiProbe() as probe, \
                patch.object(chainlit_ui, "get_chat_llm", return_value=SimpleNamespace(invoke=invoke)), \
                self.assertLogs("latticegpt.chat", level="INFO") as captured:
            probe.session.set("product_job_id", None)
            agents = []
            for provider in ("api", "api", "api", "api"):
                probe.session.set("provider", provider)
                probe.session.set("model_name", MODEL_DEFAULTS[provider])
                result = await chainlit_ui._run_product_chat("Explain a unit cell only.")
                self.assertEqual(result["status"], "answered")
                agents.append(probe.session.get("product_chat_agent"))
        self.assertTrue(all(agent is agents[0] for agent in agents))
        events = [json.loads(row.getMessage()) for row in captured.records]
        starts = [event for event in events if event["event"] == "run_started"]
        self.assertEqual([event["timeout_seconds"] for event in starts], [30, 30, 30, 30])
        finishes = [event for event in events if event["event"] == "run_finished"]
        self.assertEqual([event["budget_used"]["model_calls"] for event in finishes], [2, 4, 6, 8])
        self.assertTrue(all(event["budget_used"]["tool_calls"] == 0 for event in finishes))

    async def test_chat_dispatch_logs_current_prompt_version_and_preserves_workflow_default(self):
        def invoke(messages):
            history = json.loads(messages[-1].content)
            context = json.loads(history[-1]["content"])
            action = ({"kind": "plan", "task": "conversation"} if context.get("phase") == "planning"
                      else {"kind": "answer", "text": "合成解释"})
            return SimpleNamespace(content=json.dumps(action, ensure_ascii=False))

        llm = SimpleNamespace(invoke=invoke)
        with _UiProbe() as probe, patch.object(chainlit_ui, "get_chat_llm", return_value=llm), \
                self.assertLogs("latticegpt.chat", level="INFO") as captured:
            probe.session.set("product_job_id", None)
            probe.session.set("provider", "api")
            probe.session.set("model_name", MODEL_DEFAULTS["api"])
            result = await chainlit_ui._run_product_chat("解释流程")
            default_profile = chainlit_ui._current_model_runtime_profile()

        self.assertEqual(result["status"], "answered")
        events = [json.loads(row.getMessage()) for row in captured.records]
        chat_profiles = [event["profile"] for event in events if event["event"] == "model_profile"]
        self.assertEqual(len(chat_profiles), 2)
        self.assertTrue(all(profile["prompt_template_version"] == "chat-agent-auto-query-v3" for profile in chat_profiles))
        self.assertEqual(default_profile["prompt_template_version"], "query-plan-prompt-v1")
        self.assertTrue(all(set(profile) == {"prompt_template_version"} for profile in chat_profiles))

    async def test_settings_panel_contains_only_model_selection(self):
        import latticegpt_v1_app as entry
        panels = []
        def panel(inputs):
            panels.extend(inputs)
            return SimpleNamespace(send=AsyncMock())
        with _UiProbe() as probe, patch.object(entry.cl, "ChatSettings", side_effect=panel):
            probe.session.set("provider", "api")
            probe.session.set("model_name", "gpt-4")
            await entry.send_product_model_settings()
        self.assertEqual([item.id for item in panels], ["chat_model"])
        self.assertEqual(panels[0].initial, "api/gpt-4")
        self.assertEqual(set(panels[0].items.values()), {
            "api/gpt-4o-mini", "api/gpt-4", "api/gpt-5.5", "api/gpt-5-mini"})

    async def test_unified_model_choice_updates_exact_pair_and_preserves_job(self):
        import latticegpt_v1_app as entry
        for provider, models in entry.MODEL_CATALOG.items():
            if provider == "local":
                continue
            for model in models:
                with self.subTest(provider=provider, model=model), _UiProbe() as probe, \
                        patch.object(chainlit_ui, "invalidate_product_chat") as invalidate:
                    probe.session.set("provider", "api")
                    probe.session.set("model_name", entry.MODEL_DEFAULTS["api"])
                    probe.session.set("product_job_id", "saved-job")
                    await entry.on_v1_settings_update({"chat_model": f"{provider}/{model}"})
                    self.assertEqual((probe.session.get("provider"), probe.session.get("model_name")), (provider, model))
                    self.assertEqual(probe.session.get("product_job_id"), "saved-job")
                    self.assertEqual(invalidate.call_count, int((provider, model) != ("api", entry.MODEL_DEFAULTS["api"])))

    async def test_existing_local_session_switches_to_api_when_settings_open(self):
        import latticegpt_v1_app as entry
        panels = []
        def panel(inputs):
            panels.extend(inputs)
            return SimpleNamespace(send=AsyncMock())
        with _UiProbe() as probe, patch.object(entry.cl, "ChatSettings", side_effect=panel), \
                patch.object(chainlit_ui, "invalidate_product_chat") as invalidate:
            probe.session.set("provider", "local")
            probe.session.set("model_name", "qwen2.5:14b-instruct")
            await entry.send_product_model_settings()
            self.assertEqual((probe.session.get("provider"), probe.session.get("model_name")),
                             ("api", "gpt-4o-mini"))
            self.assertEqual(panels[0].initial, "api/gpt-4o-mini")
            invalidate.assert_called_once()

    async def test_explicit_local_model_selection_is_rejected(self):
        import latticegpt_v1_app as entry
        for settings in ({"chat_model": "local/qwen2.5:14b-instruct"},
                         {"provider": "local", "model_local": "qwen2.5:14b-instruct"}):
            with self.subTest(settings=settings), _UiProbe() as probe, \
                    patch.object(chainlit_ui, "invalidate_product_chat") as invalidate:
                probe.session.set("provider", "api")
                probe.session.set("model_name", "gpt-4o-mini")
                await entry.on_v1_settings_update(settings)
                self.assertEqual((probe.session.get("provider"), probe.session.get("model_name")),
                                 ("api", "gpt-4o-mini"))
                invalidate.assert_not_called()
                self.assertTrue(probe.messages)

    async def test_same_or_invalid_model_choice_keeps_query_and_current_model(self):
        import latticegpt_v1_app as entry
        with _UiProbe() as probe, patch.object(chainlit_ui, "invalidate_product_chat") as invalidate:
            probe.session.set("provider", "api")
            probe.session.set("model_name", "gpt-4")
            await entry.on_v1_settings_update({"chat_model": "api/gpt-4"})
            self.assertEqual(len(probe.messages), 0)
            await entry.on_v1_settings_update({"chat_model": "local/gpt-4"})
            self.assertEqual((probe.session.get("provider"), probe.session.get("model_name")), ("api", "gpt-4"))
            invalidate.assert_not_called()

    async def test_stale_manual_session_and_settings_cannot_override_automatic_count(self):
        import latticegpt_v1_app as entry
        query = SyntheticQuery()
        phases = []

        def model(messages):
            context = json.loads(messages[-1]["content"])
            phases.append(context.get("phase"))
            if context.get("phase") == "planning":
                return {"kind": "plan", "task": "count", "arguments": {"conditions": []}}
            return {"kind": "answer", "text": "本轮实际计数为3条。"}

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            probe.session.set("product_read_kind", "数据库明细")
            probe.session.set("product_read_requirement", {"kind": "records"})
            await entry.on_v1_settings_update({"product_read_kind": "普通对话"})
            handled = await chainlit_ui.handle_product_message(SimpleNamespace(content="符合条件的有多少条？", elements=[]))
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertTrue(handled)
            self.assertEqual(query.requests, [{"schema_version": "latticegpt_database_read_request_v1", "kind": "count", "filters": []}])
            self.assertEqual(phases[0], "planning")
            self.assertEqual(len(phases), 1)
            # The program card owns numeric facts; a model paraphrase is not
            # required to be shown when the recorded result is available.
            self.assertIn("3 records match", output)
            self.assertNotIn("本轮实际计数为3条", output)
            self.assertNotIn("请先选择对应任务", output)

    async def test_automatic_turns_keep_query_conditions_and_records_view(self):
        import latticegpt_v1_app as entry
        query = SyntheticQuery()
        plans = {
            "第一轮": {"kind": "plan", "task": "records", "arguments": {
                "conditions": [{"field": "C11", "op": "gt", "value": 11, "unit": "GPa"}],
                "order_by": [{"field": "C11", "direction": "desc"}], "limit": 2}},
            "第二轮": {"kind": "plan", "task": "count", "arguments": {
                "inherit": True, "conditions": [{"field": "C11", "op": "gt", "value": 22, "unit": "GPa"}]}},
            "第三轮": {"kind": "plan", "task": "records", "arguments": {"inherit": True}},
        }

        def model(messages):
            context = json.loads(messages[-1]["content"])
            if context.get("phase") == "planning":
                user = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                return plans[user]
            return {"kind": "answer", "text": "已依据本轮实际结果回答。"}

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            probe.session.set("provider", "api")
            probe.session.set("model_name", MODEL_DEFAULTS["api"])
            for text in ("第一轮", "第二轮", "第三轮"):
                await entry.on_v1_settings_update({"provider": "local"})
                await chainlit_ui.handle_product_message(SimpleNamespace(content=text, elements=[]))

            self.assertEqual(len(query.requests), 3)
            self.assertEqual(query.requests[1], {
                "schema_version": "latticegpt_database_read_request_v1", "kind": "count",
                "filters": [{"field": "C11", "op": "gt", "value": .2}],
            })
            self.assertEqual(query.requests[2]["filters"], [{"field": "C11", "op": "gt", "value": .2}])
            self.assertEqual(query.requests[2]["limit"], 2)
            self.assertEqual(query.requests[2]["order_by"], [
                {"field": "C11", "direction": "desc"}, {"field": "sample_id", "direction": "asc"}])

    async def test_model_change_invalidates_query_state_despite_legacy_task_setting(self):
        import latticegpt_v1_app as entry
        query = SyntheticQuery()

        def model(messages):
            context = json.loads(messages[-1]["content"])
            if context.get("phase") == "planning":
                user = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                return ({"kind": "plan", "task": "records", "arguments": {"conditions": [], "limit": 1}}
                        if user == "先读取" else {"kind": "plan", "task": "count", "arguments": {"inherit": True}})
            return {"kind": "answer", "text": "已读取实际结果。"}

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            probe.session.set("provider", "api")
            probe.session.set("model_name", MODEL_DEFAULTS["api"])
            first = await chainlit_ui._run_product_chat("先读取")
            self.assertEqual(first["status"], "answered")
            await entry.on_v1_settings_update({"provider": "api", "model_api": entry.MODEL_CATALOG["api"][-1],
                                               "product_read_kind": {"kind": "count"}})
            second = await chainlit_ui._run_product_chat("继承计数")
            self.assertEqual(probe.session.get("provider"), "api")
            self.assertEqual(second["status"], "needs_clarification")
            self.assertEqual(len(query.requests), 1)

    async def test_automatic_conversation_ignores_stale_manual_count_requirement(self):
        query = SyntheticQuery()

        def model(messages):
            context = json.loads(messages[-1]["content"])
            return ({"kind": "plan", "task": "conversation"} if context.get("phase") == "planning"
                    else {"kind": "answer", "text": "count_records是计数工具。"})

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            probe.session.set("product_read_kind", "数据库计数")
            probe.session.set("product_read_requirement", {"kind": "count"})
            result = await chainlit_ui._run_product_chat("请解释count_records这个名称")
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["text"], "count_records是计数工具。")
            self.assertEqual(query.requests, [])
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertIn("Chat explanation · No database query in this turn", output)

    async def test_workflow_answer_without_database_read_is_visibly_marked(self):
        query = SyntheticQuery()

        def model(messages):
            context = json.loads(messages[-1]["content"])
            return ({"kind": "plan", "task": "workflow"} if context.get("phase") == "planning"
                    else {"kind": "answer", "text": "数据库共有999999条。"})

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            result = await chainlit_ui._run_product_chat("说明下一步")
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["answer_basis"], "workflow")
            self.assertEqual(result["query_state"], {"status": "not_queried"})
            self.assertEqual(query.requests, [])
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertIn("Workflow explanation · No database query in this turn", output)

    async def test_execution_receipt_explains_without_planning_or_database_read(self):
        from tests.test_product_job_state import _job
        query = SyntheticQuery()
        job = _job("FAILED")
        job.update(workflow_disposition="HOLD", failure={"stage": "INVERSE_RUNNING", "message": "synthetic"})
        calls = []

        def model(messages):
            calls.append(messages)
            self.fail('A recorded receipt must not invoke either planning or execution model calls.')

        with _UiProbe() as probe, patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=model), \
                patch.object(chainlit_ui, "_chat_read_job", return_value=job), \
                self.assertLogs('latticegpt.chat', level='INFO') as captured:
            probe.session.set("product_job_id", job["job_id"])
            probe.session.set("product_read_requirement", {"kind": "count"})
            receipt = {"action": "start_conformal", "job_id": job["job_id"], "revision": 4,
                       "status": "FAILED", "session_generation": chainlit_ui._session_generation(), "failed": True}
            result = await chainlit_ui._run_product_chat("说明执行回执", receipt=receipt)
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result['answer_origin'], 'program_job_facts')
            self.assertEqual(result['job_evidence']['job_id'], job['job_id'])
            self.assertEqual(result['job_evidence']['revision'], job['revision'])
            self.assertIn('Candidate computation failed', result['text'])
            self.assertIn('No action was retried', result['text'])
            self.assertEqual(calls, [])
            self.assertEqual(query.requests, [])
            self.assertEqual(probe.session.get('product_chat_agent').history[-1]['content'], result['text'])
            self.assertIn(result['text'], [m.get('output', '') for m in probe.messages.values()])
            events = [json.loads(record.getMessage()) for record in captured.records]
            receipts = [event['receipt'] for event in events if event['event'] == 'execution_receipt']
            self.assertEqual(len(receipts), 1)
            self.assertTrue(receipts[0]['failed'])
            self.assertEqual(receipts[0]['revision'], job['revision'])
