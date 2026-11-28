"""Tests for solid-color multi-size icons, HiDPI loading, and .apk MIME icon association."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets

from droidloom_settings.backend.mime_assoc import (
    APK_ICON_NAME,
    APK_MIME,
    _refresh_desktop_caches,
    install_apk_mime_icon,
    is_apk_mime_icon_installed,
    remove_apk_mime_icon,
)
from droidloom_settings.ui.icons import (
    ICON_SIZES,
    build_multisize_icon,
    load_app_icon,
    load_brand_pixmap,
)


def _get_app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["test"])
    return app


class IconsAndMimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _get_app()

    def test_svg_has_solid_background_and_explicit_dimensions(self) -> None:
        svg_path = ROOT / "assets" / f"{APK_ICON_NAME}.svg"
        self.assertTrue(svg_path.is_file(), f"Missing SVG: {svg_path}")
        raw = svg_path.read_text(encoding="utf-8")
        self.assertNotIn("linearGradient", raw)
        self.assertNotIn("radialGradient", raw)
        self.assertNotIn("url(#", raw)

        root = ET.fromstring(raw)
        self.assertEqual(root.attrib.get("width"), "128")
        self.assertEqual(root.attrib.get("height"), "128")
        self.assertEqual(root.attrib.get("viewBox"), "0 0 128 128")

        rects = [
            el for el in root.iter()
            if el.tag.endswith("}rect") or el.tag == "rect"
        ]
        self.assertGreaterEqual(len(rects), 1)
        self.assertEqual(rects[0].attrib.get("fill"), "#1e88e5")

    def test_generated_png_icons_exist_and_are_crisp_solid_color(self) -> None:
        hicolor = ROOT / "assets" / "icons" / "hicolor"
        for size in ICON_SIZES:
            png_path = hicolor / f"{size}x{size}" / "apps" / f"{APK_ICON_NAME}.png"
            self.assertTrue(png_path.is_file(), f"Missing generated PNG: {png_path}")
            img = QtGui.QImage(str(png_path))
            self.assertFalse(img.isNull(), f"Null QImage for {png_path}")
            self.assertEqual(img.width(), size)
            self.assertEqual(img.height(), size)

        img_128 = QtGui.QImage(
            str(hicolor / "128x128" / "apps" / f"{APK_ICON_NAME}.png")
        )
        tl = img_128.pixelColor(18, 18)
        br = img_128.pixelColor(110, 110)
        corner = img_128.pixelColor(1, 1)

        self.assertEqual((tl.red(), tl.green(), tl.blue(), tl.alpha()), (0x1E, 0x88, 0xE5, 255))
        self.assertEqual((br.red(), br.green(), br.blue(), br.alpha()), (0x1E, 0x88, 0xE5, 255))
        self.assertEqual(corner.alpha(), 0)

    def test_load_app_icon_and_brand_pixmap(self) -> None:
        multisize = build_multisize_icon()
        self.assertFalse(multisize.isNull())
        avail = {(s.width(), s.height()) for s in multisize.availableSizes()}
        for expected in (16, 64, 128, 256, 512):
            self.assertIn((expected, expected), avail)

        app_icon = load_app_icon()
        self.assertFalse(app_icon.isNull())

        pm = load_brand_pixmap(20, 2.5)
        self.assertFalse(pm.isNull())
        self.assertGreaterEqual(pm.width(), 40)

    def test_apk_mime_icon_override_install_query_and_remove(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dl_mime_unit_") as tmp:
            tmp_path = Path(tmp)
            ok, msg = install_apk_mime_icon(tmp_path)
            self.assertTrue(ok, msg)
            self.assertTrue(is_apk_mime_icon_installed(tmp_path))

            probe_code = (
                "from PySide6.QtCore import QMimeDatabase\n"
                "db = QMimeDatabase()\n"
                "mt = db.mimeTypeForFile('sample.apk', QMimeDatabase.MatchMode.MatchExtension)\n"
                "print(mt.name())\n"
                "print(mt.iconName())\n"
                "print(','.join(mt.suffixes()))\n"
            )
            env = os.environ.copy()
            env["XDG_DATA_HOME"] = str(tmp_path)
            res = subprocess.run(
                [sys.executable, "-c", probe_code],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            self.assertEqual(lines[0], APK_MIME)
            self.assertEqual(lines[1], APK_ICON_NAME)
            self.assertIn("apk", lines[2].split(","))

            rem_ok, rem_msg = remove_apk_mime_icon(tmp_path)
            self.assertTrue(rem_ok, rem_msg)
            self.assertFalse(is_apk_mime_icon_installed(tmp_path))

    def test_refresh_desktop_caches_preserves_host_kde_icon_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dl_kcache_") as tmp:
            tmp_path = Path(tmp)
            cache_dir = tmp_path / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            kcache = cache_dir / "icon-cache.kcache"
            kcache.write_bytes(b"host-plasma-cache")
            old_cache = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = str(cache_dir)
            try:
                _refresh_desktop_caches(tmp_path / "data", clear_kde_icon_cache=True)
            finally:
                if old_cache is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = old_cache
            self.assertTrue(kcache.is_file())
            self.assertEqual(kcache.read_bytes(), b"host-plasma-cache")


if __name__ == "__main__":
    unittest.main()
