"""V4 cards exercise real Chainlit transport without a running provider."""
import asyncio
import copy
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from product_workflow import chainlit_ui as ui
from tests.test_product_chainlit_mesh_upload import _UiProbe


class CardContrastTests(unittest.TestCase):
    def test_muted_text_has_normal_text_contrast_on_light_card_surfaces(self):
        import re
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / "public/conversation-previews.css").read_text(encoding="utf-8")
        def color(selector, prop):
            rule = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]+)\}", css, re.M)
            return re.search(re.escape(prop) + r"\s*:\s*#([0-9a-fA-F]+)\b", rule[1])[1]
        def luminance(rgb):
            if len(rgb) == 3:
                rgb = "".join(c * 2 for c in rgb)
            channels = [int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
            return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))
        ink = luminance(color(".bone-task", "--bone-muted"))
        for surface in (".bone-task", ".bone-candidate", ".bone-preview-status"):
            with self.subTest(surface=surface):
                bg = luminance(color(surface, "background"))
                self.assertGreaterEqual((max(ink, bg) + 0.05) / (min(ink, bg) + 0.05), 4.5)


class CardTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_callback_cannot_publish_after_same_job_is_reopened(self):
        for callback,name in ((ui.confirm_target,'product_confirm_target'),(ui.select_candidate,'product_select_candidate')):
            with self.subTest(name=name), _UiProbe() as probe:
                job_id=probe.session.get('product_job_id')
                action=ui.cl.Action(name=name,label='Confirm',payload={'job_id':job_id,'expected_revision':3,
                    'candidate_id':'cell-1','expected_candidate_set_sha256':'source'})
                ui._issue_workflow_confirmation(action)
                async def returned(*args,**kwargs):
                    # A -> B -> A leaves the same Job ID but a new UI generation.
                    probe.session.set('product_session_generation','reopened-generation')
                    return {'job_id':job_id,'revision':4,'status':'CANDIDATES_READY'}
                with patch.object(ui,'_workflow',return_value=SimpleNamespace(select_candidate=lambda:None,
                        confirm_target_and_run_inverse=lambda:None)), patch.object(ui.asyncio,'to_thread',side_effect=returned), \
                        patch.object(ui,'_show_candidates',new_callable=AsyncMock) as show:
                    await callback(action)
                show.assert_not_awaited()

    async def test_media_reuses_equal_bytes_and_replaces_recovered_placeholder(self):
        with _UiProbe():
            with patch.object(ui.cl.Image,'_create',new_callable=AsyncMock) as create:
                await ui._media_url('same-source',b'placeholder')
                await ui._media_url('same-source',b'placeholder')
                await ui._media_url('same-source',b'recovered-image')
            self.assertEqual(create.await_count,2)

    async def test_late_business_reply_stays_historical_and_issues_no_action(self):
        with _UiProbe() as probe:
            old_id=probe.session.get('product_job_id')
            probe.session.set('product_job_id','job-'+'B'*24)
            action=ui.cl.Action(name='product_select_candidate',payload={'job_id':old_id},label='Select')
            await ui._send_job_stage_card(old_id,'candidates','',actions=[action],card_revision=3,
                model={'kind':'candidates','candidates':[{'id':'old','action':action.to_dict()}]})
            state=ui._job_ui_state(old_id)
            self.assertTrue(state['cards']['candidates']['component'].props['historical'])
            self.assertNotIn(action.id,state.get('workflow_confirmations',{}))

    async def test_target_failure_does_not_highlight_generation_stage(self):
        with _UiProbe() as probe:
            job={'job_id':probe.session.get('product_job_id'),'revision':2,'status':'FAILED',
                 'failure':{'stage':'TARGET_CONFIRMED','message':'invalid target'}}
            await ui._show_job_summary(job)
            model=ui._job_ui_state(job['job_id'])['cards']['summary']['component'].props
            self.assertEqual(model['phase'],1)

    async def test_refresh_during_selection_does_not_unlock_group(self):
        from tests.test_product_preview_integration import candidate_job
        with _UiProbe() as probe:
            job=candidate_job();state=ui._job_ui_state(job['job_id'])
            state.update(selection_pending='click',selection_running=True,selection_revision=job['revision'])
            await ui._show_candidates(job)
            card=state['cards']['candidates']
            self.assertTrue(card['component'].props['locked'])
            self.assertEqual(card['component'].props['confirmationState'],'pending')
            await ui.invalidate_product_waits()

    async def test_embedded_action_registered_before_component_is_visible(self):
        with _UiProbe() as probe:
            job_id = probe.session.get('product_job_id')
            action = ui.cl.Action(name='product_select_candidate', label='Select', payload={
                'job_id':job_id, 'expected_revision':3, 'candidate_id':'cell-1'})
            original = probe.record
            async def record(kind, data):
                if kind == 'element' and data.get('type') == 'custom':
                    self.assertIn(action.id, ui._job_ui_state(job_id)['workflow_confirmations'])
                await original(kind, data)
            with patch.object(ui.cl.context.emitter, 'send_element', side_effect=lambda d: record('element',d)):
                message = await ui._send_job_stage_card(job_id,'candidates','',actions=[action],card_revision=3,
                    model={'kind':'candidates','candidates':[{'id':'cell-1','action':action.to_dict()}]})
            self.assertEqual(message.actions, [])
            self.assertEqual(message.elements[0].name,'BoneTaskCard')
            self.assertTrue(await ui._consume_workflow_confirmation(action,action.name))

    async def test_preview_patch_keeps_component_and_consumed_action(self):
        with _UiProbe() as probe:
            job_id = probe.session.get('product_job_id')
            action=ui.cl.Action(name='product_select_candidate',label='Select',payload={'job_id':job_id})
            await ui._send_job_stage_card(job_id,'candidates','',actions=[action],card_revision=3,
                model={'kind':'candidates','candidates':[{'id':'a','previewUrl':None,'action':action.to_dict()}]})
            await ui._consume_workflow_confirmation(action,action.name)
            target=ui._capture_preview_target(job_id,'candidates','source')
            element=target['card']['component']
            self.assertEqual(element.props['componentId'],element.id)
            records=copy.deepcopy(ui._job_ui_state(job_id)['workflow_confirmations'])
            self.assertTrue(await ui._patch_card(target,{'candidates':[{'id':'a','previewUrl':'/image'}]}))
            self.assertIs(target['card']['component'],element)
            self.assertEqual(ui._job_ui_state(job_id)['workflow_confirmations'],records)
            self.assertNotIn(('remove_element',{'id':element.id}),probe.events)

    async def test_switch_retires_old_card_without_erasing_history(self):
        with _UiProbe() as probe:
            job_id=probe.session.get('product_job_id')
            await ui._send_job_stage_card(job_id,'summary','',card_revision=3,model={'kind':'summary','phase':2})
            card=ui._job_ui_state(job_id)['cards']['summary']
            await ui.invalidate_product_waits()
            self.assertTrue(card['component'].props['historical'])
            self.assertEqual(card['component'].props['jobId'],job_id)

    async def test_initial_file_check_stops_on_busy_or_timeout(self):
        for error, reason in ((ui.PreviewBusy, "busy"), (ui.PreviewTimeout, "timed out")):
            with self.subTest(error=error), _UiProbe() as probe:
                job = {"job_id": probe.session.get("product_job_id"), "revision": 11, "status": "VIEWER_READY"}
                with patch.object(ui._INPUT_EXECUTOR, "run", new=AsyncMock(side_effect=error())):
                    await ui._show_readonly_result(job)
                    await asyncio.gather(*probe.session.get("product_preview_tasks"))
                model = probe.card_props("conformal")
                self.assertEqual(model["verificationState"], "review")
                self.assertIn(reason, model["previewMessage"].lower())
                self.assertEqual(model["previewState"], "unavailable")
                self.assertTrue(any(a["name"] == "product_refresh_job" for a in model["actions"]))

    async def test_result_links_publish_before_render_finishes(self):
        with _UiProbe() as probe:
            job={'job_id':probe.session.get('product_job_id'),'revision':11,'status':'VIEWER_READY'}
            entered=asyncio.Event();release=asyncio.Event()
            async def render(*_):
                entered.set();await release.wait();return b'png'
            view={'revision':11,'model':{'kind':'result','viewerUrl':'http://viewer','files':[]},'stl_path':'file.stl','source':'verified'}
            with patch.object(ui,'_load_result_view',return_value=view), patch.object(ui,'_render_result_picture',side_effect=render):
                await ui._show_readonly_result(job)
                await entered.wait()
                card=ui._job_ui_state(job['job_id'])['cards']['conformal']
                self.assertEqual(card['component'].props['viewerUrl'],'http://viewer')
                self.assertEqual(card['component'].props['previewState'],'preparing')
                release.set()
                await asyncio.gather(*(probe.session.get('product_preview_tasks') or []))

class DomainAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_ask_and_lease_share_deadline_after_component_send(self):
        import time
        from product_workflow.ui_cards import DomainAsk
        from tests.test_product_chainlit_mesh_upload import _RealAskProbe
        times={}
        async def answer(probe,wire):
            self.assertEqual(wire['spec']['type'],'element')
            self.assertLessEqual(wire['spec']['timeout'],120)
            self.assertGreater(wire['spec']['timeout'],119)
            self.assertLess(time.monotonic(),times['deadline'])
            return {'submitted':True,'payload':{'confirm':True}}
        with _RealAskProbe(answer):
            element=ui.cl.CustomElement(name='BoneTaskCard',props={'kind':'mesh','askActive':False})
            async def ready(e):
                self.assertIsNotNone(e.for_id)
                times['deadline']=time.monotonic()+120
                e.props['_deadline']=times['deadline']
                return True
            finished=AsyncMock()
            result=await DomainAsk(element=element,ready=ready,finished=finished).send()
            self.assertTrue(result['submitted'])
            finished.assert_awaited_once_with(element)

if __name__=='__main__':unittest.main()
