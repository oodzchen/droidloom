"""Host runtime services, live nftables overlay rules, cgroup v2 limits, PipeWire, and diagnostics."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .user_configs import save_session_mode

DROIDLOOMCTL = "/usr/bin/droidloomctl"
DROIDLOOM_DOCTOR = "/usr/bin/droidloom-doctor"
CGROUP_DROPIN_PATH = Path("/etc/systemd/system/droidloomd.service.d/90-droidloom-resources.conf")


def _run(
    args: list[str], timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def systemctl_user(args: list[str], timeout: float = 15.0) -> tuple[bool, str]:
    try:
        res = _run(["systemctl", "--user", *args], timeout=timeout)
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def systemctl_system_query(unit: str) -> dict[str, str]:
    try:
        res = _run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,UnitFileState",
            ],
            timeout=5.0,
        )
        info: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return info
    except Exception:
        return {}


def systemctl_user_query(unit: str) -> dict[str, str]:
    try:
        res = _run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,UnitFileState",
            ],
            timeout=5.0,
        )
        info: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return info
    except Exception:
        return {}


def set_droidloom_autostart(enabled: bool) -> tuple[bool, str]:
    return systemctl_user(["enable" if enabled else "disable", "droidloom.service"])


def run_droidloomctl(args: list[str], timeout: float = 180.0) -> tuple[bool, str]:
    try:
        res = _run([DROIDLOOMCTL, *args], timeout=timeout)
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def start_droidloom(mode: str | None = None) -> tuple[bool, str]:
    cmd = ["start"]
    if mode in ("desktop", "mobile"):
        save_session_mode(mode)
        cmd.extend(["--mode", mode])
    return run_droidloomctl(cmd, timeout=180.0)


def stop_droidloom() -> tuple[bool, str]:
    return run_droidloomctl(["stop"], timeout=60.0)


def restart_droidloom(mode: str | None = None) -> tuple[bool, str]:
    cmd = ["restart"]
    if mode in ("desktop", "mobile"):
        save_session_mode(mode)
        cmd.extend(["--mode", mode])
    return run_droidloomctl(cmd, timeout=180.0)


def backup_android_storage(dest_file: str) -> tuple[bool, str]:
    return run_droidloomctl(["backup", dest_file], timeout=3600.0)


def restore_android_storage(src_file: str) -> tuple[bool, str]:
    try:
        res = _run(["pkexec", DROIDLOOMCTL, "restore", src_file], timeout=3600.0)
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def run_doctor() -> dict[str, Any]:
    exe = DROIDLOOM_DOCTOR if Path(DROIDLOOM_DOCTOR).is_file() else shutil.which("droidloom-doctor")
    if not exe:
        return {"error": "未找到 droidloom-doctor 可执行文件"}
    try:
        res = _run([exe, "--format", "json"], timeout=15.0)
        if res.stdout.strip():
            return json.loads(res.stdout)
        return {"error": res.stderr.strip() or f"退出码 {res.returncode}"}
    except Exception as exc:
        return {"error": str(exc)}


def read_systemd_journal(user_unit: str | None = None, system_unit: str | None = None, lines: int = 120) -> str:
    args = ["journalctl", "-n", str(max(10, min(1000, lines))), "--no-pager"]
    if user_unit:
        args.extend(["--user", "-u", user_unit])
    elif system_unit:
        args.extend(["-u", system_unit])
    try:
        res = _run(args, timeout=10.0)
        return res.stdout or res.stderr or "(无日志)"
    except Exception as exc:
        return f"读取 journal 失败: {exc}"


def detect_host_proxy_candidates() -> list[dict[str, Any]]:
    """Detect common local proxy ports listening on host via /proc/net/tcp* and ss."""
    known_ports = {
        7897: "Clash Verge / Mihomo",
        7890: "Clash / Mihomo",
        7891: "Clash SOCKS",
        10808: "v2rayN / Xray SOCKS",
        10809: "v2rayN / Xray HTTP",
        20171: "v2rayA",
        1080: "SOCKS5",
        8080: "HTTP Proxy",
        8118: "Privoxy",
    }
    listening_ports: dict[int, str] = {}
    ss_bin = shutil.which("ss")
    if ss_bin:
        try:
            res = _run([ss_bin, "-tlnH"], timeout=4.0)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    cols = line.split()
                    if len(cols) >= 4:
                        addr = cols[3]
                        if ":" in addr:
                            port_str = addr.rsplit(":", 1)[1]
                            if port_str.isdigit():
                                p = int(port_str)
                                listening_ports[p] = addr
        except Exception:
            pass
    # Also probe known ports directly on 127.0.0.1 in case ss omits something
    found: list[dict[str, Any]] = []
    for port, label in known_ports.items():
        active = port in listening_ports
        if not active:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            try:
                active = s.connect_ex(("127.0.0.1", port)) == 0
            except OSError:
                active = False
            finally:
                s.close()
        if active:
            found.append(
                {
                    "port": port,
                    "label": label,
                    "addr": listening_ports.get(port, f"127.0.0.1:{port}"),
                }
            )
    return found


def check_host_ipv4_forwarding() -> bool | None:
    try:
        val = Path("/proc/sys/net/ipv4/ip_forward").read_text(encoding="utf-8").strip()
        return val == "1"
    except OSError:
        return None


def check_firewalld_droidloom_status() -> str:
    """Inspect firewalld state without invoking firewall-cmd (which triggers Polkit auth)."""
    info = systemctl_system_query("firewalld.service")
    active = info.get("ActiveState", "")
    load = info.get("LoadState", "")
    if load == "not-found" or not info:
        return "未安装"
    if active != "active":
        return f"未运行 ({active or 'inactive'})"
    zone = Path("/usr/lib/firewalld/zones/droidloom.xml").is_file()
    pol = Path("/usr/lib/firewalld/policies/droidloom-egress.xml").is_file()
    if zone and pol:
        return "运行中（已配置放行策略）"
    return "运行中（未检测到 droidloom zone/policy）"


def list_host_bridges() -> list[str]:
    """List existing Linux bridge interfaces on the host."""
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return []
    bridges: list[str] = []
    for entry in sorted(net_dir.iterdir(), key=lambda p: p.name):
        if (entry / "bridge").is_dir():
            bridges.append(entry.name)
    return bridges


def list_host_network_interfaces(net_root: Path = Path("/sys/class/net")) -> list[dict[str, Any]]:
    """Enumerate usable host network interfaces and bridges via read-only sysfs."""
    if not net_root.is_dir():
        return []
    kind_rank = {"bridge": 0, "ethernet": 1, "wifi": 2, "other": 3}
    items: list[dict[str, Any]] = []
    for entry in sorted(net_root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name == "lo" or name.startswith(("dlh", "ms")):
            continue
        is_bridge = (entry / "bridge").is_dir()
        if not is_bridge:
            try:
                flags_val = int((entry / "flags").read_text(encoding="utf-8").strip(), 0)
                if flags_val & 0x10:  # IFF_POINTOPOINT (e.g. tailscale0/tun/wg)
                    continue
            except (OSError, ValueError):
                pass
        try:
            operstate = (entry / "operstate").read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            operstate = "unknown"

        if is_bridge:
            kind = "bridge"
        elif (entry / "wireless").is_dir():
            kind = "wifi"
        elif (entry / "device").exists():
            kind = "ethernet"
        else:
            kind = "other"

        items.append(
            {
                "name": name,
                "kind": kind,
                "state": operstate,
                "bridge_ready": is_bridge,
            }
        )

    items.sort(
        key=lambda x: (
            kind_rank.get(x["kind"], 9),
            0 if x["state"] == "up" else 1,
            x["name"],
        )
    )
    return items


def apply_live_nftables_rules(
    uid: int,
    host_ip: str,
    cell_ip: str,
    offline_mode: bool,
    allow_host_ports: list[dict[str, Any]],
    port_forwards: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Apply live nftables rules for the running cell without requiring droidloomd restart."""
    filter_table = f"droidloom_u{uid}"
    nat_table = f"droidloom_u{uid}_nat"
    host_iface = f"dlh{uid}"
    comment_tag = "droidloom-settings"

    lines: list[str] = [
        "set -euo pipefail",
        # Clean previous droidloom-settings tagged rules from filter_table and nat_table
        f"""for tbl in "inet {filter_table}" "ip {nat_table}"; do
  if nft list table $tbl >/dev/null 2>&1; then
    nft -a list table $tbl | awk '/{comment_tag}/ {{for(i=1;i<=NF;i++) if($i=="handle") print $(i+1)}}' | while read -r h; do
      chain=$(nft -a list table $tbl | awk -v h="$h" '/chain / {{c=$2}} /handle '"$h"'$/ {{print c; exit}}')
      [ -n "$chain" ] && nft delete rule $tbl "$chain" handle "$h" || true
    done
  fi
done""",
    ]

    nft_cmds: list[str] = []
    # Allow host ports at top of input chain so they come before `iifname "dlh..." drop`
    for entry in allow_host_ports:
        proto = str(entry.get("protocol", "tcp")).lower()
        if proto not in ("tcp", "udp"):
            continue
        port = int(entry.get("port", 0))
        if not (1 <= port <= 65535):
            continue
        nft_cmds.append(
            f'insert rule inet {filter_table} input iifname "{host_iface}" ip saddr {cell_ip}/32 ip daddr {host_ip}/32 {proto} dport {port} accept comment "{comment_tag}"'
        )
        # Also allow loopback proxy listeners if host proxy listens only on 127.0.0.1? Note: if proxy binds 127.0.0.1 only, user needs LAN/0.0.0.0 or DNAT; we also handle route_localnet when port is explicitly allowed? Keep safe: insert accept rule on host_ip.

    if offline_mode:
        nft_cmds.append(
            f'insert rule inet {filter_table} forward iifname "{host_iface}" ip saddr {cell_ip}/32 drop comment "{comment_tag}"'
        )

    if port_forwards:
        for ch, hook in (("prerouting", "prerouting"), ("output", "output")):
            nft_cmds.append(
                f"add chain ip {nat_table} {ch} {{ type nat hook {hook} priority dstnat; policy accept; }}"
            )
        for fw in port_forwards:
            proto = str(fw.get("protocol", "tcp")).lower()
            hp = int(fw.get("host_port", 0))
            cp = int(fw.get("cell_port", 0))
            bind = str(fw.get("bind_address") or "127.0.0.1").strip()
            if proto not in ("tcp", "udp") or not (1 <= hp <= 65535) or not (1 <= cp <= 65535):
                continue
            nft_cmds.append(
                f'insert rule inet {filter_table} forward oifname "{host_iface}" ip daddr {cell_ip}/32 {proto} dport {cp} ct state new accept comment "{comment_tag}"'
            )
            for ch in ("prerouting", "output"):
                nft_cmds.append(
                    f'add rule ip {nat_table} {ch} ip daddr {bind} {proto} dport {hp} dnat to {cell_ip}:{cp} comment "{comment_tag}"'
                )
        nft_cmds.append(
            f'add rule ip {nat_table} postrouting oifname "{host_iface}" ip daddr {cell_ip}/32 masquerade comment "{comment_tag}"'
        )

    if nft_cmds:
        nft_script = "\n".join(nft_cmds) + "\n"
        lines.append(
            f"if nft list table inet {filter_table} >/dev/null 2>&1; then\n"
            f"cat <<'EOF_NFT' | nft -f -\n{nft_script}EOF_NFT\n"
            f"else\n"
            f"echo 'Cell network table is not active (Android stopped?)' >&2; exit 2\n"
            f"fi"
        )

    script = "\n".join(lines)
    try:
        res = _run(["pkexec", "/bin/sh", "-c", script], timeout=60.0)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            return False, msg or f"退出码 {res.returncode}"
        return True, "实时 nftables 规则已应用到运行中的网络命名空间"
    except Exception as exc:
        return False, str(exc)


@dataclass
class ResourceLimits:
    cpu_quota_percent: int | None = None  # e.g., 400 (%)
    allowed_cpus: str = ""  # e.g., "0-5"
    cpu_weight: int | None = None  # 1..10000
    memory_high_mb: int | None = None
    memory_max_mb: int | None = None
    memory_swap_max_mb: int | None = None
    tasks_max: int | None = None
    io_weight: int | None = None  # 1..10000


def load_resource_limits(path: Path = CGROUP_DROPIN_PATH) -> ResourceLimits:
    limits = ResourceLimits()
    if not path.is_file():
        return limits
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return limits
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "CPUQuota" and v.endswith("%") and v[:-1].isdigit():
            limits.cpu_quota_percent = int(v[:-1])
        elif k == "AllowedCPUs":
            limits.allowed_cpus = v
        elif k == "CPUWeight" and v.isdigit():
            limits.cpu_weight = int(v)
        elif k == "MemoryHigh" and v.endswith("M") and v[:-1].isdigit():
            limits.memory_high_mb = int(v[:-1])
        elif k == "MemoryMax" and v.endswith("M") and v[:-1].isdigit():
            limits.memory_max_mb = int(v[:-1])
        elif k == "MemorySwapMax" and v.endswith("M") and v[:-1].isdigit():
            limits.memory_swap_max_mb = int(v[:-1])
        elif k == "TasksMax" and v.isdigit():
            limits.tasks_max = int(v)
        elif k == "IOWeight" and v.isdigit():
            limits.io_weight = int(v)
    return limits


def save_resource_limits(limits: ResourceLimits) -> tuple[bool, str]:
    lines = ["[Service]"]
    props: list[str] = []

    if limits.cpu_quota_percent and limits.cpu_quota_percent > 0:
        lines.append(f"CPUQuota={limits.cpu_quota_percent}%")
        props.append(f"CPUQuota={limits.cpu_quota_percent}%")
    else:
        props.append("CPUQuota=")

    if limits.allowed_cpus.strip():
        cpus = limits.allowed_cpus.strip()
        if not re.fullmatch(r"[0-9,\-]+", cpus):
            return False, "AllowedCPUs 格式无效（示例：0-3 或 0,2,4）"
        lines.append(f"AllowedCPUs={cpus}")
        props.append(f"AllowedCPUs={cpus}")
    else:
        props.append("AllowedCPUs=")

    if limits.cpu_weight and 1 <= limits.cpu_weight <= 10000:
        lines.append(f"CPUWeight={limits.cpu_weight}")
        props.append(f"CPUWeight={limits.cpu_weight}")
    else:
        props.append("CPUWeight=")

    for attr, key in (
        ("memory_high_mb", "MemoryHigh"),
        ("memory_max_mb", "MemoryMax"),
        ("memory_swap_max_mb", "MemorySwapMax"),
    ):
        val = getattr(limits, attr)
        if val is not None and val > 0:
            lines.append(f"{key}={val}M")
            props.append(f"{key}={val}M")
        else:
            props.append(f"{key}=infinity")

    if limits.tasks_max and limits.tasks_max > 0:
        lines.append(f"TasksMax={limits.tasks_max}")
        props.append(f"TasksMax={limits.tasks_max}")
    else:
        props.append("TasksMax=infinity")

    if limits.io_weight and 1 <= limits.io_weight <= 10000:
        lines.append(f"IOWeight={limits.io_weight}")
        props.append(f"IOWeight={limits.io_weight}")
    else:
        props.append("IOWeight=")

    has_any = len(lines) > 1
    tmp_path: Path | None = None
    try:
        dropin_dir = CGROUP_DROPIN_PATH.parent
        cmds = ["set -euo pipefail"]
        if has_any:
            fd, tmp = tempfile.mkstemp(prefix="droidloom-res-", suffix=".conf")
            tmp_path = Path(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            cmds.extend(
                [
                    f"mkdir -p {shlex.quote(str(dropin_dir))}",
                    f"install -m 0644 -o root -g root {shlex.quote(str(tmp_path))} {shlex.quote(str(CGROUP_DROPIN_PATH))}",
                ]
            )
        else:
            cmds.append(f"rm -f {shlex.quote(str(CGROUP_DROPIN_PATH))}")

        cmds.append("systemctl daemon-reload")
        prop_args = " ".join(shlex.quote(p) for p in props)
        cmds.append(f"systemctl set-property droidloomd.service {prop_args} || true")

        res = _run(["pkexec", "/bin/sh", "-c", " ; ".join(cmds)], timeout=60.0)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            return False, msg or f"退出码 {res.returncode}"
        return True, "资源限制已持久化到 systemd 并即时应用到 droidloomd.service"
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def list_pipewire_sinks() -> list[dict[str, str]]:
    """List PipeWire audio output sinks using pactl or pw-dump."""
    sinks: list[dict[str, str]] = []
    pactl = shutil.which("pactl")
    if pactl:
        try:
            res = _run([pactl, "-f", "json", "list", "sinks"], timeout=4.0)
            if res.returncode == 0 and res.stdout.strip():
                for item in json.loads(res.stdout):
                    name = str(item.get("name", ""))
                    desc = str(item.get("description") or name)
                    if name:
                        sinks.append({"name": name, "description": desc})
                return sinks
        except Exception:
            pass
    pw_dump = shutil.which("pw-dump")
    if pw_dump:
        try:
            res = _run([pw_dump, "Node"], timeout=4.0)
            if res.returncode == 0 and res.stdout.strip():
                for item in json.loads(res.stdout):
                    props = (item.get("info") or {}).get("props") or {}
                    if props.get("media.class") == "Audio/Sink":
                        name = str(props.get("node.name", ""))
                        desc = str(props.get("node.description") or name)
                        if name:
                            sinks.append({"name": name, "description": desc})
        except Exception:
            pass
    return sinks


def control_system_service_privileged(unit: str, action: str) -> tuple[bool, str]:
    if action not in ("start", "stop", "restart"):
        return False, "不支持的服务操作"
    try:
        res = _run(["pkexec", "systemctl", action, unit], timeout=60.0)
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out or f"已执行 {action} {unit}"
    except Exception as exc:
        return False, str(exc)


def list_braun_cameras() -> list[dict[str, Any]]:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return []
    try:
        res = _run(
            [
                gdbus,
                "call",
                "--system",
                "--dest",
                "org.braun",
                "--object-path",
                "/org/braun",
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.braun.Camera1",
                "Cameras",
            ],
            timeout=4.0,
        )
        if res.returncode != 0:
            return []
        text = res.stdout.strip()
        # Parse simple fields like 'name': <'UVC Camera'>
        entries: list[dict[str, Any]] = []
        for block in re.findall(r"\{([^}]+)\}", text):
            item: dict[str, Any] = {}
            for key in ("id", "name", "facing"):
                m = re.search(rf"'{key}':\s*<'([^']*)'>", block)
                if m:
                    item[key] = m.group(1)
            for key in ("width", "height"):
                m = re.search(rf"'{key}':\s*<(?:uint32\s+)?(\d+)>", block)
                if m:
                    item[key] = int(m.group(1))
            if item:
                entries.append(item)
        return entries
    except Exception:
        return []
