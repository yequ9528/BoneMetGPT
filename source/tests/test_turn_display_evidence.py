"""Capture published Chainlit messages with synthetic inputs at the normal app entry."""
import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import latticegpt_v1_app as app
from product_workflow import chainlit_ui as ui
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_chat_tools import SyntheticQuery
from tests.test_product_job_state import _job


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.probe = self.stack.enter_context(_UiProbe())
        self.probe.session.set("product_job_id", None)
        self.query = SyntheticQuery()
        self.answers = []
        self.stack.enter_context(patch.object(ui, "_chat_model", side_effect=lambda _: self.answers.pop(0)))
        self.stack.enter_context(patch.object(ui, "_query_service", return_value=self.query))
        self.create = self.stack.enter_context(patch.object(ui, "_create_and_show_target"))

    async def asyncTearDown(self):
        self.stack.close()

    async def turn(self, name, plan, answer=None):
        seen = set(self.probe.messages)
        self.answers[:] = [plan, answer or {"kind": "answer", "text": "SYNTHETIC_FALSE_MODEL_RESULT"}]
        await app.on_v1_message(SimpleNamespace(content=name, elements=[]))
        messages = [m["output"] for k, m in self.probe.messages.items() if k not in seen]
        path = os.getenv("BMG_ACCEPTANCE_ARTIFACTS")
        if path:
            sample = {"input": name, "published_messages": messages,
                      "executed_requests_through_turn": self.query.requests}
            Path(path, f"display-{self._testMethodName}-{name}.json").write_text(
                json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
        return "\n".join(messages)

    async def test_filters_and_inherited_count_then_clarification(self):
        plan = {"kind": "plan", "task": "records", "arguments": {
            "conditions": [{"field": "C11", "op": "gt", "value": 11, "unit": "GPa"},
                           {"field": "relative_density", "op": "gte", "value": 12.3456789123, "unit": "%"}],
            "order_by": [{"field": "C11", "direction": "desc"}], "limit": 2}}
        rows = await self.turn("records", plan)
        count = await self.turn("count", {"kind": "plan", "task": "count", "arguments": {"inherit": True}})
        clarify = await self.turn("clarify", {"kind": "plan", "task": "records", "arguments": {
            "conditions": [{"field": "C44", "op": "gt", "value": 9}]}})
        for text in (rows, count):
            self.assertIn("0.1 C/Eb", text)
            self.assertIn(str(self.query.requests[0]["filters"][1]["value"]), text)
            self.assertIn("110 GPa", text)
            self.assertIn("percent", text.lower())
            self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", text)
        self.assertIn("Limit: 2", rows)
        self.assertIn("sample_id ascending", rows)
        self.assertIn("Showing 2 records", rows)
        self.assertEqual(rows.count("| Record |"), 1)
        self.assertIn("C66=", rows)
        self.assertIn("inherited", count.lower())
        self.assertIn("3 records match", count)
        self.assertNotIn("Limit:", count)
        self.assertNotIn("| Record |", count)
        self.assertIn("C44", clarify)
        self.assertIn("No database query in this turn", clarify)
        self.assertNotIn("C11", clarify)
        self.assertEqual(len(self.query.requests), 2)
        self.create.assert_not_called()

    async def test_plain_unfiltered_count_publishes_only_the_result(self):
        before = set(self.probe.messages)
        text = await self.turn("plain-count", {"kind": "plan", "task": "count", "arguments": {}})
        self.assertIn("3 records match", text)
        self.assertNotIn("Reference database query · This turn", text)
        self.assertNotIn("Conditions used:", text)
        self.assertNotIn("This turn used the bound read-only reference database.", text)
        self.assertEqual(len(set(self.probe.messages) - before), 1)

    async def test_empty_error_and_unverified_return(self):
        original = self.query.execute_request
        def read(request):
            result = original(request)
            if len(self.query.requests) == 1:
                result.update(status="empty", data={"rows": [], "returned_count": 0})
            elif len(self.query.requests) == 2:
                result.update(status="error", error={"code": "DATABASE_BUSY", "message": "PRIVATE_FAILURE"})
            elif len(self.query.requests) == 3:
                result["provenance"]["source_sha256"] = "wrong"
            else:
                raise OSError("PRIVATE_FAILURE")
            return result
        self.query.execute_request = read
        for i, name in enumerate(("empty", "busy", "mismatch", "exception"), 1):
            request = {"kind": "plan", "task": "records", "arguments": {
                "conditions": [{"field": "sample_id", "op": "eq", "value": i}], "limit": 1}}
            text = await self.turn(name, request)
            self.assertIn(f"sample_id = {i}", text)
            self.assertIn("Limit: 1", text)
            self.assertNotIn("PRIVATE_FAILURE", text)
            self.assertNotIn("| Record |", text)
            self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", text)
            if i == 1:
                self.assertIn("No matching records", text)
            else:
                self.assertIn("no verified", text.lower())
                self.assertNotIn("No matching records", text)
            if i == 2:
                self.assertIn("database is busy", text.lower())
            if i == 3:
                self.assertIn("verification", text.lower())
            if i == 4:
                self.assertIn("read failed", text.lower())
        self.assertEqual(len(self.query.requests), 4)
        self.assertFalse(self.probe.actions)
        self.create.assert_not_called()

    async def test_count_and_overview_cannot_publish_an_invalid_empty_status(self):
        original = self.query.execute_request
        def malformed(request):
            return dict(original(request), status="empty")
        self.query.execute_request = malformed
        for kind in ("count", "overview"):
            with self.subTest(kind=kind):
                text = await self.turn("malformed-" + kind, {"kind": "plan", "task": kind, "arguments": {}})
                self.assertIn("no verified", text.lower())
                self.assertNotIn("3 records match", text)
                self.assertNotIn("3 eligible records", text)
        self.assertEqual(len(self.query.requests), 2)

    async def test_overview_and_proposal_preserve_confirmation(self):
        overview = await self.turn("overview", {"kind": "plan", "task": "overview", "arguments": {}})
        self.assertIn("Release-manifest statistics", overview)
        self.assertIn("3 eligible records", overview)
        self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", overview)
        await self.turn("records", {"kind": "plan", "task": "records", "arguments": {"limit": 1}})
        proposal = await self.turn("proposal", {"kind": "plan", "task": "workflow",
            "arguments": {"intent": "reference_target", "position": 1}})
        self.assertIn("Reference database sample: 17", proposal)
        self.assertIn("waiting for confirmation", proposal.lower())
        actions = [v for v in self.probe.actions.values() if v["name"] == "product_chat_confirm"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(set(actions[0]["payload"]), {"token"})
        self.assertNotIn(actions[0]["payload"]["token"], proposal)
        self.assertEqual(proposal.count("| Component |"), 1)
        self.assertEqual(len(self.query.requests), 2)
        self.create.assert_not_called()

    async def test_current_job_evidence_and_scientific_scope(self):
        job = _job("TARGET_PROPOSED")
        self.probe.session.set("product_job_id", job["job_id"])
        with patch.object(ui, "_chat_read_job", return_value=job):
            text = await self.turn("readjob", {"kind": "plan", "task": "workflow"})
        self.assertIn("Target prepared", text)
        self.assertNotIn("Job record read this turn", text)
        self.assertNotIn(f"revision {job['revision']}", text)
        self.assertNotIn("unverified by this read", text)
        self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", text)
        self.assertFalse(self.query.requests)
        self.create.assert_not_called()

    async def test_verified_read_survives_a_followup_failure(self):
        def model(_):
            if not self.query.requests:
                return {"kind": "plan", "task": "count", "arguments": {}, "explain_result": True}
            raise OSError("PRIVATE_MODEL_FAILURE")
        # The first message creates the session agent with this fake model.
        with patch.object(ui, "_chat_model", side_effect=model):
            text = await self.turn("explanation-error", {})
        self.assertIn("3 records match", text)
        self.assertIn("database read succeeded", text)
        self.assertIn("explanation failed", text)
        self.assertNotIn("PRIVATE_MODEL_FAILURE", text)
        self.assertEqual(len(self.query.requests), 1)

    async def test_verified_read_keeps_displayed_clarification_and_exact_user_history(self):
        from product_workflow.chat_agent import _history_for_model
        question = "Should the next C44 threshold use GPa or C/Eb?"
        text = await self.turn("which-component", {"kind": "plan", "task": "count", "arguments": {}, "explain_result": True},
                               {"kind": "clarify", "text": question})
        self.assertIn("3 records match", text)
        self.assertIn(question, text)
        agent = self.probe.session.get("product_chat_agent")
        self.assertIn(question, agent.history[-1]["content"])
        self.assertIn(question, str(_history_for_model(agent.history)))
        self.assertEqual(agent.history[-2]["content"], "which-component")
        self.assertEqual(len(self.query.requests), 1)

    async def test_hidden_database_answer_does_not_reappear_in_the_next_model_request(self):
        text = await self.turn("show-count", {"kind": "plan", "task": "count", "arguments": {}})
        self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", text)
        agent = self.probe.session.get("product_chat_agent")
        self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", str(agent.history))
        calls = []
        model = agent.model
        def capture(messages):
            calls.append(messages)
            return model(messages)
        agent.model = capture
        await self.turn("continue-reading", {"kind": "plan", "task": "conversation"},
                        {"kind": "answer", "text": "Which component should we discuss?"})
        self.assertEqual(len(calls), 2)
        for messages in calls:
            self.assertIn({"role": "user", "content": "show-count"}, messages)
            self.assertNotIn("SYNTHETIC_FALSE_MODEL_RESULT", str(messages))
        self.assertEqual(len(self.query.requests), 1)

    async def test_changed_session_during_read_discards_old_conditions(self):
        original = self.query.execute_request
        def read(request):
            outcome = original(request)
            self.probe.session.get("product_chat_tools").invalidate()
            return outcome
        self.query.execute_request = read
        text = await self.turn("old-session", {"kind": "plan", "task": "records", "arguments": {
            "conditions": [{"field": "sample_id", "op": "eq", "value": 58121}], "limit": 1}})
        self.assertIn("discarded", text.lower())
        self.assertNotIn("58121", text)
        self.assertIn("session", text.lower())
        self.assertNotIn("latest saved Job", text)
        self.assertNotIn("| Record |", text)
        self.assertFalse(self.probe.actions)
        self.assertEqual(self.probe.session.get("product_chat_agent").history, [])
        self.assertEqual(len(self.query.requests), 1)

    async def test_changed_job_during_read_discards_old_target_conditions(self):
        job = _job("TARGET_PROPOSED")
        self.probe.session.set("product_job_id", job["job_id"])
        self.probe.session.set("product_job_revision", job["revision"])
        original = self.query.execute_request
        def read(request):
            outcome = original(request)
            job["revision"] += 1
            return outcome
        self.query.execute_request = read
        with patch.object(ui, "_chat_read_job", return_value=job):
            text = await self.turn("old-target", {"kind": "plan", "task": "records", "arguments": {
                "conditions": [{"field": "C11", "op": "gte", "value": 0.125, "unit": "C/Eb"}]}})
        self.assertIn("discarded", text.lower())
        self.assertNotIn("0.125", text)
        self.assertNotIn("| Record |", text)
        self.assertEqual(len(self.query.requests), 1)

    async def test_late_explanation_does_not_republish_a_changed_job_reference(self):
        job = _job("TARGET_PROPOSED")
        job["target"].update(physical_9d_gpa={"C11": 27.5}, normalized_9d={"C11": .25})
        self.probe.session.set("product_job_id", job["job_id"])
        self.probe.session.set("product_job_revision", job["revision"])
        def model(messages):
            if json.loads(messages[-1]["content"]).get("phase") == "planning":
                return {"kind": "plan", "task": "count", "explain_result": True,
                        "arguments": {"conditions": [
                            {"field": "C11", "op": "gte", "value": 27.5, "unit": "GPa"}]}}
            job["revision"] += 1
            raise OSError("PRIVATE_MODEL_FAILURE")
        with patch.object(ui, "_chat_read_job", return_value=job), patch.object(ui, "_chat_model", side_effect=model):
            text = await self.turn("stale-explanation", {})
        self.assertEqual(text.count("3 records match"), 1)  # The valid card preceded the change.
        self.assertIn("discarded", text.lower())
        self.assertNotIn("Same-value references", text)
        self.assertNotIn("PRIVATE_MODEL_FAILURE", text)
        self.assertEqual(len(self.query.requests), 1)
        self.assertFalse(self.probe.session.get("product_chat_agent").history)

    async def test_unavailable_service_cannot_claim_execution(self):
        with patch.object(ui, "_query_service", side_effect=OSError("PRIVATE_FACTORY_FAILURE")):
            text = await self.turn("before-read", {"kind": "plan", "task": "count",
                "arguments": {"conditions": [{"field": "sample_id", "op": "eq", "value": 42}]}})
        self.assertIn("was not submitted", text)
        self.assertIn("Prepared conditions", text)
        self.assertIn("sample_id = 42", text)
        self.assertNotIn("PRIVATE_FACTORY_FAILURE", text)
        self.assertFalse(self.query.requests)

    async def test_same_value_references_use_planning_facts_and_never_assume_a_source(self):
        def plan(kind, value, unit="C/Eb"):
            args = {"conditions": [{"field": "C11", "op": "gte", "value": value, "unit": unit}]}
            return {"kind": "plan", "task": kind, "arguments": args}

        for row in self.query.rows:
            row["C11"] = .125
        first = await self.turn("first-read", plan("records", .125))
        self.assertNotIn("Same-value references", first)
        selected = {"state": "PROPOSED", "record_index": 1, "record_count": 1, "physical_9d_gpa": {"C11": 13.75}}
        self.probe.session.set("product_pending_csv_target", selected)
        for row in self.query.rows:
            row["C11"] = .375
        followup = await self.turn("previous-rows", plan("records", .125))
        self.assertIn("Same-value references", followup)
        self.assertIn("prior sample 17: C11=0.125 C/Eb", followup)
        self.assertIn("prior sample 42: C11=0.125 C/Eb", followup)
        self.assertIn("prior sample 93: C11=0.125 C/Eb", followup)
        self.assertIn("1 more matching reference", followup)
        csv = await self.turn("selected-csv", plan("count", .125))
        self.assertIn("selected CSV record 1: C11=13.75 GPa", csv)
        self.probe.session.set("product_pending_csv_target", None)
        previous_no_longer_bound = await self.turn("obsolete-value", plan("count", .125))
        self.assertNotIn("Same-value references", previous_no_longer_bound)
        self.assertNotIn("prior sample", previous_no_longer_bound)

        job = _job("TARGET_PROPOSED")
        job["target"]["physical_9d_gpa"] = {"C11": 27.5}
        job["target"]["normalized_9d"] = {"C11": .25}
        self.probe.session.set("product_job_id", job["job_id"])
        self.probe.session.set("product_job_revision", job["revision"])
        with patch.object(self.probe.session.get("product_chat_tools"), "read_job", return_value=job):
            target = await self.turn("job-gpa", plan("count", 27.5, "GPa"))
            miss = await self.turn("near-match", plan("count", 27.5000000001, "GPa"))
        self.assertIn("current Job target (revision 4): C11=27.5 GPa", target)
        self.assertEqual(target.count("current Job target"), 1)
        self.assertNotIn("prior sample", target)
        self.assertNotIn("Same-value references", miss)
