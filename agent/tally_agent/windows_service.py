"""
windows_service.py — registers/runs this agent as a native Windows
Service via pywin32's win32serviceutil/ServiceFramework, so it starts
automatically on boot and survives the installing user logging off —
the real deployment mode this whole agent is built for (see package
__init__.py's docstring).

Real code, but CANNOT be imported or run on a non-Windows machine
(win32serviceutil/win32service/servicemanager only exist inside
pywin32) and is NOT live-verified here — no Windows machine exists in
this project's environment. Every function below raises a clear
RuntimeError on import/call from a non-Windows platform rather than
failing with a confusing ImportError deep in pywin32 — see
_require_windows().

install_service()/start_service()/stop_service()/remove_service() are
thin wrappers around win32serviceutil's own module-level functions
(HandleCommandLine against this same module is the conventional
pywin32 pattern — see installer/install_service.ps1 for how the
installer actually invokes this).
"""
from __future__ import annotations

import sys

SERVICE_NAME = "TallyAgent"
SERVICE_DISPLAY_NAME = "Tally Agent (DMS Integration)"
SERVICE_DESCRIPTION = (
    "Relays TallyPrime data to the DMS central server over an outbound "
    "WebSocket connection. Safe to stop; TallyPrime itself is unaffected."
)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            "windows_service.py requires Windows (pywin32) — this is the real Windows Service "
            "integration, disclosed as unverified on this project's own dev machines (no Windows "
            "box exists here). On any other platform, run the agent in the foreground instead: "
            "`tally_agent run`."
        )


class TallyAgentService:  # pragma: no cover - Windows-only, see module docstring
    """win32serviceutil.ServiceFramework subclass — constructed lazily
    inside _service_class() below so importing this module on a non-
    Windows platform never touches win32serviceutil at class-definition
    time (it isn't importable there at all)."""


def _service_class():  # pragma: no cover - Windows-only
    _require_windows()
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    from .cli import run_agent_blocking  # local import avoids a hard click/pywin32 coupling at module load

    class _Service(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._agent_stop = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._agent_stop is not None:
                self._agent_stop()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._agent_stop = run_agent_blocking(stop_event_setter=lambda stopper: setattr(self, "_agent_stop", stopper))

    return _Service


def install_service() -> None:  # pragma: no cover - Windows-only
    _require_windows()
    import win32serviceutil
    win32serviceutil.InstallService(
        pythonClassString=f"{__name__}._service_class_instance",
        serviceName=SERVICE_NAME, displayName=SERVICE_DISPLAY_NAME,
        description=SERVICE_DESCRIPTION, startType="auto",
    )


def start_service() -> None:  # pragma: no cover - Windows-only
    _require_windows()
    import win32serviceutil
    win32serviceutil.StartService(SERVICE_NAME)


def stop_service() -> None:  # pragma: no cover - Windows-only
    _require_windows()
    import win32serviceutil
    win32serviceutil.StopService(SERVICE_NAME)


def remove_service() -> None:  # pragma: no cover - Windows-only
    _require_windows()
    import win32serviceutil
    win32serviceutil.RemoveService(SERVICE_NAME)


def service_status() -> str:  # pragma: no cover - Windows-only
    _require_windows()
    import win32serviceutil
    status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
    return {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING"}.get(status[1], "UNKNOWN")


if __name__ == "__main__":  # pragma: no cover - Windows-only entry point, invoked by pywin32's service manager
    _require_windows()
    import win32serviceutil
    win32serviceutil.HandleCommandLine(_service_class())
