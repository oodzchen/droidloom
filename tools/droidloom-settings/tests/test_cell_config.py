"""Unit tests for cell_config helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from droidloom_settings.backend.cell_config import (
    ExtensionsConfig,
    list_host_gpus,
    load_extensions_config,
    save_extensions_sidecar,
    validate_shared_directory_entry,
)


class CellConfigTests(unittest.TestCase):
    def test_list_host_gpus_from_mock_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drm = root / "drm"
            drm.mkdir()
            # PCI NVIDIA GPU
            drv_nv = root / "drivers/nvidia"
            drv_nv.mkdir(parents=True)
            pci_nv = root / "pci/0000:04:00.0"
            pci_nv.mkdir(parents=True)
            (pci_nv / "driver").symlink_to(drv_nv)
            node0 = drm / "renderD128"
            node0.mkdir()
            (node0 / "device").symlink_to(pci_nv)

            # Platform Qualcomm Adreno GPU
            drv_msm = root / "drivers/msm_dpu"
            drv_msm.mkdir(parents=True)
            plat = root / "platform/ae01000.display-controller"
            plat.mkdir(parents=True)
            (plat / "driver").symlink_to(drv_msm)
            node1 = drm / "renderD129"
            node1.mkdir()
            (node1 / "device").symlink_to(plat)

            gpus = list_host_gpus(drm)
            self.assertEqual(len(gpus), 2)
            self.assertEqual(gpus[0].vendor, "NVIDIA")
            self.assertEqual(gpus[0].device, "0000:04:00.0")
            self.assertEqual(gpus[1].vendor, "Qualcomm")
            self.assertEqual(gpus[1].render_node, "/dev/dri/renderD129")

    def test_validate_shared_directory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(validate_shared_directory_entry(tmp, "MyFiles", os.getgid()))
            self.assertIsNotNone(validate_shared_directory_entry(tmp, "Android", os.getgid()))
            self.assertIsNotNone(validate_shared_directory_entry(tmp, "bad/name", os.getgid()))
            self.assertIsNotNone(
                validate_shared_directory_entry("relative/dir", "OkName", os.getgid())
            )

    def test_extensions_sidecar_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            ext = ExtensionsConfig(
                network={"mode": "offline", "dns": ["9.9.9.9"]},
                device_identity={"model": "TestPad"},
                proxy_bridge_enabled=True,
                proxy_bridge_port=7897,
            )
            save_extensions_sidecar(ext, config_dir=cfg_dir)
            loaded = load_extensions_config({}, config_dir=cfg_dir)
            self.assertEqual(loaded.network["mode"], "offline")
            self.assertEqual(loaded.device_identity["model"], "TestPad")
            self.assertTrue(loaded.proxy_bridge_enabled)
            self.assertEqual(loaded.proxy_bridge_port, 7897)


if __name__ == "__main__":
    unittest.main()
