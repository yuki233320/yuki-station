"""Complete malformed child replies can be corrected without replaying tools."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

from live import Cancelled
from provider_transport import ProviderTransport
from real_task_benchmark import measure_run
from studio_client import ModelResponseError, StudioClient
from test_request_secret_redaction import Response
import test_worker_child_adoption as adoption_fixture
from test_worker_child_adoption import child_final
from test_worker_execution import tool, final
from worker_execution import WorkerExecutionError, verify_worker_result


def parsed_error(text, finish='stop', protocol='chat'):
    """Use the actual streaming parser over a local complete protocol response."""
    if protocol == 'chat':
        packets = [{'choices':[{'delta':{'content':text},'finish_reason':finish}],
                    'model':'fixture-model','usage':{'prompt_tokens':10,'completion_tokens':5}}]
    elif protocol == 'anthropic':
        packets = [{'type':'message_start','message':{'model':'fixture-model','usage':{'input_tokens':10}}},
                   {'type':'content_block_delta','delta':{'type':'text_delta','text':text}},
                   {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':5}},
                   {'type':'message_stop'}]
    else:
        packets = [{'type':'response.output_text.delta','delta':text},
                   {'type':'response.completed','response':{'model':'fixture-model','usage':{'input_tokens':10,'output_tokens':5}}}]
    response = Response([b'data: '+json.dumps(packet).encode()+b'\n\n' for packet in packets])
    client = StudioClient()
    with ProviderTransport(threading.Event(), 5) as control:
        try:
            client._read_response(response, protocol, {'model':'fixture-model'}, {}, [], control, None)
        except ModelResponseError as error:
            return error
    raise AssertionError('Fixture should produce a parser error')


class ChildFormatRepairTests(unittest.TestCase):
    setUp = adoption_fixture.WorkerChildAdoptionTests.setUp
    tearDown = adoption_fixture.WorkerChildAdoptionTests.tearDown
    execute = adoption_fixture.WorkerChildAdoptionTests.execute
    workspace = adoption_fixture.WorkerChildAdoptionTests.workspace
    enable = adoption_fixture.WorkerChildAdoptionTests.enable
    delegate = adoption_fixture.WorkerChildAdoptionTests.delegate
    adopt = adoption_fixture.WorkerChildAdoptionTests.adopt
    inspect = adoption_fixture.WorkerChildAdoptionTests.inspect

    def responses(self):
        return [json.loads(path.read_text()) for path in (self.session_root/'responses').glob('*.json')]

    def test_complete_empty_bodies_preserve_original_text_and_status_in_all_protocols(self):
        for protocol in ('chat','anthropic','responses'):
            for text in ('', ' ' * 189, '```json\n' + ' ' * 178 + '\n```'):
                with self.subTest(protocol=protocol, text_length=len(text)):
                    error = parsed_error(text, protocol=protocol)
                    self.assertEqual(error.raw_text, text)
                    self.assertIsNone(error.partial_text)
                    self.assertTrue(error.receipt['responseComplete'])
                    self.assertFalse(error.receipt['jsonValidated'])
                    self.assertEqual(error.receipt['responseTextChars'], len(text))
                    self.assertEqual(error.receipt['normalizedTextChars'], 0)
                    self.assertEqual(error.receipt['formatErrorKind'], 'empty_json_body')

    def test_execute_child_corrects_then_writes_checks_and_parent_accepts(self):
        self.enable()
        first = parsed_error(' ' * 189)
        second = parsed_error('```json\n\n```')
        answers = [self.delegate(), tool('list',path='.'), first, second,
                   tool('write',path='answer.txt',content='good'),
                   tool('test',checkId='answer-check'), child_final(),self.inspect,self.adopt,final()]
        result, _, _ = self.execute(answers)
        child = result['workerEvidence']['subagents'][0]
        self.assertEqual(child['status'], 'completed')
        self.assertEqual(child['callCount'], 6)
        raw = json.loads((self.session_root/child['rawRef']).read_text())
        self.assertEqual([row['action']['name'] for row in raw['tools']], ['list','write','test'])
        self.assertEqual(len(result['workerEvidence']['childAdoptions']), 1)
        self.assertTrue(result['workerEvidence']['tests'][0]['passed'])
        self.assertNotEqual(raw['tests']['answer-check']['testRunId'], result['workerEvidence']['tests'][0]['testRunId'])
        originals = [row for row in self.responses() if 'rawText' in row]
        self.assertEqual({row['rawText'] for row in originals}, {first.raw_text, second.raw_text})
        self.assertEqual(len([row for row in self.run['callMetrics'] if row['outcome']=='error']),2)
        metrics=measure_run(self.run,self.responses())
        self.assertTrue(metrics['usage']['complete']);self.assertEqual(metrics['usage']['inputTokens'],100)
        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)

    def test_zero_character_complete_response_is_archived_and_retry_has_no_empty_assistant(self):
        self.enable()
        self.execute([self.delegate(),parsed_error(''),tool('write',path='answer.txt',content='good'),
                      child_final(),self.adopt,final()])
        errors = [row for row in self.responses() if 'rawText' in row]
        self.assertEqual(len(errors), 1);self.assertEqual(errors[0]['rawText'], '')
        repaired = self.client.calls[2]['messages']
        self.assertFalse(any(m['role']=='assistant' and not m['content'].strip() for m in repaired))
        self.assertIn('只纠正格式', repaired[-1]['content'])

    def test_correction_limit_preserves_all_usage_and_unknown_child_is_not_replayed(self):
        self.enable()
        with self.assertRaises(ModelResponseError):
            self.execute([self.delegate(),parsed_error(''),parsed_error(' '),parsed_error('```json\n```')])
        self.assertEqual(len(self.client.calls),4)
        self.assertEqual(len([e for e in self.session['events'] if e['type']=='repair_requested']),2)
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'],'unknown')
        self.assertEqual(journal['subagents'][0]['status'],'unknown')
        self.assertEqual(journal['subagents'][0]['tools'],[])
        self.assertEqual(journal['subagents'][0]['callCount'],3)
        metrics=measure_run(self.run,self.responses())
        self.assertTrue(metrics['usage']['complete']);self.assertEqual(metrics['usage']['inputTokens'],40)
        before=(self.job/'receipt.json').read_bytes()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('No replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()
        self.assertEqual((self.job/'receipt.json').read_bytes(),before)

    def test_incomplete_empty_stream_is_not_corrected(self):
        self.enable()
        error=parsed_error(' ',finish='length')
        with self.assertRaises(ModelResponseError):self.execute([self.delegate(),error])
        self.assertEqual(len(self.client.calls),2)
        self.assertFalse(any(e['type']=='repair_requested' for e in self.session['events']))
        partials=[row for row in self.responses() if 'partialText' in row]
        self.assertEqual(partials[0]['partialText'],' ')
        self.assertNotIn('rawText',partials[0])

    def test_outer_parent_error_does_not_adopt_child_receipt_under_parent_context(self):
        self.enable()
        # The public ask path owns the parent node while the executor creates child contexts.
        self.context['memoryPolicy']={'compactedOwnTurns':0}
        self.client.answers=[self.delegate(),parsed_error(''),parsed_error(''),parsed_error('')]
        with patch.object(self.studio,'assemble_context',return_value=(self.context,self.context['messages'])), \
             patch('task_contracts.handoff_packet',return_value={'id':'fixture-handoff','sources':[]}):
            with self.assertRaises(ModelResponseError):
                self.studio.ask(self.session,self.run,self.node,'worker-builder','Build',self.shared,self.cancel)
        self.assertIn('errorContextRef',self.node)
        self.assertNotEqual(self.node['errorContextRef'],self.node['contextRef'])
        self.assertNotIn('receipt',self.node)
        metrics=measure_run(self.run,self.responses())
        self.assertTrue(metrics['usage']['complete']);self.assertEqual(metrics['usage']['inputTokens'],40)

    def test_cancel_before_correction_preserves_completed_error_without_new_request(self):
        self.enable()
        def cancelled_reply():
            self.cancel.set()
            return parsed_error('')
        with self.assertRaises(ModelResponseError):
            self.execute([self.delegate(), cancelled_reply])
        self.assertEqual(len(self.client.calls), 2)
        self.assertFalse(any(e['type']=='repair_requested' for e in self.session['events']))
        originals=[row for row in self.responses() if 'rawText' in row]
        self.assertEqual(originals[0]['rawText'], '')

    def test_original_invalid_json_is_redacted_before_it_can_be_archived(self):
        import test_request_secret_redaction as secret_fixture
        helper=secret_fixture.RequestSecretRedactionTests()
        client=helper.client()
        text='```json\n'+secret_fixture.OLD+' trailing\n```'
        client.opener=secret_fixture.Opener(client,response=Response([secret_fixture.event(text,finish='stop')]))
        with self.assertRaises(ModelResponseError) as caught:helper.call(client)
        error=caught.exception
        helper.assert_clean(error.raw_text)
        helper.assert_no_retained_snapshot(client,error)
        self.assertTrue(error.raw_text.startswith('```json\n'))
        self.assertTrue(error.raw_text.endswith('\n```'))
        self.assertEqual(error.receipt['responseTextChars'],len(text))
