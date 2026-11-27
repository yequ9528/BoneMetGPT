"""Viewer V3: read-only original reference, source labels and optional projection."""
import unittest
from unittest.mock import Mock, patch

from tests import test_product_viewer_server as fixtures


RAW = b'''*Node
1,0,0,0
2,1,0,0
3,1,1,0
4,0,1,0
5,0,0,1
6,1,0,1
7,1,1,1
8,0,1,1
*Element, type=C3D8R
10,1,2,3,4,5,6,7,8
*Element, type=S4R
20,1,2,3,4
'''


class OriginalReferenceTests(unittest.TestCase):
    def test_original_keeps_shells_native_coordinates_and_distinct_domain(self):
        from product_workflow.viewer_reference import build_original_reference
        value = build_original_reference(RAW, selected_ids=[10])
        self.assertEqual(value['status'], 'available')
        self.assertEqual(value['node_count'], 8)
        self.assertEqual(value['element_count'], 2)
        self.assertEqual(value['groups']['selected']['element_ids'], [10])
        self.assertEqual(value['groups']['excluded_other']['element_ids'], [20])
        self.assertEqual(value['nodes'][0]['xyz'], [0., 0., 0.])
        self.assertEqual(value['bbox']['max'], [1., 1., 1.])

    def test_limit_and_unsupported_types_never_claim_complete_geometry(self):
        from product_workflow.viewer_reference import build_original_reference
        self.assertEqual(build_original_reference(RAW, max_elements=1)['status'], 'too_large')
        unsupported = RAW.replace(b'type=S4R', b'type=UNKNOWN4')
        value = build_original_reference(unsupported)
        self.assertEqual(value['status'], 'unavailable')
        self.assertIn('UNKNOWN4', value['unsupported_types'])
        self.assertNotIn('nodes', value)

    def test_selected_ids_cannot_invent_or_select_shell_elements(self):
        from product_workflow.viewer_reference import build_original_reference
        for ids in ([99], [20]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                build_original_reference(RAW, selected_ids=ids)


class ViewerIncludeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProductViewerServerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_only_explicit_original_mesh_include_requests_optional_geometry(self):
        from product_viewer_server import create_product_viewer_app
        builder = Mock(return_value={'job_id': self.fixture.job_id})
        client = create_product_viewer_app(self.fixture.config, preflight=fixtures.viewer_preflight(), payload_builder=builder).test_client()
        path = '/api/viewer/product/jobs/' + self.fixture.job_id
        self.assertEqual(client.get(path).status_code, 200)
        self.assertNotIn('include_original_mesh', builder.call_args.kwargs)
        self.assertEqual(client.get(path + '?include=original_mesh').status_code, 200)
        self.assertIs(builder.call_args.kwargs.get('include_original_mesh'), True)
        self.assertEqual(client.get(path + '?include=arbitrary_path').status_code, 400)

    def test_target_record_numbers_and_predictions_are_preserved_without_mutation(self):
        from copy import deepcopy
        from product_viewer_payloads import _build_target, _build_candidate
        job = {'target': {'source': {'kind': 'uploaded_csv', 'name': 'targets.csv', 'record_index': 3, 'record_count': 27}}}
        old = deepcopy(job)
        target = _build_target(job)
        self.assertEqual(target['source']['record_index'], 3)
        self.assertEqual(target['source']['record_count'], 27)
        self.assertEqual(job, old)
        candidate = {'candidate_id': 'c1', 'nodes': [[0,0,0],[1,0,0]], 'connections': [[0,1]], 'diameter': .1,
                     'predicted_stiffness': {'C11': 6.5}, 'relative_density': .12}
        display = _build_candidate(candidate, recommended_id='c1', selected_id='c1')
        self.assertEqual(display['predicted_stiffness'], {'C11': 6.5})
        self.assertEqual(display['relative_density'], .12)

    def test_original_parse_failure_is_local_but_identity_and_domain_mismatch_are_fatal(self):
        import hashlib
        from product_viewer_payloads import _build_mesh
        from product_workflow.artifacts import ProductFileIdentityError
        from product_workflow.store import JobStore
        store = JobStore(self.fixture.config.paths.runtime_root)
        folder = store.jobs_root / self.fixture.job_id
        folder.mkdir(parents=True)
        path = folder / 'source.inp'
        path.write_bytes(RAW)
        digest = hashlib.sha256(RAW).hexdigest().upper()
        mesh = dict(stored_path=str(path), raw_sha256=digest, binding_sha256='A'*64,
                    original_filename='bone.inp', upload_id='u', element_type_original='C3D8R',
                    element_type_effective='C3D8', node_count=8, element_count=1,
                    units='mm', frame='source_native_cartesian_frame', precheck_status='PASS',
                    design_domain={'selected_element_ids':[10]})
        job = {'job_id':self.fixture.job_id,'revision':4,'mesh_binding':{},
               'mesh_uploads':[{'upload_id':'u','stored_path':str(path)}]}
        builder = Mock(return_value={'status':'available'})
        with patch('product_viewer_payloads.normalize_job_mesh_binding', return_value=mesh):
            self.assertNotIn('original_visualization', _build_mesh(job,builder,store))
            path.write_bytes(RAW+b'changed')
            with self.assertRaises(ProductFileIdentityError):
                _build_mesh(job,builder,store,include_original_mesh=True)
            path.write_bytes(RAW)
            mesh['design_domain']['selected_element_ids']=[99]
            with self.assertRaises(ProductFileIdentityError):
                _build_mesh(job,builder,store,include_original_mesh=True)
            path.write_bytes(b'bad inp')
            mesh['raw_sha256']=hashlib.sha256(b'bad inp').hexdigest().upper()
            value = _build_mesh(job,builder,store,include_original_mesh=True)
            self.assertEqual(value['original_visualization']['status'],'unavailable')


if __name__ == '__main__':
    unittest.main()
