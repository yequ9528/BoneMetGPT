"""Core inverse dialogue contracts; no model or algorithm computation."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from product_workflow.chat_agent import ChatAgent
from product_workflow.chat_tools import ChatCase, ChatTools
from product_workflow.presentation import recorded_chat_reply


def tools_for(job, read_result=lambda _: {'status': 'verified'}):
    tools = ChatTools(query_service=lambda: None, read_job=lambda _: copy.deepcopy(job),
                      read_result=read_result, current_job_id=lambda: job['job_id'], session_id='acceptance')
    # Keep storage and actual read/dispatch guards; this fixture has no geometry.
    tools._job_view = lambda value: copy.deepcopy(value)
    return tools


class DialogueAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_workflow_prepares_generation_and_conformal_without_running_them(self):
        from llm_provider import resolve_model_runtime_profile
        from product_workflow.contracts import target_from_physical_9d
        from tests.test_product_chat_workflow import ChatWorkflowTests
        from tests.test_product_workflow_mesh_runtime import VALID_INP, target_values
        fixture = ChatWorkflowTests()
        fixture.make_service()
        profile = resolve_model_runtime_profile(provider='api', model_name='gpt-4o-mini',
                                                selection_source='session_selection', session_bound=True)
        job = fixture.service.create_job(target_from_physical_9d(target_values(), source_kind='csv_upload',
            source_sha256='A'*64, source_name='synthetic.csv'), model_runtime_profile=profile)
        for action, question in (
                ('confirm_target_and_generate', 'Confirm this target and prepare the card to generate three candidates.'),
                ('start_conformal', 'Prepare the confirmation card to start conformal generation.')):
            with self.subTest(action=action):
                tools = ChatTools(query_service=lambda: fixture.query, read_job=fixture.service.get_consistent_job,
                    read_result=lambda _: self.fail('No result read expected'),
                    current_job_id=lambda: job['job_id'], session_id=action)
                calls, shown = [], []
                def model(messages):
                    context = json.loads(messages[-1]['content'])
                    calls.append(context['phase'])
                    if context['phase'] == 'planning':
                        return {'kind': 'plan', 'task': 'workflow'}
                    current = context['context']['job']
                    self.assertIn(action, current['allowed_actions'])
                    return {'kind': 'tool', 'name': 'prepare_workflow_action',
                            'arguments': {'action': action, 'job_ref': current['job_ref']}}
                async def show(name, result):
                    shown.append((name, copy.deepcopy(result)))
                before = copy.deepcopy(fixture.service.get_consistent_job(job['job_id']))
                result = await ChatAgent(model, tools, on_tool=show).run(question)
                self.assertEqual(result['status'], 'waiting_for_user')
                self.assertEqual(calls, ['planning', 'execution'])
                self.assertEqual(shown[-1][0], 'prepare_workflow_action')
                self.assertEqual(shown[-1][1]['action'], action)
                self.assertEqual(next(iter(tools.pending.values()))['state'], 'issued')
                self.assertEqual(fixture.service.get_consistent_job(job['job_id']), before)
                self.assertEqual(fixture.compute_calls, 0)
            if action == 'confirm_target_and_generate':
                # Advance only with the existing synthetic inverse fixture and
                # actual selection/mesh contracts; no conformal runner is used.
                job = fixture.service.confirm_target_and_run_inverse(job['job_id'], expected_revision=job['revision'],
                    authorization={'authorized': True})
                job = fixture.service.select_candidate(job['job_id'], job['selection']['selected_candidate_id'],
                    expected_revision=job['revision'], expected_candidate_set_sha256=job['candidate_set']['candidate_set_sha256'],
                    reason='Synthetic user confirmation')
                job = fixture.service.bind_mesh(job['job_id'], original_filename='synthetic.inp', content=VALID_INP,
                    expected_revision=job['revision'], confirmed_contract={'units': 'mm',
                        'frame': 'source_native_cartesian_frame', 'design_domain': 'whole_file_implant_domain'})
        self.assertEqual(fixture.inverse_calls, 1)

    async def test_action_names_and_candidate_counts_are_not_repaired_into_plans(self):
        for arguments in ({'intent': 'confirm_target_and_generate', 'number_of_candidates': 3},
                          {'intent': 'confirm_target_and_generate', 'number_of_candidates': 5},
                          {'intent': 'start_conformal'}):
            with self.subTest(arguments=arguments):
                read = Mock(side_effect=AssertionError('An invalid plan must stop before reading the Job'))
                tools = ChatTools(query_service=lambda: None, read_job=read, read_result=read,
                                  current_job_id=lambda: 'job-'+'F'*24, session_id='invalid-plan')
                model = Mock(return_value={'kind': 'plan', 'task': 'workflow', 'arguments': arguments})
                result = await ChatAgent(model, tools).run('Prepare the requested workflow confirmation.')
                self.assertEqual(result['status'], 'error')
                model.assert_called_once()
                read.assert_not_called()
                self.assertEqual(tools.pending, {})

    def test_comparison_program_renders_nmse_percent_and_relative_norm_without_changing_rank(self):
        job = {'target': {'physical_9d_gpa': {'C11': 20.}}, 'candidates': [
            {'candidate_id': 'rank-one', 'rank': 1, 'score': .01, 'nmse': .0025,
             'relative_density': .3, 'eligible': True, 'predicted_stiffness': {'C11': 21.}},
            {'candidate_id': 'rank-two', 'rank': 2, 'score': .02, 'nmse': .0004,
             'relative_density': .2, 'eligible': True, 'predicted_stiffness': {'C11': 20.4}},
            {'candidate_id': 'historical', 'rank': 3, 'score': .03,
             'relative_density': .25, 'eligible': True, 'predicted_stiffness': {'C11': 19.}}]}
        job['comparison_facts'] = {'p10_recommended_candidate_id': 'rank-one'}
        before = copy.deepcopy(job)
        answer = 'Fabricated claim: rank-two has higher density and NMSE determines rank.'
        text = recorded_chat_reply({'status': 'answered', 'answer_basis': 'workflow',
            'workflow_intent': 'compare_candidates', 'text': answer}, {'job': job})
        self.assertIn('| NMSE (%) | Relative L2 error (%) |', text)
        self.assertIn('| `rank-one` | 0.3 | 0.0025 | 0.25 | 5 | Eligible |', text)
        self.assertIn('| `rank-two` | 0.2 | 0.0004 | 0.04 | 2 | Eligible |', text)
        self.assertIn('| `historical` | 0.25 | Not recorded | Not recorded | Not recorded | Eligible |', text)
        self.assertIn('sum((prediction - target)^2) / sum(target^2)', text)
        self.assertIn('not accuracy 99.75%', text)
        self.assertLess(text.index('| 1 | `rank-one`'), text.index('| 2 | `rank-two`'))
        self.assertNotIn(answer, text)
        self.assertIn('Global P-10 recommendation: Rank 1', text)
        self.assertEqual(job, before)

    async def test_default_budget_allows_twenty_receipts_without_reset(self):
        tools = ChatTools(query_service=lambda: None, read_job=lambda _: None,
                          read_result=lambda _: None, current_job_id=lambda: None, session_id='budget')
        agent = ChatAgent(lambda _: {'kind': 'answer', 'text': 'Saved status.'}, tools)
        for _ in range(20):
            self.assertEqual((await agent.run('Read status', receipt={'action': 'stage'}))['status'], 'answered')
        self.assertEqual(tools.case.used['model_calls'], 20)
        self.assertEqual((await agent.run('More', receipt={}))['status'], 'budget_exhausted')

    async def test_same_job_changed_during_answer_never_enters_history(self):
        job = {'job_id': 'job-'+'A'*24, 'revision': 1, 'status': 'CANDIDATES_READY'}
        tools = tools_for(job)
        calls = []
        reads = []
        def read(job_id):
            reads.append(job_id)
            if len(reads) > 1:
                job['revision'] = 2
            return copy.deepcopy(job)
        tools.read_job = read
        def model(messages):
            phase = json.loads(messages[-1]['content'])['phase']
            calls.append(phase)
            self.assertEqual(phase, 'planning')
            return {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates'}}
        agent = ChatAgent(model, tools)
        result = await agent.run('Compare current candidates')
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(agent.history, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(reads), 2)

    async def test_final_result_read_is_also_a_fact_dependency(self):
        job = {'job_id': 'job-'+'B'*24, 'revision': 3, 'status': 'VIEWER_READY'}
        package = {'status': 'verified', 'job_id': job['job_id'], 'revision': 3, 'files': {}, 'summary': {}}
        tools = tools_for(job, read_result=lambda _: copy.deepcopy(package))
        def model(messages):
            self.assertEqual(json.loads(messages[-1]['content']).get('phase'), 'planning')
            return {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'read_result'}}
        async def after_read(name, result):
            if name == 'get_design_result':
                job['revision'] += 1
        agent = ChatAgent(model, tools, on_tool=after_read)
        self.assertEqual((await agent.run('Read the final result'))['status'], 'stale')
        self.assertEqual(agent.history, [])

    async def test_concept_answer_does_not_depend_on_unread_job(self):
        job = {'job_id': 'job-'+'C'*24, 'revision': 1, 'status': 'CANDIDATES_READY'}
        tools = tools_for(job)
        def model(messages):
            if json.loads(messages[-1]['content'])['phase'] == 'planning':
                return {'kind': 'plan', 'task': 'conversation'}
            job['revision'] += 1
            return {'kind': 'answer', 'text': 'NMSE is a squared relative norm.'}
        self.assertEqual((await ChatAgent(model, tools).run('Define NMSE'))['status'], 'answered')
        self.assertEqual(tools.case.used['tool_calls'], 0)

    async def test_candidate_stage_result_request_never_opens_final_package(self):
        job = {'job_id': 'job-'+'D'*24, 'revision': 1, 'status': 'CANDIDATES_READY'}
        reader = Mock(side_effect=AssertionError('No final package exists'))
        tools = tools_for(job, reader)
        action = await tools.bind_workflow_plan({'intent': 'read_result'}, run_id='stage')
        self.assertIsNone(action)
        self.assertEqual(tools.job_context['status'], 'CANDIDATES_READY')
        reader.assert_not_called()

    async def test_missing_file_is_reported_without_marking_compute_failed(self):
        from product_workflow.artifacts import ProductFileMissingError
        job = {'job_id': 'job-'+'E'*24, 'revision': 4, 'status': 'VIEWER_READY'}
        reader = Mock(side_effect=ProductFileMissingError('six-file artifact is missing'))
        tools = tools_for(job, reader)
        result = await tools.execute('get_design_result', {}, run_id='read')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'RESULT_FILE_MISSING')
        self.assertEqual(result['job_status'], 'VIEWER_READY')
        self.assertEqual(job['revision'], 4)
        reader.assert_called_once()


class CandidateComparisonFactsTests(unittest.TestCase):
    def facts(self, candidates, recommended='recommended'):
        from product_workflow.chat_tools import _candidate_comparison_facts
        before = copy.deepcopy(candidates)
        result = _candidate_comparison_facts(candidates, recommended)
        self.assertEqual(candidates, before)
        return result

    def test_density_and_nmse_do_not_replace_recorded_p10_recommendation(self):
        candidates = [
            dict(candidate_id='lighter', rank=3, eligible=True, score=.12, relative_density=.3, nmse=.02),
            dict(candidate_id='recommended', rank=1, eligible=True, score=.1, relative_density=.4, nmse=.03),
            dict(candidate_id='best_nmse', rank=2, eligible=True, score=.11, relative_density=.35, nmse=.01),
        ]
        facts = self.facts(candidates)
        self.assertEqual(facts['p10_recommended_candidate_id'], 'recommended')
        self.assertEqual(facts['lowest_recorded_density_candidate_ids'], ['lighter'])
        self.assertEqual(facts['lowest_recorded_nmse_candidate_ids'], ['best_nmse'])
        delta = facts['deltas_from_recommended']['lighter']
        self.assertGreater(delta['score'], 0)
        self.assertLess(delta['relative_density'], 0)
        self.assertLess(delta['nmse'], 0)
        self.assertEqual(facts['recorded_counts'], {'score': 3, 'relative_density': 3, 'nmse': 3})

    def test_ties_and_missing_metrics_remain_explicit(self):
        facts = self.facts([
            dict(candidate_id='recommended', rank=1, eligible=True, score=.1, relative_density=.3, nmse=None),
            dict(candidate_id='equal_density', rank=2, eligible=True, score=.2, relative_density=.3, nmse=.02),
            dict(candidate_id='unrecorded', rank=3, eligible=True, score=.3, relative_density=None, nmse=None),
        ])
        self.assertEqual(facts['lowest_recorded_density_candidate_ids'], ['recommended', 'equal_density'])
        self.assertEqual(facts['recorded_counts']['relative_density'], 2)
        self.assertEqual(facts['eligible_count'], 3)
        self.assertIsNone(facts['deltas_from_recommended']['equal_density']['nmse'])
        self.assertIsNone(facts['deltas_from_recommended']['unrecorded']['relative_density'])

    def test_ineligible_extremes_are_not_suggested_and_no_recommendation_is_invented(self):
        rows = [dict(candidate_id='recommended', rank=None, eligible=False, score=0, relative_density=.01, nmse=0),
                dict(candidate_id='allowed', rank=1, eligible=True, score=.1, relative_density=.3, nmse=.01)]
        facts = self.facts(rows)
        self.assertIsNone(facts['p10_recommended_candidate_id'])
        self.assertEqual(facts['lowest_recorded_density_candidate_ids'], ['allowed'])
        self.assertEqual(facts['deltas_from_recommended'], {})
        empty = self.facts(rows[:1])
        self.assertEqual(empty['eligible_count'], 0)
        self.assertEqual(empty['lowest_recorded_density_candidate_ids'], [])


class ControlledReplyContractsTests(unittest.IsolatedAsyncioTestCase):
    def job(self, status='CANDIDATES_READY'):
        from product_workflow.chat_tools import _candidate_comparison_facts
        candidates = [
            dict(candidate_id='recommended', rank=1, eligible=True, score=.1, relative_density=.4, nmse=.02),
            dict(candidate_id='selected', rank=2, eligible=True, score=.2, relative_density=.3, nmse=.03),
            dict(candidate_id='third', rank=3, eligible=False, score=.3, relative_density=.1, nmse=.01,
                 exclusion_reasons=['not eligible'])]
        return dict(job_id='job-'+'C'*24, revision=4, status=status, candidates=candidates,
                    candidate_confirmation='CONFIRMED', selection={'selected_candidate_id':'selected','selected_rank':2},
                    allowed_actions=[], target={'physical_9d_gpa':{}},
                    comparison_facts=_candidate_comparison_facts(candidates,'recommended'))

    async def test_explicit_actions_bind_current_reference_without_execution_model(self):
        for intent, action in [('prepare_generation','confirm_target_and_generate'),('prepare_conformal','start_conformal')]:
            with self.subTest(intent=intent):
                job=self.job('TARGET_PROPOSED' if intent=='prepare_generation' else 'AWAITING_CONFORMAL_AUTHORIZATION')
                job['allowed_actions']=[action]
                tools=tools_for(job)
                model=Mock(return_value={'kind':'plan','task':'workflow','arguments':{'intent':intent}})
                shown=[]
                async def show(name,result): shown.append((name,result))
                result=await ChatAgent(model,tools,on_tool=show).run('Prepare the requested confirmation.')
                self.assertEqual(result['status'],'waiting_for_user')
                model.assert_called_once()
                self.assertEqual(shown[-1][1]['action'],action)
                token=shown[-1][1]['confirmation_ref']
                self.assertEqual((await tools.consume_action(token))['action'],action)
                from product_workflow.chat_tools import ChatToolError
                with self.assertRaises(ChatToolError): await tools.consume_action(token)

    async def test_controlled_comparison_uses_one_plan_and_canonical_history(self):
        job=self.job(); tools=tools_for(job)
        model=Mock(return_value={'kind':'plan','task':'workflow','arguments':{'intent':'compare_candidates','focus':'selection'}})
        agent=ChatAgent(model,tools)
        result=await agent.run('Which candidate did I confirm and what is its tradeoff?')
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['answer_origin'],'program_job_facts')
        model.assert_called_once()
        self.assertEqual(result['job_evidence']['revision'],4)
        self.assertEqual(agent.history[-1]['content'],result['text'])
        self.assertIn('selected',result['text'])
        self.assertNotIn('higher density',result['text'])

    async def test_comparison_selector_and_focus_are_closed(self):
        from product_workflow.chat_tools import ChatToolError
        for extra in ({'ranks':[]},{'ranks':[True]},{'ranks':[2,2]}, {'ranks':[9]},
                      {'ranks':[1],'candidate_ids':['selected']}, {'candidate_ids':['missing']},
                      {'focus':'anything'}, {'focus':'selection','ranks':[1]}, {'component':'C11'}):
            with self.subTest(extra=extra):
                tools=tools_for(self.job())
                with self.assertRaises(ChatToolError):
                    await tools.bind_workflow_plan({'intent':'compare_candidates',**extra},run_id='test')
        tools=tools_for(self.job())
        await tools.bind_workflow_plan({'intent':'compare_candidates','focus':'density','ranks':[3,1]},run_id='test')
        self.assertEqual(tools.job_context['job_id'],self.job()['job_id'])

    async def test_generic_wrong_facts_and_receipt_do_not_enter_history(self):
        job=self.job(); tools=tools_for(job)
        wrong='Selected has higher density because NMSE recommends it; the user preferred this.'
        model=Mock(side_effect=[{'kind':'plan','task':'workflow'},{'kind':'answer','text':wrong}])
        agent=ChatAgent(model,tools)
        result=await agent.run('Tell me the current Job state.')
        self.assertEqual(result['answer_origin'],'program_job_facts')
        self.assertNotIn(wrong,str(agent.history))
        calls=model.call_count
        receipt=await agent.run('Recorded selection',receipt={'action':'select_candidate','job_id':job['job_id']})
        self.assertEqual(model.call_count,calls)
        self.assertEqual(receipt['text'],'Selected Rank 2 · `selected`.')

    async def test_result_body_requires_matching_real_tool_read(self):
        job=self.job('VIEWER_READY')
        package=dict(status='verified',job_id=job['job_id'],revision=4,selected_candidate_id='selected',
                     files={'stl':{'size_bytes':40,'url':'http://example.invalid/stl'}},summary={})
        read=Mock(return_value=package); tools=tools_for(job,read)
        model=Mock(return_value={'kind':'plan','task':'workflow','arguments':{'intent':'read_result'}})
        agent=ChatAgent(model,tools); result=await agent.run('Read existing files.')
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['design_result'],package)
        self.assertEqual(result['answer_origin'],'program_job_facts')
        model.assert_called_once(); read.assert_called_once()
        self.assertEqual(agent.history[-1]['content'],result['text'])
        package['revision']=3
        other=ChatAgent(model,tools_for(job,read))
        self.assertEqual((await other.run('Read files.'))['status'],'stale')
        self.assertEqual(other.history,[])

    async def test_action_binding_rejects_unknown_fields_other_jobs_and_unavailable_state(self):
        from product_workflow.chat_tools import ChatToolError
        for args in ({'intent':'prepare_generation','number_of_candidates':5},
                     {'intent':'prepare_conformal','job_id':'job-'+'F'*24}):
            tools=tools_for(self.job())
            with self.assertRaises(ChatToolError): await tools.bind_workflow_plan(args,run_id='guard')
            self.assertEqual(tools.pending,{})
        tools=tools_for(self.job())
        model=Mock(return_value={'kind':'plan','task':'workflow','arguments':{'intent':'prepare_generation'}})
        rejected=await ChatAgent(model,tools).run('Prepare generation')
        self.assertEqual(rejected['status'],'error')
        self.assertEqual(rejected['failure']['code'],'ACTION_STATE_NOT_ALLOWED')
        self.assertEqual(rejected['job_status'],'CANDIDATES_READY')
        self.assertEqual(tools.pending,{})

    async def test_missing_internal_reference_is_not_an_incomplete_user_target(self):
        tools=tools_for(self.job('TARGET_PROPOSED'))
        model=Mock(side_effect=[{'kind':'plan','task':'workflow'},
                 {'kind':'tool','name':'prepare_workflow_action','arguments':{'action':'confirm_target_and_generate'}}])
        result=await ChatAgent(model,tools).run('Prepare generation')
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['failure']['stage'],'action_prepare')
        self.assertEqual(tools.pending,{})


class InvocationDiagnosticsTests(unittest.TestCase):
    def test_invalid_json_records_size_before_parse_and_calls_once(self):
        from llm_provider import invoke_chat_action
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content='not JSON')
        events = []
        with self.assertRaises(json.JSONDecodeError):
            invoke_chat_action(llm, [], trace=lambda event, **fields: events.append((event, fields)))
        llm.invoke.assert_called_once()
        self.assertEqual(next(fields['raw_length'] for event, fields in events if event == 'model_raw_response'), 8)
        self.assertNotIn('not JSON', json.dumps(events))
        self.assertEqual(events[-1][1]['stage'], 'json_parse')

    def test_provider_error_is_distinct_and_safe(self):
        from llm_provider import invoke_chat_action
        llm = Mock()
        llm.invoke.side_effect = RuntimeError('Authorization: Bearer secret-value')
        events = []
        with self.assertRaises(RuntimeError):
            invoke_chat_action(llm, [], trace=lambda event, **fields: events.append((event, fields)))
        llm.invoke.assert_called_once()
        self.assertEqual(events[-1][1]['stage'], 'model_call')
        self.assertNotIn('secret-value', json.dumps(events))

    def test_diagnostic_callback_failure_does_not_change_result(self):
        from llm_provider import invoke_chat_action
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content='{"kind":"answer","text":"ok"}')
        self.assertEqual(invoke_chat_action(llm, [], trace=Mock(side_effect=OSError('log offline')))['text'], 'ok')
        llm.invoke.assert_called_once()


class TargetReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def _database_replacement_turn(self, *, external_change=False):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from tests.test_product_chat_tools import SyntheticQuery
        from tests.test_product_ui_v5 import csv_bytes
        with _UiProbe() as probe, patch.object(ui, '_query_service', return_value=SyntheticQuery()):
            probe.session.set('product_job_id', None)
            await ui._handle_csv_upload(csv_bytes(1), filename='old.csv')
            old = next(iter(ui._creation_state()['actions'].values()))
            selected_csv = copy.deepcopy(probe.session.get('product_pending_csv_target'))
            tools = ui._product_chat_tools()
            await tools.execute('query_records', {'conditions': [], 'limit': 3})
            show = ui._present_chat_confirmation
            async def present(result):
                await show(result)
                if external_change:
                    ui._set_pending_csv_target({**selected_csv, 'filename': 'external.csv'})
            plan = {'kind': 'plan', 'task': 'workflow',
                    'arguments': {'intent': 'reference_target', 'position': 2}}
            with patch.object(ui, '_chat_model', return_value=plan) as model, \
                    patch.object(ui, '_present_chat_confirmation', side_effect=present):
                result = await ui._run_product_chat('Use database record 2 as the target instead of the CSV.')
            model.assert_called_once()
            self.assertIsNone(probe.session.get('product_job_id'))
            self.assertEqual(old['state'], 'RETIRED')
            return result, tools

    async def test_actual_agent_database_replacement_keeps_new_confirmation_usable(self):
        result, tools = await self._database_replacement_turn()
        self.assertEqual(result['status'], 'waiting_for_user')
        self.assertIsNone(tools.pending_csv_target)
        self.assertEqual(len(tools.pending), 1)
        record = next(iter(tools.pending.values()))
        self.assertEqual(record['state'], 'issued')
        self.assertEqual(record['target']['source']['sample_id'], 42)

    async def test_external_csv_change_during_database_confirmation_still_revokes_it(self):
        result, tools = await self._database_replacement_turn(external_change=True)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(tools.pending, {})
        self.assertEqual(tools.pending_csv_target['filename'], 'external.csv')

    async def test_single_csv_preview_keeps_database_card_then_create_retires_it(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from tests.test_product_ui_v5 import csv_bytes
        with _UiProbe():
            tools = ui._product_chat_tools()
            tools.pending['database'] = {'action': 'create_job', 'state': 'issued'}
            tools.pending['mesh'] = {'action': 'request_mesh_upload', 'state': 'issued'}
            await ui._handle_csv_upload(csv_bytes(1), filename='single.csv')
            self.assertEqual(tools.pending['database']['state'], 'issued')
            token, record = next(iter(ui._creation_state()['actions'].items()))
            async def accepted(*args, **kwargs):
                self.assertEqual(tools.pending['database']['state'], 'retired')
                self.assertEqual(tools.pending['mesh']['state'], 'issued')
                self.assertEqual(record['state'], 'CREATING')
            with patch.object(ui, '_create_and_show_target', side_effect=accepted), patch.object(ui, '_continue_product_chat', new=AsyncMock()):
                await ui.create_target(ui.cl.Action(name='product_create_target', label='Create', payload={'token': token}))
            self.assertEqual(tools.pending['database']['state'], 'retired')

    async def test_manual_csv_choice_retires_database_creation_only(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from tests.test_product_ui_v5 import csv_bytes
        with _UiProbe():
            tools = ui._product_chat_tools()
            tools.pending.update({'db': {'action':'create_job','state':'issued'},
                                  'writing': {'action':'create_job','state':'consumed'},
                                  'select': {'action':'select_candidate','state':'issued'}})
            await ui._handle_csv_upload(csv_bytes(3), filename='multi.csv')
            await ui.product_csv_select(ui._csv_import_state()['row_actions'][2])
            self.assertEqual(tools.pending['db']['state'], 'retired')
            self.assertEqual(tools.pending['writing']['state'], 'consumed')
            self.assertEqual(tools.pending['select']['state'], 'issued')

    async def test_database_proposal_retires_csv_but_keeps_new_confirmation(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from tests.test_product_ui_v5 import csv_bytes
        with _UiProbe():
            await ui._handle_csv_upload(csv_bytes(1), filename='old.csv')
            old = next(iter(ui._creation_state()['actions'].values()))
            tools = ui._product_chat_tools()
            tools.pending['new'] = {'action':'create_job', 'state':'issued'}
            result = {'status':'awaiting_confirmation', 'confirmation_ref':'new', 'action':'create_job'}
            with patch.object(ui, '_present_chat_confirmation', new=AsyncMock()) as show:
                await ui._present_chat_tool('prepare_design_target', result)
            self.assertEqual(old['state'], 'RETIRED')
            self.assertEqual(tools.pending['new']['state'], 'issued')
            show.assert_awaited_once()

    async def test_replacement_revokes_before_slow_ui_updates(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from tests.test_product_ui_v5 import csv_bytes
        with _UiProbe():
            await ui._handle_csv_upload(csv_bytes(1), filename='old.csv')
            old = next(iter(ui._creation_state()['actions'].values()))
            entered, release = asyncio.Event(), asyncio.Event()
            async def wait_remove(*args):
                entered.set()
                await release.wait()
            with patch.object(ui.cl.Action, 'remove', wait_remove):
                task = asyncio.create_task(ui._retire_target_proposals())
                await entered.wait()
                self.assertEqual(old['state'], 'RETIRED')
                release.set()
                await task


if __name__ == '__main__':
    unittest.main()
