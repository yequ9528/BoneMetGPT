"""Capture the actual provider-facing messages with synthetic local tools and a fake LLM."""
import copy
from contextlib import ExitStack
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from product_workflow import chainlit_ui as ui
from product_workflow.chat_agent import ChatAgent
from product_workflow.chat_tools import ChatTools
from product_workflow.contracts import BASE_MATERIAL_MODULUS_GPA, STIFFNESS_NAMES, target_from_physical_9d, canonical_sha256
from tests.test_product_job_state import _job
from tests.test_inverse_dialogue_acceptance import tools_for
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_chat_tools import SyntheticQuery


class ModelMessageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actions = []
        self.calls = []
        self.stack = ExitStack()
        self.probe = self.stack.enter_context(_UiProbe())
        self.probe.session.set('provider', 'api')
        self.probe.session.set('model_name', 'gpt-4o-mini')
        self.stack.enter_context(patch.object(ui, 'get_chat_llm', return_value=SimpleNamespace(invoke=self.invoke)))
        self.query = SyntheticQuery()
        self.tools = ChatTools(query_service=lambda: self.query, read_job=lambda _: None,
                               current_job_id=lambda: None, session_id='synthetic', read_result=lambda _: None)
        self.agent = ChatAgent(ui._chat_model, self.tools)
        self.addCleanup(self.save_captures)

    async def asyncTearDown(self):
        self.stack.close()

    def invoke(self, messages):
        self.calls.append([{'type': m.type, 'content': m.content} for m in messages])
        response = self.actions.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(self.history())
        if isinstance(response, str):
            return SimpleNamespace(content=response)
        return SimpleNamespace(content=json.dumps(response))

    def save_captures(self):
        root = os.environ.get('BMG_ACCEPTANCE_ARTIFACTS')
        if root:
            path = Path(root) / ('provider-' + self._testMethodName + '.json')
            with path.open('x', encoding='utf-8') as stream:
                json.dump(self.calls, stream, ensure_ascii=False, indent=2)

    def history(self, index=-1):
        return json.loads(self.calls[index][-1]['content'])

    def envelope(self, index=-1):
        return json.loads(self.history(index)[-1]['content'])

    def conversation(self, answer):
        self.actions.extend([{'kind': 'plan', 'task': 'conversation'}, {'kind': 'answer', 'text': answer}])

    def plan_filter_from(self, read_value):
        def plan(history):
            context = json.loads(history[-1]['content'])['context']
            value, unit = read_value(context)
            return {'kind': 'plan', 'task': 'records', 'arguments': {'limit': 5, 'conditions': [
                {'field': 'C11', 'op': 'gte', 'value': value, 'unit': unit}]}}
        self.actions.append(plan)

    def assert_frozen_filter(self, value):
        expected = {'schema_version': 'latticegpt_database_read_request_v1', 'kind': 'records',
                    'filters': [{'field': 'C11', 'op': 'gte', 'value': value}], 'limit': 5,
                    'order_by': [{'field':'sample_id', 'direction':'asc'}]}
        tools = self.probe.session.get('product_chat_tools') or self.tools
        self.assertEqual(tools.last_database_read['query_identity'], canonical_sha256(expected))
        self.assertEqual(self.query.requests[-1], expected)

    async def test_followup_binds_actual_database_row_in_its_original_units(self):
        self.query.rows[0].update({n: (i + 1) * .0712345 for i, n in enumerate(STIFFNESS_NAMES)})
        self.actions.append({'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}})
        self.assertEqual((await self.agent.run('Show one database record.'))['status'], 'answered')
        self.tools.records['result']['provenance']['diagnostic'] = 'SYNTHETIC-RAW-TRACE'
        reference = self.tools.records['result_ref']
        def threshold(context):
            result = context['records']['result']
            self.assertEqual(result['source'], 'formal_150w_database')
            self.assertEqual(result['data']['rows'], self.query.rows[:1])
            self.assertEqual(result['units'], {'stiffness': 'C/Eb', 'relative_density': 'ratio',
                                                'diameter': 'normalized_ratio'})
            return result['data']['rows'][0]['C11'], result['units']['stiffness']
        self.plan_filter_from(threshold)
        result = await self.agent.run("Find five records at or above the previous row's C11 in database units.")
        self.assertEqual(result['status'], 'answered', result)
        self.assert_frozen_filter(.0712345)
        self.assertEqual(len(self.query.requests), 2)
        self.assertNotIn(reference, str(self.calls))
        self.assertNotIn('SYNTHETIC-RAW-TRACE', str(self.calls))

    async def test_ui_selected_csv_and_overridden_candidate_supply_distinct_thresholds(self):
        from product_workflow.inputs import parse_target_csv_upload
        job = await self.cache_active_job()
        preferred = job['candidate_set']['candidates'][0]
        selected = {**preferred, 'candidate_id': 'candidate-2', 'rank': 2,
                    'predicted_stiffness': dict.fromkeys(STIFFNESS_NAMES, 27.5)}
        job['candidate_set']['candidates'].append(selected)
        job['selection'].update(selected_candidate_id=selected['candidate_id'], selected_rank=2,
                                selection_source='user_override')
        self.tools.current_job_revision = lambda: job['revision']
        self.tools.job_context = None
        self.probe.session.set('product_chat_tools', self.tools)
        state = ui._csv_import_state()
        rows = [','.join(STIFFNESS_NAMES), ','.join(['99'] * 9), ','.join(['6.875'] * 9)]
        state['parsed'] = parse_target_csv_upload('\n'.join(rows).encode(), filename='PRIVATE-PICK.csv')
        with patch.object(ui, '_offer_target', new=AsyncMock()):
            await ui._select_csv_record(state, 2)
        def selected_candidate(ctx):
            current = ctx['job']
            pick = current['selection']
            self.assertEqual((pick['selected_rank'], pick['selection_source'], pick['recommended_candidate_id']),
                             (2, 'user_override', preferred['candidate_id']))
            candidate = next(row for row in current['candidates'] if row['candidate_id'] == pick['selected_candidate_id'])
            self.assertEqual(current['candidate_confirmation'], 'UNCONFIRMED')
            return candidate['predicted_stiffness']['C11'], current['candidate_units']['predicted_stiffness']
        self.plan_filter_from(selected_candidate)
        self.assertEqual((await self.agent.run("Use the selected candidate's predicted C11 as the lower bound."))['status'], 'answered')
        self.assert_frozen_filter(.25)
        self.assertIsNone(self.tools.job_context)
        await self.tools.execute('get_job_context', {})
        def selected_csv(ctx):
            pending = ctx['pending_csv_target']
            self.assertEqual((pending['source'], pending['record_index'], pending['record_count'], pending['state']),
                             ('csv_upload', 2, 2, 'PROPOSED'))
            self.assertEqual(selected_candidate(ctx), (27.5, 'GPa'))
            self.assertEqual(ctx['job']['target']['physical_9d_gpa']['C11'], 16.5)
            return pending['physical_9d_gpa']['C11'], pending['unit']
        self.plan_filter_from(selected_csv)
        self.assertEqual((await self.agent.run("Use only the selected CSV record's C11 as the lower bound."))['status'], 'answered')
        self.assert_frozen_filter(.0625)
        self.assertNotIn('PRIVATE-PICK', str(self.calls))
        self.assertEqual(self.tools.pending, {})

    async def cache_active_job(self):
        job = _job()
        target = {n: 16.5 + i for i, n in enumerate(STIFFNESS_NAMES)}
        job['target'] = target_from_physical_9d(target, source_kind='csv_upload', source_name='PRIVATE-TARGET.csv',
            source_sha256='B' * 64, source_record_index=2, source_record_count=3)
        job['candidate_set']['candidates'][0].update(rank=1, relative_density=.23, normalized_diameter=.19,
            predicted_stiffness={n: v + 1 for n, v in target.items()})
        job['inverse_result'] = {'diagnostics': [{'trace': 'PRIVATE-DIAGNOSTIC'}]}
        self.tools.current_job_id = lambda: job['job_id']
        self.tools.read_job = lambda _: copy.deepcopy(job)
        await self.tools.execute('get_job_context', {})
        return job

    async def test_followup_binds_current_job_target_in_gpa_once_and_distinguishes_selected_csv(self):
        job = await self.cache_active_job()
        csv_values = {n: 33. + i for i, n in enumerate(STIFFNESS_NAMES)}
        self.tools.set_pending_csv_target(dict(record_index=3, record_count=4, state='PROPOSED',
            physical_9d_gpa=csv_values, filename='PRIVATE-NEW.csv', sha256='F' * 64))
        before = self.tools.snapshot()
        def threshold(context):
            current = context['job']
            target = current['target']
            self.assertEqual((current['job_id'], current['revision']), (job['job_id'], job['revision']))
            self.assertEqual(target['source'], {'kind':'csv_upload', 'record_index':2, 'record_count':3, 'units':'GPa'})
            self.assertEqual(target['physical_9d_gpa'], job['target']['physical_9d_gpa'])
            self.assertEqual(target['normalized_9d'], job['target']['normalized_9d'])
            self.assertEqual(target['units'], {'physical_9d_gpa': 'GPa', 'normalized_9d': 'C/Eb'})
            candidate = current['candidates'][0]
            self.assertEqual(candidate['predicted_stiffness'], job['candidate_set']['candidates'][0]['predicted_stiffness'])
            self.assertEqual((candidate['origin'], candidate['normalized_diameter'], candidate['relative_density']),
                             ('inverse_generated_structure', .19, .23))
            self.assertEqual(current['candidate_units'], {'predicted_stiffness': 'GPa', 'relative_density': 'ratio',
                                                         'normalized_diameter': 'normalized_ratio'})
            self.assertEqual(current['selection']['selected_candidate_id'], candidate['candidate_id'])
            csv = context['pending_csv_target']
            self.assertEqual((csv['source'], csv['state'], csv['record_index'], csv['unit'], csv['physical_9d_gpa']),
                             ('csv_upload', 'PROPOSED', 3, 'GPa', csv_values))
            return target['physical_9d_gpa']['C11'], target['units']['physical_9d_gpa']
        self.plan_filter_from(threshold)
        result = await self.agent.run("Use the current Job target's physical C11 as the minimum. Find five records.")
        self.assertEqual(result['status'], 'answered', result)
        self.assert_frozen_filter(16.5 / BASE_MATERIAL_MODULUS_GPA)
        self.assertEqual(self.tools.last_query['conditions'][0]['value'], 16.5)
        self.assertEqual(self.tools.last_query['conditions'][0]['unit'], 'GPa')
        self.assertEqual(self.tools.snapshot()['job'], before['job'])
        self.assertEqual(self.tools.snapshot()['pending_csv_target'], before['pending_csv_target'])
        self.assertEqual(len(self.query.requests), 1)
        for marker in ('PRIVATE-', before['job']['job_ref'], before['job']['job_sha256'], 'inverse_summary'):
            self.assertNotIn(marker, str(self.calls))

    async def test_stale_job_and_database_bindings_are_omitted_before_planning(self):
        job = await self.cache_active_job()
        await self.tools.execute('query_records', {'limit': 1})
        self.query.source_sha256 = 'E' * 64
        job['revision'] += 1
        self.actions.append({'kind':'clarify', 'text':'Please identify a current numerical source.'})
        self.assertEqual((await self.agent.run('Which cached values can I refer to?'))['status'], 'needs_clarification')
        context = self.envelope()['context']
        self.assertIsNone(context['records'])
        self.assertIsNone(context['job'])
        self.assertEqual(len(self.query.requests), 1)

    async def test_reopened_current_target_can_bind_first_query_without_action_cache(self):
        job = _job()
        job['target'] = target_from_physical_9d({n:27.5 for n in STIFFNESS_NAMES}, source_kind='csv_upload',
            source_name='PRIVATE-REOPEN.csv', source_sha256='A' * 64)
        store = SimpleNamespace(get_consistent=Mock(side_effect=lambda _: copy.deepcopy(job)))
        with patch.object(ui, '_job_store', return_value=store), patch.object(ui, '_query_service', return_value=self.query), \
                patch.object(ui, 'render_product_job', new=AsyncMock()):
            self.assertTrue(await ui.resume_product_job(job['job_id']))
            current = ui._product_chat_tools()
            self.assertIsNone(current.job_context)
            # A legitimate saved update can advance beyond the revision displayed at reopen.
            job['revision'] += 1
            job['target'] = target_from_physical_9d({n:30.8 for n in STIFFNESS_NAMES}, source_kind='csv_upload',
                source_name='PRIVATE-REOPEN.csv', source_sha256='A' * 64)
            self.plan_filter_from(lambda c: (c['job']['target']['physical_9d_gpa']['C11'],
                                            c['job']['target']['units']['physical_9d_gpa']))
            result = await ui._run_product_chat("Find five database records at or above my current target's physical C11.")
            self.assertEqual(result['status'], 'answered', result)
            self.assertIsNone(current.job_context)
            self.assertEqual(current.pending, {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.query.requests), 1)
        self.assertEqual(self.envelope(0)['context']['job']['revision'], job['revision'])
        self.assert_frozen_filter(30.8 / BASE_MATERIAL_MODULUS_GPA)
        for marker in ('PRIVATE-REOPEN', 'job_ref', 'job_sha256', 'candidate_set_sha256'):
            self.assertNotIn(marker, self.envelope(0)['context']['job'])
            self.assertNotIn(marker, json.dumps(self.envelope(0)['context']))

    async def test_cold_conversation_receives_current_values_and_discards_a_late_answer(self):
        job = _job()
        job['target'] = target_from_physical_9d({n:27.5 for n in STIFFNESS_NAMES}, source_kind='csv_upload',
            source_name='PRIVATE-REOPEN.csv', source_sha256='A' * 64)
        self.tools.current_job_id = lambda: job['job_id']
        self.tools.current_job_revision = lambda: 4
        self.tools.read_job = lambda _: copy.deepcopy(job)
        question = 'What is my current target C11?'
        self.conversation('The recorded target C11 is 27.5 GPa.')
        response = await self.agent.run(question)
        self.assertEqual(response['status'], 'answered')
        self.assertEqual(response['job_evidence']['revision'], 4)
        self.assertEqual(self.envelope(0)['context']['job'], self.envelope(1)['context']['job'])
        self.assertEqual(self.envelope(1)['context']['job']['target']['physical_9d_gpa']['C11'], 27.5)
        stored = copy.deepcopy(self.agent.history)
        def changed(history):
            job['revision'] += 1
            return {'kind': 'answer', 'text': 'Old values.'}
        self.actions.extend([{'kind':'plan','task':'conversation'}, changed])
        self.assertEqual((await self.agent.run(question))['status'], 'stale')
        self.assertEqual(self.agent.history, stored)
        self.assertIsNone(self.tools.job_context)

    async def test_cross_job_and_cross_session_record_sets_are_absent_from_the_planner(self):
        active = ['job-' + 'C' * 24]
        self.tools.current_job_id = lambda: active[0]
        await self.tools.execute('query_records', {'limit': 1})
        for mode in ('other-job', 'other-session'):
            if mode == 'other-job':
                active[0] = 'job-' + 'F' * 24
            else:
                active[0] = 'job-' + 'C' * 24
                self.tools.session_id = 'new-session'
            self.actions.append({'kind':'clarify', 'text':'Which reference should be used?'})
            self.assertEqual((await self.agent.run('Use the earlier row.'))['status'], 'needs_clarification')
            self.assertIsNone(self.envelope()['context']['records'])
        self.assertEqual(len(self.query.requests), 1)

    async def test_changed_job_during_numeric_planning_cannot_freeze_stale_read(self):
        job = await self.cache_active_job()
        def change(history):
            job['revision'] += 1
            return {'kind':'plan','task':'records','arguments':{'limit':5,'conditions':[
                {'field':'C11','op':'gte','value':16.5,'unit':'GPa'}]}}
        self.actions.append(change)
        result = await self.agent.run('Use the Job target as the lower bound.')
        self.assertIn(result['status'], ('error', 'stale'))
        self.assertEqual(self.query.requests, [])
        self.assertEqual(len(self.calls), 1)

    async def test_database_row_expiry_or_source_change_during_planning_blocks_numeric_read(self):
        from product_workflow.chat_tools import TTL_SECONDS
        for cause in ('expiry', 'source'):
            with self.subTest(cause=cause):
                now = [0.]
                self.tools.clock = lambda: now[0]
                await self.tools.execute('query_records', {'limit': 1})
                count_before = len(self.query.requests)
                def plan(history):
                    row = json.loads(history[-1]['content'])['context']['records']['result']['data']['rows'][0]
                    if cause == 'expiry':
                        now[0] += TTL_SECONDS
                    else:
                        self.query.projection_sha256 = 'F' * 64
                    return {'kind':'plan', 'task':'records', 'arguments':{'limit': 5, 'conditions':[
                        {'field':'C11','op':'gte','unit':'C/Eb','value':row['C11']}]}}
                self.actions.append(plan)
                result = await self.agent.run("Use the prior row's C11 as the lower bound.")
                self.assertIn(result['status'], ('needs_clarification', 'error', 'stale'))
                self.assertEqual(len(self.query.requests), count_before)

    async def test_query_result_and_history_are_projected_at_the_provider(self):
        original = self.query.execute_request
        def query(request):
            result = original(request)
            result['provenance']['diagnostic'] = 'SYNTHETIC-PROVENANCE-DETAIL'
            result['trace'] = ['SYNTHETIC-TRACE'] * 4000
            return result
        self.query.execute_request = query
        shown = []
        async def show(name, result):
            shown.append(copy.deepcopy(result))
        self.agent.on_tool = show
        text = '  Keep my path Q:/synthetic/user-given.csv and \n the 22 GPa threshold.  '
        self.agent.history = [{'role': 'user', 'content': 'Earlier user question.'},
                              {'role': 'assistant', 'content': 'SYNTHETIC-LEGACY-PROGRAM-DETAIL'},
                              {'role': 'tool', 'content': 'SYNTHETIC-LEGACY-TOOL'}]
        conditions = [{'field': 'C11', 'op': 'gte', 'value': 22, 'unit': 'GPa'}]
        self.actions.extend([{'kind': 'plan', 'task': 'records', 'arguments': {'conditions': conditions, 'limit': 1}, 'explain_result': True},
                             {'kind': 'answer', 'text': 'One synthetic row.'}])
        result = await self.agent.run(text)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(len(self.query.requests), 1)
        self.assertEqual(len(shown[0]['trace']), 4000)
        self.assertEqual(len(self.calls), 2)
        tool_messages = self.history()
        action = json.loads(tool_messages[-2]['content'])
        self.assertEqual(action, {'kind': 'tool', 'name': 'query_records',
                                  'arguments': {'conditions': conditions, 'limit': 1}})
        self.assertIn('17', tool_messages[-1]['content'])
        self.assertIn('0.01', tool_messages[-1]['content'])
        self.conversation('Explain the two units separately.')
        self.assertEqual((await self.agent.run('Explain the units.'))['status'], 'answered')
        row = self.envelope()['context']['records']['result']['data']['rows'][0]
        self.assertEqual((row['sample_id'], row['C11']), (17, 0.01))
        self.conversation('I can elaborate.')
        self.assertEqual((await self.agent.run('Continue.'))['status'], 'answered')
        for i in range(len(self.calls)):
            content = self.calls[i][-1]['content']
            for marker in ('SYNTHETIC-PROVENANCE-DETAIL', 'SYNTHETIC-TRACE', 'SYNTHETIC-LEGACY'):
                self.assertNotIn(marker, content)
            self.assertIn({'role': 'user', 'content': text}, self.history(i))
        self.assertIn({'role': 'assistant', 'content': 'Explain the two units separately.'}, self.history())
        self.assertIn({'role': 'user', 'content': 'Earlier user question.'}, self.history(0))

    async def test_controlled_answer_and_receipt_replay_use_only_safe_memory(self):
        job = {'job_id': 'job-' + 'C' * 24, 'status': 'CANDIDATES_READY', 'revision': 4,
               'candidate_confirmation': 'CONFIRMED', 'selection': {'selected_candidate_id': 'inverse_150w_0001'},
               'candidates': [], 'allowed_actions': [], 'target': {}, 'mesh': {'path': 'SYNTHETIC-MESH-PATH'}}
        agent = ChatAgent(ui._chat_model, tools_for(job))
        self.actions.append({'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates'}})
        with patch('product_workflow.presentation.build_job_fact_reply', return_value='SYNTHETIC-UI-JOB-DETAIL'):
            self.assertEqual((await agent.run('Compare current candidates.'))['text'], 'SYNTHETIC-UI-JOB-DETAIL')
            receipt = {'action': 'select_candidate', 'failed': False, 'private': 'SYNTHETIC-RECEIPT-DETAIL'}
            self.assertEqual((await agent.run('SYNTHETIC-AUTOMATED-RECEIPT-MESSAGE', receipt=receipt))['status'], 'answered')
        self.assertEqual(len(self.calls), 1)
        before = copy.deepcopy(agent.history)
        agent.history = copy.deepcopy(before)  # Same restoration operation used by the UI on failed publication.
        self.conversation('Normal next turn.')
        self.assertEqual((await agent.run('What comes next?'))['status'], 'answered')
        self.assertEqual(before, agent.history[:-2])
        for marker in ('SYNTHETIC-UI-JOB-DETAIL', 'SYNTHETIC-MESH-PATH', 'SYNTHETIC-AUTOMATED', 'SYNTHETIC-RECEIPT'):
            self.assertNotIn(marker, self.calls[-1][-1]['content'])
        self.assertIn(job['job_id'], self.calls[-1][-1]['content'])

    async def test_failed_receipt_and_bad_response_never_replay_diagnostics(self):
        self.actions.append(RuntimeError('SYNTHETIC-SDK-DETAIL'))
        receipt = {'action': 'start_conformal', 'failed': True, 'session_generation': 'SYNTHETIC-GENERATION',
                   'private': {'error': 'SYNTHETIC-RAW-RECEIPT'}}
        result = await self.agent.run('SYNTHETIC-AUTOMATED-INSTRUCTION', receipt=receipt)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.envelope()['execution_receipt']['failed'])
        self.assertEqual(self.envelope()['execution_receipt']['action'], 'start_conformal')
        self.conversation('Fresh reply.')
        self.assertEqual((await self.agent.run('Can we continue?'))['status'], 'answered')
        self.assertEqual(len(self.calls), 3)
        for marker in ('SYNTHETIC-AUTOMATED', 'SYNTHETIC-GENERATION', 'SYNTHETIC-RAW', 'SYNTHETIC-SDK'):
            self.assertNotIn(marker, str(self.calls))

    async def test_legacy_cached_job_not_matching_current_job_is_not_presented_as_current(self):
        self.tools.job_context = {'job_id': 'job-' + 'B' * 24, 'revision': 6, 'status': 'VIEWER_READY'}
        self.conversation('No currently open job.')
        self.assertEqual((await self.agent.run('Is a job open?'))['status'], 'answered')
        for i in range(len(self.calls)):
            self.assertIsNone(self.envelope(i)['context']['job'])
            self.assertNotIn('job-' + 'B' * 24, str(self.calls[i]))

    async def test_fallback_prepares_using_observed_ref_and_stale_ref_stays_rejected(self):
        now = [0.]
        self.tools.clock = lambda: now[0]
        raw_result = await self.tools.execute('query_records', {'limit': 1})
        ref = raw_result['result_ref']
        shown = []
        async def show(name, result):
            shown.append(copy.deepcopy(result))
        self.agent.on_tool = show
        def pick(history):
            context = json.loads(history[-1]['content'])['context']
            self.assertEqual(context['records']['result_ref'], ref)
            return {'kind': 'tool', 'name': 'prepare_design_target',
                    'arguments': {'result_ref': ref, 'position': 1}}
        self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, pick])
        self.assertEqual((await self.agent.run('Prepare a card for the first record.'))['status'], 'waiting_for_user')
        self.assertNotIn(ref, self.calls[0][-1]['content'])
        self.assertEqual(len(shown), 1)
        token = shown[0]['confirmation_ref']
        self.assertEqual(self.tools.pending[token]['state'], 'issued')
        self.assertEqual(shown[0]['target']['physical_9d_gpa']['C11'], 1.1)
        now[0] = 950.
        self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, {'kind': 'tool', 'name': 'prepare_design_target',
                             'arguments': {'result_ref': ref, 'position': 1}}])
        result = await self.agent.run('Use that prior record again.')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(shown), 1)
        for i in (2, 3):
            self.assertNotIn(ref, self.calls[i][-1]['content'])
            self.assertIsNone(self.envelope(i)['context']['records'])
        self.assertNotIn(token, str(self.calls))

    async def test_empty_error_and_malformed_response_have_no_retry_or_false_success(self):
        original = self.query.execute_request
        for status in ('empty', 'error'):
            def terminal(request):
                result = original(request)
                result.update(status=status, data={'returned_count': 0, 'rows': []} if status == 'empty' else None,
                              error={'code': 'BINDING_MISMATCH', 'details': 'SYNTHETIC-DIAGNOSTICS'})
                return result
            self.query.execute_request = terminal
            self.actions.append({'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}})
            before = len(self.calls)
            self.assertEqual((await self.agent.run('Read one record.'))['status'], status)
            self.assertEqual(len(self.calls), before + 1)
            self.actions.append('SYNTHETIC-MALFORMED-RAW-RESPONSE')
            self.assertEqual((await self.agent.run('What happened?'))['status'], 'error')
            self.assertEqual(len(self.calls), before + 2)
            self.assertEqual(self.envelope()['context']['last_database_read']['status'], status)
        self.assertNotIn('SYNTHETIC-DIAGNOSTICS', str(self.calls))
        self.assertNotIn('SYNTHETIC-MALFORMED-RAW-RESPONSE', str(self.calls))
        self.assertEqual(len(self.query.requests), 2)

    async def test_reopen_routes_to_fresh_session_and_rereads_the_bound_job(self):
        from tests.test_product_job_state import _job
        job = _job()
        job['target'].update(physical_9d_gpa={'C11': 22.}, source={'kind': 'csv_upload',
                             'source_name': 'SYNTHETIC-PRIVATE-CSV.csv'})
        store = SimpleNamespace(get_consistent=Mock(side_effect=lambda _: copy.deepcopy(job)))
        self.probe.session.set('product_chat_agent', self.agent)
        self.agent.history.append({'role': 'assistant', 'content': 'SYNTHETIC-OLD-SESSION'})
        with patch.object(ui, '_job_store', return_value=store), patch.object(ui, 'render_product_job', new=AsyncMock()):
            self.assertTrue(await ui.resume_product_job(job['job_id']))
            self.assertIsNone(self.probe.session.get('product_chat_agent'))
            self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, {'kind': 'answer', 'text': 'Use recorded facts.'}])
            reply = await ui._run_product_chat('Explain the reopened Job.')
        self.assertEqual(reply['status'], 'answered', reply)
        self.assertEqual(reply['answer_origin'], 'program_job_facts')
        self.assertIsNot(self.probe.session.get('product_chat_agent'), self.agent)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.envelope(0)['context']['active_job_id'], job['job_id'])
        self.assertEqual(self.envelope(0)['context']['job']['target']['physical_9d_gpa']['C11'], 22.)
        current = self.envelope(1)['context']['job']
        self.assertEqual(current['job_id'], job['job_id'])
        self.assertEqual(current['revision'], job['revision'])
        self.assertNotIn('SYNTHETIC-PRIVATE-CSV', str(self.calls))
        self.assertNotIn('SYNTHETIC-OLD-SESSION', str(self.calls))
        self.assertTrue(all(call.args == (job['job_id'],) for call in store.get_consistent.call_args_list))

    async def test_model_echo_and_machine_shaped_clarification_stay_out_of_later_turns(self):
        result = await self.tools.execute('query_records', {'limit': 1})
        ref = result['result_ref']
        def echo(history):
            self.assertEqual(json.loads(history[-1]['content'])['context']['records']['result_ref'], ref)
            return {'kind': 'clarify', 'text': 'Can you confirm record ' + ref + '?'}
        self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, echo,
                             {'kind': 'clarify', 'text': 'Check https://example.invalid/private-detail then reply.'}])
        self.assertIn(ref, (await self.agent.run('Which record can I choose?'))['text'])
        self.assertEqual((await self.agent.run('Explain the options.'))['status'], 'needs_clarification')
        self.conversation('Normal follow-up.')
        self.assertEqual((await self.agent.run('Explain once more.'))['status'], 'answered')
        for call in self.calls[2:]:
            self.assertNotIn(ref, call[-1]['content'])
            self.assertNotIn('private-detail', call[-1]['content'])
        self.assertTrue(any('private-detail' in row['content'] for row in self.agent.history))

    async def test_over_budget_user_and_nontext_program_object_stop_before_provider(self):
        self.assertEqual((await self.agent.run({'path': 'PRIVATE-OBJECT'}))['status'], 'error')
        self.assertEqual((await self.agent.run('A' * 80000))['status'], 'error')
        self.assertEqual(self.calls, [])

    async def test_job_read_followup_passes_full_action_protocol_and_only_recorded_facts(self):
        job = {'job_id': 'job-' + 'D' * 24, 'revision': 2, 'status': 'TARGET_PROPOSED', 'candidates': [],
               'allowed_actions': ['confirm_target_and_generate'], 'candidate_confirmation': 'UNCONFIRMED',
               'target': {'physical_9d_gpa': {'C11': 22.}, 'source': {'kind': 'csv_upload',
                          'source_name': 'SYNTHETIC-UPLOAD-LOCATION'}},
               'runtime_detail': {'blob': 'SYNTHETIC-JOB-DEBUG'}}
        tools = tools_for(job)
        delivered = []
        async def show(name, result):
            delivered.append((name, copy.deepcopy(result)))
        agent = ChatAgent(ui._chat_model, tools, on_tool=show)
        read = {'kind': 'tool', 'name': 'get_job_context', 'arguments': {'job_id': job['job_id']}}
        def prepare(history):
            self.assertEqual(json.loads(history[-2]['content']), read)
            facts = json.loads(history[-1]['content'])
            self.assertEqual(facts['job_id'], job['job_id'])
            self.assertEqual(facts['candidate_confirmation'], 'UNCONFIRMED')
            self.assertEqual(facts['allowed_actions'], ['confirm_target_and_generate'])
            self.assertEqual(facts['job_ref'], delivered[0][1]['job_ref'])
            return {'kind': 'tool', 'name': 'prepare_workflow_action', 'arguments': {
                'job_ref': facts['job_ref'], 'action': 'confirm_target_and_generate'}}
        self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, read, prepare])
        result = await agent.run('Read the current Job, then prepare its generation confirmation card.')
        self.assertEqual(result['status'], 'waiting_for_user', result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(delivered[0][1]['runtime_detail']['blob'], 'SYNTHETIC-JOB-DEBUG')
        token = delivered[-1][1]['confirmation_ref']
        self.assertEqual(tools.pending[token]['state'], 'issued')
        for marker in (token, 'SYNTHETIC-JOB-DEBUG', 'SYNTHETIC-UPLOAD-LOCATION'):
            self.assertNotIn(marker, str(self.calls))

    async def test_cold_job_cannot_rewind_below_open_revision(self):
        job = await self.cache_active_job()
        self.tools.job_context = None
        self.tools.current_job_revision = lambda: job['revision'] + 1
        self.actions.append({'kind': 'clarify', 'text': 'Which source?'})
        await self.agent.run('Use my current target C11.')
        self.assertIsNone(self.envelope()['context']['job'])
        self.assertIsNone(self.tools.job_context)

    async def test_cold_first_explanation_has_recorded_metrics_and_surrogate_scope(self):
        for schema, metric in (('latticegpt_candidate_set_v1', 'score'), ('latticegpt_candidate_set_v2', 'nmse')):
            with self.subTest(schema=schema):
                job = await self.cache_active_job()
                job['candidate_set']['schema_version'] = schema
                row = job['candidate_set']['candidates'][0]
                row.update(nmse=.0025, score=.123)
                self.tools.job_context = None
                self.tools.current_job_revision = lambda: job['revision']
                self.conversation('The matching error refers to the saved surrogate prediction.')
                await self.agent.run('What error is recorded for the selected candidate, and was it FEA validated?')
                current = self.envelope()['context']['job']
                chosen = next(item for item in current['candidates'] if item['candidate_id'] == current['selection']['selected_candidate_id'])
                self.assertEqual(current['ranking']['metric'], metric)
                self.assertEqual((chosen['nmse'], current['candidate_units']['nmse']), (.0025, 'ratio'))
                if metric == 'score':
                    self.assertEqual(chosen['score'], .123)
                else:
                    self.assertNotIn('score', chosen)
                self.assertEqual(current['candidate_interpretation']['prediction'], 'forward_surrogate')
                self.assertEqual(current['candidate_interpretation']['validation'], 'surrogate prediction only')
                self.assertIsNone(self.tools.job_context)
                self.assertEqual(self.tools.pending, {})

    async def test_source_rejected_in_planning_stays_hidden_in_workflow_execution(self):
        await self.tools.execute('query_records', {'limit': 1})
        self.query.source_sha256 = 'F' * 64
        self.actions.extend([{'kind': 'plan', 'task': 'workflow'}, {'kind': 'clarify', 'text': 'Which source?'}])
        await self.agent.run('Explain how I could select a record.')
        self.assertIsNone(self.envelope(0)['context']['records'])
        self.assertIsNone(self.envelope(1)['context']['records'])
        self.assertNotIn(self.tools.records['result_ref'], str(self.calls))

    async def test_conversation_rejects_row_source_changed_during_answer(self):
        await self.tools.execute('query_records', {'limit': 1})
        def replace_source(history):
            self.query.manifest_sha256 = 'F' * 64
            return {'kind': 'answer', 'text': 'The current C11 is 0.01.'}
        self.actions.extend([{'kind': 'plan', 'task': 'conversation'}, replace_source])
        result = await self.agent.run('Explain the previous C11.')
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(self.agent.history, [])
        self.assertEqual(len(self.query.requests), 1)

    async def test_revised_job_during_workflow_binding_cannot_issue_fresh_card_for_old_plan(self):
        from llm_provider import resolve_model_runtime_profile
        from tests.test_product_chat_workflow import ChatWorkflowTests
        from tests.test_product_workflow_mesh_runtime import target_values
        fixture = ChatWorkflowTests()
        fixture.make_service()
        profile = resolve_model_runtime_profile(provider='api', model_name='gpt-4o-mini',
                                                selection_source='session_selection', session_bound=True)
        job = fixture.service.create_job(target_from_physical_9d(target_values(), source_kind='csv_upload',
            source_sha256='A' * 64, source_name='synthetic.csv'), model_runtime_profile=profile)
        self.tools.read_job = lambda _: copy.deepcopy(job)
        self.tools.current_job_id = lambda: job['job_id']
        current = await self.tools.execute('get_job_context', {})
        self.assertIn('confirm_target_and_generate', current['allowed_actions'])
        bind = self.tools.bind_workflow_plan
        async def change_during_read(args, *, run_id):
            job['revision'] += 1
            return await bind(args, run_id=run_id)
        self.tools.bind_workflow_plan = change_during_read
        self.actions.append({'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'prepare_generation'}})
        result = await self.agent.run('Prepare a generation confirmation card for the current target.')
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(self.tools.pending, {})
        self.assertEqual(self.agent.history, [])

    async def test_unselected_csv_values_stay_absent_across_both_provider_phases(self):
        for summary in (dict(record_index=3, record_count=2, state='PROPOSED'),
                        dict(record_index=1, record_count=2, state='RETIRED')):
            with self.subTest(summary=summary):
                self.tools.set_pending_csv_target({**summary, 'physical_9d_gpa': dict.fromkeys(STIFFNESS_NAMES, 55.5)})
                self.conversation('Please select a valid row.')
                self.assertEqual((await self.agent.run('What is the selected CSV C11?'))['status'], 'answered')
                for offset in (-2, -1):
                    selected = self.envelope(offset)['context']['pending_csv_target']
                    self.assertNotIn('physical_9d_gpa', selected)
                    self.assertNotIn('55.5', str(selected))

    async def test_clarification_cannot_publish_expired_row_or_revised_job(self):
        from product_workflow.chat_tools import TTL_SECONDS
        job = await self.cache_active_job()
        now = [0]
        self.tools.clock = lambda: now[0]
        for cause in ('revision', 'expiry'):
            with self.subTest(cause=cause):
                await self.tools.execute('get_job_context', {})
                await self.tools.execute('query_records', {'limit': 1})
                def clarify(history):
                    if cause == 'expiry':
                        now[0] += TTL_SECONDS
                    else:
                        job['revision'] += 1
                    return {'kind': 'clarify', 'text': 'Use the value just shown?'}
                self.actions.append(clarify)
                self.assertEqual((await self.agent.run('Which C11?'))['status'], 'stale')
                self.assertEqual(self.agent.history, [])

    async def test_changed_job_or_result_source_during_database_explanation(self):
        job = await self.cache_active_job()
        for cause in ('job', 'result_source'):
            with self.subTest(cause=cause):
                await self.tools.execute('get_job_context', {})
                def late(history):
                    if cause == 'job':
                        job['revision'] += 1
                    else:
                        self.query.projection_sha256 = 'E' * 64
                    return {'kind': 'answer', 'text': 'Use the earlier result as current.'}
                self.actions.extend([{'kind': 'plan', 'task': 'records', 'arguments': {'limit': 2}, 'explain_result': True}, late])
                result = await self.agent.run('Show two rows using the current target.')
                self.assertEqual(result['status'], 'stale')
                self.assertNotIn('display_read', result)
                self.assertEqual(self.agent.history, [])
        self.assertEqual(len(self.query.requests), 2)

    async def test_completed_read_does_not_republish_changed_job_after_failure_or_timeout(self):
        import asyncio
        job = await self.cache_active_job()
        for interruption in ('error', 'timeout'):
            with self.subTest(interruption=interruption):
                await self.tools.execute('get_job_context', {})
                async def model(messages):
                    envelope = json.loads(messages[-1]['content'])
                    if envelope.get('phase') == 'planning':
                        return {'kind': 'plan', 'task': 'records', 'arguments': {'limit': 1}, 'explain_result': True}
                    job['revision'] += 1
                    if interruption == 'timeout':
                        await asyncio.Event().wait()
                    raise ValueError('synthetic interrupted explanation')
                agent = ChatAgent(model, self.tools, timeout_seconds=.35)
                result = await agent.run('Find one reference for my current target.')
                self.assertEqual(result['status'], 'stale')
                self.assertNotIn('display_read', result)
                self.assertEqual(agent.history, [])
        self.assertEqual(len(self.query.requests), 2)

