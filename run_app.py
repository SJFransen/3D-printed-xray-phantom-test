"""Launch the MSF Phantom QA web app.

    python run_app.py [port]

Reads configuration from .env (see .env.example). Binds to 127.0.0.1 by
default — for a server deployment set PHANTOMQA_HOST=0.0.0.0 and put a
TLS-terminating reverse proxy in front; see README "Deploying on a server".
"""

import os
import sys

import uvicorn

from phantom_qa.config import get_config

_PROCESS_POWER_THROTTLING = 4
_POWER_THROTTLING_VERSION = 1
_POWER_THROTTLING_EXECUTION_SPEED = 1

def _run_at_full_speed() -> bool:
    """Ask Windows not to slow this process down to save power.

    Windows runs a process whose window is in the background on its slowest,
    power-saving settings (EcoQoS). The terminal running this server is in
    the background the whole time the operator works in the browser, and
    measured on the reference scans that made moving from registration to the
    patterns take 2.3 s instead of 0.94 s. This opts this one process out;
    nothing else on the machine changes, and the setting ends with the
    process.

    Only the local starter does this. A server deployment runs gunicorn,
    which never runs this file. True when Windows accepted; False on any
    other system, on a Windows too old to know the setting, or on any error
    at all — the server then simply runs as it always did, so this must never
    stop it starting."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _ThrottlingState(ctypes.Structure):   # PROCESS_POWER_THROTTLING_STATE
            _fields_ = [("Version", wintypes.ULONG),
                        ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        # Before Windows 8 kernel32 has no SetProcessInformation at all, and
        # reaching for it raises; that is one of the errors caught below.
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        kernel32.SetProcessInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        # Execution speed is the setting being controlled, and its state is
        # "off": the process is never throttled, whatever its window does.
        state = _ThrottlingState(_POWER_THROTTLING_VERSION,
                                 _POWER_THROTTLING_EXECUTION_SPEED, 0)
        return bool(kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING,
            ctypes.byref(state), ctypes.sizeof(state)))
    except Exception:
        return False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(
        os.environ.get("PHANTOMQA_PORT", 8777))
    host = os.environ.get("PHANTOMQA_HOST", "127.0.0.1")
    cfg = get_config()
    print(f"MSF Phantom QA — {cfg.summary()}")
    if os.name == "nt":
        print("  Windows execution throttling: " +
              ("disabled for this process" if _run_at_full_speed() else "unchanged"))
    if not cfg.auth_enabled:
        print("  !! authentication is DISABLED — do not expose this port to a "
              "network. Set PHANTOMQA_ENV=production in .env before deploying.")
    uvicorn.run("phantom_qa.webapp.main:app", host=host, port=port,
                log_level="info", proxy_headers=cfg.behind_proxy,
                forwarded_allow_ips=os.environ.get("PHANTOMQA_FORWARDED_ALLOW_IPS", "127.0.0.1")
                if cfg.behind_proxy else None)
