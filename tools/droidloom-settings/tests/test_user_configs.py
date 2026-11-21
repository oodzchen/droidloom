"""Unit tests for user_configs and orientation mapping."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from droidloom_settings.backend.orientation_daemon import map_orientation_to_rotation
from droidloom_settings.backend.user_configs import (
    DensityConfig,
    OrientationConfig,
    WindowPolicyData,
    WindowPreference,
    get_desktop_launch_resolution,
    load_density_config,
    load_orientation_config,
    load_window_policy,
    save_density_config,
    save_orientation_config,
    save_window_policy,
    set_desktop_launch_resolution,
)


class UserConfigsTests(unittest.TestCase):
    def test_window_policy_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            pol = WindowPolicyData(
                default=WindowPreference(mode="windowed", width=960, height=680),
                applications={
                    "org.mozilla.firefox": WindowPreference(mode="fit_output", width=None, height=None)
                },
            )
            save_window_policy(pol, config_dir=d)
            loaded = load_window_policy(config_dir=d)
            self.assertEqual(loaded.default.width, 960)
            self.assertEqual(loaded.default.height, 680)
            self.assertEqual(loaded.applications["org.mozilla.firefox"].mode, "fit_output")

    def test_density_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            cfg = DensityConfig(dpi_per_scale=160.0, outputs={"DP-1": 200.0})
            save_density_config(cfg, config_dir=d)
            loaded = load_density_config(config_dir=d)
            self.assertEqual(loaded.dpi_per_scale, 160.0)
            self.assertEqual(loaded.outputs, {"DP-1": 200.0})

    def test_desktop_resolution_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apps = Path(tmp)
            desktop = apps / "droidloom-test.desktop"
            desktop.write_text(
                "[Desktop Entry]\n"
                "Name=Test\n"
                "Exec=/usr/bin/droidloomctl launch com.example.game --component com.example.game/.Main\n"
                "StartupWMClass=com.example.game\n",
                encoding="utf-8",
            )
            self.assertIsNone(get_desktop_launch_resolution("com.example.game", apps_dir=apps))
            self.assertTrue(
                set_desktop_launch_resolution("com.example.game", "2560x1440", apps_dir=apps)
            )
            self.assertEqual(
                get_desktop_launch_resolution("com.example.game", apps_dir=apps), "2560x1440"
            )
            content = desktop.read_text(encoding="utf-8")
            self.assertIn("--resolution 2560x1440", content)
            self.assertIn("X-Droidloom-Resolution=2560x1440", content)

            self.assertTrue(set_desktop_launch_resolution("com.example.game", None, apps_dir=apps))
            self.assertIsNone(get_desktop_launch_resolution("com.example.game", apps_dir=apps))
            self.assertNotIn("--resolution", desktop.read_text(encoding="utf-8"))

    def test_orientation_config_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            cfg = OrientationConfig(
                follow_host_gravity=True, rotation_offset=1, force_fixed_to_user_rotation=True
            )
            save_orientation_config(cfg, config_dir=d)
            loaded = load_orientation_config(config_dir=d)
            self.assertTrue(loaded.follow_host_gravity)
            self.assertEqual(loaded.rotation_offset, 1)

        self.assertEqual(map_orientation_to_rotation("normal", 0), 0)
        self.assertEqual(map_orientation_to_rotation("right-up", 1), 2)
        self.assertIsNone(map_orientation_to_rotation("undefined", 0))


if __name__ == "__main__":
    unittest.main()
