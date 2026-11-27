import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def range_target():
        from product_workflow.contracts import STIFFNESS_NAMES
        fields = [{"field": name, "raw_value_C_over_Eb": 0.02,
                   "effective_value_C_over_Eb": 0.019999999552965164,
                   "minimum": 0.0, "maximum": 1.0,
                   "raw_delta_from_minimum": 0.02, "raw_delta_from_maximum": -0.98,
                   "effective_delta_from_minimum": 0.019999999552965164,
                   "effective_delta_from_maximum": -0.9800000004470348,
                   "rounding_classification": "IN_RANGE", "status": "PASS"}
                  for name in STIFFNESS_NAMES]
        fields[0].update(raw_value_C_over_Eb=0.11651468508929674,
                         effective_value_C_over_Eb=0.11651468276977539,
                         maximum=0.11651468276977539,
                         raw_delta_from_maximum=2.31952135276714e-9,
                         effective_delta_from_maximum=0.0,
                         rounding_classification="ROUNDS_TO_BOUNDARY")
        return {"range_check": {"status": "PASS", "criterion": "strict_float32_C_over_Eb_v1",
                                "scaler_y_identity": {"sha256": "A" * 64}, "fields": fields},
                "p09_validation": {"status": "HOLD"}, "validation_failure_kind": "P09"}

    async def test_target_failure_card_separates_p09_from_scaler_and_shows_numeric_evidence(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        job = {"job_id": "job-" + "A" * 24, "revision": 2, "status": "FAILED",
               "target": self.range_target(),
               "failure": {"stage": "TARGET_CONFIRMED", "code": "P09", "message": "P09 rejected",
                           "details": {"scaler_path": "I:\\private\\scaler_Y.pt"}}}
        service = SimpleNamespace(confirm_target_and_run_inverse=lambda *args, **kwargs: job)
        with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=service), \
                patch.object(ui, "_consume_workflow_confirmation", new=AsyncMock(return_value=True)), \
                patch.object(ui, "_continue_product_chat", new=AsyncMock()):
            await ui.confirm_target(SimpleNamespace(payload={"job_id": job["job_id"], "expected_revision": 0}))
            text = "\n".join(m["output"] for m in probe.messages.values())
            detail = "\n".join(str(e.get("content", "")) for e in probe.elements.values())
        self.assertIn("Scaler range: `PASS`", detail)
        self.assertIn("P-09: `HOLD`", detail)
        self.assertIn("0.11651468508929674", detail)
        self.assertIn("0.11651468276977539", detail)
        self.assertIn("2.31952135276714e-09", detail)
        self.assertIn("C66", detail)
        self.assertIn("Target P-09 validation did not pass", text)
        self.assertIn("candidate computation was not started", text)
        self.assertNotIn("P09 rejected", text)
        self.assertIn("Target P-09 validation did not pass", detail)
        self.assertNotIn("P09 rejected", detail)
        self.assertIn("TARGET_CONFIRMED", detail)
        self.assertNotIn("I:\\private", text)
        self.assertNotIn("I:\\private", detail)
        self.assertNotIn("0.11651468508929674", text)

    async def test_candidate_card_retains_target_range_evidence(self):
        sent = []
        async def capture(_job_id, stage, _content, **kwargs):
            if stage == "candidates":
                sent.append(json.dumps(kwargs["evidence"]))
        job = {"job_id": "job-" + "A" * 24, "revision": 3, "target": self.range_target(),
               "candidate_set": {"candidates": [{"rank": i, "candidate_id": str(i), "score": 0.0,
                   "p09_validation": {"status": "PASS"}, "normalized_diameter": 0.1, "eligible": False}
                   for i in (1, 2, 3)]}}
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe(), patch.object(ui, "_send_job_stage_card", side_effect=capture), \
                patch.object(ui, "_confirmed_hash", return_value=None):
            await ui._show_candidates(job)
        self.assertEqual(len(sent), 1)
        self.assertIn("0.11651468508929674", sent[0])
        self.assertIn("strict_float32_C_over_Eb_v1", sent[0])

    async def test_legacy_target_evidence_is_displayed_without_mutation(self):
        import copy
        target = {"range_status": "HOLD", "p09_validation": {"status": "PASS"}}
        before = copy.deepcopy(target)
        text = ui._target_validation_evidence(target)
        self.assertIn("This historical Job has no recorded", text)
        self.assertEqual(target, before)
        self.assertNotIn("float32 C/Eb |", text)

    async def test_invalid_conversion_does_not_display_p09_as_evaluated(self):
        target = self.range_target()
        target["p09_validation"] = {"status": "HOLD", "evaluated": False,
                                    "error_code": "INVALID_FLOAT32_CONVERSION"}
        target["validation_failure_kind"] = "INVALID_CONVERSION"
        text = ui._target_validation_evidence(target)
        self.assertIn("P-09 not evaluated", text)
        self.assertIn("INVALID_FLOAT32_CONVERSION", text)
        self.assertIn("INVALID_CONVERSION", text)

    async def test_chainlit_update_appends_actions_until_explicit_removal(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe() as probe:
            first = ui.cl.Action(name="first", payload={})
            message = ui.cl.Message(content="fixture", actions=[first])
            await message.send()
            second = ui.cl.Action(name="second", payload={})
            message.actions = [second]
            await message.update()
            self.assertEqual(set(probe.actions), {first.id, second.id})
            await first.remove()
            self.assertEqual(set(probe.actions), {second.id})

    async def test_card_replacement_removes_before_publishing_and_zero_actions_clear(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        job = _upload_job()
        with _UiProbe() as probe:
            await ui.send_product_mesh_entry_card(job)
            first = probe.current_entry()
            await ui.send_product_mesh_entry_card({**job, "revision": 8})
            second = probe.current_entry()
            events = [(kind, payload.get("id")) for kind, payload in probe.events]
            self.assertLess(events.index(("remove_action", first.id)), events.index(("embedded_action", second.id)))
            await ui._send_job_stage_card(job["job_id"], "mesh_entry", "bound", actions=[], card_revision=9, terminal=True)
            self.assertFalse(probe.actions)

    async def test_upload_action_is_invalid_until_send_finishes_and_revision_matches_card(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        job = _upload_job()
        with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(ui, "_ask_for_product_mesh", new=AsyncMock()) as ask:
            probe.block_event = "embedded_action"
            publication = asyncio.create_task(ui.send_product_mesh_entry_card(job))
            await probe.entered.wait()
            pending = probe.stage_actions("mesh_entry")[0]
            await ui.product_mesh_upload_entry(pending)
            ask.assert_not_awaited()
            probe.release.set()
            await publication
            probe.block_event = None
            with self.assertRaisesRegex(ValueError, "revision mismatch"):
                await ui._send_job_stage_card(job["job_id"], "mesh_entry", "wrong revision", card_revision=8, actions=[ui.cl.Action(name="product_mesh_upload_entry", payload={"job_id": job["job_id"], "expected_revision": 7})])
            await ui.product_mesh_upload_entry(pending)
            ask.assert_not_awaited()

    async def test_late_revision_and_unversioned_progress_cannot_replace_new_terminal_card(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe() as probe:
            current = await ui._send_job_stage_card("job-" + "A"*24, "conformal", "final", card_revision=4, terminal=True)
            before = list(probe.events)
            await ui._send_job_stage_card("job-" + "A"*24, "conformal", "old", card_revision=3)
            await ui._send_runtime_status("late running", job_id="job-" + "A"*24)
            self.assertEqual(current.content, "[Workflow status]\n\nfinal")
            self.assertEqual(probe.events, before)

    async def test_stage_sends_serialize_without_blocking_other_stages_or_jobs(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe() as probe:
            job_id = "job-" + "A"*24
            await ui._send_job_stage_card(job_id, "candidates", "before", card_revision=3)
            probe.block_event = "update_step"
            newer = asyncio.create_task(ui._send_job_stage_card(job_id, "candidates", "new", card_revision=4))
            await probe.entered.wait()
            older = asyncio.create_task(ui._send_job_stage_card(job_id, "candidates", "old", card_revision=3))
            await ui._send_job_stage_card(job_id, "target", "other stage", card_revision=4)
            await ui._send_job_stage_card("job-" + "B"*24, "candidates", "other job", card_revision=1)
            probe.release.set()
            await asyncio.gather(newer, older)
            self.assertEqual(probe.session.get(f"product_stage_card:{job_id}:candidates").content, "[Workflow status]\n\nnew")

    async def test_failed_remove_or_update_invalidates_old_and_new_upload_actions(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        for failure in ("remove_action", "update_step", "embedded_action"):
            with self.subTest(failure=failure), _UiProbe() as probe:
                job = _upload_job()
                with patch.object(ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(ui, "_ask_for_product_mesh", new=AsyncMock()) as ask:
                    await ui.send_product_mesh_entry_card(job)
                    previous = probe.current_entry()
                    probe.fail_event = failure
                    await ui.send_product_mesh_entry_card(job)
                    probe.fail_event = None
                    await ui.product_mesh_upload_entry(previous)
                    for action in list(probe.actions.values()):
                        await ui.product_mesh_upload_entry(SimpleNamespace(**action))
                    self.assertTrue(any("The upload entry could not be updated. Refresh Job status; do not upload again." in m["output"] for m in probe.messages.values()))
                ask.assert_not_awaited()

    async def test_shared_cards_keep_target_candidates_start_refresh_and_home_actions(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe() as probe:
            await ui.send_product_start_card()
            await ui.send_product_start_card()
            for stage, names in (("target", ["product_confirm_target"]), ("candidates", ["product_select_candidate"]*3), ("mesh", ["product_start_conformal", "product_refresh_job"])):
                for revision in (3, 4):
                    await ui._send_job_stage_card("job-"+"A"*24, stage, stage, card_revision=revision, actions=[ui.cl.Action(name=name, payload={}) for name in names])
            names = [a["name"] for a in probe.actions.values()]
            self.assertEqual(len(names), 8)
            self.assertEqual(names.count("product_select_candidate"), 3)
            self.assertEqual(names.count("product_start_entry"), 1)
            self.assertEqual(names.count("product_resume_entry"), 1)

    async def test_cancelled_card_send_keeps_partial_actions_tracked_and_invalid(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        job = _upload_job()
        with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(ui, "_ask_for_product_mesh", new=AsyncMock()) as ask:
            await ui.send_product_mesh_entry_card(job)
            old = probe.current_entry()
            probe.block_event = "embedded_action"
            replacement = asyncio.create_task(ui.send_product_mesh_entry_card(job))
            await probe.entered.wait()
            replacement.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replacement
            await ui.product_mesh_upload_entry(old)
            partial = probe.stage_actions("mesh_entry")[0]
            await ui.product_mesh_upload_entry(partial)
            ask.assert_not_awaited()
            probe.block_event = None
            await ui.send_product_mesh_entry_card(job)
            self.assertNotEqual(partial.id, probe.current_entry().id)

    async def test_failed_runtime_snapshot_card_rejects_late_unversioned_progress(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        job = {"job_id": "job-"+"A"*24, "revision": 10, "status": "FAILED", "failure": {"stage": "fixture", "message": "fixture failed"}}
        service = SimpleNamespace(start_conformal=lambda *args, **kwargs: job)
        from product_workflow.runtime_manager import RuntimeExecutionManager
        with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=service), patch.object(ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()):
            action = ui.cl.Action(name="product_start_conformal", label="synthetic confirmation", payload={"job_id": job["job_id"], "expected_revision": 8})
            ui._issue_workflow_confirmation(action)
            await ui.start_conformal(action)
            before = dict(probe.messages)
            await ui._send_runtime_status("late running", job_id=job["job_id"])
            self.assertEqual(before, probe.messages)
    async def test_start_and_target_cards_use_markdown_without_literal_html(self):
        sent = []
        details = []
        async def capture(*args, **kwargs):
            sent.append(args[2])
            details.append(kwargs.get('evidence'))
        from product_workflow.contracts import target_from_physical_9d
        from tests.test_product_workflow_mesh_runtime import target_values
        with patch.object(ui, '_send_job_stage_card', side_effect=capture):
            await ui.send_product_start_card()
        sent.append(ui._target_card(target_from_physical_9d(target_values(), source_kind='csv_upload', source_sha256='A'*64, source_name='test.csv'), 'job-'+'A'*24))
        for content in sent:
            self.assertNotIn('<details>', content)
            self.assertNotIn('<summary>', content)
            self.assertNotIn('</details>', content)
        self.assertNotIn('mechanical or manufacturing validation', sent[0])
        self.assertIn('NOT_FULL_DUAL_SPACE_FUSION', details[0])
        self.assertNotIn('target SHA256', sent[1])
        self.assertIn('GPa', sent[1])

    async def test_runtime_stage_is_updated_and_invalid_result_does_not_expose_links(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from product_workflow.artifacts import ProductFileIdentityError
        job = {"job_id": "job-"+"A"*24, "revision": 10}
        with _UiProbe() as probe:
            await ui._send_runtime_status('accepted', job_id=job['job_id'])
            stage_key = f"product_stage_card:{job['job_id']}:conformal"
            original = probe.session.get(stage_key)
            with patch.object(ui, '_load_result_view', side_effect=ProductFileIdentityError('secret/path')):
                await ui._show_readonly_result(job)
                pending = tuple(probe.session.get("product_preview_tasks") or ())
                self.assertTrue(pending)
                await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
            self.assertIs(probe.session.get(stage_key), original)
            sends = [payload for kind, payload in probe.events
                     if kind == "send_step" and payload["id"] == original.id]
            updates = [payload for kind, payload in probe.events
                       if kind == "update_step" and payload["id"] == original.id]
            self.assertEqual(len(sends), 1)
            self.assertGreaterEqual(len(updates), 1)
            props = probe.card_props("conformal")
            self.assertIn('FILE_IDENTITY_MISMATCH', props['message'])
            self.assertFalse(props.get('viewerUrl'))
            self.assertFalse(props.get('files'))
            self.assertNotIn('secret', props['message'])

    async def test_missing_attachment_path_reports_instead_of_silent_return(self):
        sent = []
        def message(**kwargs):
            async def send():
                sent.append(kwargs['content'])
            return SimpleNamespace(send=send)
        session = SimpleNamespace(get=lambda key: key == 'product_mode')
        with patch.object(ui.cl, 'user_session', session), patch.object(ui.cl, 'Message', message), patch.object(ui, '_planner') as planner:
            result = await ui.handle_product_message(SimpleNamespace(content='', elements=[SimpleNamespace(name='target.csv', path=None)]))
        self.assertTrue(result)
        self.assertTrue(sent)
        planner.assert_not_called()

    async def test_attachment_failure_is_visible_without_retry(self):
        sent = []
        def message(**kwargs):
            async def send(): sent.append(kwargs['content'])
            return SimpleNamespace(send=send)
        session = SimpleNamespace(get=lambda key: key == 'product_mode')
        with patch.object(ui.cl, 'user_session', session), patch.object(ui.cl, 'Message', message), patch.object(ui, '_handle_attachment', AsyncMock(side_effect=ValueError('bad CSV'))) as handler:
            self.assertTrue(await ui.handle_product_message(SimpleNamespace(content='', elements=[SimpleNamespace(name='target.csv', path='target.csv')])))
        handler.assert_awaited_once()
        self.assertTrue(any('[Error]' in text for text in sent))

    async def test_heading_starts_on_its_own_line(self):
        self.assertIn('\n\n# ', ui._workflow_status('# Title'))

    async def test_model_settings_panel_is_available(self):
        import latticegpt_v1_app as entry
        panel = SimpleNamespace(send=AsyncMock())
        values = {'provider': 'api', 'model_name': 'gpt-4o-mini'}
        session = SimpleNamespace(get=lambda key: values.get(key),
                                  set=lambda key, value: values.__setitem__(key, value))
        with patch.object(entry.cl, 'ChatSettings', return_value=panel), \
                patch.object(entry.cl, 'user_session', session):
            await entry.send_product_model_settings()
        panel.send.assert_awaited_once()

    async def test_selected_model_is_used_for_the_selected_provider(self):
        import latticegpt_v1_app as entry
        values = {}
        session = SimpleNamespace(set=lambda key, value: values.__setitem__(key, value), get=lambda key: values.get(key))
        with patch.object(entry.cl, 'user_session', session), patch.object(entry.cl, 'Message', return_value=SimpleNamespace(send=AsyncMock())):
            await entry.on_v1_settings_update({'provider': 'api', 'model_api': entry.MODEL_CATALOG['api'][-1]})
        self.assertEqual(values['provider'], 'api')
        self.assertEqual(values['model_name'], entry.MODEL_CATALOG['api'][-1])
