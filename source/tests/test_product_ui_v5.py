"""V5 regressions exercise real UI callbacks with an isolated transport."""
import asyncio
import copy
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from product_workflow import ui_cards
from product_workflow.contracts import STIFFNESS_NAMES
from product_workflow.ui_previews import PreviewBusy
from tests.test_product_chainlit_mesh_upload import _UiProbe, _RealAskProbe
from tests.test_product_preview_integration import candidate_job


def csv_bytes(count=3):
    return (','.join(STIFFNESS_NAMES) + '\n' + '\n'.join(
        ','.join(str(index + 1) for _ in STIFFNESS_NAMES) for index in range(count))).encode()


class DomainDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_fractional_deadline_sends_integer_timeout(self):
        async def answer(_probe, wire):
            self.assertIs(type(wire['spec']['timeout']), int)
            self.assertEqual(wire['spec']['timeout'], 120)
            return {'submitted': True, 'payload': {'confirm': True}}
        with _RealAskProbe(answer):
            element = ui.cl.CustomElement(name='BoneTaskCard', props={'kind': 'mesh'})
            async def ready(component):
                component.props['_deadline'] = time.monotonic() + 119.25
                return True
            finished = AsyncMock()
            result = await ui_cards.DomainAsk(element=element, ready=ready, finished=finished).send()
            self.assertTrue(result['submitted'])
            finished.assert_awaited_once_with(element)

    async def test_enabling_component_cannot_outlive_the_same_deadline(self):
        entered = asyncio.Event()
        finished = AsyncMock()
        async def answer(_probe, _wire):
            await asyncio.Event().wait()
        async def blocked_update(*_args):
            entered.set()
            await asyncio.Event().wait()
        with _RealAskProbe(answer), patch.object(ui_cards, 'update_component', side_effect=blocked_update):
            element = ui.cl.CustomElement(name='BoneTaskCard', props={'kind': 'mesh'})
            async def ready(component):
                component.props['_deadline'] = time.monotonic() + .05
                return True
            task = asyncio.create_task(ui_cards.DomainAsk(element=element, ready=ready, finished=finished).send())
            await asyncio.sleep(.12)
            completed = task.done()
            if not completed:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(entered.is_set(), 'the enable update must be attempted')
            self.assertTrue(completed, 'the domain deadline must bound the enable update too')
            finished.assert_awaited_once()


class SelectionFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_cache_can_be_serialized_on_actual_chainlit_disconnect(self):
        from chainlit.session import clean_metadata
        with _UiProbe() as probe:
            await ui._media_url('candidate-source', b'png')
            metadata = clean_metadata(probe.session.values)
            self.assertIsInstance(metadata, dict)
            self.assertTrue(all(isinstance(key, str) for key in probe.session.get('product_card_media')))

    async def test_stage_details_have_a_native_sidebar_link(self):
        with _UiProbe() as probe:
            message = await ui._send_job_stage_card(probe.session.get('product_job_id'), 'details', '',
                card_revision=1, model={'kind':'entry','title':'Review'}, evidence=ui._quality_details({'status': 'passed'}))
            self.assertIn('Technical details', message.content)
            self.assertTrue(any(element.name == 'Technical details' and element.display == 'side'
                                for element in message.elements))

    async def test_feedback_failure_does_not_prevent_one_business_submission(self):
        job = candidate_job()
        submit = unittest.mock.Mock(return_value={**job, 'revision': 4})
        with _UiProbe() as probe:
            action = ui.cl.Action(name='product_select_candidate', label='Select rank 1', payload={
                'job_id': job['job_id'], 'expected_revision': 3, 'candidate_id': 'candidate-1',
                'expected_candidate_set_sha256': job['candidate_set']['candidate_set_sha256']})
            ui._issue_workflow_confirmation(action)
            with patch.object(ui, '_workflow', return_value=SimpleNamespace(select_candidate=submit)), \
                 patch.object(ui, '_candidate_feedback', new=AsyncMock(side_effect=RuntimeError('send failed'))), \
                 patch.object(ui, '_show_candidates', new=AsyncMock()), \
                 patch.object(ui, '_continue_product_chat', new=AsyncMock()):
                await ui.select_candidate(action)
                await ui.select_candidate(action)
            self.assertEqual(submit.call_count, 1)
            self.assertTrue(ui._job_ui_state(job['job_id'])['workflow_confirmations'][action.id]['consumed'])


class ResultCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_verification_does_not_need_a_free_drawing_slot(self):
        job = {'job_id': 'job-' + 'A' * 24, 'revision': 11, 'status': 'VIEWER_READY'}
        view = {'revision': 11, 'source': 'same-result', 'model': {'kind': 'result',
                'statusText': 'Result generated', 'viewerUrl': 'http://localhost/product', 'files': []}}
        with _UiProbe() as probe, patch.object(ui, '_load_result_view', return_value=view) as load, \
             patch.object(ui._PREVIEW_EXECUTOR, 'run', new=AsyncMock(side_effect=PreviewBusy('drawing busy'))), \
             patch.object(ui, '_render_result_picture', new=AsyncMock(side_effect=PreviewBusy('drawing busy'))):
            await ui._show_readonly_result(job)
            await asyncio.gather(*(probe.session.get('product_preview_tasks') or []))
            load.assert_called_once()
            self.assertEqual(probe.card_props('conformal')['viewerUrl'], 'http://localhost/product')
            self.assertEqual(probe.card_props('conformal')['previewState'], 'unavailable')


class ControlledFactPublicationTests(unittest.IsolatedAsyncioTestCase):
    def state(self, *, current=True, design_result=True):
        job_id = 'job-' + 'A' * 24
        evidence = {'job_id': job_id, 'revision': 11, 'sha256': 'B' * 64}
        job = {'job_id': job_id, 'revision': 11, 'job_sha256': evidence['sha256'],
               'status': 'VIEWER_READY', 'target': {}, 'candidate_confirmation': 'CONFIRMED',
               'selection': {'selected_candidate_id': 'cell-1', 'selected_rank': 1},
               'candidates': [{'candidate_id': 'cell-1', 'rank': 1, 'eligible': True}]}
        tools = SimpleNamespace(snapshot=lambda: {'job': copy.deepcopy(job)},
                                job_evidence_current=AsyncMock(return_value=current))
        result = {'status': 'answered', 'text': 'Recorded candidate and package facts.',
                  'answer_basis': 'workflow', 'answer_origin': 'program_job_facts',
                  'workflow_intent': 'read_result', 'job_evidence': evidence}
        if design_result:
            from product_workflow.presentation import build_job_fact_reply
            result['design_result'] = {'status': 'verified', 'job_id': job_id, 'revision': 11,
                'selected_candidate_id': 'cell-1',
                'viewer_url': 'http://fixture.invalid/product?job_id=' + job_id,
                'files': {'b31': {'size_bytes': 846, 'url': 'http://fixture.invalid/files/b31'}},
                'viewer_display_verified': False}
            result['text'] = build_job_fact_reply(job, focus='result', design_result=result['design_result'])
        before = [{'role': 'user', 'content': 'Previous request'},
                  {'role': 'assistant', 'content': 'Previous published facts.'}]
        agent = SimpleNamespace(tools=tools, history=copy.deepcopy(before))
        async def run(_text, *, receipt=None):
            if result.get('design_result'):
                await ui._present_chat_tool('get_design_result', result['design_result'])
            agent.history.extend([{'role': 'user', 'content': _text},
                                  {'role': 'assistant', 'content': result['text']}])
            return copy.deepcopy(result)
        agent.run = run
        return tools, agent, result, before

    async def test_result_tool_defers_success_and_error_messages_until_final_publication(self):
        with _UiProbe() as probe:
            for result in ({'status': 'verified', 'viewer_url': 'http://fixture.invalid/result'},
                           {'status': 'error', 'error': {'message': 'Missing file'}}):
                await ui._present_chat_tool('get_design_result', result)
            self.assertEqual(probe.messages, {})

    async def test_verified_result_has_one_final_message_and_bound_links(self):
        tools, agent, expected, _ = self.state()
        with _UiProbe() as probe, patch.object(ui, '_product_chat_tools', return_value=tools):
            probe.session.set('product_chat_agent', agent)
            result = await ui._run_product_chat_locked('Read the existing result')
            self.assertEqual(result['status'], 'answered')
            self.assertEqual(len(probe.messages), 1)
            content = next(iter(probe.messages.values()))['output']
            self.assertIn(expected['text'], content)
            self.assertEqual(content.count(expected['design_result']['viewer_url']), 1)
            self.assertIn('Recorded files (1): `b31`', content)
            self.assertNotIn(expected['design_result']['files']['b31']['url'], content)
            self.assertEqual(content.count('http://'), 1)
            self.assertEqual(agent.history[-1]['content'], expected['text'])

    async def test_result_that_became_stale_never_leaks_verified_text_or_link(self):
        tools, agent, expected, before = self.state(current=False)
        with _UiProbe() as probe, patch.object(ui, '_product_chat_tools', return_value=tools):
            probe.session.set('product_chat_agent', agent)
            result = await ui._run_product_chat_locked('Read the existing result')
            self.assertEqual(result['status'], 'stale')
            content = '\n'.join(message['output'] for message in probe.messages.values())
            self.assertNotIn(expected['text'], content)
            self.assertNotIn('http://fixture.invalid', content)
            self.assertEqual(agent.history, before)
            self.assertEqual(len(probe.messages), 1)

    async def test_program_marker_without_evidence_or_with_wrong_package_cannot_publish(self):
        for defect in ('missing_evidence', 'wrong_package_revision', 'wrong_context_revision', 'wrong_context_hash'):
            with self.subTest(defect=defect):
                tools, agent, expected, before = self.state()
                if defect == 'missing_evidence':
                    expected.pop('job_evidence')
                elif defect == 'wrong_package_revision':
                    expected['design_result']['revision'] = 10
                elif defect == 'wrong_context_revision':
                    tools.snapshot = lambda: {'job': {'job_id': expected['job_evidence']['job_id'], 'revision': 10}}
                else:
                    job = tools.snapshot()['job']
                    job['job_sha256'] = 'C' * 64
                    tools.snapshot = lambda: {'job': job}
                with _UiProbe() as probe, patch.object(ui, '_product_chat_tools', return_value=tools):
                    probe.session.set('product_chat_agent', agent)
                    result = await ui._run_product_chat_locked('Read result')
                    self.assertEqual(result['status'], 'stale')
                    self.assertEqual(agent.history, before)
                    self.assertTrue(all('http://fixture.invalid' not in m['output'] for m in probe.messages.values()))

    async def test_final_send_failure_rolls_back_only_this_turn_history(self):
        tools, agent, _, before = self.state(design_result=False)
        with _UiProbe() as probe, patch.object(ui, '_product_chat_tools', return_value=tools):
            probe.fail_event = 'send_step'
            probe.session.set('product_chat_agent', agent)
            with self.assertRaisesRegex(RuntimeError, 'fixture transport failure'):
                await ui._run_product_chat_locked('Explain the existing selection')
            self.assertEqual(agent.history, before)

    async def test_selection_receipt_keeps_one_matching_history_fact_without_another_message(self):
        tools, agent, expected, before = self.state(design_result=False)
        expected.update(answer_basis='receipt', text='Selected Rank 2 · `inverse_150w_0002`.')
        with _UiProbe() as probe, patch.object(ui, '_product_chat_tools', return_value=tools):
            probe.session.set('product_chat_agent', agent)
            await ui.cl.Message(content=expected['text']).send()
            result = await ui._run_product_chat_locked('Recorded selection receipt',
                receipt={'action': 'select_candidate', 'failed': False})
            self.assertEqual(result['status'], 'answered')
            self.assertEqual(len(probe.messages), 1)
            self.assertEqual(agent.history[:-2], before)
            self.assertEqual(agent.history[-1]['content'], expected['text'])
            self.assertEqual(sum(m['content'] == expected['text'] for m in agent.history), 1)


class CsvSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_feedback_cancel_keeps_the_accepted_write_owned(self):
        entered = asyncio.Event()
        submit = unittest.mock.Mock(return_value={'job_id':'job-'+'B'*24,'revision':1})
        async def blocked_update(*_args):
            entered.set()
            await asyncio.Event().wait()
        with _UiProbe(), patch.object(ui,'_workflow',return_value=SimpleNamespace(create_job=submit)), \
             patch.object(ui,'_current_model_runtime_profile',return_value={}):
            await ui._handle_csv_upload(csv_bytes(3),filename='targets.csv')
            await ui.product_csv_select(ui._csv_import_state()['row_actions'][1])
            token, record = next((t,r) for t,r in ui._creation_state()['actions'].items() if r['state']=='ISSUED')
            with patch.object(ui,'update_component',side_effect=blocked_update):
                task=asyncio.create_task(ui.create_target(ui.cl.Action(name='product_create_target',label='Create',payload={'token':token})))
                await entered.wait()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                for _ in range(60):
                    if ui._creation_state().get('busy') is None:
                        break
                    await asyncio.sleep(.005)
            self.assertEqual(submit.call_count,1)
            self.assertIsNone(ui._creation_state().get('busy'))
            self.assertIn(record['state'],{'CREATED','CREATED_UI_PENDING'})

    async def test_task_switch_during_row_retirement_cannot_reissue_a_proposal(self):
        with _UiProbe():
            await ui._handle_csv_upload(csv_bytes(3),filename='targets.csv')
            await ui.product_csv_select(ui._csv_import_state()['row_actions'][1])
            action=ui._csv_import_state()['row_actions'][3]
            entered, release=asyncio.Event(),asyncio.Event()
            async def blocked_remove(_action):
                entered.set()
                await release.wait()
            with patch.object(ui.cl.Action,'remove',blocked_remove):
                pending=asyncio.create_task(ui.product_csv_select(action))
                await entered.wait()
                await ui.invalidate_product_waits()
                ui.cl.user_session.set('product_job_id','job-'+'B'*24)
                release.set()
                await pending
            self.assertIsNone(ui.cl.user_session.get('product_pending_csv_target'))
            self.assertFalse(any(record['state']=='ISSUED' for record in ui._creation_state()['actions'].values()))

    async def test_cancelled_creation_keeps_ownership_until_the_write_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def create(_target, **_kwargs):
            entered.set()
            release.wait(3)
            return {'job_id': 'job-'+'B'*24, 'revision': 1, 'status': 'TARGET_PROPOSED'}
        with _UiProbe(), patch.object(ui, '_workflow', return_value=SimpleNamespace(create_job=create)), \
             patch.object(ui, '_current_model_runtime_profile', return_value={}), \
             patch.object(ui, '_show_target_card', new=AsyncMock()):
            await ui._handle_csv_upload(csv_bytes(1), filename='single.csv')
            token, record = next(iter(ui._creation_state()['actions'].items()))
            action = ui.cl.Action(name='product_create_target', label='Create', payload={'token': token})
            task = asyncio.create_task(ui.create_target(action))
            for _ in range(60):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                self.assertIs(ui._creation_state().get('busy'), record)
            finally:
                release.set()
                for _ in range(60):
                    if record['state'] != 'CREATING':
                        break
                    await asyncio.sleep(.005)
            self.assertIn(record['state'], {'CREATED', 'CREATED_UI_PENDING'})
            self.assertIsNone(ui._creation_state().get('busy'))

    async def test_old_attachment_read_cannot_replace_a_newer_import(self):
        entered, release = threading.Event(), threading.Event()
        def read(path):
            if str(path).endswith('old.csv'):
                entered.set()
                release.wait(3)
                return csv_bytes(3)
            return csv_bytes(4)
        with _UiProbe(), patch.object(ui.Path, 'read_bytes', read):
            old = asyncio.create_task(ui._handle_attachment(SimpleNamespace(name='old.csv', path='old.csv')))
            for _ in range(60):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            await ui._handle_attachment(SimpleNamespace(name='new.csv', path='new.csv'))
            release.set()
            await old
            self.assertEqual(ui._csv_import_state()['parsed']['filename'], 'new.csv')

    async def test_switching_row_retires_only_its_previous_proposal(self):
        with _UiProbe() as probe:
            current_job = probe.session.get('product_job_id')
            await ui._handle_csv_upload(csv_bytes(), filename='targets.csv')
            state = ui._csv_import_state()
            await ui.product_csv_select(state['row_actions'][1])
            old_token = next(token for token, record in ui._creation_state()['actions'].items()
                             if record['state'] == 'ISSUED')
            await ui.product_csv_select(ui._csv_import_state()['row_actions'][3])
            self.assertEqual(ui._creation_state()['actions'][old_token]['state'], 'RETIRED')
            latest = [r for r in ui._creation_state()['actions'].values() if r['state'] == 'ISSUED']
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]['target']['source']['record_index'], 3)
            self.assertEqual(probe.session.get('product_job_id'), current_job)
            self.assertEqual(ui._product_chat_tools().snapshot()['pending_csv_target']['record_index'], 3)
            with patch.object(ui, '_create_and_show_target', new=AsyncMock()) as create:
                await ui.create_target(ui.cl.Action(name='product_create_target', label='Old', payload={'token': old_token}))
            create.assert_not_awaited()

    async def test_csv_component_payload_is_paginated_and_invalid_rows_keep_identity(self):
        with _UiProbe():
            await ui._handle_csv_upload(csv_bytes(23), filename='many.csv')
            state = ui._csv_import_state()
            props = state['component'].props
            self.assertEqual(len(props['rows']), 20)
            self.assertEqual(props['recordCount'], 23)
            self.assertEqual(props['pageCount'], 2)
            await ui.product_csv_page(state['page_actions'][2])
            props = ui._csv_import_state()['component'].props
            self.assertEqual([row['recordIndex'] for row in props['rows']], [21, 22, 23])

    async def test_creation_busy_cannot_be_reset_by_replacing_csv(self):
        with _UiProbe():
            await ui._handle_csv_upload(csv_bytes(), filename='a.csv')
            state = ui._csv_import_state()
            original = state['import_token']
            creation = {'state': 'CREATE_WRITE_UNCERTAIN', 'action_id': 'keep'}
            ui._creation_state()['busy'] = creation
            await ui._handle_csv_upload(csv_bytes(4), filename='b.csv')
            self.assertIs(ui._creation_state()['busy'], creation)
            self.assertEqual(ui._csv_import_state()['import_token'], original)


if __name__ == '__main__':
    unittest.main()
