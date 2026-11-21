"""CellSpec (/etc/droidloom/cell.json), host GPUs, storage mapping, and extensions config."""

from __future__ import annotations

import copy
import json
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .user_configs import atomic_write_json, xdg_config_dir

DEFAULT_CELL_SPEC = Path("/etc/droidloom/cell.json")
DEFAULT_RUNTIME_ENV = Path("/etc/droidloom/runtime.env")
DEFAULT_PACKAGE_STATE = Path("/etc/droidloom/package-state.json")
SUPERVISOR_BIN = Path("/usr/bin/droidloom-supervisor")

XDG_USER_FOLDERS = [
    ("XDG_DOWNLOAD_DIR", "Downloads", "Download"),
    ("XDG_PICTURES_DIR", "Pictures", "Pictures"),
    ("XDG_DOCUMENTS_DIR", "Documents", "Documents"),
    ("XDG_MUSIC_DIR", "Music", "Music"),
    ("XDG_VIDEOS_DIR", "Videos", "Movies"),
]


@dataclass
class HostGpuInfo:
    render_node: str
    device: str
    driver: str
    vendor: str | None

    @property
    def label(self) -> str:
        v = self.vendor or "Unsupported"
        return f"{v} GPU {self.device} ({self.driver}, {self.render_node})"


def gpu_vendor_for_driver(driver: str) -> str | None:
    if driver == "amdgpu":
        return "AMD"
    if driver in ("i915", "xe"):
        return "Intel"
    if driver == "nvidia":
        return "NVIDIA"
    if driver in ("msm", "msm_dpu", "msm_mdp5"):
        return "Qualcomm"
    if driver in ("panthor", "panfrost"):
        return "Mali"
    return None


def is_pci_address(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]", value))


def list_host_gpus(sys_drm: Path = Path("/sys/class/drm")) -> list[HostGpuInfo]:
    gpus: list[HostGpuInfo] = []
    if not sys_drm.is_dir():
        return gpus
    for node in sorted(sys_drm.iterdir(), key=lambda p: p.name):
        name = node.name
        if not name.startswith("renderD") or not name[7:].isdigit():
            continue
        dev_link = node / "device"
        drv_link = dev_link / "driver"
        try:
            dev_real = dev_link.resolve(strict=True)
            drv_real = drv_link.resolve(strict=True)
        except OSError:
            continue
        dev_name = dev_real.name
        drv_name = drv_real.name
        vendor = gpu_vendor_for_driver(drv_name)
        if is_pci_address(dev_name) or vendor in ("Qualcomm", "Mali"):
            gpus.append(
                HostGpuInfo(
                    render_node=f"/dev/dri/{name}",
                    device=dev_name,
                    driver=drv_name,
                    vendor=vendor,
                )
            )
    return gpus


def resolve_user_folders(host_uid: int) -> list[dict[str, Any]]:
    """Mirror droidloom-supervisor user_folders::resolve preview."""
    try:
        pw = pwd.getpwuid(host_uid)
    except KeyError:
        return []
    home = Path(pw.pw_dir)
    gid = pw.pw_gid
    user_dirs_file = home / ".config/user-dirs.dirs"
    text = ""
    if user_dirs_file.is_file():
        try:
            text = user_dirs_file.read_text(encoding="utf-8")
        except OSError:
            text = ""

    def configured(key: str) -> Path | None:
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith(key):
                continue
            rest = s[len(key) :].lstrip()
            if not rest.startswith("="):
                continue
            val = rest[1:].strip()
            if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                raw = val[1:-1]
                p = home / raw[5:].lstrip("/") if raw.startswith("$HOME") else Path(raw)
                if p.is_absolute() and p != home and str(p).startswith(str(home)):
                    return p
        return None

    out: list[dict[str, Any]] = []
    for key, usual, android_name in XDG_USER_FOLDERS:
        src = configured(key) or (home / usual)
        exists = src.is_dir()
        owned = False
        setgid = False
        if exists:
            try:
                st = src.lstat()
                owned = st.st_uid == host_uid and st.st_gid == gid
                setgid = bool(st.st_mode & 0o2000)
            except OSError:
                pass
        out.append(
            {
                "xdg_key": key,
                "source": str(src),
                "android_name": android_name,
                "exists": exists,
                "owned": owned,
                "setgid": setgid,
            }
        )
    return out


_EXTENSIONS_SUPPORT_CACHE: bool | None = None


def detect_supervisor_extensions_support(
    supervisor_bin: Path = SUPERVISOR_BIN, base_spec: dict[str, Any] | None = None
) -> bool:
    """Check whether installed droidloom-supervisor accepts network/device_identity fields."""
    global _EXTENSIONS_SUPPORT_CACHE
    if _EXTENSIONS_SUPPORT_CACHE is not None:
        return _EXTENSIONS_SUPPORT_CACHE
    if not supervisor_bin.is_file():
        _EXTENSIONS_SUPPORT_CACHE = False
        return False

    probe_spec = copy.deepcopy(base_spec) if base_spec else load_cell_json()
    if not probe_spec:
        probe_spec = {
            "host_uid": os.getuid(),
            "subordinate_uids": {"start": 300000, "count": 100000},
            "subordinate_gids": {"start": 300000, "count": 100000},
            "image_dir": "/usr/lib/droidloom/images",
            "vendor_image": "/usr/lib/droidloom/images/images/vendor.raw.img",
            "data_dir": f"/var/lib/droidloom/users/{os.getuid()}/data",
            "runtime_dir": f"/run/droidloom/cells/u{os.getuid()}",
            "render_node": "/dev/dri/renderD128",
            "denial_socket": f"/run/user/{os.getuid()}/droidloom/native-bridge.sock",
        }
    probe_spec["network"] = {"mode": "nat"}
    probe_spec["device_identity"] = {"model": "Probe"}
    tmp_path: Path | None = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="droidloom-probe-", suffix=".json")
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(probe_spec, fh)
        res = subprocess.run(
            [str(supervisor_bin), "plan", "--spec", str(tmp_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        _EXTENSIONS_SUPPORT_CACHE = res.returncode == 0
    except Exception:
        _EXTENSIONS_SUPPORT_CACHE = False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return _EXTENSIONS_SUPPORT_CACHE


def load_cell_json(path: Path = DEFAULT_CELL_SPEC) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_runtime_env(path: Path = DEFAULT_RUNTIME_ENV) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_package_state(path: Path = DEFAULT_PACKAGE_STATE) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class ExtensionsConfig:
    """Extended network + device identity settings stored in cell.json or extensions-v1.json."""

    network: dict[str, Any] = field(default_factory=lambda: {"mode": "nat"})
    device_identity: dict[str, Any] = field(default_factory=dict)
    proxy_bridge_enabled: bool = False
    proxy_bridge_port: int = 7897
    proxy_bridge_bypass: str = "localhost,127.0.0.1,10.177.0.0/30"


def load_extensions_config(
    cell: dict[str, Any] | None = None, config_dir: Path | None = None
) -> ExtensionsConfig:
    path = (config_dir or xdg_config_dir()) / "extensions-v1.json"
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    cell_data = cell if cell is not None else load_cell_json()
    net = copy.deepcopy(cell_data.get("network") or raw.get("network") or {"mode": "nat"})
    ident = copy.deepcopy(cell_data.get("device_identity") or raw.get("device_identity") or {})
    return ExtensionsConfig(
        network=net,
        device_identity=ident,
        proxy_bridge_enabled=bool(raw.get("proxy_bridge_enabled", False)),
        proxy_bridge_port=int(raw.get("proxy_bridge_port", 7897)),
        proxy_bridge_bypass=str(
            raw.get("proxy_bridge_bypass", "localhost,127.0.0.1,10.177.0.0/30")
        ),
    )


def save_extensions_sidecar(ext: ExtensionsConfig, config_dir: Path | None = None) -> None:
    path = (config_dir or xdg_config_dir()) / "extensions-v1.json"
    atomic_write_json(
        path,
        {
            "schema": 1,
            "network": ext.network,
            "device_identity": ext.device_identity,
            "proxy_bridge_enabled": ext.proxy_bridge_enabled,
            "proxy_bridge_port": ext.proxy_bridge_port,
            "proxy_bridge_bypass": ext.proxy_bridge_bypass,
        },
        mode=0o600,
    )


def validate_shared_directory_entry(
    source: str, android_name: str, host_gid: int
) -> str | None:
    src = Path(source)
    if not src.is_absolute() or ".." in src.parts:
        return "宿主机目录必须是绝对路径且不能包含 .."
    if not src.is_dir():
        return f"目录不存在: {source}"
    if (
        not android_name
        or len(android_name) > 255
        or "/" in android_name
        or android_name in (".", "..")
        or android_name.lower() == "android"
    ):
        return "Android 共享目录名称必须是非保留的单个目录名（不能为 Android 或包含 /）"
    if host_gid < 0:
        return "host_gid 无效"
    return None


def validate_cell_spec_with_supervisor(spec: dict[str, Any]) -> tuple[bool, str]:
    if not SUPERVISOR_BIN.is_file():
        return True, ""
    tmp_path: Path | None = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="droidloom-val-", suffix=".json")
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2)
        res = subprocess.run(
            [str(SUPERVISOR_BIN), "plan", "--spec", str(tmp_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            return False, msg or "droidloom-supervisor 校验失败"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def save_cell_spec_privileged(
    spec: dict[str, Any],
    runtime_env_lines: list[str] | None = None,
    setgid_dirs: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate and atomically write /etc/droidloom/cell.json (and optional runtime.env) in one pkexec call."""
    clean_spec = copy.deepcopy(spec)
    if not detect_supervisor_extensions_support(base_spec=clean_spec):
        clean_spec.pop("network", None)
        clean_spec.pop("device_identity", None)

    ok, err = validate_cell_spec_with_supervisor(clean_spec)
    if not ok:
        return False, f"配置未通过 droidloom-supervisor 校验：{err}"

    spec_text = json.dumps(clean_spec, indent=2, ensure_ascii=False) + "\n"
    tmp_spec_fd, tmp_spec_str = tempfile.mkstemp(prefix="droidloom-cell-", suffix=".json")
    tmp_env_str: str | None = None
    try:
        with os.fdopen(tmp_spec_fd, "w", encoding="utf-8") as fh:
            fh.write(spec_text)

        cmds = [
            "set -euo pipefail",
            f"cp -f {DEFAULT_CELL_SPEC} {DEFAULT_CELL_SPEC}.bak 2>/dev/null || true",
            f"install -m 0644 -o root -g root {shlex_quote(tmp_spec_str)} {DEFAULT_CELL_SPEC}",
        ]
        if runtime_env_lines is not None:
            tmp_env_fd, tmp_env_str = tempfile.mkstemp(prefix="droidloom-env-", suffix=".env")
            with os.fdopen(tmp_env_fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(runtime_env_lines) + "\n")
            cmds.append(
                f"install -m 0644 -o root -g root {shlex_quote(tmp_env_str)} {DEFAULT_RUNTIME_ENV}"
            )
        if setgid_dirs:
            for d in setgid_dirs:
                if Path(d).is_dir():
                    cmds.append(f"chmod g+s {shlex_quote(d)} 2>/dev/null || true")
        cmds.append(
            f"command -v restorecon >/dev/null && restorecon -F {DEFAULT_CELL_SPEC} {DEFAULT_RUNTIME_ENV} 2>/dev/null || true"
        )
        script = " ; ".join(cmds)
        res = subprocess.run(
            ["pkexec", "/bin/sh", "-c", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            return False, msg or f"pkexec 退出码 {res.returncode}"
        return True, "已保存到 /etc/droidloom/cell.json（重启 Droidloom 后生效）"
    finally:
        Path(tmp_spec_str).unlink(missing_ok=True)
        if tmp_env_str is not None:
            Path(tmp_env_str).unlink(missing_ok=True)


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def build_runtime_env_for_gpu(gpu_choice: str) -> list[str] | None:
    gpus = list_host_gpus()
    supported = [g for g in gpus if g.vendor is not None]
    if not supported:
        return None
    choice = gpu_choice.lower().strip()
    renderer: HostGpuInfo | None = None
    if choice == "auto":
        renderer = next((g for g in supported if g.vendor == "NVIDIA"), supported[0])
    elif choice in ("amd", "intel", "nvidia", "qualcomm", "mali"):
        renderer = next(
            (g for g in supported if (g.vendor or "").lower() == choice), None
        )
    else:
        norm = choice if is_pci_address(choice) else f"0000:{choice}"
        renderer = next((g for g in gpus if g.device == norm), None)
    if renderer is None:
        return None
    if renderer.vendor == "NVIDIA":
        native = next((g for g in supported if g.vendor != "NVIDIA"), renderer)
        return [
            "DROIDLOOM_HOST_PEER_PID=0",
            "DROIDLOOM_HOST_PEER_UID=1000",
            "DROIDLOOM_HOST_PEER_GID=1000",
            f"DROIDLOOM_RENDER_NODE={native.render_node}",
            f"DROIDLOOM_GPU={renderer.device}",
        ]
    return [
        "DROIDLOOM_HOST_PEER_PID=0",
        "DROIDLOOM_HOST_PEER_UID=1000",
        "DROIDLOOM_HOST_PEER_GID=1000",
        f"DROIDLOOM_RENDER_NODE={renderer.render_node}",
    ]


def directory_disk_usage_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    du = shutil.which("du")
    if du:
        try:
            res = subprocess.run(
                [du, "-sb", str(path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if res.returncode == 0 and res.stdout:
                return int(res.stdout.split()[0])
        except Exception:
            pass
    return None
