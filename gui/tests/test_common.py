"""Unit tests for vmvpn_common (no Gtk). Run: python3 -m unittest discover -s gui/tests -v"""

import os
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)

import vmvpn_common as vc
from gi.repository import GLib

STUB_SCRIPT = """#!/bin/bash
set -u
dir="${STUB_DIR:?}"
i=0
if [[ -f "$dir/count" ]]; then i=$(cat "$dir/count"); fi
i=$((i + 1))
echo "$i" > "$dir/count"
echo "$*" >> "$dir/calls"
cat > "$dir/stdin.$i"
if [[ -f "$dir/out.$i" ]]; then cat "$dir/out.$i"; fi
if [[ -f "$dir/err.$i" ]]; then cat "$dir/err.$i" >&2; fi
rc=0
if [[ -f "$dir/rc.$i" ]]; then rc=$(cat "$dir/rc.$i"); fi
exit "$rc"
"""


def drive(predicate, timeout=15):
    """Iterate the GLib main context until predicate() or timeout."""
    ctx = GLib.MainContext.default()
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("timed out waiting for main loop")
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


class CliStub:
    """Bash stub standing in for the vmvpn CLI; one step per invocation."""

    def __init__(self, root):
        self.dir = os.path.join(root, "stub")
        os.makedirs(self.dir)
        self.path = os.path.join(root, "vmvpn-stub")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(STUB_SCRIPT)
        os.chmod(self.path, stat.S_IRWXU)
        self._next_step = 1

    def add_step(self, rc=0, out="", err=""):
        i = self._next_step
        self._next_step += 1
        if rc:
            self._write("rc.%d" % i, "%d\n" % rc)
        if out:
            self._write("out.%d" % i, out)
        if err:
            self._write("err.%d" % i, err)
        return i

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def call_count(self):
        try:
            with open(os.path.join(self.dir, "count")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 0

    def calls(self):
        try:
            with open(os.path.join(self.dir, "calls")) as f:
                return [line.rstrip("\n") for line in f]
        except OSError:
            return []

    def stdin(self, i):
        with open(os.path.join(self.dir, "stdin.%d" % i)) as f:
            return f.read()


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vmvpn-test-")
        self.stub = CliStub(self.tmp)
        self._saved_env = {}
        for name in ("VMVPN_CLI", "STUB_DIR", "XDG_STATE_HOME"):
            self._saved_env[name] = os.environ.get(name)
        os.environ["VMVPN_CLI"] = self.stub.path
        os.environ["STUB_DIR"] = self.stub.dir
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        self.done = None  # (rc, stdout, stderr)

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _on_done(self, rc, stdout, stderr):
        self.done = (rc, stdout, stderr)

    def run_flow(self, ask_password=None, ask_trust=None, password=None):
        flow = vc.ConnectFlow(
            ask_password or (lambda msg, cb: cb(None, False)),
            ask_trust or (lambda new, saved, cb: cb(False)),
            self._on_done,
            password=password,
        )
        flow.start()
        drive(lambda: self.done is not None)
        return flow


class TestFindCli(FlowTestCase):
    def test_env_override(self):
        self.assertEqual(vc.find_cli(), self.stub.path)


class TestConnectFlow(FlowTestCase):
    def test_plain_success(self):
        self.stub.add_step(rc=0, out="VPN connected.\n")
        self.run_flow()
        self.assertEqual(self.done[0], 0)
        self.assertEqual(self.stub.calls(), ["vpn-connect"])

    def test_password_prompt_then_success(self):
        self.stub.add_step(rc=4)  # needs password
        self.stub.add_step(rc=0, out="VPN connected.\n")
        asks = []

        def ask(msg, cb):
            asks.append(msg)
            cb("pw", False)

        self.run_flow(ask_password=ask)
        self.assertEqual(self.done[0], 0)
        self.assertEqual(asks, [None])
        self.assertEqual(
            self.stub.calls(), ["vpn-connect", "vpn-connect --password-stdin"]
        )
        self.assertEqual(self.stub.stdin(2), "pw\n")

    def test_password_cancel_reports_rc4(self):
        self.stub.add_step(rc=4)
        self.run_flow(ask_password=lambda msg, cb: cb(None, False))
        self.assertEqual(self.done[0], 4)
        self.assertEqual(self.stub.call_count(), 1)

    def test_login_failed_reprompts_with_message(self):
        self.stub.add_step(rc=2, out="Login failed\n")
        self.stub.add_step(rc=0, out="VPN connected.\n")
        asks = []

        def ask(msg, cb):
            asks.append(msg)
            cb("betterpw", False)

        self.run_flow(ask_password=ask)
        self.assertEqual(self.done[0], 0)
        self.assertEqual(len(asks), 1)
        self.assertIn("Login failed", asks[0])
        self.assertEqual(self.stub.stdin(2), "betterpw\n")

    def test_cert_untrusted_trust_reruns_with_flag(self):
        self.stub.add_step(
            rc=3, out="VMVPN_CERT_UNTRUSTED new=AA:BB:CC saved=\n"
        )
        self.stub.add_step(rc=0, out="VPN connected.\n")
        trusts = []

        def ask_trust(new_fp, saved_fp, cb):
            trusts.append((new_fp, saved_fp))
            cb(True)

        self.run_flow(ask_trust=ask_trust)
        self.assertEqual(self.done[0], 0)
        self.assertEqual(trusts, [("AA:BB:CC", None)])
        self.assertIn(
            "--trust-fingerprint AA:BB:CC", self.stub.calls()[1]
        )

    def test_cert_untrusted_reject_reports_rc3(self):
        self.stub.add_step(
            rc=3, out="VMVPN_CERT_UNTRUSTED new=AA:BB:CC saved=00:11\n"
        )
        trusts = []

        def ask_trust(new_fp, saved_fp, cb):
            trusts.append((new_fp, saved_fp))
            cb(False)

        self.run_flow(ask_trust=ask_trust)
        self.assertEqual(self.done[0], 3)
        self.assertEqual(trusts, [("AA:BB:CC", "00:11")])
        self.assertEqual(self.stub.call_count(), 1)

    def test_remember_password_saves_via_password_set(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=0)  # connect with password
        self.stub.add_step(rc=0)  # password-set
        self.run_flow(ask_password=lambda msg, cb: cb("secretpw", True))
        self.assertEqual(self.done[0], 0)
        self.assertEqual(self.stub.call_count(), 3)
        self.assertEqual(self.stub.calls()[2], "password-set --stdin")
        self.assertEqual(self.stub.stdin(3), "secretpw\n")

    def test_no_remember_skips_password_set(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=0)
        self.run_flow(ask_password=lambda msg, cb: cb("pw", False))
        self.assertEqual(self.done[0], 0)
        self.assertEqual(self.stub.call_count(), 2)
        self.assertNotIn("password-set", " ".join(self.stub.calls()))

    def test_password_prompt_cap(self):
        for _ in range(5):
            self.stub.add_step(rc=4)
        asks = []

        def ask(msg, cb):
            asks.append(msg)
            cb("pw", False)

        self.run_flow(ask_password=ask)
        self.assertEqual(self.done[0], 4)
        self.assertEqual(len(asks), vc.ConnectFlow.MAX_PASSWORD_PROMPTS)
        self.assertEqual(
            self.stub.call_count(), vc.ConnectFlow.MAX_PASSWORD_PROMPTS + 1
        )

    def test_trust_prompt_cap(self):
        for _ in range(3):
            self.stub.add_step(rc=3, out="VMVPN_CERT_UNTRUSTED new=AA saved=\n")
        self.run_flow(ask_trust=lambda n, s, cb: cb(True))
        self.assertEqual(self.done[0], 3)
        self.assertEqual(self.stub.call_count(), 2)

    def test_password_dropped_when_done(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=0)
        flow = self.run_flow(ask_password=lambda msg, cb: cb("pw", False))
        self.assertIsNone(flow._password)


class TestParsers(FlowTestCase):
    def test_parse_status_valid(self):
        text = '{"schema":1,"vpn":{"state":"connected","raw":"x"}}'
        obj = vc.parse_status(text)
        self.assertEqual(obj["vpn"]["state"], "connected")

    def test_parse_status_invalid(self):
        self.assertIsNone(vc.parse_status("not json"))
        self.assertIsNone(vc.parse_status("[1,2]"))
        self.assertIsNone(vc.parse_status(""))

    def test_parse_cert_untrusted(self):
        self.assertEqual(
            vc.parse_cert_untrusted("VMVPN_CERT_UNTRUSTED new=AA:BB saved=\n"),
            ("AA:BB", None),
        )
        self.assertEqual(
            vc.parse_cert_untrusted(
                "noise\nVMVPN_CERT_UNTRUSTED new=DD saved=CC:11\n"
            ),
            ("DD", "CC:11"),
        )
        self.assertIsNone(vc.parse_cert_untrusted("nothing here"))
        self.assertIsNone(vc.parse_cert_untrusted(None))


class TestRunCliAndLog(FlowTestCase):
    def test_run_cli_captures_output_and_rc(self):
        self.stub.add_step(rc=5, out="some out\n", err="some err\n")
        result = {}
        vc.run_cli(
            ["bogus"],
            lambda rc, out, err: result.update(rc=rc, out=out, err=err),
        )
        drive(lambda: "rc" in result)
        self.assertEqual(result["rc"], 5)
        self.assertEqual(result["out"], "some out\n")
        self.assertEqual(result["err"], "some err\n")

    def test_log_has_args_and_rc_but_never_password(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=0)
        self.stub.add_step(rc=0)  # password-set
        self.run_flow(ask_password=lambda msg, cb: cb("Sup3rSecret!", True))
        self.assertEqual(self.done[0], 0)
        log_path = vc.gui_log_path()
        with open(log_path, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("vpn-connect", log)
        self.assertIn("password-set", log)
        self.assertIn("exit 0", log)
        self.assertNotIn("Sup3rSecret!", log)

    def test_run_cli_missing_binary_reports_rc1(self):
        os.environ["VMVPN_CLI"] = os.path.join(self.tmp, "nonexistent")
        result = {}
        vc.run_cli(
            ["status"],
            lambda rc, out, err: result.update(rc=rc, out=out, err=err),
        )
        drive(lambda: "rc" in result)
        self.assertEqual(result["rc"], 1)


if __name__ == "__main__":
    unittest.main()
