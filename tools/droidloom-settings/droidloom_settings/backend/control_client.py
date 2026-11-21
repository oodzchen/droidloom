"""Client for the local droidloomd Unix lifecycle control socket."""

from __future__ import annotations

import array
import base64
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_CONTROL_SOCKET = "/run/droidloom/control.sock"
DEFAULT_CELL_SPEC = "/etc/droidloom/cell.json"
MAX_APK_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ControlError(Exception):
    """Error returned when talking to droidloomd."""


@dataclass
class ControlResponse:
    """Decoded response from droidloomd."""

    ok: bool
    state: str
    message: str
    host_pid: int | None = None
    launch: str | None = None
    applications: list[dict[str, Any]] | None = None
    application_icon: str | None = None
    application_icons: dict[str, str] | None = None
    diagnostics: str | None = None
    stats: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
    games_driver: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlResponse":
        return cls(
            ok=bool(data.get("ok", False)),
            state=str(data.get("state", "stopped")),
            message=str(data.get("message", "")),
            host_pid=data.get("host_pid"),
            launch=data.get("launch"),
            applications=data.get("applications"),
            application_icon=data.get("application_icon"),
            application_icons=data.get("application_icons"),
            diagnostics=data.get("diagnostics"),
            stats=data.get("stats"),
            application=data.get("application"),
            display=data.get("display"),
            games_driver=data.get("games_driver"),
            raw=data,
        )


class ControlClient:
    """Synchronous Unix-socket client matching droidloomd's JSON + SCM_RIGHTS wire protocol."""

    def __init__(self, socket_path: str | Path = DEFAULT_CONTROL_SOCKET) -> None:
        self.socket_path = Path(socket_path)

    def exists(self) -> bool:
        return self.socket_path.exists()

    def request(
        self,
        payload: dict[str, Any],
        fd: int | None = None,
        on_progress: Callable[[str], None] | None = None,
        timeout: float = 260.0,
    ) -> ControlResponse:
        """Send a control request wrapped in ProgressRequest envelope."""
        envelope = json.dumps({"request": payload}, separators=(",", ":")).encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(str(self.socket_path))
            except FileNotFoundError as exc:
                raise ControlError("Droidloom daemon is not running (control socket absent)") from exc
            except ConnectionRefusedError as exc:
                raise ControlError("Droidloom daemon refused connection (not running)") from exc
            except OSError as exc:
                raise ControlError(f"Failed to connect to droidloomd: {exc}") from exc

            if fd is not None:
                anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
                sock.sendmsg([envelope], anc)
            else:
                sock.sendall(envelope)
            sock.shutdown(socket.SHUT_WR)

            buf = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise ControlError("droidloomd response exceeded 8 MiB limit")
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl]).strip()
                    del buf[: nl + 1]
                    if not line:
                        continue
                    obj = json.loads(line.decode("utf-8"))
                    if "progress" in obj and len(obj) == 1:
                        if on_progress is not None:
                            on_progress(str(obj["progress"]))
                    else:
                        return ControlResponse.from_dict(obj)

            tail = bytes(buf).strip()
            if not tail:
                raise ControlError("droidloomd closed connection without response")
            obj = json.loads(tail.decode("utf-8"))
            if "progress" in obj and len(obj) == 1:
                raise ControlError("droidloomd ended after progress without final result")
            return ControlResponse.from_dict(obj)
        except ControlError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ControlError(f"Request timed out: {exc}") from exc
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ControlError(f"Control protocol error: {exc}") from exc
        finally:
            sock.close()

    def status(self) -> ControlResponse:
        if not self.exists():
            return ControlResponse(ok=True, state="stopped", message="Droidloom is stopped")
        return self.request({"command": "status"}, timeout=5.0)

    def stats(self) -> ControlResponse:
        if not self.exists():
            return ControlResponse(ok=True, state="stopped", message="Droidloom is stopped")
        return self.request({"command": "stats"}, timeout=5.0)

    def wait_ready(self, on_progress: Callable[[str], None] | None = None) -> ControlResponse:
        return self.request({"command": "wait_ready"}, on_progress=on_progress, timeout=140.0)

    def list_applications(self, user: int = 0) -> list[dict[str, Any]]:
        resp = self.request({"command": "list_applications", "user": user}, timeout=15.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp.applications or []

    def application_icons(
        self, components: list[str], user: int = 0, size: int = 96
    ) -> dict[str, bytes]:
        """Fetch PNG icon bytes in batches of up to 32 components."""
        out: dict[str, bytes] = {}
        for idx in range(0, len(components), 32):
            batch = components[idx : idx + 32]
            resp = self.request(
                {
                    "command": "application_icons",
                    "components": batch,
                    "user": user,
                    "size": size,
                },
                timeout=30.0,
            )
            if resp.ok and resp.application_icons:
                for comp, b64 in resp.application_icons.items():
                    try:
                        out[comp] = base64.b64decode(b64)
                    except Exception:
                        pass
        return out

    def application_details(self, package: str, user: int = 0) -> dict[str, Any]:
        resp = self.request(
            {"command": "application_details", "package": package, "user": user},
            timeout=15.0,
        )
        if not resp.ok:
            raise ControlError(resp.message)
        return resp.application or {}

    def application_action(self, package: str, action: str, user: int = 0) -> ControlResponse:
        resp = self.request(
            {
                "command": "application_action",
                "package": package,
                "user": user,
                "action": action,
            },
            timeout=30.0,
        )
        if not resp.ok:
            raise ControlError(resp.message)
        return resp

    def launch(
        self,
        package: str,
        component: str | None = None,
        resolution: str | None = None,
        user: int = 0,
        spec: str = DEFAULT_CELL_SPEC,
    ) -> ControlResponse:
        req: dict[str, Any] = {
            "command": "launch",
            "spec": spec,
            "package": package,
            "user": user,
        }
        if component:
            req["component"] = component
        if resolution:
            req["resolution"] = resolution
        resp = self.request(req, timeout=30.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp

    def display(self) -> dict[str, Any]:
        resp = self.request({"command": "display"}, timeout=10.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp.display or {}

    def set_dpi(self, dpi: int, spec: str = DEFAULT_CELL_SPEC) -> ControlResponse:
        resp = self.request({"command": "set_dpi", "spec": spec, "dpi": dpi}, timeout=120.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp

    def set_night_mode(self, night: bool) -> ControlResponse:
        resp = self.request({"command": "set_night_mode", "night": night}, timeout=15.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp

    def games_driver(self) -> str:
        resp = self.request({"command": "games_driver"}, timeout=10.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return str(resp.games_driver or "angle")

    def set_games_driver(self, driver: str) -> ControlResponse:
        resp = self.request({"command": "set_games_driver", "driver": driver}, timeout=15.0)
        if not resp.ok:
            raise ControlError(resp.message)
        return resp

    def diagnostics(
        self,
        package: str | None = None,
        lines: int = 200,
        crashes: bool = False,
        user: int = 0,
    ) -> str:
        resp = self.request(
            {
                "command": "diagnostics",
                "package": package,
                "user": user,
                "lines": max(1, min(2000, lines)),
                "crashes": crashes,
            },
            timeout=20.0,
        )
        if not resp.ok:
            raise ControlError(resp.message)
        return resp.diagnostics or resp.message

    def install_apk(
        self,
        apk_path: str | Path,
        user: int = 0,
        on_progress: Callable[[str], None] | None = None,
    ) -> ControlResponse:
        path = Path(apk_path)
        if not path.is_file():
            raise ControlError(f"APK file does not exist: {path}")
        size = path.stat().st_size
        if size == 0 or size > MAX_APK_BYTES:
            raise ControlError("APK file is empty or exceeds 2 GiB")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            resp = self.request(
                {"command": "install", "user": user},
                fd=fd,
                on_progress=on_progress,
                timeout=260.0,
            )
            if not resp.ok:
                raise ControlError(resp.message)
            return resp
        finally:
            os.close(fd)
