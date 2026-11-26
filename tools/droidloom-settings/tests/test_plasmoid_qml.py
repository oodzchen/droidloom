"""Offscreen QML compilation & metadata validation for the Plasma 6 Control Center Plasmoid."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine

PLASMOID_DIR = Path(__file__).resolve().parents[1] / "plasmoid"


class PlasmoidQmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication(["qml-test"])
        cls.engine = QQmlEngine()
        for qml_dir in ("/usr/lib64/qt6/qml", "/usr/lib/qt6/qml"):
            if Path(qml_dir).is_dir():
                cls.engine.addImportPath(qml_dir)

    def test_metadata_json_is_valid_plasma6_applet(self) -> None:
        meta = json.loads((PLASMOID_DIR / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["KPackageStructure"], "Plasma/Applet")
        self.assertEqual(meta["KPlugin"]["Id"], "org.droidloom.ControlCenter")
        self.assertEqual(meta["X-Plasma-API-Minimum-Version"], "6.0")
        self.assertEqual(meta["X-Plasma-MainScript"], "ui/main.qml")
        self.assertNotIn("X-Plasma-NotificationArea", meta)
        main_xml = (PLASMOID_DIR / "contents/config/main.xml").read_text(encoding="utf-8")
        self.assertIn('name="quickTiles"', main_xml)
        self.assertIn('name="quickTilesExpanded"', main_xml)
        self.assertIn('name="showNotificationBadge"', main_xml)
        svg = (PLASMOID_DIR / "contents/icons/droidloom-symbolic.svg").read_text(encoding="utf-8")
        self.assertIn('fill-rule="evenodd"', svg)

    def test_all_qml_files_compile_without_errors(self) -> None:
        qml_files = sorted(PLASMOID_DIR.rglob("*.qml"))
        self.assertGreaterEqual(len(qml_files), 8)
        for qml_path in qml_files:
            comp = QQmlComponent(self.engine, QUrl.fromLocalFile(str(qml_path)))
            if comp.isError():
                errs = "\n".join(e.toString() for e in comp.errors())
                self.fail(f"QML compile failed for {qml_path.name}:\n{errs}")
            self.assertEqual(
                comp.status(),
                QQmlComponent.Status.Ready,
                f"{qml_path.name} not Ready: {[e.toString() for e in comp.errors()]}",
            )


if __name__ == "__main__":
    unittest.main()
