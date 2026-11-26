"""Exercise the real RPC pipes without invoking OMP, a model, or a subscription."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fourmilab import builder


class RpcLifecycle(unittest.TestCase):
    def invoke(self, body):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fixture = root / 'rpc-fixture.py'
            mark = root / 'closed'
            fixture.write_text('import json, os, sys\nfrom pathlib import Path\nx=Path(os.environ["FOURMILAB_TURN_DIR"])\n' + body, encoding='utf-8')
            popen = subprocess.Popen
            def launch(_argv, **kwargs):
                return popen([sys.executable, '-u', str(fixture), str(mark)], **kwargs)
            with patch.object(builder, 'check_route'), patch.object(subprocess, 'Popen', launch):
                result = builder.turn('unused', builder.MODEL, 'fixed test profile')
            return result, mark.exists()

    def test_receipt_is_kept_and_rpc_releases_resources_on_eof(self):
        result, closed = self.invoke('assert Path.cwd() != x\nq=json.loads(sys.stdin.buffer.readline())\n'
                                     'print(json.dumps({"id":q["id"],"type":"response","command":"get_available_commands",'
                                     '"success":True,"data":{"commands":[{"source":"extension","name":"fourmilab-compatibility-build"}]}}),flush=True)\n'
                                     'sys.stdin.buffer.readline()\n(x/"output.json").write_text(json.dumps({"error":"profile"}))\n'
                                     'sys.stdin.buffer.read()\nPath(sys.argv[1]).touch()\n')
        self.assertEqual({'error': 'profile'}, result)
        self.assertTrue(closed, 'OMP must get EOF and dispose resources, not be terminated on success')

    def test_unattended_interactive_request_blocks_without_a_response(self):
        result, closed = self.invoke('q=json.loads(sys.stdin.buffer.readline()); assert q["type"]=="get_available_commands"\n'
                                     'print(json.dumps({"type":"extension_ui_request","method":"confirm","id":"test"}),flush=True)\n'
                                     'if sys.stdin.buffer.read()==b"": Path(sys.argv[1]).touch()\n')
        self.assertEqual({'error': 'approval'}, result)
        self.assertTrue(closed, 'The unattended builder must not answer an approval request')

    def test_only_a_discovered_fixed_command_can_start_generation(self):
        fixture = (
            'import threading\n'
            'def serve():\n'
            '    request = json.loads(sys.stdin.buffer.readline())\n'
            '    assert request["type"] == "get_available_commands"\n'
            '    print(json.dumps({"id":request["id"],"type":"response","command":request["type"],'
            '"success":True,"data":{"commands":[{"source":"extension","name":"fourmilab-compatibility-build"}]}}),flush=True)\n'
            '    run = json.loads(sys.stdin.buffer.readline())\n'
            '    assert run["type"] == "prompt" and run["message"] == "/fourmilab-compatibility-build"\n'
            '    (x/"output.json").write_text(json.dumps({"actual_model":"fixture","source":"fixed","stop":"stop"}))\n'
            '    sys.stdin.buffer.read(); Path(sys.argv[1]).touch()\n'
            'thread = threading.Thread(target=serve, daemon=True); thread.start(); thread.join(1.5)\n'
            'if thread.is_alive(): os._exit(2)\n'
        )
        result, closed = self.invoke(fixture)
        self.assertEqual(result.get('source'), 'fixed')
        self.assertTrue(closed)

    def test_missing_extension_never_falls_through_to_a_model_prompt(self):
        result, closed = self.invoke(
            'import threading\n'
            'def serve():\n'
            '    q = json.loads(sys.stdin.buffer.readline())\n'
            '    print(json.dumps({"id":q["id"],"type":"response","command":"get_available_commands",'
            '"success":True,"data":{"commands":[{"source":"prompt","name":"fourmilab-compatibility-build"}]}}),flush=True)\n'
            '    if sys.stdin.buffer.read() == b"": Path(sys.argv[1]).touch()\n'
            't=threading.Thread(target=serve,daemon=True); t.start(); t.join(1.5)\n'
            'if t.is_alive(): os._exit(2)\n'
        )
        self.assertEqual(result, {'error': 'profile'})
        self.assertTrue(closed, 'Unregistered extension must receive EOF, not a fallback prompt')

    def test_unreviewed_model_is_rejected_before_starting_a_process(self):
        with patch.object(builder, 'check_route'), patch.object(subprocess, 'Popen', side_effect=AssertionError('child started')):
            with self.assertRaisesRegex(ValueError, 'model'):
                builder.turn('unused', 'openai-codex/not-the-pinned-model', 'fixed test profile')
