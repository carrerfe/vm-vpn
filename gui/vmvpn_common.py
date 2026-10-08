"""Shared, Gtk-free helpers for the vmvpn GUI processes.

Only imports GLib/Gio so it can be reused by both the GTK3 tray and the
GTK4 window process.
"""

import json
import os
import re
import shutil
import time

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# Exit codes of the vmvpn CLI (see `vmvpn` usage / README).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_FAILED = 2
EXIT_CERT_UNTRUSTED = 3
EXIT_PASSWORD_REQUIRED = 4
EXIT_CONFIG_INVALID = 5

_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_KEEP_BYTES = 1024 * 1024

_CERT_UNTRUSTED_RE = re.compile(
    r"^VMVPN_CERT_UNTRUSTED\s+new=(\S*)\s+saved=(\S*)\s*$", re.MULTILINE
)


def find_cli():
    """Locate the vmvpn CLI: $VMVPN_CLI, then the sibling script, then PATH."""
    override = os.environ.get("VMVPN_CLI")
    if override:
        return override
    sibling = os.path.realpath(
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "vmvpn")
    )
    if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        return sibling
    return shutil.which("vmvpn")


def gui_log_path():
    """Path of the shared GUI log; the parent directory is created."""
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    log_dir = os.path.join(state_home, "vmvpn")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "gui.log")


def _append_log(text):
    try:
        path = gui_log_path()
        if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-_LOG_KEEP_BYTES, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"--- log truncated ---\n")
                f.write(tail)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass


def _log_result(rc, stdout, stderr):
    buf = ""
    if stdout:
        buf += stdout
        if not stdout.endswith("\n"):
            buf += "\n"
    if stderr:
        buf += "[stderr]\n" + stderr
        if not stderr.endswith("\n"):
            buf += "\n"
    buf += "[%s] exit %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), rc)
    _append_log(buf)


def run_cli(args, callback, stdin_text=""):
    """Run `vmvpn <args>` asynchronously.

    callback(rc, stdout, stderr) is invoked on the main loop. Stdin is always
    piped (closed when stdin_text is empty) so the CLI never sees a tty.
    Every invocation is appended to the GUI log - except stdin_text, which may
    carry the VPN password and is never logged.
    """
    argv = [find_cli(), *args]
    _append_log(
        "[%s] $ %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv))
    )

    try:
        proc = Gio.Subprocess.new(
            argv,
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as exc:
        _log_result(1, "", "spawn failed: %s" % exc.message)
        callback(EXIT_ERROR, "", "spawn failed: %s" % exc.message)
        return None

    def _done(proc, result):
        try:
            _ok, stdout, stderr = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            _log_result(EXIT_ERROR, "", "communicate failed: %s" % exc.message)
            callback(EXIT_ERROR, "", "communicate failed: %s" % exc.message)
            return
        stdout = stdout or ""
        stderr = stderr or ""
        rc = proc.get_exit_status() if proc.get_if_exited() else EXIT_ERROR
        if proc.get_if_signaled():
            stderr += "\n[killed by signal %d]" % proc.get_term_sig()
        _log_result(rc, stdout, stderr)
        callback(rc, stdout, stderr)

    proc.communicate_utf8_async(stdin_text if stdin_text else None, None, _done)
    return proc


def parse_status(text):
    """Parse `vmvpn status --json` output; returns a dict or None."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_cert_untrusted(stdout):
    """Extract (new_fp, saved_fp_or_None) from VMVPN_CERT_UNTRUSTED output."""
    match = _CERT_UNTRUSTED_RE.search(stdout or "")
    if not match:
        return None
    return match.group(1), (match.group(2) or None)


class ConnectFlow:
    """GTK-agnostic state machine driving `vmvpn vpn-connect`.

    ask_password(error_message_or_None, cb): cb(password_or_None, remember)
    ask_trust(new_fp, saved_fp_or_None, cb): cb(trusted_bool)
    on_done(rc, stdout, stderr): called once when the flow terminates.
    """

    MAX_PASSWORD_PROMPTS = 3
    MAX_TRUST_PROMPTS = 1

    def __init__(self, ask_password, ask_trust, on_done, password=None):
        self._ask_password = ask_password
        self._ask_trust = ask_trust
        self._on_done = on_done
        self._password = password
        self._remember = False
        self._trust_fp = None
        self._pending_new_fp = None
        self._pw_prompts = 0
        self._trust_prompts = 0
        self._last = (EXIT_ERROR, "", "")
        self._proc = None
        self._cancelled = False
        self._finished = False

    def start(self):
        self._run()

    def cancel(self):
        self._cancelled = True
        if self._proc is not None:
            try:
                self._proc.force_exit()
            except Exception:
                pass

    # -- internals ---------------------------------------------------------

    def _run(self):
        args = ["vpn-connect"]
        if self._password is not None:
            args.append("--password-stdin")
        if self._trust_fp is not None:
            args += ["--trust-fingerprint", self._trust_fp]
        stdin_text = self._password + "\n" if self._password is not None else ""
        self._proc = run_cli(args, self._on_connect_done, stdin_text=stdin_text)

    def _on_connect_done(self, rc, stdout, stderr):
        self._proc = None
        if self._cancelled or self._finished:
            return
        self._last = (rc, stdout, stderr)

        if rc in (EXIT_PASSWORD_REQUIRED, EXIT_LOGIN_FAILED):
            if self._pw_prompts >= self.MAX_PASSWORD_PROMPTS:
                self._finish(rc, stdout, stderr)
                return
            self._pw_prompts += 1
            message = (
                "Login failed — check your password"
                if rc == EXIT_LOGIN_FAILED
                else None
            )
            self._ask_password(message, self._on_password)
            return

        if rc == EXIT_CERT_UNTRUSTED:
            parsed = parse_cert_untrusted(stdout)
            if parsed is None or self._trust_prompts >= self.MAX_TRUST_PROMPTS:
                self._finish(rc, stdout, stderr)
                return
            self._trust_prompts += 1
            self._pending_new_fp, saved_fp = parsed
            self._ask_trust(self._pending_new_fp, saved_fp, self._on_trust)
            return

        if rc == EXIT_OK and self._remember and self._password:
            self._save_password()
            return

        self._finish(rc, stdout, stderr)

    def _on_password(self, password, remember):
        if self._cancelled or self._finished:
            return
        if password is None:
            self._finish(*self._last)
            return
        self._password = password
        self._remember = bool(remember)
        self._run()

    def _on_trust(self, trusted):
        if self._cancelled or self._finished:
            return
        if not trusted:
            self._finish(*self._last)
            return
        self._trust_fp = self._pending_new_fp
        self._run()

    def _save_password(self):
        rc, stdout, stderr = self._last
        password = self._password

        def _saved(_rc2, _out2, _err2):
            if self._cancelled or self._finished:
                return
            self._finish(rc, stdout, stderr)

        self._proc = run_cli(
            ["password-set", "--stdin"], _saved, stdin_text=password + "\n"
        )

    def _finish(self, rc, stdout, stderr):
        if self._finished:
            return
        self._finished = True
        self._password = None  # drop the credential reference
        self._on_done(rc, stdout, stderr)
