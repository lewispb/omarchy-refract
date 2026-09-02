"""End-to-end run of the bridge process over its stdin/stdout contract.

No OpenRGB server or Hue bridge is involved: the connect goes to a local port
that nothing listens on, and the Hue backend is disabled.
"""

import json
import os
import socket
import subprocess
import sys
import unittest

BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openrgb_bridge.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeProcessTest(unittest.TestCase):
    def run_bridge(self, stdin_bytes, timeout=15):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", XDG_STATE_HOME=self.state_dir)
        proc = subprocess.run([sys.executable, BRIDGE], input=stdin_bytes, capture_output=True, timeout=timeout, env=env)
        events = [json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()]
        return proc, events

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_hello_then_failed_connect_then_quit(self):
        port = free_port()
        commands = (
            json.dumps({"op": "connect", "host": "127.0.0.1", "port": port}) + "\n"
            + json.dumps({"op": "hue", "enabled": False, "address": ""}) + "\n"
            + json.dumps({"op": "quit"}) + "\n"
        ).encode()
        proc, events = self.run_bridge(commands)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(events[0]["event"], "hello")
        self.assertIn("openrgbBinary", events[0])
        states = [e for e in events if e["event"] == "state"]
        self.assertTrue(states, events)
        self.assertFalse(states[0]["connected"])
        self.assertIn("error", states[0])
        self.assertEqual(states[0]["devices"], [])
        self.assertEqual(states[0]["hue"]["status"], "disabled")

    def test_commands_written_together_are_all_processed(self):
        # Three commands in one write: the second must not wait for the third.
        commands = (
            json.dumps({"op": "apply", "anchors": ["FF0000"]}) + "\n"
            + json.dumps({"op": "off", "device": 0}) + "\n"
            + json.dumps({"op": "quit"}) + "\n"
        ).encode()
        proc, events = self.run_bridge(commands)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        results = [(e["op"], e["ok"]) for e in events if e["event"] == "result"]
        self.assertEqual(results, [("apply", False), ("off", False)])

    def test_stdin_close_ends_the_process(self):
        proc, events = self.run_bridge(b"")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual([e["event"] for e in events], ["hello"])

    def test_garbage_lines_are_ignored(self):
        proc, events = self.run_bridge(b"not json\n\n{\"op\":\"quit\"}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(proc.stderr, b"")


if __name__ == "__main__":
    unittest.main()
