import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).parents[1] / 'bin/orchestrator_compact.py'
spec = importlib.util.spec_from_file_location('coordinator', MODULE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
SID = '12345678-1234-1234-1234-123456789abc'


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'ORCH_COMPACT_STATE_DIR': self.tmp.name,
                                            'ORCH_COMPACT_CONFIG': str(Path(self.tmp.name) / 'config.json'),
                                            'TMUX_PANE': '%12'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.identity = {'pane': '%12', 'pid': '123', 'tty': '/dev/ttys001', 'command': 'claude'}
        self.tmux_patch = patch.object(c, 'pane_identity', return_value=self.identity)
        self.tmux_patch.start()
        self.addCleanup(self.tmux_patch.stop)

    def start(self):
        result = c.hook({'hook_event_name': 'SessionStart', 'session_id': SID, 'cwd': self.tmp.name})
        self.assertIn('additionalContext', result['hookSpecificOutput'])
        return c.read_json(c.session_dir(SID) / 'binding.json')

    def test_subagent_cannot_register_or_emit_reminder(self):
        self.assertIsNone(c.hook({'hook_event_name': 'SessionStart', 'session_id': SID, 'agent_id': 'agent-1'}))
        self.assertFalse((c.session_dir(SID) / 'binding.json').exists())
        self.start()
        self.assertIsNone(c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID,
                                  'agent_id': 'agent-1', 'transcript_path': 'anything'}))

    def test_thresholds_use_input_cache_counts(self):
        self.start()
        t = Path(self.tmp.name) / 't.jsonl'
        for n, tier in [(199999, 'normal'), (200000, 'soft'), (300000, 'preferred'), (400000, 'escalated')]:
            t.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {
                'input_tokens': 1, 'cache_read_input_tokens': n - 1,
                'cache_creation_input_tokens': 0, 'output_tokens': 900}}}) + '\n')
            c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID, 'transcript_path': str(t)})
            self.assertEqual(c.read_json(c.session_dir(SID) / 'usage.json')['tier'], tier)

    def test_request_gates_and_duplicate(self):
        b = self.start()
        handoff = Path(self.tmp.name) / 'handoff.md'
        handoff.write_text('Next: inspect the project status.')
        class Args: pass
        a = Args()
        a.session, a.token, a.handoff, a.reason = SID, b['token'], str(handoff), 'phase done'
        a.agents_reconciled, a.force = False, False
        with self.assertRaises(ValueError): c.request(a)
        a.agents_reconciled = True
        with self.assertRaises(ValueError): c.request(a)
        a.force = True
        self.assertEqual(c.request(a), 0)
        with self.assertRaises(ValueError): c.request(a)

    def test_stop_requires_empty_task_registry(self):
        b = self.start()
        handoff = Path(self.tmp.name) / 'handoff.md'
        handoff.write_text('state')
        class Args: pass
        a = Args()
        a.session, a.token, a.handoff, a.reason = SID, b['token'], str(handoff), 'phase done'
        a.agents_reconciled, a.force = True, True
        c.request(a)
        with patch.object(c.subprocess, 'Popen') as popen:
            c.hook({'hook_event_name': 'Stop', 'session_id': SID, 'background_tasks': [{'type':'subagent'}]})
            popen.assert_not_called()
            self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'queued')
            c.hook({'hook_event_name': 'Stop', 'session_id': SID, 'background_tasks': []})
            popen.assert_called_once()
            self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'stop_seen')

    def test_delivery_fails_closed_without_idle_prompt(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json', {'state':'stop_seen', 'handoff':'/tmp/handoff'})
        with patch.object(c, 'tmux', return_value='Claude is working') as tmux:
            with patch.object(c.time, 'sleep'):
                self.assertEqual(c.deliver(SID), 2)
            self.assertFalse(any(call.args and call.args[0] == 'send-keys' for call in tmux.call_args_list))
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'failed')

    def test_postcompact_confirms_without_summary_logging(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json', {'state':'sent', 'created':0})
        c.hook({'hook_event_name':'PostCompact', 'session_id':SID, 'trigger':'manual',
                'compact_summary':'private conversation content'})
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'confirmed')
        self.assertNotIn('private conversation content', (c.session_dir(SID) / 'events.jsonl').read_text())


if __name__ == '__main__': unittest.main()
