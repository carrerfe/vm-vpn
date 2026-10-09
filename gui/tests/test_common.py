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
        self.warnings = []

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
            on_warning=self.warnings.append,
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

    def test_remember_saves_on_generic_failure(self):
        """rc=1 (not login failure) still saves a 'remembered' password."""
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=1, err="VPN connection failed\n")
        self.stub.add_step(rc=0)  # password-set
        self.run_flow(ask_password=lambda msg, cb: cb("pw", True))
        self.assertEqual(self.done[0], 1)
        self.assertEqual(self.stub.calls()[2], "password-set --stdin")

    def test_remember_skipped_on_login_failed(self):
        """rc=2 never stores the (bad) password."""
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=2, out="Login failed\n")
        asks = iter([
            ("pw", True),
            (None, False),  # user cancels the re-prompt
        ])

        def ask(msg, cb):
            pw, remember = next(asks)
            cb(pw, remember)

        self.run_flow(ask_password=ask)
        self.assertEqual(self.done[0], 2)
        self.assertNotIn("password-set", " ".join(self.stub.calls()))

    def test_password_set_failure_calls_on_warning(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(rc=0)  # connect OK
        self.stub.add_step(rc=1, err="keyring is locked\n")
        self.run_flow(ask_password=lambda msg, cb: cb("pw", True))
        self.assertEqual(self.done[0], 0)
        self.assertEqual(self.warnings, ["keyring is locked"])

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

    def test_run_cli_on_line_streams_and_collects(self):
        """on_line gets each line live; callback still gets full output."""
        self.stub.add_step(
            rc=0,
            out="==> Creating the VM…\ndetail one\ndetail two\n",
            err="warn line\n",
        )
        lines = []
        result = {}
        vc.run_cli(
            ["start"],
            lambda rc, out, err: result.update(rc=rc, out=out, err=err),
            on_line=lines.append,
        )
        drive(lambda: "rc" in result)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(
            result["out"], "==> Creating the VM…\ndetail one\ndetail two\n"
        )
        self.assertEqual(result["err"], "warn line\n")
        self.assertIn("==> Creating the VM…", lines)
        self.assertIn("warn line", lines)
        self.assertEqual(len(lines), 4)

    def test_connect_flow_passes_on_line(self):
        self.stub.add_step(rc=4)
        self.stub.add_step(
            rc=0, out="==> Connecting to g:443…\n==> Connected.\n"
        )
        lines = []
        flow = vc.ConnectFlow(
            lambda msg, cb: cb("pw", False),
            lambda n, s, cb: cb(False),
            self._on_done,
            on_line=lines.append,
        )
        flow.start()
        drive(lambda: self.done is not None)
        self.assertEqual(self.done[0], 0)
        self.assertIn("==> Connecting to g:443…", lines)
        self.assertIn("==> Connected.", lines)

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


class TestUiHelpers(FlowTestCase):
    """UI prefs/autostart/processes are owned by the CLI — the GUI side is
    only thin wrappers over `vmvpn ui …` and `vmvpn vm-autostart …`."""

    def _run(self, fn, *args):
        self.done = None
        fn(*args, self._on_done)
        drive(lambda: self.done is not None)

    def test_set_tray_enabled_calls_cli(self):
        self._run(vc.set_tray_enabled, True)
        self.assertEqual(self.stub.calls(), ["ui tray on"])
        self.stub.add_step()
        self._run(vc.set_tray_enabled, False)
        self.assertEqual(self.stub.calls(), ["ui tray on", "ui tray off"])

    def test_set_setup_prompt_calls_cli(self):
        self._run(vc.set_setup_prompt, False)
        self.assertEqual(self.stub.calls(), ["ui setup-prompt off"])

    def test_set_vm_autostart_calls_cli(self):
        self._run(vc.set_vm_autostart, True)
        self.assertEqual(self.stub.calls(), ["vm-autostart on"])

    def test_open_ui_calls_cli_with_page(self):
        self._run(vc.open_ui, "settings")
        self.assertEqual(self.stub.calls(), ["ui settings"])

    def test_ui_state_reads_status_json(self):
        self.stub.add_step(out=json.dumps({
            "ui": {"tray_enabled": False, "tray_running": True,
                   "window_running": False, "setup_prompt": True},
        }))
        ui = vc.ui_state()
        self.assertFalse(ui["tray_enabled"])
        self.assertTrue(ui["tray_running"])
        self.assertEqual(self.stub.calls(), ["status --json"])

    def test_ui_state_empty_on_failure(self):
        self.stub.add_step(rc=1, err="boom")
        self.assertEqual(vc.ui_state(), {})

    def test_release_line(self):
        self.stub.add_step(out="vmvpn 1.1.0 (abc123, main, installed x)\n")
        self.assertEqual(
            vc.release_line(), "vmvpn 1.1.0 (abc123, main, installed x)"
        )
        self.assertEqual(self.stub.calls(), ["--version"])

    def test_release_line_fallback_on_failure(self):
        self.stub.add_step(rc=1)
        self.assertEqual(vc.release_line(), "vmvpn %s" % vc.VERSION)


class TestCliUiPrefs(unittest.TestCase):
    """ui.json semantics, exercised through the real CLI (bash)."""

    def _cli_path(self):
        return os.path.realpath(
            os.path.join(os.path.dirname(vc.__file__), "..", "vmvpn")
        )

    def _run_cli(self, tmp, *args):
        env = dict(
            os.environ,
            XDG_CONFIG_HOME=os.path.join(tmp, "config"),
            XDG_STATE_HOME=os.path.join(tmp, "state"),
            XDG_DATA_HOME=os.path.join(tmp, "data"),
            VMVPN_VM_NAME="vmvpn-unittest-nonexistent",
            VPN_CONFIG=os.path.join(tmp, "vpn-config.json"),
            DISPLAY="",
            WAYLAND_DISPLAY="",
        )
        return subprocess.run(
            [self._cli_path(), *args],
            capture_output=True, text=True, timeout=30, env=env,
            stdin=subprocess.DEVNULL,
        )

    def _prefs(self, tmp):
        path = os.path.join(tmp, "config", "vmvpn", "ui.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_cli(tmp, "ui", "tray")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("enabled", out.stdout)
            out = self._run_cli(tmp, "ui", "setup-prompt")
            self.assertIn("enabled", out.stdout)

    def test_tray_off_writes_pref_and_removes_autostart(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_cli(tmp, "ui", "tray", "on")
            autostart = os.path.join(
                tmp, "config", "autostart", "vmvpn-tray.desktop"
            )
            with open(autostart, encoding="utf-8") as f:
                content = f.read()
            self.assertIn('ui --login', content)
            self.assertIn('"', content)  # Exec path is quoted
            out = self._run_cli(tmp, "ui", "tray", "off")
            self.assertIn("disabled", out.stdout)
            self.assertFalse(os.path.exists(autostart))
            self.assertEqual(self._prefs(tmp), {"tray": False})

    def test_unknown_keys_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_dir = os.path.join(tmp, "config", "vmvpn")
            os.makedirs(prefs_dir)
            with open(os.path.join(prefs_dir, "ui.json"), "w") as f:
                json.dump({"custom_key": {"x": 1}, "tray": True}, f)
            self._run_cli(tmp, "ui", "setup-prompt", "off")
            prefs = self._prefs(tmp)
            self.assertEqual(prefs["custom_key"], {"x": 1})
            self.assertFalse(prefs["setup_prompt"])
            self.assertTrue(prefs["tray"])

    def _fake_install(self, tmp):
        """A fake installed layout: <tmp>/install/vmvpn + vmvpn-gui/."""
        inst = os.path.join(tmp, "install")
        os.makedirs(os.path.join(inst, "vmvpn-gui"), exist_ok=True)
        cli = os.path.join(inst, "vmvpn")
        with open(self._cli_path(), "rb") as f:
            data = f.read()
        with open(cli, "wb") as f:
            f.write(data)
        os.chmod(cli, 0o755)
        return cli

    def _run_installed(self, tmp, *args):
        env = dict(
            os.environ,
            XDG_CONFIG_HOME=os.path.join(tmp, "config"),
            XDG_STATE_HOME=os.path.join(tmp, "state"),
            XDG_DATA_HOME=os.path.join(tmp, "data"),
            VMVPN_VM_NAME="vmvpn-unittest-nonexistent",
            VPN_CONFIG=os.path.join(tmp, "vpn-config.json"),
            DISPLAY="",
            WAYLAND_DISPLAY="",
        )
        return subprocess.run(
            [self._fake_install(tmp), *args],
            capture_output=True, text=True, timeout=30, env=env,
            stdin=subprocess.DEVNULL,
        )

    def test_old_autostart_entry_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst_cli = self._fake_install(tmp)
            auto_dir = os.path.join(tmp, "config", "autostart")
            os.makedirs(auto_dir)
            entry = os.path.join(auto_dir, "vmvpn-tray.desktop")
            with open(entry, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\nType=Application\n"
                        'Exec="%s/vmvpn-gui/vmvpn-tray"\n'
                        % os.path.dirname(inst_cli))
            out = self._run_installed(tmp, "ui", "tray")
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(entry, encoding="utf-8") as f:
                content = f.read()
            self.assertIn('Exec="%s" ui --login' % inst_cli, content)

    def test_autostart_entry_pointing_elsewhere_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fake_install(tmp)
            auto_dir = os.path.join(tmp, "config", "autostart")
            os.makedirs(auto_dir)
            entry = os.path.join(auto_dir, "vmvpn-tray.desktop")
            for exec_line in (
                'Exec="/other/install/vmvpn" ui --login',
                'Exec="/other/install/vmvpn-gui/vmvpn-tray"',
            ):
                with open(entry, "w", encoding="utf-8") as f:
                    f.write("[Desktop Entry]\nType=Application\n"
                            + exec_line + "\n")
                out = self._run_installed(tmp, "ui", "tray")
                self.assertEqual(out.returncode, 0, out.stderr)
                with open(entry, encoding="utf-8") as f:
                    self.assertIn(exec_line, f.read())

    def test_checkout_never_touches_autostart(self):
        """A checkout's `ui`/`ui --login`/`ui tray` must not write the
        login autostart entry (that's the installed layout's job)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_cli(tmp, "ui", "tray")   # default pref: on
            self.assertEqual(out.returncode, 0, out.stderr)
            out = self._run_cli(tmp, "ui", "--login")
            self.assertEqual(out.returncode, 0, out.stderr)
            out = self._run_cli(tmp, "ui")           # no display -> rc 1
            self.assertEqual(out.returncode, 1)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "config", "autostart",
                             "vmvpn-tray.desktop")))

    def test_ui_without_display_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_cli(tmp, "ui")
            self.assertEqual(out.returncode, 1)
            self.assertIn("graphical session", out.stderr)


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
        # The vm block is not part of the defaults: unmanaged keys stay absent.
        example.pop("vm", None)
        self.assertEqual(cfg, example)
        self.assertNotIn("password", cfg)
        self.assertNotIn("vm", cfg)

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

    def test_update_vm_config_untouched_rows_stay_absent(self):
        cfg = {"gateway": "g.example.com"}
        values = {"cpus": 2, "memory_mib": 512, "disk_gib": 20,
                  "swap_mib": 0, "swappiness": None}
        written = vc.update_vm_config(cfg, values, set())
        self.assertEqual(written, set())
        self.assertNotIn("vm", cfg)

    def test_update_vm_config_writes_only_managed_or_touched(self):
        cfg = {"vm": {"cpus": 2, "swap_mib": 1024}}
        values = {"cpus": 4, "memory_mib": 768, "disk_gib": 20,
                  "swap_mib": 0, "swappiness": None}
        written = vc.update_vm_config(cfg, values, {"memory_mib"})
        self.assertEqual(written, {"cpus", "swap_mib", "memory_mib"})
        self.assertEqual(cfg["vm"]["cpus"], 4)
        self.assertEqual(cfg["vm"]["swap_mib"], 0)
        self.assertEqual(cfg["vm"]["memory_mib"], 768)
        self.assertNotIn("disk_gib", cfg["vm"])
        self.assertNotIn("swappiness", cfg["vm"])

    def test_update_vm_config_null_value_stays_unmanaged(self):
        cfg = {"vm": {"swappiness": None}}
        written = vc.update_vm_config(cfg, {"swappiness": 100}, set())
        self.assertEqual(written, set())
        self.assertIsNone(cfg["vm"]["swappiness"])

    def test_update_vm_config_touched_swappiness_off_writes_null(self):
        cfg = {"vm": {"swappiness": 100}}
        vc.update_vm_config(cfg, {"swappiness": None}, {"swappiness"})
        self.assertIn("swappiness", cfg["vm"])
        self.assertIsNone(cfg["vm"]["swappiness"])

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

    def test_save_preserves_existing_file_mode(self):
        path = self._cfg_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(path, 0o640)
        vc.save_config(path, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


class TestPlaceholders(unittest.TestCase):
    """Example values in vpn-config.json.example mean "not set up"."""

    def test_placeholder_values_detected(self):
        self.assertTrue(vc.is_placeholder("vpn.example.com"))
        self.assertTrue(vc.is_placeholder("your-username"))

    def test_real_values_not_placeholders(self):
        for v in ("gw.corp.example", "jdoe", "", None, "vpn.example.org"):
            self.assertFalse(vc.is_placeholder(v), "value %r" % v)

    def _cli_path(self):
        return os.path.realpath(
            os.path.join(os.path.dirname(vc.__file__), "..", "vmvpn")
        )

    def _placeholder_config(self, tmp):
        path = os.path.join(tmp, "vpn-config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"gateway": "vpn.example.com", "port": 443,
                 "username": "your-username"},
                f,
            )
        return path

    def test_cli_status_json_marks_placeholder_config_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._placeholder_config(tmp)
            env = dict(
                os.environ,
                VPN_CONFIG=cfg,
                VMVPN_VM_NAME="vmvpn-unittest-nonexistent",
            )
            out = subprocess.run(
                [self._cli_path(), "status", "--json"],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            status = json.loads(out.stdout)
            self.assertTrue(status["config"]["exists"])
            self.assertFalse(status["config"]["valid"])
            self.assertEqual(
                status["config"]["reason"], "placeholder values"
            )

    def test_cli_rejects_placeholder_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._placeholder_config(tmp)
            env = dict(
                os.environ,
                VPN_CONFIG=cfg,
                VMVPN_VM_NAME="vmvpn-unittest-nonexistent",
            )
            out = subprocess.run(
                [self._cli_path(), "vpn-status"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL, env=env,
            )
            self.assertEqual(out.returncode, 5)
            self.assertIn("placeholder", out.stderr)
            self.assertIn("vmvpn setup", out.stderr)


class TestHostPortParsing(unittest.TestCase):
    def test_plain_host(self):
        self.assertEqual(vc.parse_host_port("vpn.example.com"),
                         ("vpn.example.com", 443))

    def test_host_port(self):
        self.assertEqual(vc.parse_host_port("vpn.example.com:8443"),
                         ("vpn.example.com", 8443))

    def test_url(self):
        self.assertEqual(
            vc.parse_host_port("https://vpn.example.com:10443/sslvpn"),
            ("vpn.example.com", 10443),
        )
        self.assertEqual(
            vc.parse_host_port("https://vpn.example.com/path"),
            ("vpn.example.com", 443),
        )

    def test_custom_default_port(self):
        self.assertEqual(vc.parse_host_port("h", 1234), ("h", 1234))

    def test_invalid(self):
        for bad in ("", "   ", "host name", None):
            self.assertEqual(
                vc.parse_host_port(bad)[0], None, "input %r" % bad
            )


class TestFindFreePort(unittest.TestCase):
    def test_free_port_returned(self):
        port = vc.find_free_port(39000)
        self.assertGreaterEqual(port, 39000)

    def test_skips_busy_port(self):
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        try:
            port = vc.find_free_port(busy)
            self.assertNotEqual(port, busy)
        finally:
            s.close()


class TestGuiExecutables(unittest.TestCase):
    """vmvpn-tray / vmvpn-window are internal components: --help works with
    no display, unknown options are rejected."""

    GUI = os.path.dirname(vc.__file__)

    def _run(self, script, *args):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        return subprocess.run(
            ["python3", os.path.join(self.GUI, script), *args],
            capture_output=True, text=True, timeout=15, env=env,
            stdin=subprocess.DEVNULL,
        )

    def test_help(self):
        for script in ("vmvpn-tray", "vmvpn-window"):
            out = self._run(script, "--help")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("vmvpn ui", out.stdout)
            self.assertIn("internal component", out.stdout)

    def test_unknown_option_rejected(self):
        for script in ("vmvpn-tray", "vmvpn-window"):
            out = self._run(script, "--bogus")
            self.assertEqual(out.returncode, 2)
            self.assertIn("vmvpn ui", out.stderr)


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
        # release metadata may follow in parentheses; the prefix is fixed.
        self.assertTrue(
            out.stdout.startswith("vmvpn %s" % vc.VERSION),
            "unexpected --version output: %r" % out.stdout,
        )

    def test_version_at_least_latest_tag(self):
        """VMVPN_VERSION must be >= the latest reachable v* tag."""
        repo = os.path.dirname(os.path.dirname(self._cli_path()))
        is_wt = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        if is_wt.returncode != 0:
            self.skipTest("not a git checkout")
        tags = subprocess.run(
            ["git", "-C", repo, "tag", "--merged", "HEAD",
             "--list", "v[0-9]*", "--sort=-version:refname"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        if not tags:
            self.skipTest("no v* tags reachable")
        latest = tuple(int(x) for x in tags[0].lstrip("v").split("."))
        current = tuple(int(x) for x in vc.VERSION.split("."))
        self.assertGreaterEqual(
            current, latest,
            "VERSION %s is older than tag %s" % (vc.VERSION, tags[0]),
        )


if __name__ == "__main__":
    unittest.main()
