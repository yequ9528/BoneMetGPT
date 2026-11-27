"""Owner geometry and product NMSE contracts; computations are controlled fixtures."""
import copy
import csv
import math
import os
import uuid
from contextlib import nullcontext
import unittest
from pathlib import Path

from product_workflow.contracts import canonical_sha256
from product_workflow.job_state import candidate_confirmation_status, build_product_job_projection
from tests import test_product_workflow_jobs as fixtures
nine = fixtures.nine


def delivered_result():
    from services.truss_inverse_service import POINT_NAMES
    from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
    candidates = []
    target = nine()
    for index, (component, error, density) in enumerate([
        ('C11', .2, .4), ('C12', .25, .3), ('C11', .4, .35)], 1):
        prediction = dict(target)
        prediction[component] += error
        candidates.append(dict(candidate_id=f'candidate-{index}', cad_ready=True,
            predicted_stiffness=prediction, relative_density=density,
            compressed_parameters=dict.fromkeys(POINT_NAMES, .5),
            nmse=sum((prediction[k]-target[k])**2 for k in target)/sum(v*v for v in target.values()),
            **build_unit_cell([density]+[.5]*9), source_order=index))
    return dict(status='success', candidates=candidates,
        algorithm={'version':'truss_latent_20261110','prediction_kind':'forward_surrogate'},
        diagnostics=[{'target_index':1,'diversity_threshold':.16,'diversity_fallback':False}],
        elapsed_ms=1, failure=None)


class OwnerGeometryTests(unittest.TestCase):
    def test_owner_nodes_connections_and_unrounded_diameter(self):
        from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
        cell = build_unit_cell([.37]+[.5]*9)
        self.assertEqual(len(cell['nodes']), 15)
        self.assertEqual(len(cell['connections']), 32)
        self.assertEqual(cell['nodes'][9:11], [[.5,.5,0.],[.5,.5,1.]])
        length = 8*math.sqrt(.75)+24*math.sqrt(.5)
        self.assertAlmostEqual(cell['diameter'], 2*math.sqrt(.37/(math.pi*length)), places=14)
        self.assertNotEqual(cell['diameter'], round(cell['diameter'], 6))
        for theta in ([0.]+[.5]*9, [.3, float('nan')]+[.5]*8,
                      [.3,1.1]+[.5]*8, [.3,0.,0.,0.]+[.5]*6):
            with self.subTest(theta=theta), self.assertRaises(ValueError): build_unit_cell(theta)

    def test_prediction_only_converter_binds_target_and_full_geometry(self):
        from services.truss_inverse_service import build_delivered_candidates, POINT_NAMES, C_NAMES
        target = nine()
        directory = Path(os.environ['LATTICEGPT_TEST_RUNTIME_ROOT']) / ('converter-' + uuid.uuid4().hex)
        directory.mkdir(parents=True)
        with nullcontext(directory):
            path = Path(directory)/'prediction.csv'
            fields = ['target_id','nmse','relative_density',*POINT_NAMES,*C_NAMES]
            def write(rows):
                with path.open('w', newline='', encoding='utf-8') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader(); writer.writerows(rows)
            rows=[dict(target_id=1,nmse=c['nmse'],relative_density=c['relative_density'],
                **c['compressed_parameters'],**c['predicted_stiffness']) for c in delivered_result()['candidates']]
            write(rows)
            actual=build_delivered_candidates(path,[target])
            self.assertEqual([c['source_order'] for c in actual],[1,2,3])
            self.assertEqual(actual[0]['candidate_id'],'inverse_150w_0001')
            self.assertEqual(actual[0]['target_stiffness'],target)
            self.assertEqual(len(actual[0]['connections']),32)
            self.assertNotEqual(actual[0]['diameter'],round(actual[0]['diameter'],6))
            rows[0]['nmse'] = .5; write(rows)
            with self.assertRaisesRegex(ValueError,'NMSE'): build_delivered_candidates(path,[target])
            rows[0]['nmse'] = 0.; rows[0]['target_id']=2; write(rows)
            with self.assertRaisesRegex(ValueError,'target_id'): build_delivered_candidates(path,[target])


class NmseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProductWorkflowJobTests(); self.fixture.setUp()
        self.service=self.fixture.service
        self.raw=delivered_result()
        self.service.inverse_runner=lambda *_a,**_kw: copy.deepcopy(self.raw)

    def tearDown(self): self.fixture.tearDown()

    def ready(self):
        job=self.service.create_job(self.fixture.target,model_runtime_profile=self.fixture.profile)
        return self.service.confirm_target_and_run_inverse(job['job_id'],expected_revision=job['revision'],authorization={'authorized':True})

    def test_new_nmse_differs_from_p10_without_changing_old_path(self):
        new=self.ready()
        collection=new['candidate_set']
        self.assertEqual(collection['schema_version'],'latticegpt_candidate_set_v2')
        self.assertEqual(collection['ranking_contract'],'truss_owner_nmse_v1')
        self.assertNotIn('p10_contract',collection)
        self.assertEqual(collection['recommended_candidate_id'],'candidate-1')
        self.assertTrue(all('score' not in c for c in collection['candidates']))
        self.assertEqual(candidate_confirmation_status(new),'UNCONFIRMED')
        legacy=copy.deepcopy(self.raw); legacy.pop('algorithm')
        old,_=self.service._build_candidate_set(self.fixture.target,legacy)
        self.assertEqual(old['schema_version'],'latticegpt_candidate_set_v1')
        self.assertEqual(old['recommended_candidate_id'],'candidate-2')

    def test_ineligible_first_keeps_rank_and_recommended_confirmation_is_valid(self):
        self.raw['candidates'][0]['cad_ready']=False
        job=self.ready(); collection=job['candidate_set']
        self.assertEqual(collection['candidates'][0]['rank'],1)
        self.assertEqual(collection['recommended_candidate_id'],'candidate-2')
        selected=self.service.select_candidate(job['job_id'],'candidate-2',expected_revision=job['revision'],
            expected_candidate_set_sha256=collection['candidate_set_sha256'])
        self.assertEqual(candidate_confirmation_status(selected),'CONFIRMED')
        self.assertEqual(selected['selection']['selection_source'],'recommended_candidate')
        self.assertEqual(selected['selection']['selected_rank'],2)
        self.assertTrue(build_product_job_projection(selected)['capabilities']['upload_mesh'])
        trace=self.service._product_traceability(selected,selected['candidate_set']['candidates'][1])
        self.assertEqual(trace['selection_policy'],'product_candidate_selection_v2')
        from candidate_cell_adapter import _validate_candidate, _validate_traceability
        _,_,normalized=_validate_candidate(selected['candidate_set']['candidates'][1])
        self.assertEqual(_validate_traceability(trace,normalized)[0],[])
        with self.assertRaises(ValueError):
            self.service.select_candidate(job['job_id'],'candidate-1',expected_revision=job['revision'],
                expected_candidate_set_sha256=collection['candidate_set_sha256'])

    def test_no_eligible_candidates_remain_readable_with_no_selection(self):
        for candidate in self.raw['candidates']: candidate['cad_ready']=False
        job=self.ready()
        current=self.service.get_consistent_job(job['job_id'])
        self.assertEqual(current['workflow_disposition'],'HOLD')
        self.assertIsNone(current['selection'])
        self.assertIsNone(current['candidate_set']['recommended_candidate_id'])
        projection=build_product_job_projection(current)
        self.assertFalse(projection['capabilities']['select_candidate'])
        self.assertFalse(projection['capabilities']['start_conformal'])

    def test_equal_nmse_uses_source_order_and_bad_nmse_is_output_failure(self):
        self.raw['candidates'][1]['predicted_stiffness']=copy.deepcopy(self.raw['candidates'][0]['predicted_stiffness'])
        self.raw['candidates'][1]['nmse']=self.raw['candidates'][0]['nmse']
        job=self.ready()
        self.assertEqual([c['rank'] for c in job['candidate_set']['candidates']],[1,2,3])
        self.raw['candidates'][0]['nmse']=float('nan')
        failed=self.ready()
        self.assertEqual(failed['status'],'FAILED')
        self.assertEqual(failed['failure']['code'],'INVERSE_OUTPUT_INVALID')


    def test_new_state_rejects_wrong_rank_and_selection_version_even_with_updated_hash(self):
        from product_workflow.job_state import validate_job_snapshot_by_schema
        for mutation in ('rank','version'):
            job=self.ready()
            if mutation=='rank': job['candidate_set']['candidates'][0]['rank']=2
            else: job['selection']['schema_version']='latticegpt_candidate_selection_v1'
            job['candidate_set']['candidate_set_sha256']=canonical_sha256({k:v for k,v in job['candidate_set'].items() if k!='candidate_set_sha256'})
            with self.assertRaises(ValueError): validate_job_snapshot_by_schema(job)


class ComparisonFactsTests(unittest.TestCase):
    def view(self, owner=True):
        from product_workflow.chat_tools import _candidate_comparison_facts
        candidates = delivered_result()['candidates']
        for i,c in enumerate(candidates,1): c.update(rank=i,eligible=True,score=None if owner else i*.01)
        return dict(candidates=candidates, ranking={'label':'NMSE' if owner else 'P-10','metric':'nmse' if owner else 'score'},
            comparison_facts=_candidate_comparison_facts(candidates,'candidate-1'),
            selection={'selected_candidate_id':'candidate-2'},candidate_confirmation='CONFIRMED',target={'physical_9d_gpa':nine()})

    def test_metrics_and_component_focus_keep_density_tradeoff_and_actual_choice(self):
        from product_workflow.presentation import build_job_fact_reply, recorded_chat_reply
        for owner in (True,False):
            view=self.view(owner)
            for focus in ('metrics','component_delta','selection','overall'):
                text=build_job_fact_reply(view,focus=focus)
                self.assertIn('relative density',text)
                self.assertIn('Confirmed selection: Rank 2',text)
                self.assertIn('Global '+('NMSE' if owner else 'P-10')+' recommendation: Rank 1',text)
                if owner: self.assertNotIn('P-10',text)
                else: self.assertIn('P-10',text)
            table=recorded_chat_reply(dict(status='answered',answer_origin='program_job_facts',workflow_intent='compare_candidates',text=text),{'job':view})
            if owner: self.assertNotIn('P-10',table)

    def test_action_state_failure_is_not_rendered_as_missing_units(self):
        from product_workflow.presentation import recorded_chat_reply
        failure=dict(code='ACTION_STATE_NOT_ALLOWED',stage='action_prepare',message='Current Job does not permit generation.',next_step='Read current Job.')
        answer=recorded_chat_reply(dict(status='error',failure=failure,job_status='CANDIDATES_READY'),{})
        self.assertIn('ACTION_STATE_NOT_ALLOWED',answer)
        self.assertIn('CANDIDATES_READY',answer)
        self.assertNotIn('unit',answer.lower())


class StandardPreviewTests(unittest.TestCase):
    def test_single_and_batch_render_same_saved_geometry(self):
        from product_workflow.ui_previews import render_standard_candidate_png, render_candidate_previews
        from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
        candidates=[]
        for i in range(3):
            geometry=build_unit_cell([.3+i*.05]+[.5]*9)
            candidates.append(dict(candidate_id=f'candidate-{i+1}',rank=i+1,**geometry,normalized_diameter=geometry['diameter']))
        batch=render_candidate_previews(candidates)
        for c,preview in zip(candidates,batch):
            self.assertIsNone(preview.error)
            self.assertEqual(render_standard_candidate_png(c), preview.png)
            self.assertTrue(preview.png.startswith(b'\x89PNG'))


class ReviewRegressionTests(unittest.TestCase):
    def test_selection_focus_always_includes_real_scope_tradeoffs(self):
        from product_workflow.presentation import build_job_fact_reply
        for confirmed in (False,True):
            view=ComparisonFactsTests().view()
            view['candidate_confirmation']='CONFIRMED' if confirmed else 'UNCONFIRMED'
            view['selection']['selected_candidate_id']='candidate-1'
            reply=build_job_fact_reply(view,focus='selection')
            self.assertIn('relative density is lower',reply)
            self.assertIn('0.3 versus 0.4',reply)
        old=ComparisonFactsTests().view(False)
        reply=build_job_fact_reply(old,ranks=[2])
        self.assertNotIn('global NMSE recommendation',reply)
        self.assertIn('global P-10 recommendation',reply)

    def test_new_projections_do_not_fabricate_legacy_score_or_p10_identity(self):
        from product_workflow.chat_tools import ChatTools
        from product_viewer_payloads import _build_candidates
        fixture=NmseWorkflowTests();fixture.setUp()
        try:
            job=fixture.ready()
            tools=ChatTools(query_service=lambda:None,read_job=lambda _:job,read_result=lambda _:None,current_job_id=lambda:job['job_id'],session_id='projection')
            view=tools._job_view(job)
            self.assertNotIn('p10_recommended_candidate_id',view['comparison_facts'])
            self.assertNotIn('score',view['comparison_facts']['recorded_counts'])
            self.assertNotIn('score',view['candidates'][0])
            self.assertNotIn('score',_build_candidates(job)[0])
        finally:fixture.tearDown()

    def test_adapter_owner_coordinates_round_trip_without_decimal_truncation(self):
        from candidate_cell_adapter import _validate_candidate,_build_custom_cell_payload,_write_custom_cell_yml
        from TrussInvertibleDesign.src.unit_cell_geometry import build_unit_cell
        import yaml
        candidate=delivered_result()['candidates'][0]
        candidate.update(build_unit_cell([.3,.12345678912345678]+[.5]*8))
        _,_,normalized=_validate_candidate(candidate)
        payload=_build_custom_cell_payload(normalized)
        expected=2*candidate['nodes'][8][0]-1
        self.assertEqual(payload['nodes'][9][0],expected)
        directory=Path(os.environ['LATTICEGPT_TEST_RUNTIME_ROOT'])/('yaml-'+uuid.uuid4().hex);directory.mkdir()
        path=directory/'cell.yml';_write_custom_cell_yml(path,payload,full_precision=True)
        self.assertEqual(yaml.safe_load(path.read_text())['nodes'][9][0],expected)


class StateErrorRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_result_preserves_confirmed_selection_tradeoff_and_signed_difference(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from unittest.mock import patch

        fixture = NmseWorkflowTests()
        fixture.setUp()
        try:
            before = fixture.ready()
            selected = fixture.service.select_candidate(
                before['job_id'], 'candidate-2', expected_revision=before['revision'],
                expected_candidate_set_sha256=before['candidate_set']['candidate_set_sha256'],
            )
            question = '我刚通过界面确认了第2名。请读取当前记录，说明真正已确认的候选与系统推荐的区别，继续比较密度和目标匹配误差，并解释负刚度差值是否表示负刚度。'
            plan = {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'read_result'}}
            with _UiProbe() as probe, patch.object(ui, '_chat_model', return_value=plan) as model, \
                    patch.object(ui, '_chat_read_job', side_effect=fixture.service.get_consistent_job), \
                    patch.object(ui, '_chat_read_result', side_effect=AssertionError('result is not ready')):
                probe.session.set('product_job_id', selected['job_id'])
                result = await ui._run_product_chat(question)
                visible = '\n'.join(row['output'] for row in probe.messages.values())
                history = probe.session.get('product_chat_agent').history
            self.assertEqual(result['status'], 'answered')
            for fact in ('Confirmed selection: Rank 2', 'candidate-1', 'relative density', 'NMSE',
                         'prediction minus target', 'does not mean negative stiffness'):
                self.assertIn(fact, visible)
                self.assertIn(fact, history[-1]['content'])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(fixture.service.get_consistent_job(selected['job_id']), selected)
        finally:
            fixture.tearDown()

    async def test_ineligible_selector_and_busy_replacement_are_typed_errors(self):
        from product_workflow.chat_tools import ChatTools, ChatWorkflowStateError
        from unittest.mock import AsyncMock
        fixture=NmseWorkflowTests();fixture.setUp()
        try:
            fixture.raw['candidates'][0]['cad_ready']=False;job=fixture.ready()
            tools=ChatTools(query_service=lambda:None,read_job=lambda _:job,read_result=lambda _:None,current_job_id=lambda:job['job_id'],session_id='guard')
            with self.assertRaises(ChatWorkflowStateError):
                await tools.bind_workflow_plan({'intent':'choose_candidate','rank':1},run_id='guard')
            tools.proposal={'proposal_ref':'p','result_ref':'r','target':job['target']}
            tools._record_cache=lambda _:{}
            tools._check_source=AsyncMock()
            tools._job=AsyncMock(return_value={'status':'INVERSE_RUNNING'})
            with self.assertRaises(ChatWorkflowStateError):
                await tools._prepare({'action':'create_job','proposal_ref':'p'})
        finally:fixture.tearDown()
