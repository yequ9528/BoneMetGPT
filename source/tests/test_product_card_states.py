"""Actual card callbacks over isolated Jobs; no LLM or conformal execution."""
import asyncio
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from product_workflow.chat_tools import ChatTools
from product_workflow.service import CandidateConfirmationCommitError
from product_workflow.ui_previews import CandidatePreview
from tests.test_product_chainlit_mesh_upload import _UiProbe


def pictures(items):
    return tuple(CandidatePreview(item["candidate_id"], item["preview_label"], b"image", None)
                 for item in items)


class CandidateCardStateTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        from tests.test_product_workflow_mesh_runtime import ProductWorkflowMeshRuntimeTests
        case = ProductWorkflowMeshRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    async def settle(self, probe):
        await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))

    async def test_uncertain_confirmation_locks_group_upload_and_late_picture_cannot_unlock(self):
        case = self.fixture()
        job = case.ready
        drawing, release = threading.Event(), threading.Event()

        def delayed_pictures(items):
            drawing.set()
            if not release.wait(3):
                raise AssertionError("test failed to release preview")
            return pictures(items)

        outcome = {"status": "UNCERTAIN", "job_id": job["job_id"]}
        with (_UiProbe() as probe, patch.object(ui, "_workflow", return_value=case.service),
              patch.object(ui, "render_candidate_previews", side_effect=delayed_pictures),
              patch.object(ui, "_continue_product_chat", new=AsyncMock()),
              patch.object(case.service, "select_candidate", side_effect=CandidateConfirmationCommitError(outcome)) as select):
            probe.session.set("product_job_id", job["job_id"])
            await ui._show_candidates(job)
            self.assertTrue(await asyncio.to_thread(drawing.wait, 2))
            actions = [a for a in probe.stage_actions("candidates") if a.name == "product_select_candidate"]
            entry = probe.current_entry()
            before_revision = case.service.get_consistent_job(job["job_id"])["revision"]
            try:
                await ui.select_candidate(actions[0])
                for stage in ("candidates", "mesh_entry"):
                    self.assertTrue(probe.card_props(stage)["locked"])
                    self.assertEqual(probe.card_props(stage)["confirmationState"], "uncertain")
                self.assertIsNone(ui._job_ui_state(job["job_id"])["entry"])
                self.assertFalse(any(a["name"] in {"product_select_candidate", "product_mesh_upload_entry"}
                                     for a in probe.actions.values()))
                await ui.select_candidate(actions[1])
                await ui.product_mesh_upload_entry(entry)
                select.assert_called_once()
                before = copy.deepcopy(ui._job_ui_state(job["job_id"])["workflow_confirmations"])
            finally:
                release.set()
                await self.settle(probe)
            self.assertEqual(ui._job_ui_state(job["job_id"])["workflow_confirmations"], before)
            self.assertTrue(all(c["previewUrl"] for c in probe.card_props("candidates")["candidates"]))
            for stage in ("candidates", "mesh_entry"):
                self.assertTrue(probe.card_props(stage)["locked"])
                self.assertEqual(probe.card_props(stage)["confirmationState"], "uncertain")
            self.assertEqual(case.service.get_consistent_job(job["job_id"])["revision"], before_revision)
            self.assertEqual(case.adapter_calls, [])
            self.assertEqual(case.conformal_calls, [])

    async def test_natural_language_and_card_confirmations_compete_for_one_real_selection(self):
        for first in ("card", "chat"):
            with self.subTest(first=first):
                case = self.fixture()
                case.service, job, case.adapter_calls, case.conformal_calls = case._r3_service_and_ready_job()
                entered, release = threading.Event(), threading.Event()
                original_select = case.service.select_candidate

                def slow_select(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("test failed to release selection")
                    return original_select(*args, **kwargs)

                with (_UiProbe() as probe, patch.object(ui, "_workflow", return_value=case.service),
                      patch.object(ui, "render_candidate_previews", side_effect=pictures),
                      patch.object(ui, "_continue_product_chat", new=AsyncMock()),
                      patch.object(ui, "get_chat_llm", side_effect=AssertionError("LLM is outside this test")) as llm,
                      patch.object(case.service, "select_candidate", side_effect=slow_select) as select):
                    probe.session.set("product_job_id", job["job_id"])
                    await ui._show_candidates(job)
                    await self.settle(probe)
                    actions = [a for a in probe.stage_actions("candidates") if a.name == "product_select_candidate"]
                    tools = ChatTools(query_service=lambda: (_ for _ in ()).throw(AssertionError("no DB")),
                        read_job=case.service.get_consistent_job, read_result=lambda _: None,
                        current_job_id=lambda: probe.session.get("product_job_id"), session_id=ui._session_generation())
                    context = await tools.execute("get_job_context", {})
                    request = await tools.execute("prepare_workflow_action", {"action":"select_candidate",
                        "candidate_id":actions[1].payload["candidate_id"], "job_ref":context["job_ref"]})
                    await ui._present_chat_confirmation(request)
                    chat = SimpleNamespace(**next(a for a in probe.actions.values() if a["name"] == "product_chat_confirm"))
                    with patch.object(ui, "_product_chat_tools", return_value=tools):
                        callbacks = {"card":lambda:ui.select_candidate(actions[0]), "chat":lambda:ui.confirm_chat_action(chat)}
                        pending = asyncio.create_task(callbacks[first]())
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                            self.assertTrue(probe.card_props("candidates")["locked"])
                            self.assertEqual(probe.card_props("candidates")["confirmationState"], "pending")
                            await callbacks["chat" if first == "card" else "card"]()
                            select.assert_called_once()
                        finally:
                            release.set()
                            await pending
                        await self.settle(probe)
                    selected = actions[0 if first == "card" else 1].payload["candidate_id"]
                    final = case.service.get_consistent_job(job["job_id"])
                    self.assertEqual(final["revision"], job["revision"] + 1)
                    self.assertEqual(final["selection"]["selected_candidate_id"], selected)
                    self.assertEqual(probe.card_props("candidates")["selectedId"], selected)
                    self.assertEqual(probe.card_props("candidates")["confirmationState"], "confirmed")
                    self.assertFalse(probe.card_props("candidates")["locked"])
                    self.assertEqual(probe.current_entry().payload["expected_revision"], final["revision"])
                    await ui.select_candidate(actions[0])
                    await ui.confirm_chat_action(chat)
                    select.assert_called_once()
                    llm.assert_not_called()
                    self.assertEqual(case.adapter_calls, [])
                    self.assertEqual(case.conformal_calls, [])


if __name__ == "__main__":
    unittest.main()
