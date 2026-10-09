"""Shared, Gtk-free helpers for the vmvpn GUI processes.

Only imports GLib/Gio so it can be reused by both the GTK3 tray and the
GTK4 window process.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

VERSION = "1.1.0"

# Exit codes of the vmvpn CLI (see `vmvpn` usage / README).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_FAILED = 2
EXIT_CERT_UNTRUSTED = 3
EXIT_PASSWORD_REQUIRED = 4
EXIT_CONFIG_INVALID = 5
EXIT_BUSY = 6

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


def log_line(text):
    """Append a single timestamped line to the GUI log."""
    _append_log("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))


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


def run_cli(args, callback, stdin_text="", log=True, on_line=None):
    """Run `vmvpn <args>` asynchronously.

    callback(rc, stdout, stderr) is invoked on the main loop. Stdin is always
    piped (closed when stdin_text is empty) so the CLI never sees a tty.
    Every invocation is appended to the GUI log when log=True - except
    stdin_text, which may carry the VPN password and is never logged.
    High-frequency callers (status polls) pass log=False and report failures
    through StatusPollLogger instead.

    When on_line is given, stdout and stderr are read line by line as they
    arrive and each decoded line is passed to on_line(line); the callback
    still receives the full output at the end.
    """
    cli = find_cli()
    if cli is None:
        _append_log(
            "[%s] $ vmvpn %s\nvmvpn CLI not found\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(args))
        )

        def _deliver():
            callback(EXIT_ERROR, "", "vmvpn CLI not found")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_deliver)
        return None

    argv = [cli, *args]
    if log:
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
        if log:
            _log_result(1, "", "spawn failed: %s" % exc.message)
        callback(EXIT_ERROR, "", "spawn failed: %s" % exc.message)
        return None

    if on_line is not None:
        return _run_cli_streaming(proc, argv, callback, stdin_text, log, on_line)

    def _done(proc, result):
        try:
            _ok, stdout, stderr = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            if log:
                _log_result(EXIT_ERROR, "", "communicate failed: %s" % exc.message)
            callback(EXIT_ERROR, "", "communicate failed: %s" % exc.message)
            return
        stdout = stdout or ""
        stderr = stderr or ""
        rc = proc.get_exit_status() if proc.get_if_exited() else EXIT_ERROR
        if proc.get_if_signaled():
            stderr += "\n[killed by signal %d]" % proc.get_term_sig()
        if log:
            _log_result(rc, stdout, stderr)
        callback(rc, stdout, stderr)

    proc.communicate_utf8_async(stdin_text if stdin_text else None, None, _done)
    return proc


def _run_cli_streaming(proc, argv, callback, stdin_text, log, on_line):
    """Line-streaming variant of run_cli's tail (proc already spawned).

    Reads both pipes with Gio.DataInputStream so each line reaches on_line
    without blocking the main loop; wait_async + EOF counters trigger the
    final callback with the complete stdout/stderr.
    """
    out_lines = []
    err_lines = []
    state = {"waited": False, "eof": 0}

    def _feed(stream, buf):
        dis = Gio.DataInputStream.new(stream)

        def _read():
            dis.read_line_async(GLib.PRIORITY_DEFAULT, None, _on_line)

        def _on_line(source, res):
            try:
                line, _length = source.read_line_finish_utf8(res)
            except GLib.Error:
                line = None
            if line is None:
                state["eof"] += 1
                _maybe_done()
                return
            buf.append(line)
            try:
                on_line(line)
            except Exception:
                pass
            _read()

        _read()

    def _maybe_done():
        if not state["waited"] or state["eof"] < 2:
            return
        stdout = "".join(l + "\n" for l in out_lines)
        stderr = "".join(l + "\n" for l in err_lines)
        rc = proc.get_exit_status() if proc.get_if_exited() else EXIT_ERROR
        if proc.get_if_signaled():
            stderr += "[killed by signal %d]" % proc.get_term_sig()
        if log:
            _log_result(rc, stdout, stderr)
        callback(rc, stdout, stderr)

    def _on_wait(_proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        state["waited"] = True
        _maybe_done()

    _feed(proc.get_stdout_pipe(), out_lines)
    _feed(proc.get_stderr_pipe(), err_lines)

    istream = proc.get_stdin_pipe()
    try:
        if stdin_text:
            istream.write_all(stdin_text.encode("utf-8"))
        istream.close()
    except GLib.Error:
        pass

    proc.wait_async(None, _on_wait)
    return proc


class StatusPollLogger:
    """Log a status-poll failure once per ok -> failing transition.

    Status polls run with log=False so they do not spam gui.log. Feed every
    poll result to report(); a failure is logged once (with stderr) until a
    poll succeeds again.
    """

    def __init__(self):
        self._failing = False

    def report(self, ok, stderr=""):
        if ok:
            self._failing = False
            return
        if self._failing:
            return
        self._failing = True
        detail = (stderr or "").strip() or "no error output"
        _append_log(
            "[%s] status poll failing: %s\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), detail)
        )


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


# -- UI management -----------------------------------------------------------
#
# The vmvpn CLI owns every piece of UI state: the ui.json preferences, the
# autostart desktop entries and starting/stopping the tray and window
# processes. The helpers below are thin wrappers — nothing here writes
# desktop entries or prefs files directly.


def _cli_call(*args, timeout=15):
    """Run `vmvpn <args>` synchronously; returns (rc, stdout, stderr)."""
    cli = find_cli()
    if cli is None:
        return EXIT_ERROR, "", "vmvpn CLI not found"
    try:
        proc = subprocess.run(
            [cli, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EXIT_ERROR, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def status_json():
    """Parsed `vmvpn status --json` dict, or None on failure."""
    rc, out, _err = _cli_call("status", "--json")
    if rc != 0:
        return None
    return parse_status(out)


def ui_state():
    """The `ui` block of `status --json` (tray_enabled, running flags…)."""
    status = status_json() or {}
    ui = status.get("ui")
    return ui if isinstance(ui, dict) else {}


def release_line():
    """`vmvpn --version` output ('vmvpn X.Y.Z (commit, ref, …)')."""
    rc, out, _err = _cli_call("--version")
    if rc == 0 and out.strip():
        return out.strip()
    return "vmvpn %s" % VERSION


def _noop(_rc, _out, _err):
    pass


def set_tray_enabled(enabled, callback=None):
    """Ask the CLI to enable/disable the tray (pref + autostart + process)."""
    return run_cli(
        ["ui", "tray", "on" if enabled else "off"], callback or _noop
    )


def set_setup_prompt(enabled, callback=None):
    """Ask the CLI to toggle the 'propose setup at login' preference."""
    return run_cli(
        ["ui", "setup-prompt", "on" if enabled else "off"],
        callback or _noop,
    )


def set_vm_autostart(enabled, callback=None):
    """Ask the CLI to enable/disable VM start at login."""
    return run_cli(
        ["vm-autostart", "on" if enabled else "off"], callback or _noop
    )


def open_ui(page, callback=None):
    """Ask the CLI to open/present the app window on `page`."""
    return run_cli(["ui", page], callback or _noop)


# -- config ------------------------------------------------------------------

# Example values shipped in vpn-config.json.example. A config still
# carrying them counts as "not set up" (the CLI agrees: `status --json`
# reports config.valid=false with reason "placeholder values").
PLACEHOLDER_GATEWAY = "vpn.example.com"
PLACEHOLDER_USERNAME = "your-username"


def is_placeholder(value):
    """True when *value* is an example placeholder that means "unset"."""
    return value in (PLACEHOLDER_GATEWAY, PLACEHOLDER_USERNAME)


def config_path():
    """Path of the vpn-config.json the CLI uses (VPN_CONFIG, else beside the
    CLI script)."""
    env = os.environ.get("VPN_CONFIG")
    if env:
        return env
    cli = find_cli()
    if cli:
        return os.path.join(os.path.dirname(os.path.realpath(cli)),
                            "vpn-config.json")
    return os.path.join(os.getcwd(), "vpn-config.json")


def secret_tool_available():
    return shutil.which("secret-tool") is not None


_CONFIG_DEFAULTS = {
    "gateway": "vpn.example.com",
    "port": 443,
    "username": "your-username",
    "socks_proxy": {
        "enabled": True,
        "port": 1080,
        "auto_start": True,
        "auto_stop": True,
    },
    "http_proxy": {
        "enabled": False,
        "port": 3128,
        "auto_start": False,
        "auto_stop": False,
    },
}


def load_config(path):
    """Load the VPN config as a dict (file order preserved).

    A missing or unreadable file returns defaults matching
    vpn-config.json.example (without a password key).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return copy.deepcopy(_CONFIG_DEFAULTS)


def save_config(path, data):
    """Write the VPN config atomically: temp file + os.replace.

    Round-trip through load_config and mutate the returned dict so unknown
    keys and their order in the existing file are preserved. Writes with
    2-space indentation and a trailing newline. Mode: an existing file keeps
    its mode; a new file is created 0600.
    """
    try:
        keep_mode = os.stat(path).st_mode & 0o777
    except OSError:
        keep_mode = 0o600
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".vpn-config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), keep_mode)
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, keep_mode)


def parse_host_port(text, default_port=443):
    """Parse 'host', 'host:port' or 'scheme://host[:port][/path]'.

    Returns (host, port); port falls back to default_port when absent.
    Returns (None, default_port) for empty/invalid input.
    """
    s = (text or "").strip()
    if not s:
        return None, default_port
    s = s.split("://", 1)[-1]      # drop scheme
    s = s.split("/", 1)[0]        # drop path/query base
    s = s.rsplit("@", 1)[-1]      # drop userinfo
    s = s.split("?")[0].split("#")[0]
    port = default_port
    host = s
    if s.count(":") == 1:          # host:port (not IPv6)
        host, _, p = s.partition(":")
        if p.isdigit():
            port = int(p)
        elif p:
            host = s               # non-numeric suffix: keep as host
    host = host.strip()
    if not host or any(c.isspace() for c in host):
        return None, default_port
    return host, port


def find_free_port(start=1080, host="127.0.0.1", limit=100):
    """Return the first TCP port >= start that can be bound on `host`."""
    import socket

    for port in range(start, start + limit):
        s = socket.socket()
        try:
            s.bind((host, port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    raise RuntimeError("no free port in range %d-%d" % (start, start + limit))


# VM keys the CLI understands inside the optional "vm" config block.
VM_CONFIG_KEYS = ("cpus", "memory_mib", "disk_gib", "swap_mib", "swappiness")


def update_vm_config(cfg, values, touched):
    """Merge GUI VM settings into cfg's "vm" block (managed-key semantics).

    A setting the user never set is unmanaged: a vm key is written only when
    it is already managed (present and non-None in the existing "vm" block)
    or it is in `touched` (the user edited that row). `values` maps key ->
    new value; None writes null (explicitly unmanaged, e.g. swappiness off).
    Returns the set of keys written.
    """
    vm = cfg.get("vm")
    if not isinstance(vm, dict):
        vm = None
    written = set()
    for key in VM_CONFIG_KEYS:
        if key not in values:
            continue
        managed = vm is not None and vm.get(key) is not None
        if managed or key in touched:
            if vm is None:
                vm = {}
                cfg["vm"] = vm
            vm[key] = values[key]
            written.add(key)
    return written


class ConnectFlow:
    """GTK-agnostic state machine driving `vmvpn vpn-connect`.

    ask_password(error_message_or_None, cb): cb(password_or_None, remember)
    ask_trust(new_fp, saved_fp_or_None, cb): cb(trusted_bool)
    on_done(rc, stdout, stderr): called once when the flow terminates.
    on_warning(msg): optional, invoked when saving the password to the
        keyring fails (msg from the CLI's stderr).
    on_line(line): optional, invoked for each output line of vpn-connect
        as it is produced (for progress display).
    """

    MAX_PASSWORD_PROMPTS = 3
    MAX_TRUST_PROMPTS = 1

    def __init__(self, ask_password, ask_trust, on_done, password=None,
                 on_warning=None, on_line=None):
        self._ask_password = ask_password
        self._ask_trust = ask_trust
        self._on_done = on_done
        self._on_warning = on_warning
        self._on_line = on_line
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
        self._proc = run_cli(
            args,
            self._on_connect_done,
            stdin_text=stdin_text,
            on_line=self._on_line,
        )

    def _on_connect_done(self, rc, stdout, stderr):
        self._proc = None
        if self._cancelled or self._finished:
            return
        self._last = (rc, stdout, stderr)

        if rc in (EXIT_PASSWORD_REQUIRED, EXIT_LOGIN_FAILED):
            if self._pw_prompts >= self.MAX_PASSWORD_PROMPTS:
                self._finish_or_save(rc, stdout, stderr)
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
                self._finish_or_save(rc, stdout, stderr)
                return
            self._trust_prompts += 1
            self._pending_new_fp, saved_fp = parsed
            self._ask_trust(self._pending_new_fp, saved_fp, self._on_trust)
            return

        self._finish_or_save(rc, stdout, stderr)

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

    def _finish_or_save(self, rc, stdout, stderr):
        """End the flow, saving the password first when asked to remember it.

        The password is kept unless the connect ended with EXIT_LOGIN_FAILED
        or the user cancelled a prompt (those paths call _finish directly).
        """
        self._last = (rc, stdout, stderr)
        if self._remember and self._password and rc != EXIT_LOGIN_FAILED:
            self._save_password()
        else:
            self._finish(rc, stdout, stderr)

    def _save_password(self):
        rc, stdout, stderr = self._last
        password = self._password

        def _saved(pw_rc, _out2, pw_stderr):
            if self._cancelled or self._finished:
                return
            if pw_rc != EXIT_OK and self._on_warning is not None:
                msg = (pw_stderr or "").strip().splitlines()
                self._on_warning(msg[-1] if msg else "keyring error")
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
