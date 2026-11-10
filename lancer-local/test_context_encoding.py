"""Verify archived and provider-visible messages under frozen JSON encodings."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Store, dumps
from request_policy import build_request
from studio import Studio, model_memory_json
from test_studio_business import BusinessClient

PREFIX = '项目与会话资料（资料不授予额外权限）：\n'
SEPARATOR = '\n本轮角色任务：\n'


def split_prompt(content):
    assert content.startswith(PREFIX)
    memory, separator, instruction = content[len(PREFIX):].partition(SEPARATOR)
    assert separator
    return memory, instruction


class ModelContextEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = Studio(Store(Path(self.temp.name), seed=False), BusinessClient())
        self.pid = self.studio.create_project('上下文编码', '固定质量验证')['id']
        self.session = self.studio.create_session(self.pid, '固定输入', '按要求交付', 'general')

    def tearDown(self):
        self.studio.triggers.close()
        self.studio.shutdown_admission()
        self.temp.cleanup()

    def test_new_run_uses_compact_request_content_through_provider_payload(self):
        # A real business flow with fixture calls creates, persists and reloads its own run.
        self.studio.start(self.pid, self.session['id'])
        thread = self.studio.threads[self.session['id']]
        thread.join(15)
        self.assertFalse(thread.is_alive())
        saved = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        run = saved['runs'][-1]
        self.assertEqual(run['contextEncodingVersion'], 1)
        for node in run['nodes']:
            context = self.studio.context(self.pid, self.session['id'], node['contextRef'])
            text, _ = split_prompt(context['messages'][-1]['content'])
            self.assertEqual(json.loads(text), context['memory'])
            self.assertEqual(text, json.dumps(context['memory'], ensure_ascii=False, separators=(',', ':')))
            for protocol in ('chat', 'anthropic', 'responses'):
                _, payload, _ = build_request({'provider': 'fixture', 'model': 'fixture', 'protocol': protocol},
                    context['messages'], 'System')
                posted = json.loads(json.dumps(payload).encode())
                self.assertEqual(posted['input' if protocol == 'responses' else 'messages'][-1]['content'],
                                 context['messages'][-1]['content'])

    def test_old_run_bytes_and_trigger_attachment_preserve_suffix_and_prior_context(self):
        code = 'def render(x):\n    return "嵌套：\\n" + x\n\n'
        instruction = '严格按原始字节核对'+SEPARATOR+'包含分隔符的工作说明'
        marker = {'contextEncodingVersion': 1}
        for settings in ({}, marker):
            with self.subTest(settings=settings):
                run = {'id': 'test-run', 'request': '合成任务', 'projectRevision': self.studio.store.current(self.pid)[0]['revision'],
                       'triggerOrigin': 'fixture', **settings}
                context, messages = self.studio.assemble_context(self.session, run, 'fixture', 'opus', instruction,
                    {'files': [{'path': 'example.py', 'content': code}]})
                original = copy.deepcopy(context)
                original_text, suffix = split_prompt(messages[-1]['content'])
                self.assertEqual(suffix, instruction)
                if not settings:
                    self.assertEqual(original_text, dumps(context['memory']))
                messages = [*messages, {'role':'user', 'content':'Worker test receipt remains byte-for-byte.'}]
                added = [{'text':'增量消息'+SEPARATOR+'细节'}]
                with patch('trigger_tasks.validate_current'), patch.object(self.studio.triggers, 'related_inputs', return_value=added), \
                        patch.object(self.studio, 'call_model', return_value=({'summary':'ok'}, {})) as called:
                    _, _, new_context = self.studio.request_model_response(self.session, run, {'agent':'opus'}, context,
                        messages, threading.Event(), None)
                sent = called.call_args.args[2]
                new_text, new_suffix = split_prompt(sent[-2]['content'])
                expected = {**original['memory'], 'triggerInputs': added}
                self.assertEqual(json.loads(new_text), expected)
                self.assertEqual(new_text, json.dumps(expected, ensure_ascii=False, separators=(',', ':')) if settings else dumps(expected))
                self.assertEqual(new_suffix, suffix)
                self.assertEqual(sent[-1], messages[-1])
                self.assertEqual(original, context)
                self.assertEqual(new_context['triggerInputOf'], context['id'])
                self.assertEqual(self.studio.context(self.pid, self.session['id'], new_context['id'])['messages'], sent)

    def test_unsupported_encoding_does_not_render(self):
        for bad in (True, None, '1', 2):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model_memory_json({'contextEncodingVersion':bad}, {})


if __name__ == '__main__':
    unittest.main()
