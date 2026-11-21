"""Pure-Python APK metadata, icon, ABI, and bundle inspector."""

from __future__ import annotations

import platform
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ANDROID_NS = "http://schemas.android.com/apk/res/android"

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_XML_TYPE = 0x0003
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_RESOURCE_MAP_TYPE = 0x0180
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC_TYPE = 0x0202

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11

UTF8_FLAG = 1 << 8
FLAG_SPARSE = 0x01
FLAG_OFFSET16 = 0x02
FLAG_COMPLEX = 0x0001


@dataclass
class ApkMetadata:
    """Parsed metadata from an APK or bundle file."""

    path: Path
    file_size: int
    valid_zip: bool
    is_split_bundle: bool = False
    bundle_reason: str = ""
    package: str = ""
    version_name: str = ""
    version_code: int | None = None
    min_sdk: int | None = None
    target_sdk: int | None = None
    label: str = ""
    permissions: list[str] = field(default_factory=list)
    abis: list[str] = field(default_factory=list)
    icon_bytes: bytes | None = None
    icon_path: str = ""
    abi_compatible: bool = True
    abi_status_message: str = ""
    parse_error: str = ""


def _read_u8(buf: bytes, off: int) -> int:
    return buf[off]


def _read_u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _read_u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _read_len8(buf: bytes, off: int) -> tuple[int, int]:
    val = buf[off]
    if val & 0x80:
        return ((val & 0x7F) << 8) | buf[off + 1], off + 2
    return val, off + 1


def _read_len16(buf: bytes, off: int) -> tuple[int, int]:
    val = _read_u16(buf, off)
    if val & 0x8000:
        lo = _read_u16(buf, off + 2)
        return ((val & 0x7F) << 16) | lo, off + 4
    return val, off + 2


def _parse_string_pool(chunk: bytes) -> list[str]:
    if len(chunk) < 28:
        return []
    header_size = _read_u16(chunk, 2)
    string_count = _read_u32(chunk, 8)
    flags = _read_u32(chunk, 16)
    strings_start = _read_u32(chunk, 20)
    is_utf8 = bool(flags & UTF8_FLAG)

    offsets_start = header_size
    if offsets_start + string_count * 4 > len(chunk):
        return []
    offsets = struct.unpack_from(f"<{string_count}I", chunk, offsets_start)
    strings: list[str] = []
    for off in offsets:
        pos = strings_start + off
        if pos >= len(chunk):
            strings.append("")
            continue
        try:
            if is_utf8:
                _, pos = _read_len8(chunk, pos)
                byte_len, pos = _read_len8(chunk, pos)
                raw = chunk[pos : pos + byte_len]
                strings.append(raw.decode("utf-8", errors="replace"))
            else:
                char_len, pos = _read_len16(chunk, pos)
                raw = chunk[pos : pos + char_len * 2]
                strings.append(raw.decode("utf-16-le", errors="replace"))
        except Exception:
            strings.append("")
    return strings


def _iter_chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        ctype, hdr_size, csize = struct.unpack_from("<HHI", data, pos)
        if csize < 8 or pos + csize > end:
            break
        yield ctype, hdr_size, chunk_end_or_slice(data, pos, csize)
        pos += csize


def chunk_end_or_slice(data: bytes, pos: int, csize: int) -> bytes:
    return data[pos : pos + csize]


def _parse_manifest_xml(data: bytes) -> dict[str, object]:
    result: dict[str, object] = {
        "package": "",
        "version_name": "",
        "version_code": None,
        "min_sdk": None,
        "target_sdk": None,
        "label_raw": "",
        "label_res_id": None,
        "icon_raw": "",
        "icon_res_id": None,
        "permissions": [],
    }
    if len(data) < 8:
        return result
    ctype, hdr_size, total_size = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_XML_TYPE:
        return result
    end = min(len(data), total_size)
    strings: list[str] = []
    res_map: list[int] = []

    def get_str(idx: int) -> str:
        if 0 <= idx < len(strings):
            return strings[idx]
        return ""

    for chunk_type, chunk_hdr, chunk in _iter_chunks(data, hdr_size, end):
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _parse_string_pool(chunk)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            count = (len(chunk) - chunk_hdr) // 4
            res_map = list(struct.unpack_from(f"<{count}I", chunk, chunk_hdr))
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            if len(chunk) < chunk_hdr + 20:
                continue
            ext = chunk_hdr
            ns_idx, name_idx, attr_start, attr_size, attr_count = struct.unpack_from(
                "<iiHHH", chunk, ext
            )
            tag = get_str(name_idx)
            attrs: dict[tuple[str, str], tuple[int, int, int]] = {}
            attr_base = ext + attr_start
            for i in range(attr_count):
                a_off = attr_base + i * attr_size
                if a_off + 20 > len(chunk):
                    break
                a_ns, a_name, a_raw, _, _, a_type, a_data = struct.unpack_from(
                    "<iiiHBBi", chunk, a_off
                )
                ns_str = get_str(a_ns) if a_ns >= 0 else ""
                name_str = get_str(a_name)
                if not name_str and 0 <= a_name < len(res_map):
                    rid = res_map[a_name]
                    known = {
                        0x01010001: "label",
                        0x01010002: "icon",
                        0x01010003: "name",
                        0x0101020C: "minSdkVersion",
                        0x0101021B: "versionCode",
                        0x0101021C: "versionName",
                        0x01010270: "targetSdkVersion",
                    }
                    name_str = known.get(rid, "")
                    if name_str:
                        ns_str = ANDROID_NS
                attrs[(ns_str, name_str)] = (a_raw, a_type & 0xFF, a_data & 0xFFFFFFFF)

            if tag == "manifest":
                for (_, k), (raw_idx, vtype, vdata) in attrs.items():
                    if k == "package":
                        result["package"] = (
                            get_str(vdata) if vtype == TYPE_STRING else get_str(raw_idx)
                        )
                    elif k == "versionName":
                        result["version_name"] = (
                            get_str(vdata) if vtype == TYPE_STRING else get_str(raw_idx)
                        )
                    elif k == "versionCode" and vtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                        result["version_code"] = vdata
            elif tag == "uses-sdk":
                for (_, k), (_, vtype, vdata) in attrs.items():
                    if k == "minSdkVersion" and vtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                        result["min_sdk"] = vdata
                    elif k == "targetSdkVersion" and vtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                        result["target_sdk"] = vdata
            elif tag == "uses-permission":
                for (_, k), (raw_idx, vtype, vdata) in attrs.items():
                    if k == "name":
                        perm = get_str(vdata) if vtype == TYPE_STRING else get_str(raw_idx)
                        if perm and perm not in result["permissions"]:
                            result["permissions"].append(perm)
            elif tag == "application":
                for (_, k), (raw_idx, vtype, vdata) in attrs.items():
                    if k == "label":
                        if vtype == TYPE_STRING:
                            result["label_raw"] = get_str(vdata)
                        elif vtype == TYPE_REFERENCE:
                            result["label_res_id"] = vdata
                        elif raw_idx >= 0:
                            result["label_raw"] = get_str(raw_idx)
                    elif k == "icon":
                        if vtype == TYPE_STRING:
                            result["icon_raw"] = get_str(vdata)
                        elif vtype == TYPE_REFERENCE:
                            result["icon_res_id"] = vdata
                        elif raw_idx >= 0:
                            result["icon_raw"] = get_str(raw_idx)
    return result


def _parse_arsc_resources(data: bytes, wanted_ids: set[int]) -> dict[int, list[tuple[int, str]]]:
    """Resolve resource IDs -> list of (data_type, string_or_ref_value) from resources.arsc."""
    found: dict[int, list[tuple[int, str]]] = {}
    if not wanted_ids or len(data) < 12:
        return found
    ctype, hdr_size, total_size = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_TABLE_TYPE:
        return found
    end = min(len(data), total_size)
    global_strings: list[str] = []

    for chunk_type, chunk_hdr, chunk in _iter_chunks(data, hdr_size, end):
        if chunk_type == RES_STRING_POOL_TYPE:
            global_strings = _parse_string_pool(chunk)
        elif chunk_type == RES_TABLE_PACKAGE_TYPE:
            if len(chunk) < chunk_hdr:
                continue
            pkg_id = _read_u32(chunk, 8)
            wanted_for_pkg = {rid for rid in wanted_ids if (rid >> 24) == pkg_id}
            if not wanted_for_pkg:
                continue
            for p_type, p_hdr, p_chunk in _iter_chunks(chunk, chunk_hdr, len(chunk)):
                if p_type != RES_TABLE_TYPE_TYPE or len(p_chunk) < 28:
                    continue
                type_id = _read_u8(p_chunk, 8)
                flags = _read_u8(p_chunk, 9)
                entry_count = _read_u32(p_chunk, 12)
                entries_start = _read_u32(p_chunk, 16)
                wanted_type_entries = {
                    (rid & 0xFFFF): rid
                    for rid in wanted_for_pkg
                    if ((rid >> 16) & 0xFF) == type_id
                }
                if not wanted_type_entries:
                    continue
                offsets_pos = p_hdr
                if offsets_pos >= len(p_chunk):
                    continue

                def read_entry_at(rel_off: int, rid: int) -> None:
                    pos = entries_start + rel_off
                    if pos + 8 > len(p_chunk):
                        return
                    e_size, e_flags, _ = struct.unpack_from("<HHI", p_chunk, pos)
                    if e_flags & FLAG_COMPLEX:
                        return
                    v_pos = pos + e_size
                    if v_pos + 8 > len(p_chunk):
                        return
                    _, _, vtype, vdata = struct.unpack_from("<HBBI", p_chunk, v_pos)
                    if vtype == TYPE_STRING and 0 <= vdata < len(global_strings):
                        found.setdefault(rid, []).append((vtype, global_strings[vdata]))
                    elif vtype == TYPE_REFERENCE:
                        found.setdefault(rid, []).append((vtype, hex(vdata)))

                if flags & FLAG_SPARSE:
                    for i in range(entry_count):
                        o_pos = offsets_pos + i * 4
                        if o_pos + 4 > len(p_chunk):
                            break
                        e_idx, off16 = struct.unpack_from("<HH", p_chunk, o_pos)
                        if e_idx in wanted_type_entries and off16 != 0xFFFF:
                            read_entry_at(off16 * 4, wanted_type_entries[e_idx])
                elif flags & FLAG_OFFSET16:
                    for e_idx, rid in wanted_type_entries.items():
                        o_pos = offsets_pos + e_idx * 2
                        if e_idx < entry_count and o_pos + 2 <= len(p_chunk):
                            off16 = _read_u16(p_chunk, o_pos)
                            if off16 != 0xFFFF:
                                read_entry_at(off16 * 4, rid)
                else:
                    for e_idx, rid in wanted_type_entries.items():
                        o_pos = offsets_pos + e_idx * 4
                        if e_idx < entry_count and o_pos + 4 <= len(p_chunk):
                            off32 = _read_u32(p_chunk, o_pos)
                            if off32 != 0xFFFFFFFF:
                                read_entry_at(off32, rid)
    return found


def _pick_best_label(candidates: list[tuple[int, str]]) -> str:
    for vtype, val in candidates:
        if vtype == TYPE_STRING and val.strip():
            return val.strip()
    return ""


def _pick_best_icon(zf: zipfile.ZipFile, candidates: list[tuple[int, str]], names: list[str]) -> str:
    raster_exts = (".png", ".webp", ".jpg", ".jpeg")
    density_rank = ("xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "nodpi", "anydpi")

    def score(path: str) -> tuple[int, int]:
        is_raster = 1 if path.lower().endswith(raster_exts) else 0
        rank = 0
        for idx, d in enumerate(density_rank):
            if d in path:
                rank = len(density_rank) - idx
                break
        return (is_raster, rank)

    paths = [val for vtype, val in candidates if vtype == TYPE_STRING and val in names]
    raster_paths = [p for p in paths if p.lower().endswith(raster_exts)]
    if raster_paths:
        return max(raster_paths, key=score)

    # Fallback when icon points to adaptive-icon XML: search for raster icon files with matching stem
    for p in paths:
        stem = Path(p).stem
        matched = [
            n
            for n in names
            if n.startswith("res/")
            and n.lower().endswith(raster_exts)
            and (stem in Path(n).stem or "ic_launcher" in Path(n).stem)
            and "foreground" not in Path(n).stem
        ]
        if matched:
            return max(matched, key=score)
        foregrounds = [
            n
            for n in names
            if n.startswith("res/") and n.lower().endswith(raster_exts) and "ic_launcher" in n
        ]
        if foregrounds:
            return max(foregrounds, key=score)
    return paths[0] if paths else ""


def check_abi_compatibility(abis: list[str]) -> tuple[bool, str]:
    """Compare APK native ABIs with current host architecture."""
    machine = platform.machine().lower()
    if not abis:
        return True, "纯 Java/DEX 应用（无原生库，通用兼容）"
    if machine in ("aarch64", "arm64"):
        if "arm64-v8a" in abis:
            return True, "兼容当前 ARM64 (arm64-v8a 原生)"
        return (
            False,
            f"不兼容：当前宿主为 ARM64，仅支持 arm64-v8a；此 APK 仅含 {', '.join(abis)}",
        )
    if machine in ("x86_64", "amd64"):
        if "x86_64" in abis:
            return True, "兼容当前 x86_64（原生）"
        if "arm64-v8a" in abis:
            return True, "兼容当前 x86_64（通过 Teto ARM64 转译运行）"
        return (
            False,
            f"不兼容：x86_64 不支持仅含 {', '.join(abis)} 的 32 位 ARM APK",
        )
    return True, f"原生库: {', '.join(abis)}"


def inspect_apk(apk_path: str | Path) -> ApkMetadata:
    """Inspect a local APK file and extract manifest/icon/ABI information."""
    path = Path(apk_path)
    if not path.is_file():
        return ApkMetadata(
            path=path,
            file_size=0,
            valid_zip=False,
            parse_error="文件不存在或不是普通文件",
        )
    size = path.stat().st_size
    ext = path.suffix.lower()
    if ext in (".apks", ".apkm", ".xapk"):
        return ApkMetadata(
            path=path,
            file_size=size,
            valid_zip=False,
            is_split_bundle=True,
            bundle_reason=f"检测到 {ext} 拆分安装包格式；Droidloom 当前仅支持单个独立 APK。",
        )

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if "AndroidManifest.xml" not in names:
                nested_apks = [n for n in names if n.lower().endswith(".apk")]
                if nested_apks:
                    return ApkMetadata(
                        path=path,
                        file_size=size,
                        valid_zip=True,
                        is_split_bundle=True,
                        bundle_reason="压缩包内包含多个拆分 APK，非独立安装包。",
                    )
                return ApkMetadata(
                    path=path,
                    file_size=size,
                    valid_zip=True,
                    parse_error="压缩包内缺少 AndroidManifest.xml，不是有效的 APK。",
                )

            abis = sorted(
                {
                    parts[1]
                    for n in names
                    if n.startswith("lib/") and len(parts := n.split("/")) >= 3 and parts[1]
                }
            )
            compat_ok, compat_msg = check_abi_compatibility(abis)

            manifest_data = zf.read("AndroidManifest.xml")
            parsed = _parse_manifest_xml(manifest_data)

            wanted_ids: set[int] = set()
            label_res_id = parsed.get("label_res_id")
            icon_res_id = parsed.get("icon_res_id")
            if isinstance(label_res_id, int) and label_res_id:
                wanted_ids.add(label_res_id)
            if isinstance(icon_res_id, int) and icon_res_id:
                wanted_ids.add(icon_res_id)

            res_map: dict[int, list[tuple[int, str]]] = {}
            if wanted_ids and "resources.arsc" in names:
                res_map = _parse_arsc_resources(zf.read("resources.arsc"), wanted_ids)

            label = str(parsed.get("label_raw") or "")
            if not label and isinstance(label_res_id, int):
                label = _pick_best_label(res_map.get(label_res_id, []))
            if not label:
                label = str(parsed.get("package") or path.stem)

            icon_path = str(parsed.get("icon_raw") or "")
            if (not icon_path or icon_path not in names) and isinstance(icon_res_id, int):
                icon_path = _pick_best_icon(zf, res_map.get(icon_res_id, []), names)

            icon_bytes: bytes | None = None
            if icon_path and icon_path in names and icon_path.lower().endswith(
                (".png", ".webp", ".jpg", ".jpeg")
            ):
                icon_bytes = zf.read(icon_path)

            return ApkMetadata(
                path=path,
                file_size=size,
                valid_zip=True,
                package=str(parsed.get("package") or ""),
                version_name=str(parsed.get("version_name") or ""),
                version_code=parsed.get("version_code") if isinstance(parsed.get("version_code"), int) else None,
                min_sdk=parsed.get("min_sdk") if isinstance(parsed.get("min_sdk"), int) else None,
                target_sdk=parsed.get("target_sdk") if isinstance(parsed.get("target_sdk"), int) else None,
                label=label,
                permissions=list(parsed.get("permissions") or []),
                abis=abis,
                icon_bytes=icon_bytes,
                icon_path=icon_path,
                abi_compatible=compat_ok,
                abi_status_message=compat_msg,
            )
    except zipfile.BadZipFile:
        return ApkMetadata(
            path=path,
            file_size=size,
            valid_zip=False,
            parse_error="文件不是有效的 ZIP/APK 归档",
        )
    except Exception as exc:
        return ApkMetadata(
            path=path,
            file_size=size,
            valid_zip=False,
            parse_error=f"解析 APK 失败: {exc}",
        )
