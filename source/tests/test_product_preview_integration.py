"""UI preview tests use real Chainlit elements with an isolated transport."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from tests.test_product_chainlit_mesh_upload import _UiProbe
from product_workflow.contracts import canonical_sha256
from product_workflow.ui_previews import CandidatePreview, PreviewTimeout


class PreviewPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, probe):
        job_id = probe.session.get("product_job_id")
        action = ui.cl.Action(name="product_select_candidate", label="Select rank 1", payload={
            "job_id": job_id, "expected_revision": 3, "candidate_id": "candidate-C"})
        message = await ui._send_job_stage_card(job_id, "candidates", "Candidates", actions=[action], card_revision=3)
        target = ui._capture_preview_target(job_id, "candidates", "source-1")
        return job_id, message, action, target

    async def test_picture_update_keeps_action_id_expiry_and_consumption(self):
        with _UiProbe() as probe:
            job_id, message, action, target = await self.prepare(probe)
            self.assertTrue(await ui._consume_workflow_confirmation(action, action.name))
            records = copy.deepcopy(ui._job_ui_state(job_id)["workflow_confirmations"])
            before_actions = [e for e in probe.events if e[0] in {"action", "remove_action"}]
            picture = ui.cl.Image(name="Rank 1 · candidate-C", content=b"image", display="inline")
            self.assertTrue(await ui._update_stage_preview(target, [picture]))
            self.assertEqual(ui._job_ui_state(job_id)["workflow_confirmations"], records)
            self.assertEqual([e for e in probe.events if e[0] in {"action", "remove_action"}], before_actions)
            self.assertEqual(message.actions[0].id, action.id)
            self.assertIn(picture.id, probe.elements)

    async def test_late_preview_after_same_revision_resume_is_discarded(self):
        with _UiProbe() as probe:
            job_id, message, action, target = await self.prepare(probe)
            await ui.invalidate_product_waits()
            replacement = ui.cl.Action(name=action.name, payload=action.payload, label=action.label)
            await ui._send_job_stage_card(job_id, "candidates", "Restored", actions=[replacement], card_revision=3)
            before = list(probe.events)
            self.assertFalse(await ui._update_stage_preview(target, [ui.cl.Image(name="late", content=b"late")]))
            self.assertEqual(probe.events, before)
            self.assertTrue(await ui._consume_workflow_confirmation(replacement, replacement.name))

    async def test_snapshot_is_rechecked_after_waiting_for_card_lock(self):
        with _UiProbe() as probe:
            job_id, message, action, target = await self.prepare(probe)
            card = ui._job_ui_state(job_id)["cards"]["candidates"]
            await card["lock"].acquire()
            task = asyncio.create_task(ui._update_stage_preview(target, [ui.cl.Image(name="late", content=b"late")]))
            await asyncio.sleep(0)
            probe.session.set("product_session_generation", "replacement-generation")
            before = list(probe.events)
            card["lock"].release()
            self.assertFalse(await task)
            self.assertEqual(probe.events, before)

    async def test_business_refresh_still_rebuilds_confirmation_actions(self):
        with _UiProbe() as probe:
            job_id, message, action, target = await self.prepare(probe)
            replacement = ui.cl.Action(name=action.name, payload=action.payload, label=action.label)
            await ui._send_job_stage_card(job_id, "candidates", "Refreshed", actions=[replacement], card_revision=3)
            self.assertFalse(await ui._consume_workflow_confirmation(action, action.name))
            self.assertTrue(await ui._consume_workflow_confirmation(replacement, replacement.name))


def candidate_job():
    candidates = [{"candidate_id": f"candidate-{rank}", "rank": rank, "score": rank / 100,
                   "p09_validation": {"status": "PASS"}, "eligible": True,
                   "nodes": [[0, 0, 0], [1, 1, 1]], "connections": [[0, 1]],
                   "normalized_diameter": 0.05, "predicted_stiffness": {}}
                  for rank in (3, 1, 2)]
    candidate_set = {"candidates": candidates, "recommended_candidate_id": "candidate-1"}
    candidate_set["candidate_set_sha256"] = canonical_sha256(candidate_set)
    return {"job_id": "job-" + "A" * 24, "revision": 3, "status": "CANDIDATES_READY",
            "target": {}, "candidate_set": candidate_set,
            "selection": {"recommended_candidate_id": "candidate-1", "selected_candidate_id": "candidate-1"}}


class CandidatePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_instruction_uses_the_recorded_ranking(self):
        for schema, metric in (("latticegpt_candidate_set_v2", "NMSE"), (None, "P-10")):
            with self.subTest(metric=metric), _UiProbe() as probe, patch.object(
                ui, "_PREVIEW_EXECUTOR", run=AsyncMock(side_effect=ui.PreviewBusy("fixture"))
            ):
                job = candidate_job()
                collection = job["candidate_set"]
                if schema:
                    collection["schema_version"] = schema
                    del collection["candidate_set_sha256"]
                    collection["candidate_set_sha256"] = canonical_sha256(collection)
                await ui._show_candidates(job)
                await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
                self.assertEqual(probe.card_props("candidates")["message"],
                                 f"{metric} determines rank. Only eligible candidates can be selected.")

    async def test_batch_failure_preserves_live_candidate_fields_and_confirmation(self):
        cases = [(ui.PreviewBusy, "Preview is busy"), (ui.PreviewTimeout, "timed out"),
                 (ui.PreviewSourceError, "source could not be verified")]
        for error, text in cases:
            with (self.subTest(error=error.__name__), _UiProbe() as probe,
                  patch.object(ui, "_viewer_job_url", return_value="http://localhost/product"),
                  patch.object(ui, "_PREVIEW_EXECUTOR", run=AsyncMock(side_effect=error("fixture")))):
                job = candidate_job()
                await ui._show_candidates(job)
                state = ui._job_ui_state(job["job_id"])
                card = state["cards"]["candidates"]
                actions = probe.stage_actions("candidates")
                # Hold publication while the failure waits, as another update completes one slot.
                async with card["lock"]:
                    await asyncio.sleep(0)
                    cells = copy.deepcopy(card["model"]["candidates"])
                    cells[1].update(previewState="ready", previewUrl="/already-ready")
                    card["model"] = {**card["model"], "candidates": cells}
                    records = state["workflow_confirmations"]
                    records[actions[1].id]["issued_at"] -= 901
                    self.assertTrue(await ui._consume_workflow_confirmation(actions[2], actions[2].name))
                    before = copy.deepcopy(records)
                await asyncio.wait_for(card["preview_task"], 3)
                actual = probe.card_props("candidates")["candidates"]
                self.assertEqual(records, before)
                self.assertEqual(actual[1], cells[1])
                for index in (0, 2):
                    cell = actual[index]
                    self.assertEqual(cell["previewState"], "unavailable")
                    self.assertIn(text, cell["previewMessage"])
                    self.assertEqual(cell, {**cells[index], "previewState": "unavailable",
                                            "previewMessage": cell["previewMessage"]})
                self.assertTrue(await ui._consume_workflow_confirmation(actions[0], actions[0].name))
                for action in actions[:3]:
                    self.assertFalse(await ui._consume_workflow_confirmation(action, action.name))

    async def test_manual_refresh_restores_images_and_rejects_previous_error(self):
        job = candidate_job()
        pictures = tuple(CandidatePreview(f"candidate-{n}", f"Rank {n}", b"png", None)
                         for n in range(1, 4))
        with (_UiProbe() as probe, patch.object(ui, "_viewer_job_url", return_value="http://localhost/product"),
              patch.object(ui, "_job_store", return_value=SimpleNamespace(get_consistent=lambda _: job)),
              patch.object(ui, "build_product_job_projection", return_value={"view_state": "candidate"}),
              patch.object(ui, "_PREVIEW_EXECUTOR", run=AsyncMock(side_effect=[ui.PreviewBusy(), pictures]))):
            await ui._show_candidates(job)
            card = ui._job_ui_state(job["job_id"])["cards"]["candidates"]
            await asyncio.wait_for(card["preview_task"], 3)
            old = ui._capture_preview_target(job["job_id"], "candidates", card["preview_source"])
            self.assertTrue(all(c["previewState"] == "unavailable" for c in card["model"]["candidates"]))
            refresh = next(a for a in probe.stage_actions("candidates") if a.name == "product_refresh_job")
            await ui.product_refresh_job(refresh)
            await asyncio.wait_for(card["preview_task"], 3)
            self.assertFalse(await ui._update_stage_preview(old, [], note="late error"))
            for cell in probe.card_props("candidates")["candidates"]:
                self.assertEqual(cell["previewState"], "ready")
                self.assertTrue(cell["previewUrl"])
                self.assertFalse(cell.get("previewMessage"))

    async def test_ranked_slots_and_failure_placeholder_do_not_shift(self):
        received = []
        def render(items):
            received.extend(item["candidate_id"] for item in items)
            return tuple(CandidatePreview(item["candidate_id"], item["preview_label"],
                         b"placeholder" if index == 1 else b"picture", "render failed" if index == 1 else None)
                         for index, item in enumerate(items))
        with _UiProbe() as probe, patch.object(ui, "render_candidate_previews", side_effect=render), \
                patch.object(ui, "_viewer_job_url", return_value="http://localhost/product"), \
                patch.object(ui, "_confirmed_hash", return_value=None):
            await ui._show_candidates(candidate_job())
            before = copy.deepcopy(ui._job_ui_state(probe.session.get("product_job_id"))["workflow_confirmations"])
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            self.assertEqual(received, ["candidate-1", "candidate-2", "candidate-3"])
            card = ui._job_ui_state(probe.session.get("product_job_id"))["cards"]["candidates"]
            cells = card["component"].props["candidates"]
            self.assertEqual([(cell["id"], cell["rank"]) for cell in cells],
                             [("candidate-1", 1), ("candidate-2", 2), ("candidate-3", 3)])
            self.assertEqual([cell["name"] for cell in cells], ["Rank 1", "Rank 2", "Rank 3"])
            self.assertTrue(all(cell["previewUrl"] for cell in cells))
            self.assertEqual([cell["previewState"] for cell in cells], ["ready", "unavailable", "ready"])
            self.assertEqual([cell["action"]["payload"]["candidate_id"] for cell in cells],
                             ["candidate-1", "candidate-2", "candidate-3"])
            self.assertEqual(ui._job_ui_state(probe.session.get("product_job_id"))["workflow_confirmations"], before)

    async def test_mismatched_source_never_calls_renderer(self):
        job = candidate_job()
        job["candidate_set"]["candidates"][0]["normalized_diameter"] = 0.7
        with _UiProbe() as probe, patch.object(ui, "render_candidate_previews") as render, \
                patch.object(ui, "_viewer_job_url", return_value="http://localhost/product"), \
                patch.object(ui, "_confirmed_hash", return_value=None):
            await ui._show_candidates(job)
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            render.assert_not_called()
            self.assertIn("source could not be verified", json.dumps(probe.card_props("candidates")))


class DomainPreviewTimingTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, fail=False, late=False):
        from tests.test_product_mesh_domain import DomainWorkflowTests, MIXED
        from tests.test_product_chainlit_mesh_upload import _RealAskProbe
        case = DomainWorkflowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        job = case.propose()
        asked = []
        async def answer(probe, payload):
            asked.append(payload)
            lease = ui._job_ui_state(job["job_id"])["busy"]
            self.assertEqual(lease["stage"], "AWAIT_DOMAIN")
            self.assertGreater(lease["expires_at"] - ui.time.monotonic(), 115)
            return probe.response(confirm=False)
        with _RealAskProbe(answer) as probe, patch.object(ui, "_workflow", return_value=case.service), \
                patch.object(ui, "_refresh_mesh_entry", new=AsyncMock()):
            probe.session.set("product_job_id", job["job_id"])
            state = ui._job_ui_state(job["job_id"])
            lease = {"job_id": job["job_id"], "stage": "SUBMITTING",
                     "session_generation": ui._session_generation(), "entry_token": "test-token",
                     "confirmation_sha256": job["selection"]["confirmation"]["confirmation_sha256"]}
            state["busy"] = lease
            async def prepare(*args, **kwargs):
                self.assertEqual(lease["stage"], "PREPARING_PREVIEW")
                if fail:
                    raise PreviewTimeout("fixture")
                if late:
                    lease["expires_at"] = 0
                return {'views': {name: b"picture" for name in ("original", "selected", "excluded")},
                        'originalNodeCount': 12, 'originalElementCount': 3}
            with patch.object(ui._PREVIEW_EXECUTOR, "run", side_effect=prepare):
                result = await ui._confirm_uploaded_domain(job, content=MIXED, lease=lease)
            self.assertIsNone(result)
            self.assertEqual(len(asked), 0 if fail or late else 1)
            if fail or late:
                text = " ".join(m["output"] for m in probe.messages.values()) + json.dumps(probe.elements)
                self.assertIn("Reopen Job", text)
                self.assertNotIn("No INP was submitted", text)
            self.assertIsNone(case.service.get_consistent_job(job["job_id"])["mesh_binding"])

    async def test_confirmation_clock_starts_after_preparation(self):
        await self.exercise()

    async def test_timeout_has_no_confirmation_or_binding(self):
        await self.exercise(fail=True)

    async def test_render_returning_after_deadline_offers_recovery(self):
        await self.exercise(late=True)


class MeshRecordRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_saved_binding_explains_failure_without_start_action(self):
        job = {"job_id": "job-" + "A" * 24, "revision": 7, "status": "AWAITING_CONFORMAL_AUTHORIZATION"}
        with _UiProbe() as probe, patch.object(ui, "build_product_job_projection", return_value={"view_state": "mesh"}), \
                patch.object(ui, "normalize_job_mesh_binding", side_effect=ValueError("reference_length is invalid")):
            await ui.render_product_job(job)
            self.assertIn("saved mesh binding could not be validated", probe.card_props("mesh")["message"])
            self.assertNotIn("reference_length is invalid", probe.card_props("mesh")["message"])
            self.assertEqual([action.name for action in probe.stage_actions("mesh")], ["product_refresh_job"])
            self.assertFalse(probe.session.get("product_preview_tasks"))


class ResultPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_result_png_and_links_share_one_read(self):
        job = {"job_id": "job-" + "A" * 24, "revision": 11, "status": "VIEWER_READY"}
        view = {"revision": 11, "source": "verified-source", "model": {
                "kind": "result", "viewerUrl": "http://localhost/product",
                "files": [{"label": "Beam mesh", "url": "http://localhost/b31"}]}}
        with _UiProbe() as probe, patch.object(ui, "_load_result_view", return_value=view) as load, \
                patch.object(ui, "_render_result_picture", new=AsyncMock(return_value=b"result-picture")) as render:
            await ui._show_readonly_result(job)
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            load.assert_called_once()
            card = ui._job_ui_state(job["job_id"])["cards"]["conformal"]
            props = card["component"].props
            self.assertTrue(props["previewUrl"])
            self.assertEqual(props["previewState"], "ready")
            self.assertEqual(props["viewerUrl"], "http://localhost/product")
            render.assert_awaited_once_with(view)

    async def test_surface_preview_timeout_preserves_verified_result_entry(self):
        job = {"job_id": "job-" + "A" * 24, "revision": 11, "status": "VIEWER_READY"}
        view = {"revision":11, "source":"verified-source", "model": {
            "kind":"result", "statusText":"Result generated", "viewerUrl":"http://localhost/product",
            "files":[{"label":"Beam mesh", "url":"http://localhost/b31"}]}}
        with (_UiProbe() as probe, patch.object(ui,"_load_result_view",return_value=view) as load,
              patch.object(ui,"_render_result_picture",new=AsyncMock(side_effect=PreviewTimeout("drawing")))):
            await ui._show_readonly_result(job)
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            props = probe.card_props("conformal")
            self.assertEqual(props["viewerUrl"], "http://localhost/product")
            self.assertEqual(props["files"], view["model"]["files"])
            self.assertEqual(props["statusText"], "Result generated")
            self.assertEqual(props["previewState"], "unavailable")
            self.assertIn("timed out", props["previewMessage"])
            load.assert_called_once()

    async def test_result_verification_failure_does_not_publish_links(self):
        from product_workflow.artifacts import ProductFileIdentityError
        job = {"job_id": "job-" + "A" * 24, "revision": 11, "status": "VIEWER_READY"}
        with _UiProbe() as probe, patch.object(ui, "_load_result_view", side_effect=ProductFileIdentityError("fixture")):
            await ui._show_readonly_result(job)
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            props = probe.card_props("conformal")
            self.assertIn("FILE_IDENTITY_MISMATCH", props["message"])
            self.assertFalse(props.get("viewerUrl"))
            self.assertFalse(props.get("files"))


if __name__ == "__main__":
    unittest.main()
