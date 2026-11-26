"""User-owned Droidloom configurations under ~/.config, ~/.local/state, ~/.local/share, and XDG_RUNTIME_DIR."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_DIMENSION = 16384
DEFAULT_DPI_PER_SCALE = 230.0
MIN_DPI = 72
MAX_DPI = 1280


def xdg_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base and Path(base).is_absolute():
        return Path(base) / "droidloom"
    return Path.home() / ".config/droidloom"


def xdg_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base and Path(base).is_absolute():
        return Path(base) / "droidloom"
    return Path.home() / ".local/state/droidloom"


def xdg_applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base and Path(base).is_absolute():
        return Path(base) / "applications"
    return Path.home() / ".local/share/applications"


def xdg_runtime_droidloom_dir() -> Path | None:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and Path(rt).is_absolute():
        return Path(rt) / "droidloom"
    uid = os.getuid()
    candidate = Path(f"/run/user/{uid}/droidloom")
    return candidate


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def validate_package_name(package: str) -> bool:
    return bool(
        package
        and len(package) <= 255
        and "." in package
        and all(ch.isascii() and (ch.isalnum() or ch in "._") for ch in package)
    )


def validate_window_size(width: int, height: int) -> bool:
    return 1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION


@dataclass
class WindowPreference:
    mode: str = "windowed"  # "windowed" or "fit_output"
    width: int | None = 480
    height: int | None = 800

    def to_dict(self) -> dict[str, Any]:
        if self.mode == "fit_output":
            return {"mode": "fit_output"}
        return {
            "mode": "windowed",
            "size": {"width": int(self.width or 480), "height": int(self.height or 800)},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowPreference":
        mode = str(data.get("mode", "windowed"))
        if mode == "fit_output":
            return cls(mode="fit_output", width=None, height=None)
        size = data.get("size") or {}
        return cls(
            mode="windowed",
            width=int(size.get("width", 480)),
            height=int(size.get("height", 800)),
        )


@dataclass
class WindowPolicyData:
    default: WindowPreference = field(default_factory=WindowPreference)
    applications: dict[str, WindowPreference] = field(default_factory=dict)


def load_window_policy(config_dir: Path | None = None) -> WindowPolicyData:
    path = (config_dir or xdg_config_dir()) / "window-policy-v1.json"
    if not path.is_file():
        return WindowPolicyData()
    raw = json.loads(path.read_text(encoding="utf-8"))
    default = WindowPreference.from_dict(raw.get("default") or {})
    apps: dict[str, WindowPreference] = {}
    for pkg, pref in (raw.get("applications") or {}).items():
        apps[str(pkg)] = WindowPreference.from_dict(pref)
    return WindowPolicyData(default=default, applications=apps)


def save_window_policy(policy: WindowPolicyData, config_dir: Path | None = None) -> None:
    if policy.default.mode == "windowed" and not validate_window_size(
        policy.default.width or 0, policy.default.height or 0
    ):
        raise ValueError("默认窗口尺寸无效")
    apps_json: dict[str, Any] = {}
    for pkg, pref in sorted(policy.applications.items()):
        if not validate_package_name(pkg):
            raise ValueError(f"无效的包名: {pkg}")
        if pref.mode == "windowed" and not validate_window_size(pref.width or 0, pref.height or 0):
            raise ValueError(f"{pkg} 的窗口尺寸无效")
        apps_json[pkg] = pref.to_dict()
    doc = {
        "schema": 1,
        "default": policy.default.to_dict(),
        "applications": apps_json,
    }
    path = (config_dir or xdg_config_dir()) / "window-policy-v1.json"
    atomic_write_json(path, doc, mode=0o600)


def load_remembered_window_sizes(state_dir: Path | None = None) -> dict[str, tuple[int, int]]:
    path = (state_dir or xdg_state_dir()) / "desktop-window-sizes-v1.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[int, int]] = {}
    for pkg, size in (raw.get("applications") or {}).items():
        w = int(size.get("width", 0))
        h = int(size.get("height", 0))
        if validate_window_size(w, h):
            out[str(pkg)] = (w, h)
    return out


def clear_remembered_window_size(package: str, state_dir: Path | None = None) -> bool:
    path = (state_dir or xdg_state_dir()) / "desktop-window-sizes-v1.json"
    if not path.is_file():
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    apps = raw.get("applications") or {}
    if package not in apps:
        return False
    del apps[package]
    raw["applications"] = apps
    atomic_write_json(path, raw, mode=0o600)
    return True


@dataclass
class DensityConfig:
    dpi_per_scale: float | None = None
    outputs: dict[str, float] = field(default_factory=dict)


def load_density_config(config_dir: Path | None = None) -> DensityConfig:
    path = (config_dir or xdg_config_dir()) / "density-v1.json"
    if not path.is_file():
        return DensityConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    dps = raw.get("dpiPerScale")
    outputs = {str(k): float(v) for k, v in (raw.get("outputs") or {}).items()}
    return DensityConfig(
        dpi_per_scale=float(dps) if dps is not None else None,
        outputs=outputs,
    )


def save_density_config(cfg: DensityConfig, config_dir: Path | None = None) -> None:
    doc: dict[str, Any] = {}
    if cfg.dpi_per_scale is not None:
        val = cfg.dpi_per_scale
        doc["dpiPerScale"] = int(val) if float(val).is_integer() else round(val, 2)
    if cfg.outputs:
        doc["outputs"] = {
            k: (int(v) if float(v).is_integer() else round(v, 2))
            for k, v in sorted(cfg.outputs.items())
        }
    path = (config_dir or xdg_config_dir()) / "density-v1.json"
    atomic_write_json(path, doc, mode=0o644)


def list_drm_connectors() -> list[dict[str, str]]:
    """List DRM output connectors like DSI-1, DP-1, HDMI-A-1 with connection status."""
    drm = Path("/sys/class/drm")
    connectors: list[dict[str, str]] = []
    if not drm.is_dir():
        return connectors
    seen: set[str] = set()
    for entry in sorted(drm.iterdir()):
        name = entry.name
        if "-" not in name or not name.startswith("card"):
            continue
        conn = name.split("-", 1)[1]
        if conn in seen:
            continue
        seen.add(conn)
        status_file = entry / "status"
        status = "unknown"
        if status_file.is_file():
            try:
                status = status_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        connectors.append({"name": conn, "status": status})
    return connectors


@dataclass
class AudioSettings:
    allow_capture: bool = False
    target: str = ""
    remote: str = ""
    capture_sink: bool = False


def load_audio_settings(config_dir: Path | None = None) -> AudioSettings:
    cfg_dir = config_dir or xdg_config_dir()
    env_path = cfg_dir / "audio.env"
    allow_capture = False
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DROIDLOOM_AUDIO_CAPTURE="):
                val = line.split("=", 1)[1].strip().strip("\"'")
                allow_capture = val == "1"

    dropin_path = (
        Path.home() / ".config/systemd/user/droidloom-audio.service.d/90-droidloom-settings.conf"
    )
    target = ""
    remote = ""
    capture_sink = False
    if dropin_path.is_file():
        text = dropin_path.read_text(encoding="utf-8")
        m_t = re.search(r"--target(?:=|\s+)(\S+)", text)
        if m_t:
            target = m_t.group(1).strip("\"'")
        m_r = re.search(r"--remote(?:=|\s+)(\S+)", text)
        if m_r:
            remote = m_r.group(1).strip("\"'")
        if "--capture-sink" in text:
            capture_sink = True

    return AudioSettings(
        allow_capture=allow_capture,
        target=target,
        remote=remote,
        capture_sink=capture_sink,
    )


def save_audio_settings(settings: AudioSettings, config_dir: Path | None = None) -> None:
    cfg_dir = config_dir or xdg_config_dir()
    env_path = cfg_dir / "audio.env"
    lines = [f"DROIDLOOM_AUDIO_CAPTURE={'1' if settings.allow_capture else '0'}\n"]
    atomic_write_text(env_path, "".join(lines), mode=0o644)

    dropin_path = (
        Path.home() / ".config/systemd/user/droidloom-audio.service.d/90-droidloom-settings.conf"
    )
    if settings.target or settings.remote or settings.capture_sink:
        args = ["/usr/bin/droidloom-audio", "--socket", "%t/droidloom/audio.sock"]
        if settings.allow_capture:
            args.append("--allow-capture")
        if settings.target:
            args.extend(["--target", settings.target])
        if settings.remote:
            args.extend(["--remote", settings.remote])
        if settings.capture_sink:
            args.append("--capture-sink")
        content = "[Service]\nExecStart=\nExecStart=" + " ".join(args) + "\n"
        atomic_write_text(dropin_path, content, mode=0o644)
    elif dropin_path.is_file():
        dropin_path.unlink()


def load_session_mode() -> str:
    rt_dir = xdg_runtime_droidloom_dir()
    if rt_dir is None:
        return "desktop"
    env_file = rt_dir / "session.env"
    if not env_file.is_file():
        return "desktop"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DROIDLOOM_MODE="):
            mode = line.split("=", 1)[1].strip()
            if mode in ("desktop", "mobile"):
                return mode
    return "desktop"


def save_session_mode(mode: str) -> None:
    if mode not in ("desktop", "mobile"):
        raise ValueError("session mode must be desktop or mobile")
    rt_dir = xdg_runtime_droidloom_dir()
    if rt_dir is None:
        raise RuntimeError("XDG_RUNTIME_DIR 不可用")
    rt_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(rt_dir, 0o700)
    atomic_write_text(rt_dir / "session.env", f"DROIDLOOM_MODE={mode}\n", mode=0o600)


@dataclass
class OrientationConfig:
    follow_host_gravity: bool = False
    rotation_offset: int = 0  # 0, 1, 2, 3 (90 deg steps)
    force_fixed_to_user_rotation: bool = True
    last_manual_rotation: int | None = None  # None=auto/free, 0..3


def load_orientation_config(config_dir: Path | None = None) -> OrientationConfig:
    path = (config_dir or xdg_config_dir()) / "orientation-v1.json"
    if not path.is_file():
        return OrientationConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return OrientationConfig(
        follow_host_gravity=bool(raw.get("follow_host_gravity", False)),
        rotation_offset=int(raw.get("rotation_offset", 0)) % 4,
        force_fixed_to_user_rotation=bool(raw.get("force_fixed_to_user_rotation", True)),
        last_manual_rotation=(
            int(raw["last_manual_rotation"]) % 4
            if raw.get("last_manual_rotation") is not None
            else None
        ),
    )


def save_orientation_config(cfg: OrientationConfig, config_dir: Path | None = None) -> None:
    path = (config_dir or xdg_config_dir()) / "orientation-v1.json"
    atomic_write_json(
        path,
        {
            "schema": 1,
            "follow_host_gravity": cfg.follow_host_gravity,
            "rotation_offset": cfg.rotation_offset % 4,
            "force_fixed_to_user_rotation": cfg.force_fixed_to_user_rotation,
            "last_manual_rotation": cfg.last_manual_rotation,
        },
        mode=0o644,
    )


def _find_desktop_file_for_package(package: str, apps_dir: Path) -> Path | None:
    if not apps_dir.is_dir():
        return None
    for path in sorted(apps_dir.glob("droidloom-*.desktop")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Exec=") and f" {package}" in line:
                return path
            if line.startswith("StartupWMClass=") and line.split("=", 1)[1].strip() == package:
                return path
    return None


def get_desktop_launch_resolution(package: str, apps_dir: Path | None = None) -> str | None:
    path = _find_desktop_file_for_package(package, apps_dir or xdg_applications_dir())
    if path is None:
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("X-Droidloom-Resolution="):
            val = line.split("=", 1)[1].strip()
            if re.fullmatch(r"\d+x\d+", val):
                return val
    return None


def set_desktop_launch_resolution(
    package: str, resolution: str | None, apps_dir: Path | None = None
) -> bool:
    if resolution is not None:
        m = re.fullmatch(r"(\d+)x(\d+)", resolution.strip())
        if not m or not validate_window_size(int(m.group(1)), int(m.group(2))):
            raise ValueError("分辨率必须为 WIDTHxHEIGHT 且宽高在 1..=16384")
        resolution = f"{int(m.group(1))}x{int(m.group(2))}"

    path = _find_desktop_file_for_package(package, apps_dir or xdg_applications_dir())
    if path is None:
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    saw_key = False
    for line in lines:
        if line.startswith("X-Droidloom-Resolution="):
            saw_key = True
            if resolution is not None:
                new_lines.append(f"X-Droidloom-Resolution={resolution}")
        elif line.startswith("Exec="):
            cleaned = re.sub(r"\s+--resolution\s+\d+x\d+", "", line)
            if resolution is not None:
                cleaned = f"{cleaned} --resolution {resolution}"
            new_lines.append(cleaned)
        else:
            new_lines.append(line)
    if resolution is not None and not saw_key:
        new_lines.append(f"X-Droidloom-Resolution={resolution}")
    atomic_write_text(path, "\n".join(new_lines) + "\n", mode=0o600)
    return True


@dataclass
class PanelConfig:
    desktop_forwarding: bool = True
    default_tab: str = "control"
    show_system_tasks: bool = False


def load_panel_config(config_dir: Path | None = None) -> PanelConfig:
    path = (config_dir or xdg_config_dir()) / "panel-v1.json"
    if not path.is_file():
        return PanelConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PanelConfig()
    tab = str(raw.get("default_tab", "control"))
    if tab not in ("control", "tasks", "apps"):
        tab = "control"
    return PanelConfig(
        desktop_forwarding=bool(raw.get("desktop_forwarding", True)),
        default_tab=tab,
        show_system_tasks=bool(raw.get("show_system_tasks", False)),
    )


def save_panel_config(cfg: PanelConfig, config_dir: Path | None = None) -> None:
    tab = cfg.default_tab if cfg.default_tab in ("control", "tasks", "apps") else "control"
    path = (config_dir or xdg_config_dir()) / "panel-v1.json"
    atomic_write_json(
        path,
        {
            "schema": 1,
            "desktop_forwarding": bool(cfg.desktop_forwarding),
            "default_tab": tab,
            "show_system_tasks": bool(cfg.show_system_tasks),
        },
        mode=0o644,
    )
