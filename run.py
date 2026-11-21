#!/usr/bin/env python3
"""Zeba Saver Bot - one-command launcher.

    python3 run.py              # install deps (first time), ask for missing settings, start the bot
    python3 run.py --setup      # walk through the whole setup wizard again, then start
    python3 run.py --setup-only # only run the wizard (e.g. before enabling the systemd service)
    python3 run.py --once       # do not auto-restart if the bot stops
    python3 run.py --test-translate  # real test of every translation provider (shows exact errors)

Background (keeps running after you close the terminal, restarts itself if it crashes):
    python3 run.py --background # first run still asks its questions in the terminal, then detaches
    python3 run.py --status     # is it running?
    python3 run.py --stop       # graceful stop (unfinished batches continue on the next start)
Logs: data/logs/zeba.log

Needs only Python 3.10+. Everything else (virtualenv, packages, ffmpeg binary) is handled here.
"""
from __future__ import annotations

import atexit
import hashlib
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQS = ROOT / "requirements.txt"
OPTIONAL = ["cryptg", "tgcrypto-pyrofork", "g4f"]  # cryptg speeds up Telegram transfers, g4f = optional GPT4Free translation; skipped silently if they cannot be installed
NEEDED_MODULES = ("telethon", "pyrogram", "pydantic_settings", "yt_dlp", "twscrape", "aiosqlite", "httpx", "openai", "PIL")


def log(msg: str) -> None:
    print(f"[zeba] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"[zeba] ERROR: {msg}\n")
    sys.exit(code)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def reexec(python: Path, args: list[str]) -> None:
    cmd = [str(python), str(Path(__file__).resolve()), *args]
    if os.name == "nt":  # execv is unreliable on Windows
        sys.exit(subprocess.call(cmd))
    os.execv(str(python), cmd)


def ensure_venv(args: list[str]) -> None:
    """Create ./.venv on first run and continue inside it."""
    if os.environ.get("ZEBA_NO_VENV") or in_venv():
        return
    py = venv_python()
    if not py.exists():
        log("Creating virtual environment (.venv) ...")
        try:
            import venv

            venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
        except Exception as exc:
            shutil.rmtree(VENV, ignore_errors=True)
            die(f"Could not create a virtualenv ({exc}).\n"
                "On Debian/Ubuntu run:  sudo apt install python3-venv python3-pip   then try again.")
    reexec(py, args)


def deps_missing() -> bool:
    return any(importlib.util.find_spec(m) is None for m in NEEDED_MODULES)


def pip(*pip_args: str, quiet: bool = False) -> int:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pip_args]
    if quiet:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.call(cmd)


def ensure_deps() -> None:
    stamp = ROOT / "data" / ".deps-stamp"
    digest = hashlib.sha256(REQS.read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text().strip() == digest and not deps_missing():
        return
    log("Installing Python packages (first run only, takes a minute) ...")
    if pip("-r", str(REQS)) != 0:
        die("Package installation failed. Check your internet connection and run the command again.")
    for pkg in OPTIONAL:
        pip(pkg, quiet=True)
    if deps_missing():
        die("Some packages are still missing after installation - see the pip output above.")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest)
    log("Packages ready.")


def hints() -> None:
    if not (shutil.which("node") or shutil.which("deno") or shutil.which("bun")):
        log("Tip: install Node.js 20+ (or Deno) if YouTube downloads ever fail - yt-dlp uses it to solve YouTube challenges.")


# ------------------------------------------------------------------ background / single instance
def pid_path() -> Path:
    from unified_core.paths import data_dir

    return data_dir() / "zeba.pid"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would send Ctrl+C on Windows
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid() -> int | None:
    """PID of a live Zeba launcher (not this process), else None."""
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid() or not pid_alive(pid):
        return None
    try:  # Linux/Docker reuse small PIDs after a restart: make sure it really is our launcher
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"run.py" not in cmdline:
            return None
    except OSError:
        pass
    return pid


def acquire_pidfile() -> None:
    """Only one instance per data folder: two copies on one Telegram session would kick each other out."""
    other = running_pid()
    if other:
        die(f"Zeba is already running (pid {other}). Stop it first:  python3 run.py --stop")
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    me = str(os.getpid())

    def _release() -> None:
        try:
            if path.read_text().strip() == me:
                path.unlink()
        except OSError:
            pass

    atexit.register(_release)


def cmd_status() -> int:
    pid = running_pid()
    if pid:
        print(f"[zeba] Running (pid {pid}). Log: {pid_path().parent / 'logs' / 'zeba.log'}")
        return 0
    print("[zeba] Not running.")
    return 1


def cmd_stop() -> int:
    pid = running_pid()
    if not pid:
        print("[zeba] Not running.")
        pid_path().unlink(missing_ok=True)
        return 0
    log(f"Stopping (pid {pid}) - waiting for running jobs to finish ...")
    if os.name == "nt":
        subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 90
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.5)
    if pid_alive(pid):
        log("Did not stop in time - killing it.")
        if os.name != "nt":
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    pid_path().unlink(missing_ok=True)
    log("Stopped. Unfinished batches continue automatically on the next start.")
    return 0


def cmd_background() -> int:
    if running_pid():
        return cmd_status()
    # Anything that needs typing (login code, channel, ...) is asked here, in the terminal.
    rc = subprocess.call([sys.executable, "-m", "scripts.ensure"], cwd=ROOT)
    if rc != 0:
        return rc
    logs = pid_path().parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "zeba.out", "ab")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=ROOT, env=env,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kwargs)
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            die(f"The bot exited right away (code {proc.returncode}). See {logs / 'zeba.out'} and zeba.log")
        if running_pid() == proc.pid:
            break
    log(f"Running in the background (pid {proc.pid}). Logs: {logs / 'zeba.log'}")
    log("Check:  python3 run.py --status    Stop:  python3 run.py --stop")
    return 0


class Supervisor:
    def __init__(self, force_setup: bool, restart: bool) -> None:
        self.force_setup = force_setup
        self.restart = restart
        self.proc: subprocess.Popen | None = None
        self.stopping = False

    def _forward(self, signum, _frame) -> None:
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signum)
            except OSError:
                pass

    def _panel(self, code: int) -> int:
        """The bot cannot run (needs attention / keeps crashing). On a host with a public port (Render) exit would take
        the web page down too, so serve the browser setup panel instead: log in again / fix settings from the phone."""
        if self.stopping or not os.environ.get("PORT") or os.environ.get("ZEBA_NO_PANEL_FALLBACK"):
            return code
        log("The bot cannot run right now - keeping the web panel online so you can fix it from the browser.")
        self.proc = subprocess.Popen([sys.executable, "-m", "scripts.web_login"], cwd=ROOT, env=os.environ.copy())
        try:
            self.proc.wait()
        except KeyboardInterrupt:
            self.proc.terminate()
        return code

    def run(self) -> int:
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self._forward)
                except (ValueError, OSError):
                    pass
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        if self.force_setup:
            env["ZEBA_FORCE_SETUP"] = "1"
        delay, fast_fails = 3.0, 0
        while True:
            started = time.time()
            self.proc = subprocess.Popen([sys.executable, str(ROOT / "unified_main.py")], cwd=ROOT, env=env)
            try:
                code = self.proc.wait()
            except KeyboardInterrupt:  # the child received Ctrl+C too and shuts down gracefully
                self.stopping = True
                try:
                    code = self.proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    code = 0
            env.pop("ZEBA_FORCE_SETUP", None)  # the wizard only forces itself once
            if self.stopping or code in (0, 130, -signal.SIGINT, -signal.SIGTERM):
                return 0
            if code == 2:
                log("Not restarting: the bot needs attention (see the message above). Fix it and run again.")
                return self._panel(2)
            if not self.restart:
                return code
            if code == 3:
                log("Restart requested - restarting now.")
                time.sleep(1)
                delay, fast_fails = 3.0, 0
                continue
            fast_fails = fast_fails + 1 if time.time() - started < 30 else 0
            if fast_fails >= 5:
                log("The bot keeps crashing right after start - giving up. Check data/logs/zeba.log")
                return self._panel(code)
            log(f"Bot stopped (exit code {code}). Restarting in {delay:.0f}s ...")
            time.sleep(delay)
            delay = min(delay * 2, 60) if fast_fails else 3.0


def main() -> int:
    args = sys.argv[1:]
    if sys.version_info < (3, 10):
        die(f"Python 3.10+ is required (you have {sys.version.split()[0]}).")
    known = ("--setup", "--setup-only", "--once", "--background", "--stop", "--status", "--test-translate", "-h", "--help")
    unknown = [a for a in args if a not in known]
    if unknown or "-h" in args or "--help" in args:
        print(__doc__)
        return 0 if not unknown else 2

    os.chdir(ROOT)
    if "--status" in args:
        return cmd_status()
    if "--stop" in args:
        return cmd_stop()
    ensure_venv(args)
    ensure_deps()
    hints()

    if "--test-translate" in args:
        return subprocess.call([sys.executable, "-m", "scripts.test_translate"], cwd=ROOT)
    if "--setup-only" in args:
        return subprocess.call([sys.executable, "-m", "scripts.setup"], cwd=ROOT)
    if "--background" in args:
        return cmd_background()
    acquire_pidfile()
    return Supervisor(force_setup="--setup" in args, restart="--once" not in args).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
