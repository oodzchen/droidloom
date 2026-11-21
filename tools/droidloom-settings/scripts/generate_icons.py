#!/usr/bin/env python3
"""Render multi-size high-resolution PNG icons from org.droidloom.Settings.svg."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

ICON_NAME = "org.droidloom.Settings"
ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512)


def generate_icons(
    svg_path: Path,
    out_hicolor_dir: Path,
    sizes: tuple[int, ...] = ICON_SIZES,
) -> list[Path]:
    """Render crisp transparent RGBA PNG icons at all requested pixel sizes."""
    if not svg_path.is_file():
        raise FileNotFoundError(f"Missing SVG source icon: {svg_path}")

    app = QtWidgets.QApplication.instance() or QtGui.QGuiApplication.instance()
    if app is None:
        app = QtGui.QGuiApplication(["generate-icons"])

    renderer = QtSvg.QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG icon: {svg_path}")

    written: list[Path] = []
    for size in sizes:
        image = QtGui.QImage(
            size,
            size,
            QtGui.QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter, QtCore.QRectF(0.0, 0.0, float(size), float(size)))
        painter.end()

        out_path = out_hicolor_dir / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(out_path), "PNG"):
            raise OSError(f"Failed to save PNG icon: {out_path}")
        written.append(out_path)
    return written


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    svg_path = root / "assets" / f"{ICON_NAME}.svg"
    out_dir = root / "assets" / "icons" / "hicolor"
    if argv and len(argv) >= 2:
        svg_path = Path(argv[0])
        out_dir = Path(argv[1])
    written = generate_icons(svg_path, out_dir)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
