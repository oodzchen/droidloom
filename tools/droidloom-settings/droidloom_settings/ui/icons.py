"""HiDPI-aware multi-size application icon helpers."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui

from ..backend.mime_assoc import APK_ICON_NAME, xdg_data_home

APP_ICON_NAME = APK_ICON_NAME
ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512)


def _icon_search_roots() -> list[Path]:
    candidates = [
        xdg_data_home() / "icons/hicolor",
        Path(__file__).resolve().parents[2] / "assets/icons/hicolor",
    ]
    roots: list[Path] = []
    for candidate in candidates:
        if candidate not in roots:
            roots.append(candidate)
    return roots


def build_multisize_icon(icon_name: str = APP_ICON_NAME) -> QtGui.QIcon:
    """Build a QIcon backed by all available fixed-size PNGs (with SVG fallback)."""
    icon = QtGui.QIcon()
    roots = _icon_search_roots()
    found_png = False
    for size in ICON_SIZES:
        for root in roots:
            png_path = root / f"{size}x{size}" / "apps" / f"{icon_name}.png"
            if png_path.is_file():
                icon.addFile(str(png_path), QtCore.QSize(size, size))
                found_png = True
                break

    if not found_png:
        svg_candidates = [
            *(root / "scalable" / "apps" / f"{icon_name}.svg" for root in roots),
            Path(__file__).resolve().parents[2] / "assets" / f"{icon_name}.svg",
        ]
        for svg_path in svg_candidates:
            if svg_path.is_file():
                icon.addFile(str(svg_path))
                break
    return icon


def load_app_icon(icon_name: str = APP_ICON_NAME) -> QtGui.QIcon:
    """Load the themed icon (preserving icon name for Wayland) with multi-size PNG fallback."""
    fallback = build_multisize_icon(icon_name)
    themed = QtGui.QIcon.fromTheme(icon_name, fallback)
    if themed.isNull():
        return fallback
    return themed


def load_brand_pixmap(
    size: int,
    dpr: float = 1.0,
    icon_name: str = APP_ICON_NAME,
) -> QtGui.QPixmap:
    """Return a crisp HiDPI pixmap for the sidebar brand mark."""
    icon = load_app_icon(icon_name)
    if icon.isNull():
        return QtGui.QPixmap()
    return icon.pixmap(QtCore.QSize(size, size), max(1.0, float(dpr)))
