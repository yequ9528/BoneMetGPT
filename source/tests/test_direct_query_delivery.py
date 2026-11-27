from secrets import token_urlsafe
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import latticegpt_v1_app as entry
import httpx
from product_workflow import chainlit_ui
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_chat_tools import SyntheticQuery


class DirectQueryDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_entry_uses_one_actual_sdk_transport_with_synthetic_response(self):
        fixture_api_key = token_urlsafe(24)
        requests = []
        query = SyntheticQuery()

        def transport(_, request):
            self.assertEqual(str(request.url), "https://fixture.invalid/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], f"Bearer {fixture_api_key}")
            requests.append(json.loads(request.read()))
            self.assertEqual(len(requests), 1)
            return httpx.Response(200, json={
                "id": "synthetic", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"kind": "plan", "task": "count", "arguments": {}}),
                }}],
            })

        with _UiProbe() as probe, \
                patch.dict("os.environ", {"API_LLM_API_KEY": fixture_api_key, "API_LLM_BASE_URL": "https://fixture.invalid/v1"}), \
                patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(httpx.HTTPTransport, "handle_request", transport):
            probe.session.set("provider", "api")
            probe.session.set("model_name", "gpt-4o-mini")
            probe.session.set("product_job_id", None)
            await entry.on_v1_message(SimpleNamespace(content="Count all matching records.", elements=[]))
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertIn("3 records match", output)
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(query.requests), 1)
            self.assertEqual(probe.session.get("product_chat_case").used["model_calls"], 1)

    async def test_product_entry_completes_each_query_without_followup_model(self):
        for task in ("records", "count", "overview"):
            with self.subTest(task=task), _UiProbe() as probe:
                query = SyntheticQuery()
                plan = {"kind": "plan", "task": task, "arguments": {}}
                with patch.object(chainlit_ui, "_query_service", return_value=query), \
                        patch.object(chainlit_ui, "_chat_model", side_effect=[plan, TimeoutError()]) as model:
                    probe.session.set("product_job_id", None)
                    await entry.on_v1_message(SimpleNamespace(content="Read the database result.", elements=[]))
                self.assertEqual(model.call_count, 1)
                self.assertEqual(len(query.requests), 1)
                output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
                self.assertNotIn("follow-up explanation", output)
                self.assertNotIn("timed out", output)

    async def test_user_can_ask_about_results_in_a_followup_without_another_read(self):
        query = SyntheticQuery()
        actions = [
            {"kind": "plan", "task": "records", "arguments": {"limit": 1}},
            {"kind": "plan", "task": "conversation"},
            {"kind": "answer", "text": "Synthetic requested explanation."},
        ]
        with _UiProbe() as probe, \
                patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=actions) as model:
            probe.session.set("product_job_id", None)
            first = await chainlit_ui._run_product_chat("Show the first record.")
            self.assertEqual(first["status"], "answered")
            second = await chainlit_ui._run_product_chat("Explain the meaning of that result.")
            self.assertEqual(second["status"], "answered")
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertIn("Synthetic requested explanation.", output)
            self.assertEqual(model.call_count, 3)
            self.assertEqual(len(query.requests), 1)

    async def test_explicit_same_turn_explanation_is_delivered_after_its_result(self):
        query = SyntheticQuery()
        actions = [
            {"kind": "plan", "task": "records", "arguments": {"limit": 1}, "explain_result": True},
            {"kind": "answer", "text": "Synthetic explicitly requested result explanation."},
        ]
        with _UiProbe() as probe, \
                patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", side_effect=actions) as model:
            probe.session.set("product_job_id", None)
            result = await chainlit_ui._run_product_chat("Show one record and explain what its stiffness means.")
            self.assertEqual(result["status"], "answered")
            output = "\n".join(str(message.get("output", "")) for message in probe.messages.values())
            self.assertIn("Synthetic explicitly requested result explanation.", output)
            self.assertEqual(len(query.requests), 1)
            self.assertEqual(model.call_count, 2)

    async def test_explanation_flag_is_closed_boolean_and_does_not_authorize_tools(self):
        for flag in ("true", 1, None, {}):
            with self.subTest(flag=flag), _UiProbe() as probe:
                query = SyntheticQuery()
                plan = {"kind": "plan", "task": "count", "arguments": {}, "explain_result": flag}
                with patch.object(chainlit_ui, "_query_service", return_value=query), \
                        patch.object(chainlit_ui, "_chat_model", return_value=plan):
                    probe.session.set("product_job_id", None)
                    result = await chainlit_ui._run_product_chat("Count matching records.")
                self.assertEqual(result["status"], "error")
                self.assertEqual(query.requests, [])

    async def test_direct_result_rechecks_sources_after_presentation(self):
        query = SyntheticQuery()
        original_present = chainlit_ui._present_chat_tool

        async def change_source(name, result):
            await original_present(name, result)
            query.projection_sha256 = "F" * 64

        with _UiProbe() as probe, \
                patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_present_chat_tool", side_effect=change_source), \
                patch.object(chainlit_ui, "_chat_model", return_value={
                    "kind": "plan", "task": "records", "arguments": {"limit": 1}}) as model:
            probe.session.set("product_job_id", None)
            result = await chainlit_ui._run_product_chat("Show one record.")
            self.assertEqual(result["status"], "stale")
            self.assertEqual(model.call_count, 1)
            self.assertEqual(probe.session.get("product_chat_agent").history, [])

    async def test_requested_explanation_timeout_preserves_completed_result(self):
        query = SyntheticQuery()

        async def model(messages):
            phase = json.loads(messages[-1]["content"]).get("phase")
            if phase == "planning":
                return {"kind": "plan", "task": "records", "arguments": {"limit": 1}, "explain_result": True}
            await asyncio.Event().wait()

        with _UiProbe() as probe, \
                patch.object(chainlit_ui, "_query_service", return_value=query), \
                patch.object(chainlit_ui, "_chat_model", new=model), \
                patch.object(chainlit_ui, "chat_agent_timeout_seconds", return_value=0.1):
            probe.session.set("product_job_id", None)
            result = await chainlit_ui._run_product_chat("Show one record and explain it.")
            self.assertEqual(result["status"], "timeout")
            self.assertTrue(result["display_read"]["presented"])
            self.assertEqual(len(query.requests), 1)
