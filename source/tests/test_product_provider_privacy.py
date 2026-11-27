"""Synthetic provider-boundary checks; no sockets or credentials are used."""
import io
import json
import logging
import os
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import OpenAI

from llm_provider import describe_llm_config_safe, invoke_chat_action, invoke_json_object
from product_workflow.inputs import plan_database_read, plan_formal_query


class ProviderPrivacyTests(unittest.TestCase):
    def test_installed_sdk_httpx_transport_and_trace(self):
        marker = 'SYNTHETIC_PRIVATE_DIAGNOSTIC'
        response_text = json.dumps({'kind': 'answer', 'answer': marker})
        seen, events = [], []

        def transport(request):
            seen.append(json.loads(request.read()))
            return httpx.Response(200, json={
                'id': 'synthetic', 'object': 'chat.completion', 'created': 1, 'model': 'synthetic',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': response_text}}]})

        class LocalProvider:
            def invoke(self, messages):
                reply = sdk.chat.completions.create(model='synthetic', messages=messages)
                return reply.choices[0].message

        stream = io.StringIO()
        messages = [{'role': 'user', 'content': 'User research target 1.234 GPa'}]
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            sdk = OpenAI(api_key='synthetic-key', base_url='https://fixture.invalid/' + marker,
                         http_client=client, max_retries=0)
            with self.assertLogs(level='DEBUG') as logs, redirect_stdout(stream), redirect_stderr(stream):
                value = invoke_chat_action(LocalProvider(), messages, trace=lambda e, **kw: events.append([e, kw]))
                logging.getLogger('httpx').info('outside provider invocation')
        self.assertEqual(seen[0]['messages'], messages)
        self.assertEqual(value['answer'], marker)
        captured = json.dumps([logs.output, events, stream.getvalue()])
        for private in (marker, 'synthetic-key', messages[0]['content']):
            self.assertNotIn(private, captured)
        self.assertIn('outside provider invocation', captured)
        self.assertTrue(any(e == 'model_json_parsed' for e, _ in events))
        self.assertEqual(next(v['raw_length'] for e, v in events if e == 'model_raw_response'), len(response_text))
        if os.environ.get('BMG_ACCEPTANCE_ARTIFACTS'):
            path = Path(os.environ['BMG_ACCEPTANCE_ARTIFACTS']) / 'provider-capture.json'
            with path.open('x', encoding='utf-8') as out:
                json.dump({'synthetic_transport_input': seen, 'ordinary_logger': logs.output,
                           'actual_trace_events': events, 'stdout_stderr': stream.getvalue()}, out, indent=2)

    def test_provider_failure_and_nontext_sdk_response(self):
        for response in (ConnectionError('SYNTHETIC_PRIVATE_DIAGNOSTIC'), SimpleNamespace(private='SYNTHETIC_PRIVATE_DIAGNOSTIC')):
            provider = Mock()
            if isinstance(response, Exception):
                provider.invoke.side_effect = response
            else:
                provider.invoke.return_value = response
            events = []
            with self.assertRaises((ConnectionError, ValueError)):
                invoke_chat_action(provider, [], trace=lambda e, **kw: events.append([e, kw]))
            self.assertEqual(provider.invoke.call_count, 1)
            self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', json.dumps(events))
            self.assertEqual(events[-1][0], 'model_invocation_error')

    def test_planner_evidence_keeps_identity_without_raw_diagnostics(self):
        provider = Mock()
        provider.invoke.return_value = SimpleNamespace(content='SYNTHETIC_PRIVATE_DIAGNOSTIC')
        answer = invoke_json_object(provider, [])
        result = plan_formal_query('sample 2', planner=lambda p: {**answer, 'error': 'SYNTHETIC_PRIVATE_DIAGNOSTIC',
                                                                 'raw_excerpt': 'SYNTHETIC_PRIVATE_DIAGNOSTIC'})
        self.assertEqual(result['planner_evidence']['failure_code'], 'invalid_json')
        self.assertEqual(result['planner_evidence']['raw_response_sha256'], answer['raw_sha256'])
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', json.dumps([answer, result]))

    def test_config_description_has_no_endpoint_or_unknown_objects(self):
        marker = 'SYNTHETIC_PRIVATE_DIAGNOSTIC'
        description = describe_llm_config_safe({'base_url': marker, 'api_key': marker, 'model': {'debug': marker}})
        self.assertNotIn(marker, description)

    def test_query_planners_keep_original_question_in_the_bound_request(self):
        question = '  compare C11 targets in GPa  '
        with self.assertRaisesRegex(ValueError, 'clarification question'):
            planner = Mock(return_value={'status': 'needs_clarification', 'question': {'debug': 'SYNTHETIC'}})
            plan_formal_query(question, planner=planner)
        self.assertTrue(planner.call_args.args[0].endswith(question))
        planner = Mock(return_value={})
        try:
            plan_database_read(question, planner=planner)
        except ValueError:
            pass
        self.assertTrue(planner.call_args.args[0].endswith(question))

    def test_viewer_handles_route_error_and_access_log_without_request_details(self):
        from product_viewer_server import ProductRequestHandler, create_product_viewer_app
        from product_workflow.config import load_latticegpt_config
        from tests.test_product_viewer_server import viewer_preflight
        marker = 'SYNTHETIC_PRIVATE_DIAGNOSTIC'
        config = load_latticegpt_config(explicit={'release_root': Path.cwd()}, environ={})
        app = create_product_viewer_app(config, preflight=viewer_preflight(),
                                        payload_builder=Mock(side_effect=RuntimeError(marker)))
        with self.assertLogs('product_viewer_server', level='INFO') as logs:
            response = app.test_client().get('/api/viewer/product/jobs/job-' + 'A'*24 + '?private=' + marker)
            handler = object.__new__(ProductRequestHandler)
            handler.requestline = 'GET /?' + marker
            handler.log_request(500)
            handler.log_error('bad request: %s', marker)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json['code'], 'PRODUCT_VIEWER_INTERNAL_ERROR')
        self.assertIn('viewer_request status=500', str(logs.output))
        self.assertIn('viewer_read_rejected', str(logs.output))
        self.assertNotIn(marker, str(logs.output))

    def test_service_owned_event_file_contains_only_projected_fields(self):
        from product_workflow.chat_tools import ChatCase
        from product_workflow.service_events import open_service_events
        case = ChatCase()
        handler = open_service_events(Path.cwd(), 'chat')
        self.assertIsNotNone(handler)
        try:
            case.trace('model_request', messages=[{'content': 'SYNTHETIC_PRIVATE_DIAGNOSTIC'}], phase='planning')
            handler.handle(logging.LogRecord('latticegpt.chat', logging.ERROR, '', 0,
                                             'SYNTHETIC_PRIVATE_DIAGNOSTIC', (), None))
            logging.getLogger('product_workflow.runtime_manager').error('event=runtime_task_finished status=failed')
        finally:
            handler.close()
        raw = handler.path.read_bytes()
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([e['event'] for e in events], ['model_request', 'runtime_task_failed'])
        self.assertIn('budget_used', events[0])
        self.assertNotIn(b'SYNTHETIC_PRIVATE_DIAGNOSTIC', raw)
        if os.environ.get('BMG_ACCEPTANCE_ARTIFACTS'):
            path = Path(os.environ['BMG_ACCEPTANCE_ARTIFACTS']) / 'synthetic-chat-service.events.jsonl'
            with path.open('xb') as out:
                out.write(raw)

