"""
Phaedrus menu-bar app.

A tiny macOS menu-bar launcher for the Phaedrus dashboard. It keeps the Flask
dashboard (scripts/phaedrus/dashboard.py, http://localhost:5050) running in the
background so a non-technical user never has to touch the terminal.

Menu items:
    Open Phaedrus   — open http://localhost:5050 in the default browser
    Restart server  — stop the subprocess (if ours) and start a fresh one
    Stop server     — stop the subprocess we started
    Quit            — stop our subprocess and quit the app

Behaviour:
    - On launch it checks whether localhost:5050 already responds. If something
      is already serving there (e.g. you ran dashboard.py by hand), it reuses it
      and does NOT spawn a duplicate.
    - Otherwise it spawns the dashboard with the SAME interpreter (sys.executable)
      running dashboard.py, with cwd = the phaedrus-core repo root, so the
      dashboard's relative imports resolve exactly as they do when run manually.
    - The dashboard's stdout/stderr are appended to ~/.phaedrus/dashboard.log.
    - On quit it terminates the subprocess it started (it never kills a server
      it merely discovered).

Run:
    python scripts/phaedrus/menubar_app.py

Enable launch-at-login:
    bash scripts/phaedrus/install_menubar.sh

Note: this is a GUI app. It needs a real macOS login session to render the
menu-bar icon. It cannot draw in a headless/SSH context.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Paths and constants (no dependency on rumps so helpers stay importable) ---

PORT = 5050
URL = f"http://localhost:{PORT}/"
HEALTH_URL = f"http://127.0.0.1:{PORT}/"

# This file lives at <repo>/scripts/phaedrus/menubar_app.py, so the repo root is
# two directories up. That root is the cwd the dashboard expects.
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parent.parent.parent
DASHBOARD = HERE.parent / "dashboard.py"

PHAEDRUS_HOME = Path.home() / ".phaedrus"
LOG_PATH = PHAEDRUS_HOME / "dashboard.log"

TITLE = "◆"  # "◆" — a small lozenge; shown when there is no icon asset.


# --- Server lifecycle helpers (pure logic, safe to call without a GUI) ---

def is_server_running(timeout: float = 0.6) -> bool:
    """Return True if something already answers on localhost:5050.

    Uses a short urllib request so we never block the UI thread for long. Any
    HTTP response (even an error status) means a server is live, so we treat an
    HTTPError as "running". Only connection-level failures mean "not running".
    """
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        # The port answered with an HTTP status code — a server is there.
        return True
    except urllib.error.URLError:
        return False
    except (TimeoutError, OSError):
        return False


def spawn_dashboard() -> subprocess.Popen:
    """Start dashboard.py as a detached subprocess and return the Popen handle.

    Same interpreter (sys.executable), cwd = repo root, stdout/stderr appended to
    ~/.phaedrus/dashboard.log. The dashboard auto-opens the browser itself on
    boot (it has its own Timer-based open), so we don't double-open here.
    """
    PHAEDRUS_HOME.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", buffering=1)
    log_file.write(
        f"\n=== Phaedrus dashboard launched by menubar app at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n"
    )
    log_file.flush()

    env = dict(os.environ)
    # Make the dashboard's own `sys.path.insert(scripts/phaedrus)` redundant-safe
    # and keep imports resolving from the repo root too.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    proc = subprocess.Popen(
        [sys.executable, str(DASHBOARD)],
        cwd=str(REPO_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,  # detach from our process group
    )
    return proc


def wait_until_up(timeout: float = 12.0, interval: float = 0.4) -> bool:
    """Poll the health URL until the server answers or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_server_running(timeout=0.5):
            return True
        time.sleep(interval)
    return is_server_running(timeout=0.5)


def stop_process(proc: "subprocess.Popen | None", timeout: float = 5.0) -> None:
    """Terminate a subprocess we own, escalating to kill if it lingers."""
    if proc is None:
        return
    if proc.poll() is not None:
        return  # already exited
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except (ProcessLookupError, OSError):
        pass


# --- The GUI app (only constructed when rumps is available) ---

def _build_app():
    import rumps

    class PhaedrusMenuBar(rumps.App):
        def __init__(self):
            super().__init__(name="Phaedrus", title=TITLE, quit_button=None)
            self.proc = None  # the dashboard subprocess WE started, if any
            self.menu = [
                rumps.MenuItem("Open Phaedrus", callback=self.open_phaedrus),
                None,
                rumps.MenuItem("Restart server", callback=self.restart_server),
                rumps.MenuItem("Stop server", callback=self.stop_server),
                None,
                rumps.MenuItem("Quit", callback=self.quit_app),
            ]
            # Start (or adopt) the server shortly after launch so the menu bar
            # paints immediately and the (possibly slow) boot happens off the
            # first run-loop tick.
            rumps.Timer(self._ensure_server_once, 0.1).start()
            self._ensured = False

        # -- lifecycle --

        def _ensure_server_once(self, timer):
            if self._ensured:
                timer.stop()
                return
            self._ensured = True
            timer.stop()
            self.ensure_server()

        def ensure_server(self):
            """Adopt an already-running server, else spawn our own."""
            if is_server_running():
                # Something is already serving 5050; reuse it, don't duplicate.
                self.proc = None
                return
            try:
                self.proc = spawn_dashboard()
            except Exception as exc:  # pragma: no cover - GUI-only path
                rumps.notification(
                    "Phaedrus", "Could not start the dashboard", str(exc)
                )
                return
            if not wait_until_up():
                rumps.notification(
                    "Phaedrus",
                    "Dashboard is taking a while",
                    f"Check the log at {LOG_PATH}",
                )

        # -- menu callbacks --

        def open_phaedrus(self, _):
            if not is_server_running():
                self.ensure_server()
            # Use the system opener so it lands in the user's default browser.
            subprocess.Popen(["open", URL])

        def restart_server(self, _):
            stop_process(self.proc)
            self.proc = None
            # If a foreign server is up we cannot kill it; only start when free.
            if not is_server_running():
                self.proc = spawn_dashboard()
                wait_until_up()
            rumps.notification("Phaedrus", "Server restarted", URL)

        def stop_server(self, _):
            if self.proc is not None:
                stop_process(self.proc)
                self.proc = None
                rumps.notification("Phaedrus", "Server stopped", "")
            else:
                rumps.notification(
                    "Phaedrus",
                    "Nothing to stop",
                    "The dashboard wasn't started by this app.",
                )

        def quit_app(self, _):
            stop_process(self.proc)
            self.proc = None
            rumps.quit_application()

    return PhaedrusMenuBar()


def main():
    try:
        import rumps  # noqa: F401
    except ImportError:
        print(
            "Phaedrus menu-bar app needs the 'rumps' library.\n"
            "Install it into this project's virtualenv:\n\n"
            "    /Users/khalilalnajem/Documents/phaedrus-core/.venv/bin/pip "
            "install rumps\n\n"
            "Then run again:\n"
            "    python scripts/phaedrus/menubar_app.py",
            file=sys.stderr,
        )
        sys.exit(1)

    app = _build_app()
    app.run()


if __name__ == "__main__":
    # Allow a headless self-test of the server logic without booting the GUI:
    #   python scripts/phaedrus/menubar_app.py --selftest
    if "--selftest" in sys.argv:
        print(f"repo_root       : {REPO_ROOT}")
        print(f"dashboard       : {DASHBOARD}  (exists={DASHBOARD.exists()})")
        print(f"interpreter     : {sys.executable}")
        print(f"log_path        : {LOG_PATH}")
        print(f"server_running  : {is_server_running()}")
        sys.exit(0)
    main()
