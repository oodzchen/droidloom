"""Unit tests for pure-Python APK parser and ABI compatibility checks."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from droidloom_settings.backend.apk_parser import (
    RES_STRING_POOL_TYPE,
    RES_XML_RESOURCE_MAP_TYPE,
    RES_XML_START_ELEMENT_TYPE,
    RES_XML_TYPE,
    TYPE_INT_DEC,
    TYPE_STRING,
    UTF8_FLAG,
    check_abi_compatibility,
    inspect_apk,
)


def _encode_utf8_pool(strings: list[str]) -> bytes:
    encoded_items = []
    offsets = []
    cur = 0
    for s in strings:
        raw = s.encode("utf-8")
        item = bytes([len(s) & 0x7F, len(raw) & 0x7F]) + raw + b"\x00"
        offsets.append(cur)
        encoded_items.append(item)
        cur += len(item)
    pool_data = b"".join(encoded_items)
    while len(pool_data) % 4 != 0:
        pool_data += b"\x00"
    header_size = 28
    strings_start = header_size + len(strings) * 4
    total_size = strings_start + len(pool_data)
    header = struct.pack(
        "<HHIIIIII",
        RES_STRING_POOL_TYPE,
        header_size,
        total_size,
        len(strings),
        0,
        UTF8_FLAG,
        strings_start,
        0,
    )
    offsets_bytes = struct.pack(f"<{len(strings)}I", *offsets) if strings else b""
    return header + offsets_bytes + pool_data


def _build_start_element(name_idx: int, attrs: list[tuple[int, int, int, int, int]]) -> bytes:
    # Each attr: (ns_idx, name_idx, raw_idx, type, data)
    attr_bytes = b"".join(
        struct.pack("<iiiHBBi", ns, name, raw, 8, 0, vtype, vdata)
        for ns, name, raw, vtype, vdata in attrs
    )
    header_size = 16
    ext_size = 20
    total_size = header_size + ext_size + len(attr_bytes)
    chunk = struct.pack("<HHII", RES_XML_START_ELEMENT_TYPE, header_size, total_size, 1)
    chunk += struct.pack("<I", 0xFFFFFFFF)
    chunk += struct.pack("<iiHHHHHH", -1, name_idx, 20, 20, len(attrs), 0, 0, 0)
    return chunk + attr_bytes


def _build_minimal_axml() -> bytes:
    strings = [
        "http://schemas.android.com/apk/res/android",  # 0
        "manifest",  # 1
        "package",  # 2
        "com.example.demo",  # 3
        "versionName",  # 4
        "1.2.3",  # 5
        "versionCode",  # 6
        "application",  # 7
        "label",  # 8
        "Demo App",  # 9
        "uses-permission",  # 10
        "name",  # 11
        "android.permission.INTERNET",  # 12
        "uses-sdk",  # 13
        "minSdkVersion",  # 14
        "targetSdkVersion",  # 15
    ]
    pool = _encode_utf8_pool(strings)
    res_ids = [0] * len(strings)
    res_map_body = struct.pack(f"<{len(res_ids)}I", *res_ids)
    res_map = struct.pack("<HHI", RES_XML_RESOURCE_MAP_TYPE, 8, 8 + len(res_map_body)) + res_map_body

    manifest_el = _build_start_element(
        1,
        [
            (-1, 2, 3, TYPE_STRING, 3),
            (0, 4, 5, TYPE_STRING, 5),
            (0, 6, -1, TYPE_INT_DEC, 42),
        ],
    )
    sdk_el = _build_start_element(
        13,
        [
            (0, 14, -1, TYPE_INT_DEC, 26),
            (0, 15, -1, TYPE_INT_DEC, 35),
        ],
    )
    perm_el = _build_start_element(10, [(0, 11, 12, TYPE_STRING, 12)])
    app_el = _build_start_element(7, [(0, 8, 9, TYPE_STRING, 9)])

    body = pool + res_map + manifest_el + sdk_el + perm_el + app_el
    return struct.pack("<HHI", RES_XML_TYPE, 8, 8 + len(body)) + body


class ApkParserTests(unittest.TestCase):
    def test_parses_minimal_apk_manifest_and_abis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "demo.apk"
            with zipfile.ZipFile(apk, "w") as zf:
                zf.writestr("AndroidManifest.xml", _build_minimal_axml())
                zf.writestr("lib/arm64-v8a/libdemo.so", b"\x7fELF")
            meta = inspect_apk(apk)
            self.assertTrue(meta.valid_zip)
            self.assertFalse(meta.is_split_bundle)
            self.assertEqual(meta.package, "com.example.demo")
            self.assertEqual(meta.version_name, "1.2.3")
            self.assertEqual(meta.version_code, 42)
            self.assertEqual(meta.min_sdk, 26)
            self.assertEqual(meta.target_sdk, 35)
            self.assertEqual(meta.label, "Demo App")
            self.assertEqual(meta.permissions, ["android.permission.INTERNET"])
            self.assertEqual(meta.abis, ["arm64-v8a"])

    def test_detects_split_bundle_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apks = Path(tmp) / "bundle.apks"
            with zipfile.ZipFile(apks, "w") as zf:
                zf.writestr("base-master.apk", b"PK")
            meta = inspect_apk(apks)
            self.assertTrue(meta.is_split_bundle)

            zip_bundle = Path(tmp) / "renamed.apk"
            with zipfile.ZipFile(zip_bundle, "w") as zf:
                zf.writestr("base.apk", b"PK")
                zf.writestr("split_config.arm64_v8a.apk", b"PK")
            meta2 = inspect_apk(zip_bundle)
            self.assertTrue(meta2.is_split_bundle)

    def test_abi_compatibility_checks(self) -> None:
        ok_pure, _ = check_abi_compatibility([])
        self.assertTrue(ok_pure)
        ok_arm64, _ = check_abi_compatibility(["arm64-v8a"])
        self.assertTrue(ok_arm64)


if __name__ == "__main__":
    unittest.main()
