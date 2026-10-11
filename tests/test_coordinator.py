import importlib.util
import json
import os
import tempfile
import time
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

    def test_main_guidance_keeps_compaction_discretionary(self):
        result = c.hook({'hook_event_name': 'SessionStart', 'session_id': SID, 'cwd': self.tmp.name})
        guidance = result['hookSpecificOutput']['additionalContext']
        for phrase in ('Compaction is your decision', '600,000',
                       'at any coherent phase checkpoint', 'good stopping place',
                       'owner feedback, review, or approval', 'branch/PR/CI state',
                       'through Bash; do not print `/compact` yourself'):
            self.assertIn(phrase, guidance)
        self.assertNotIn('--force', guidance)
        self.assertIn(c.read_json(c.session_dir(SID) / 'binding.json')['token'], guidance)

    def test_custom_reminder_threshold_is_reflected_in_guidance(self):
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(json.dumps({
            'reminderThresholdTokens': 3000}))
        result = c.hook({'hook_event_name': 'SessionStart', 'session_id': SID})
        guidance = result['hookSpecificOutput']['additionalContext']
        self.assertIn('3,000', guidance)
        self.assertNotIn('600,000', guidance)

    def test_subagent_cannot_register_or_emit_reminder(self):
        self.assertIsNone(c.hook({'hook_event_name': 'SessionStart', 'session_id': SID, 'agent_id': 'agent-1'}))
        self.assertFalse((c.session_dir(SID) / 'binding.json').exists())
        self.start()
        self.assertIsNone(c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID,
                                  'agent_id': 'agent-1', 'transcript_path': 'anything'}))

    def test_only_reminder_threshold_uses_input_cache_counts(self):
        self.start()
        t = Path(self.tmp.name) / 't.jsonl'
        for n, above in [(199999, False), (200000, False), (500000, False),
                         (599999, False), (600000, True)]:
            t.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {
                'input_tokens': 1, 'cache_read_input_tokens': n - 1,
                'cache_creation_input_tokens': 0, 'output_tokens': 900}}}) + '\n')
            result = c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID,
                             'transcript_path': str(t)})
            self.assertEqual(c.read_json(c.session_dir(SID) / 'usage.json')['above_reminder'], above)
            if above:
                self.assertIn('good stopping place', result['hookSpecificOutput']['additionalContext'])
            else:
                self.assertIsNone(result)

    def test_threshold_reminder_preserves_deference(self):
        reminder = c.threshold_reminder(600000)
        self.assertIn('you may defer until a good stopping place', reminder)
        self.assertIn('save a concise handoff', reminder)

    def test_escalation_reminder_rearms_after_context_drops(self):
        self.start()
        t = Path(self.tmp.name) / 't.jsonl'
        def observe(n):
            t.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {
                'input_tokens': n, 'cache_read_input_tokens': 0,
                'cache_creation_input_tokens': 0, 'output_tokens': 1}}}) + '\n')
            return c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID,
                           'transcript_path': str(t)})
        self.assertIsNotNone(observe(600000))
        self.assertIsNone(observe(610000))
        self.assertIsNone(observe(500000))
        self.assertIsNotNone(observe(600000))

    def test_request_gates_and_duplicate(self):
        b = self.start()
        handoff = Path(self.tmp.name) / 'handoff.md'
        handoff.write_text('Next: inspect the project status.')
        class Args: pass
        a = Args()
        a.session, a.token, a.handoff, a.reason = SID, b['token'], str(handoff), 'phase done'
        a.agents_reconciled, a.owner_input_clear = False, False
        with self.assertRaises(ValueError): c.request(a)
        a.agents_reconciled = True
        with self.assertRaises(ValueError): c.request(a)
        a.owner_input_clear = True
        self.assertEqual(c.request(a), 0)
        with self.assertRaises(ValueError): c.request(a)

    def test_stop_requires_empty_task_registry(self):
        b = self.start()
        handoff = Path(self.tmp.name) / 'handoff.md'
        handoff.write_text('state')
        class Args: pass
        a = Args()
        a.session, a.token, a.handoff, a.reason = SID, b['token'], str(handoff), 'phase done'
        a.agents_reconciled, a.owner_input_clear = True, True
        c.request(a)
        with patch.object(c.subprocess, 'Popen') as popen:
            c.hook({'hook_event_name': 'Stop', 'session_id': SID, 'background_tasks': [{'type':'subagent'}]})
            popen.assert_not_called()
            self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'queued')
            c.hook({'hook_event_name': 'Stop', 'session_id': SID, 'background_tasks': []})
            popen.assert_called_once()
            self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'stop_seen')

    def test_child_lifecycle_blocks_main_compaction(self):
        self.start()
        c.hook({'hook_event_name': 'SubagentStart', 'session_id': SID,
                'agent_id': 'agent-1', 'agent_type': 'Explore'})
        self.assertEqual(c.active_agents(SID), ['agent-1'])
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state': 'queued', 'created': time.time()})
        with patch.object(c.subprocess, 'Popen') as popen:
            c.hook({'hook_event_name': 'Stop', 'session_id': SID, 'background_tasks': []})
            popen.assert_not_called()
        c.hook({'hook_event_name': 'SubagentStop', 'session_id': SID,
                'agent_id': 'agent-1', 'agent_type': 'Explore'})
        self.assertEqual(c.active_agents(SID), [])

    def test_owner_prompt_cancels_pending_request(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state': 'queued', 'created': time.time()})
        c.hook({'hook_event_name': 'UserPromptSubmit', 'session_id': SID,
                'transcript_path': '/missing/transcript'})
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'cancelled')

    def test_owner_prompt_cancels_pending_resume(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state': 'confirmed', 'resume_state': 'pending'})
        c.hook({'hook_event_name': 'UserPromptSubmit', 'session_id': SID,
                'transcript_path': '/missing/transcript'})
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['resume_state'], 'cancelled')

    def test_postcompact_starts_optional_resume_and_ignores_auto(self):
        self.start()
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'resumeAfterCompaction': True}))
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state': 'sent', 'created': time.time(), 'handoff': '/tmp/handoff'})
        with patch.object(c.subprocess, 'Popen') as popen:
            c.hook({'hook_event_name': 'PostCompact', 'session_id': SID, 'trigger': 'auto'})
            popen.assert_not_called()
            self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'sent')
            c.hook({'hook_event_name': 'PostCompact', 'session_id': SID, 'trigger': 'manual'})
            popen.assert_called_once()
        req = c.read_json(c.session_dir(SID) / 'request.json')
        self.assertEqual((req['state'], req['resume_state']), ('confirmed', 'pending'))

    def test_resume_targets_same_pane_with_handoff(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state': 'confirmed', 'resume_state': 'pending', 'handoff': '/tmp/handoff'})
        calls = []
        def fake_tmux(*args):
            calls.append(args)
            return '❯\u00a0\n───\n  ⏵ auto mode on' if args[0] == 'capture-pane' else ''
        with patch.object(c, 'tmux', side_effect=fake_tmux), patch.object(c.time, 'sleep'):
            self.assertEqual(c.resume(SID), 0)
        sends = [call for call in calls if call[0] == 'send-keys']
        self.assertEqual(sends[0][2], '%12')
        self.assertIn('/tmp/handoff', sends[0][4])
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['resume_state'], 'sent')

    def test_prompt_detection_matches_current_claude_ui(self):
        self.assertTrue(c.idle_prompt_visible("header\n❯\u00a0Try \"how does this work?\"\n───\n  ⏵ auto mode on"))
        self.assertFalse(c.idle_prompt_visible("header\n❯ user typed text\n───\n  ⏵ auto mode on"))

    def test_delivery_fails_closed_without_idle_prompt(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json', {'state':'stop_seen', 'handoff':'/tmp/handoff'})
        with patch.object(c, 'tmux', return_value='Claude is working') as tmux:
            with patch.object(c.time, 'sleep'):
                self.assertEqual(c.deliver(SID), 2)
            self.assertFalse(any(call.args and call.args[0] == 'send-keys' for call in tmux.call_args_list))
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'failed')


    def test_delivery_targets_exact_bound_pane(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state':'stop_seen', 'handoff':'/tmp/handoff'})
        calls = []
        def fake_tmux(*args):
            calls.append(args)
            if args[0] == 'capture-pane':
                return 'response\n❯\u00a0\n────────────────\n  ⏵ auto mode on'
            return ''
        with patch.object(c, 'tmux', side_effect=fake_tmux), patch.object(c.time, 'sleep'):
            self.assertEqual(c.deliver(SID), 0)
        sends = [call for call in calls if call[0] == 'send-keys']
        self.assertEqual(sends[0], ('send-keys', '-t', '%12', '-l',
                                     '/compact Preserve the durable handoff at /tmp/handoff'))
        self.assertEqual(sends[1], ('send-keys', '-t', '%12', 'Enter'))
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'sent')

    def test_expired_request_does_not_launch_worker(self):
        self.start()
        c.atomic_json(c.session_dir(SID) / 'request.json',
                      {'state':'queued', 'created':time.time()-10000})
        with patch.object(c.subprocess, 'Popen') as popen:
            c.hook({'hook_event_name':'Stop', 'session_id':SID, 'background_tasks':[]})
            popen.assert_not_called()
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'expired')

    def test_invalid_config_fails_closed(self):
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'reminderThresholdTokens': -1}))
        with self.assertRaises(ValueError): c.config()
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'softThresholdTokens': 200000}))
        with self.assertRaises(ValueError): c.config()
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'preserveHandoff': False}))
        with self.assertRaises(ValueError): c.config()
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'resumeAfterCompaction': 'false'}))
        with self.assertRaises(ValueError): c.config()

    def test_independent_session_paths(self):
        self.assertNotEqual(c.session_dir(SID), c.session_dir('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'))
        with self.assertRaises(ValueError): c.session_dir('../other')

    def test_postcompact_confirms_without_summary_logging(self):
        self.start()
        Path(os.environ['ORCH_COMPACT_CONFIG']).write_text(
            json.dumps({'resumeAfterCompaction': False}))
        c.atomic_json(c.session_dir(SID) / 'request.json', {'state':'sent', 'created':0})
        c.hook({'hook_event_name':'PostCompact', 'session_id':SID, 'trigger':'manual',
                'compact_summary':'private conversation content'})
        self.assertEqual(c.read_json(c.session_dir(SID) / 'request.json')['state'], 'confirmed')
        self.assertNotIn('private conversation content', (c.session_dir(SID) / 'events.jsonl').read_text())

    def test_after_usage_requires_postcompact_response(self):
        self.start()
        d = c.session_dir(SID)
        c.atomic_json(d / 'request.json', {'state': 'confirmed', 'confirmed_at': time.time()})
        transcript = Path(self.tmp.name) / 'conversation.jsonl'
        def sample(timestamp):
            transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': timestamp,
                'message': {'usage': {'input_tokens': 5, 'output_tokens': 1}}}) + '\n')
            c.hook({'hook_event_name': 'PostToolUse', 'session_id': SID,
                    'transcript_path': str(transcript)})
        sample('2000-01-01T00:00:00Z')
        self.assertNotIn('after', c.read_json(d / 'request.json'))
        sample('2030-01-01T00:00:00Z')
        self.assertEqual(c.read_json(d / 'request.json')['after'], 5)


if __name__ == '__main__': unittest.main()
