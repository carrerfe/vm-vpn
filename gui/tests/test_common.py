"""Unit tests for vmvpn_common (no Gtk). Run: python3 -m unittest discover -s gui/tests -v"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

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
        for name in (
            "VMVPN_CLI",
            "STUB_DIR",
            "XDG_STATE_HOME",
            "XDG_CONFIG_HOME",
        ):
            self._saved_env[name] = os.environ.get(name)
        os.environ["VMVPN_CLI"] = self.stub.path
        os.environ["STUB_DIR"] = self.stub.dir
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
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

    def test_run_cli_no_cli_found_reports_error(self):
        result = {}
        with unittest.mock.patch.object(vc, "find_cli", return_value=None):
            vc.run_cli(
                ["status", "--json"],
                lambda rc, out, err: result.update(
                    rc=rc, out=out, err=err
                ),
            )
        drive(lambda: "rc" in result)
        self.assertEqual(result["rc"], vc.EXIT_ERROR)
        self.assertEqual(result["out"], "")
        self.assertIn("CLI not found", result["err"])

    def test_log_false_writes_nothing(self):
        self.stub.add_step(rc=0, out='{"schema":1}\n')
        result = {}
        vc.run_cli(
            ["status", "--json"],
            lambda rc, out, err: result.update(
                rc=rc, out=out, err=err
            ),
            log=False,
        )
        drive(lambda: "rc" in result)
        self.assertEqual(result["rc"], 0)
        self.assertFalse(os.path.exists(vc.gui_log_path()))

    def test_status_poll_logger_logs_once_per_transition(self):
        poll_log = vc.StatusPollLogger()
        poll_log.report(False, "boom\n")
        poll_log.report(False, "boom\n")
        poll_log.report(True)
        poll_log.report(False, "boom2\n")
        with open(vc.gui_log_path(), encoding="utf-8") as f:
            log = f.read()
        self.assertEqual(log.count("status poll failing"), 2)
        self.assertIn("boom", log)
        self.assertIn("boom2", log)


class TestAutostart(FlowTestCase):
    def test_set_autostart_creates_and_removes_desktop_file(self):
        tray = os.path.join(self.tmp, "vmvpn-tray")
        self.assertFalse(vc.autostart_enabled())
        vc.set_autostart(True, tray)
        path = vc.autostart_path()
        self.assertTrue(path.startswith(os.environ["XDG_CONFIG_HOME"]))
        self.assertTrue(vc.autostart_enabled())
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("X-GNOME-Autostart-enabled=true", content)
        vc.set_autostart(False, tray)
        self.assertFalse(vc.autostart_enabled())
        self.assertFalse(os.path.exists(path))

    def test_exec_is_quoted_and_escaped(self):
        tray = os.path.join(self.tmp, 'we`ird "dir"$', "my tray")
        vc.set_autostart(True, tray)
        with open(vc.autostart_path(), encoding="utf-8") as f:
            exec_line = next(
                l for l in f if l.startswith("Exec=")
            ).rstrip("\n")
        expected = 'Exec="%s"' % re.sub(
            r"([" + '"' + "`$\\\\])", r"\\\1", tray
        )
        self.assertEqual(exec_line, expected)
        # no unescaped specials inside the quotes
        inner = exec_line[len('Exec="') : -1]
        self.assertNotRegex(inner, r'(?<!\\)["`$]')


class TestConfigHelpers(FlowTestCase):
    def _cfg_path(self, name="vpn-config.json"):
        return os.path.join(self.tmp, name)

    def test_load_missing_returns_defaults_without_password(self):
        cfg = vc.load_config(self._cfg_path("missing.json"))
        self.assertIsInstance(cfg, dict)
        example_path = os.path.join(
            os.path.dirname(vc.__file__), "..", "vpn-config.json.example"
        )
        with open(example_path, encoding="utf-8") as f:
            example = json.load(f)
        example.pop("password", None)
        self.assertEqual(cfg, example)
        self.assertNotIn("password", cfg)

    def test_load_invalid_returns_defaults(self):
        path = self._cfg_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg = vc.load_config(path)
        self.assertIn("gateway", cfg)

    def test_save_roundtrip_preserves_unknown_keys_and_order(self):
        path = self._cfg_path()
        original = {
            "gateway": "g.example.com",
            "port": 443,
            "username": "u",
            "custom_extra": {"nested": 1},
            "zzz_last": True,
        }
        vc.save_config(path, original)
        cfg = vc.load_config(path)
        cfg["username"] = "changed"
        vc.save_config(path, cfg)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
        self.assertEqual(data["custom_extra"], {"nested": 1})
        self.assertEqual(data["username"], "changed")
        self.assertEqual(list(data), list(original))
        # 2-space indentation and trailing newline
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "gateway"', text)

    def test_save_mode_0600_and_atomic_replace(self):
        path = self._cfg_path()
        vc.save_config(path, {"a": 1})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        # rewriting replaces the file; no temp files left behind
        vc.save_config(path, {"a": 2})
        self.assertEqual(vc.load_config(path), {"a": 2})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        leftovers = [
            n for n in os.listdir(self.tmp) if n.startswith(".vpn-config-")
        ]
        self.assertEqual(leftovers, [])

    def test_key_removal_via_load_save_roundtrip(self):
        path = self._cfg_path()
        vc.save_config(path, {"gateway": "g", "password": "p", "x": 1})
        cfg = vc.load_config(path)
        del cfg["password"]
        vc.save_config(path, cfg)
        self.assertNotIn("password", vc.load_config(path))


class TestVersion(FlowTestCase):
    def _cli_path(self):
        return os.path.realpath(
            os.path.join(os.path.dirname(vc.__file__), "..", "vmvpn")
        )

    def test_gui_version_matches_cli(self):
        with open(self._cli_path(), encoding="utf-8") as f:
            match = re.search(r'^VMVPN_VERSION="([^"]+)"', f.read(),
                              re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), vc.VERSION)

    def test_cli_version_flag(self):
        out = subprocess.run(
            [self._cli_path(), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "vmvpn %s" % vc.VERSION)


if __name__ == "__main__":
    unittest.main()
