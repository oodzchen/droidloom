"""Unit tests for host_runtime helpers (no Polkit / firewall-cmd on query)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from droidloom_settings.backend import host_runtime


class HostRuntimeTests(unittest.TestCase):
    def test_firewalld_check_never_invokes_firewall_cmd(self) -> None:
        with (
            mock.patch.object(
                host_runtime,
                "systemctl_system_query",
                return_value={"LoadState": "loaded", "ActiveState": "active"},
            ),
            mock.patch.object(host_runtime, "_run", side_effect=AssertionError("must not exec")),
        ):
            status = host_runtime.check_firewalld_droidloom_status()
            self.assertIn("运行中", status)

    def test_list_host_bridges_returns_list(self) -> None:
        bridges = host_runtime.list_host_bridges()
        self.assertIsInstance(bridges, list)

    def test_list_host_network_interfaces_classifies_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def _mk_iface(
                name: str,
                *,
                state: str = "up",
                flags: str = "0x1003",
                bridge: bool = False,
                wireless: bool = False,
                pci: bool = False,
            ) -> None:
                d = root / name
                d.mkdir()
                (d / "operstate").write_text(state, encoding="utf-8")
                (d / "flags").write_text(flags, encoding="utf-8")
                if bridge:
                    (d / "bridge").mkdir()
                if wireless:
                    (d / "wireless").mkdir()
                if pci:
                    (d / "device").mkdir()

            _mk_iface("lo", state="unknown", flags="0x9")
            _mk_iface("dlh1000", state="up", pci=False)
            _mk_iface("ms1000", state="unknown", flags="0x1091")
            _mk_iface("tailscale0", state="unknown", flags="0x1091")
            _mk_iface("wlp1s0", state="up", wireless=True, pci=True)
            _mk_iface("enp2s0", state="down", pci=True)
            _mk_iface("br0", state="up", bridge=True)

            ifaces = host_runtime.list_host_network_interfaces(net_root=root)
            self.assertEqual([x["name"] for x in ifaces], ["br0", "enp2s0", "wlp1s0"])
            self.assertEqual(ifaces[0]["kind"], "bridge")
            self.assertTrue(ifaces[0]["bridge_ready"])
            self.assertEqual(ifaces[1]["kind"], "ethernet")
            self.assertFalse(ifaces[1]["bridge_ready"])
            self.assertEqual(ifaces[2]["kind"], "wifi")
            self.assertFalse(ifaces[2]["bridge_ready"])

    def test_build_resource_limits_dropin_preserves_unit_defaults(self) -> None:
        ok, content, err = host_runtime.build_resource_limits_dropin(host_runtime.ResourceLimits())
        self.assertTrue(ok)
        self.assertIsNone(content)
        self.assertEqual(err, "")

        ok, content, err = host_runtime.build_resource_limits_dropin(
            host_runtime.ResourceLimits(
                cpu_quota_percent=400,
                allowed_cpus="0-3",
                memory_high_mb=8192,
                memory_max_mb=12288,
                tasks_max=4096,
            )
        )
        self.assertTrue(ok)
        assert content is not None
        self.assertIn("CPUQuota=400%", content)
        self.assertIn("AllowedCPUs=0-3", content)
        self.assertIn("MemoryHigh=8192M", content)
        self.assertIn("MemoryMax=12288M", content)
        self.assertIn("TasksMax=4096", content)

        ok, _content, err = host_runtime.build_resource_limits_dropin(
            host_runtime.ResourceLimits(allowed_cpus="bad;cmd")
        )
        self.assertFalse(ok)
        self.assertTrue(bool(err))

    def test_parse_effective_resource_limits(self) -> None:
        parsed = host_runtime.parse_effective_resource_limits(
            {
                "MemoryHigh": "13276307456",
                "MemoryMax": "16595382272",
                "MemorySwapMax": "infinity",
                "TasksMax": "8192",
                "CPUQuotaPerSecUSec": "4s",
                "AllowedCPUs": "",
                "CPUWeight": "[not set]",
                "IOWeight": "[not set]",
            }
        )
        self.assertEqual(parsed.memory_high_mb, 12661)
        self.assertEqual(parsed.memory_max_mb, 15827)
        self.assertIsNone(parsed.memory_swap_max_mb)
        self.assertEqual(parsed.tasks_max, 8192)
        self.assertEqual(parsed.cpu_quota_percent, 400)
        self.assertEqual(parsed.cpu_weight, 100)
        self.assertEqual(parsed.io_weight, 100)

        inf_parsed = host_runtime.parse_effective_resource_limits(
            {"CPUQuotaPerSecUSec": "infinity", "TasksMax": "infinity"}
        )
        self.assertIsNone(inf_parsed.cpu_quota_percent)
        self.assertIsNone(inf_parsed.tasks_max)


if __name__ == "__main__":
    unittest.main()
