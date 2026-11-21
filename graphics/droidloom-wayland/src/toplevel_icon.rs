//! Desktop entry metadata and `xdg_toplevel_icon_v1` integration.

#![forbid(unsafe_code)]

use std::env;
use std::ffi::OsString;
use std::fs;
use std::io::{self, Read, Write};
use std::os::fd::AsFd;
use std::path::{Path, PathBuf};

use flate2::read::ZlibDecoder;
use wayland_client::globals::GlobalList;
use wayland_client::protocol::{wl_buffer, wl_shm};
use wayland_client::{Connection, Dispatch, QueueHandle};
use wayland_protocols::xdg::toplevel_icon::v1::client::{
    xdg_toplevel_icon_manager_v1::{self, XdgToplevelIconManagerV1},
    xdg_toplevel_icon_v1::XdgToplevelIconV1,
};
use wayland_protocols::xdg::shell::client::xdg_toplevel::XdgToplevel;

use crate::App;

const MAX_DESKTOP_ENTRY_BYTES: u64 = 64 * 1024;
const MAX_ICON_FILE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_ICON_DIMENSION: u32 = 512;
const PNG_SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";
const ICON_SIZE_DIRS: &[&str] = &["128x128", "256x256", "96x96", "64x64", "48x48", "512x512", "32x32"];

/// Desktop launcher metadata matching an Android package on the host.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct DesktopEntry {
    /// Desktop entry ID without `.desktop` (suitable for `xdg_toplevel.set_app_id`).
    pub desktop_id: String,
    /// Localized application display name from `[Desktop Entry]` `Name=`.
    pub title: Option<String>,
    /// Icon name or path from `[Desktop Entry]` `Icon=`.
    pub icon: Option<String>,
}

/// Optional `xdg_toplevel_icon_manager_v1` global handle.
pub(super) struct Manager {
    proxy: Option<XdgToplevelIconManagerV1>,
}

/// Owned icon protocol resources attached to a `TaskWindow`.
pub(super) struct TaskIcon {
    icon: XdgToplevelIconV1,
    buffers: Vec<wl_buffer::WlBuffer>,
}

impl TaskIcon {
    /// Destroy the protocol icon before destroying its backing `wl_buffer`s.
    pub(super) fn destroy(self) {
        self.icon.destroy();
        for buffer in self.buffers {
            buffer.destroy();
        }
    }
}

impl Manager {
    /// Bind `xdg_toplevel_icon_manager_v1` if the compositor advertises it.
    pub(super) fn bind(globals: &GlobalList, qh: &QueueHandle<App>) -> Self {
        Self {
            proxy: globals.bind(qh, 1..=1, ()).ok(),
        }
    }

    /// Attach a toplevel icon from an XDG icon name or PNG icon file.
    pub(super) fn attach(
        &self,
        qh: &QueueHandle<App>,
        shm: &wl_shm::WlShm,
        toplevel: &XdgToplevel,
        icon_name: &str,
    ) -> Option<TaskIcon> {
        let manager = self.proxy.as_ref()?;
        if icon_name.is_empty() {
            return None;
        }
        if let Some((size, pixels)) = load_icon_pixels(icon_name) {
            match create_shm_buffer(shm, qh, size, &pixels) {
                Ok(buffer) => {
                    let icon = manager.create_icon(qh, ());
                    icon.add_buffer(&buffer, 1);
                    manager.set_icon(toplevel, Some(&icon));
                    return Some(TaskIcon {
                        icon,
                        buffers: vec![buffer],
                    });
                }
                Err(error) => {
                    eprintln!("Droidloom could not allocate SHM icon buffer for {icon_name}: {error}");
                }
            }
        }
        let icon = manager.create_icon(qh, ());
        icon.set_name(icon_name.to_owned());
        manager.set_icon(toplevel, Some(&icon));
        Some(TaskIcon {
            icon,
            buffers: Vec::new(),
        })
    }
}

impl Dispatch<XdgToplevelIconManagerV1, ()> for App {
    fn event(
        _state: &mut Self,
        _proxy: &XdgToplevelIconManagerV1,
        _event: xdg_toplevel_icon_manager_v1::Event,
        _data: &(),
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
    ) {
    }
}

wayland_client::delegate_noop!(App: ignore XdgToplevelIconV1);

/// Find the exported Droidloom `.desktop` entry for `package`.
pub(super) fn desktop_entry_for_package(package: &str) -> Option<DesktopEntry> {
    let applications_dir =
        xdg_data_dir(env::var_os("XDG_DATA_HOME"), env::var_os("HOME"))?.join("applications");
    desktop_entry_in_dir(&applications_dir, package)
}

fn xdg_data_dir(data_home: Option<OsString>, home: Option<OsString>) -> Option<PathBuf> {
    data_home
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| {
            home.map(PathBuf::from)
                .filter(|path| path.is_absolute())
                .map(|path| path.join(".local/share"))
        })
}

fn desktop_entry_in_dir(dir: &Path, package: &str) -> Option<DesktopEntry> {
    let entries = fs::read_dir(dir).ok()?;
    let mut candidates: Vec<(String, PathBuf)> = entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name();
            let name = name.to_str()?;
            if !name.starts_with("droidloom-") || !name.ends_with(".desktop") {
                return None;
            }
            Some((name.to_owned(), entry.path()))
        })
        .collect();
    candidates.sort_by(|left, right| left.0.cmp(&right.0));

    let mut fallback_match: Option<DesktopEntry> = None;
    for (file_name, path) in candidates {
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        if !meta.is_file() || meta.len() > MAX_DESKTOP_ENTRY_BYTES {
            continue;
        }
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };
        let Some(desktop_id) = file_name.strip_suffix(".desktop") else {
            continue;
        };
        if let Some((entry, user_zero)) = parse_desktop_entry(desktop_id, &content, package) {
            if user_zero {
                return Some(entry);
            }
            if fallback_match.is_none() {
                fallback_match = Some(entry);
            }
        }
    }
    fallback_match
}

fn parse_desktop_entry(
    desktop_id: &str,
    content: &str,
    package: &str,
) -> Option<(DesktopEntry, bool)> {
    let mut in_entry = false;
    let mut title: Option<String> = None;
    let mut icon: Option<String> = None;
    let mut matched_package = false;
    let mut user_id: Option<&str> = None;

    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') && trimmed.ends_with(']') {
            in_entry = trimmed == "[Desktop Entry]";
            continue;
        }
        if !in_entry {
            continue;
        }
        if let Some(value) = trimmed.strip_prefix("Name=") {
            if !value.is_empty() {
                title = Some(unescape_desktop_value(value));
            }
        } else if let Some(value) = trimmed.strip_prefix("Icon=") {
            if !value.is_empty() {
                icon = Some(value.to_owned());
            }
        } else if let Some(value) = trimmed.strip_prefix("X-Droidloom-Package=") {
            matched_package = value == package;
        } else if let Some(value) = trimmed.strip_prefix("X-Droidloom-User=") {
            user_id = Some(value);
        }
    }

    matched_package.then(|| {
        (
            DesktopEntry {
                desktop_id: desktop_id.to_owned(),
                title,
                icon,
            },
            user_id.is_none_or(|user| user == "0"),
        )
    })
}

fn unescape_desktop_value(value: &str) -> String {
    let mut result = String::with_capacity(value.len());
    let mut chars = value.chars();
    while let Some(character) = chars.next() {
        if character != '\\' {
            result.push(character);
            continue;
        }
        match chars.next() {
            Some('s') => result.push(' '),
            Some('n' | 'r' | 't') => result.push(' '),
            Some('\\') => result.push('\\'),
            Some(other) => {
                result.push('\\');
                result.push(other);
            }
            None => result.push('\\'),
        }
    }
    result
}

fn load_icon_pixels(icon_name: &str) -> Option<(u32, Vec<u8>)> {
    let path = resolve_icon_png_path(
        icon_name,
        env::var_os("XDG_DATA_HOME"),
        env::var_os("HOME"),
        env::var_os("XDG_DATA_DIRS"),
    )?;
    let meta = fs::metadata(&path).ok()?;
    if !meta.is_file() || meta.len() > MAX_ICON_FILE_BYTES {
        return None;
    }
    let bytes = fs::read(&path).ok()?;
    decode_png_argb8888_square(&bytes)
}

fn resolve_icon_png_path(
    icon_name: &str,
    data_home: Option<OsString>,
    home: Option<OsString>,
    data_dirs: Option<OsString>,
) -> Option<PathBuf> {
    if icon_name.is_empty() || icon_name.contains('\0') {
        return None;
    }
    let direct = Path::new(icon_name);
    if direct.is_absolute() {
        return direct.is_file().then(|| direct.to_path_buf());
    }
    if icon_name.contains('/') || icon_name.contains('\\') || icon_name.contains("..") {
        return None;
    }
    let file_name = format!("{icon_name}.png");
    let mut roots = Vec::new();
    if let Some(data_dir) = xdg_data_dir(data_home, home) {
        roots.push(data_dir);
    }
    if let Some(dirs) = data_dirs {
        for dir in env::split_paths(&dirs) {
            if dir.is_absolute() {
                roots.push(dir);
            }
        }
    }
    for root in roots {
        let hicolor = root.join("icons/hicolor");
        for size in ICON_SIZE_DIRS {
            let candidate = hicolor.join(size).join("apps").join(&file_name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        let fallback = root.join("icons").join(&file_name);
        if fallback.is_file() {
            return Some(fallback);
        }
    }
    None
}

fn create_shm_buffer(
    shm: &wl_shm::WlShm,
    qh: &QueueHandle<App>,
    size: u32,
    pixels: &[u8],
) -> io::Result<wl_buffer::WlBuffer> {
    let stride = size
        .checked_mul(4)
        .and_then(|value| i32::try_from(value).ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "icon stride overflow"))?;
    let dim = i32::try_from(size)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "icon size overflow"))?;
    let total_bytes = i32::try_from(pixels.len())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "icon byte length overflow"))?;
    let mut file = tempfile::tempfile()?;
    file.write_all(pixels)?;
    let pool = shm.create_pool(file.as_fd(), total_bytes, qh, ());
    let buffer = pool.create_buffer(0, dim, dim, stride, wl_shm::Format::Argb8888, qh, ());
    pool.destroy();
    Ok(buffer)
}

#[derive(Clone, Copy)]
struct PngHeader {
    width: u32,
    height: u32,
    bit_depth: u8,
    color_type: u8,
    interlace: u8,
}

impl PngHeader {
    fn channels(self) -> Option<usize> {
        match self.color_type {
            0 | 3 => Some(1),
            2 => Some(3),
            4 => Some(2),
            6 => Some(4),
            _ => None,
        }
    }
}

/// Decode a PNG image into square premultiplied ARGB8888 little-endian pixels.
fn decode_png_argb8888_square(bytes: &[u8]) -> Option<(u32, Vec<u8>)> {
    if bytes.len() > usize::try_from(MAX_ICON_FILE_BYTES).ok()?
        || !bytes.starts_with(PNG_SIGNATURE)
    {
        return None;
    }

    let mut offset = PNG_SIGNATURE.len();
    let mut header: Option<PngHeader> = None;
    let mut palette: Vec<[u8; 3]> = Vec::new();
    let mut trns_raw: Option<&[u8]> = None;
    let mut idat = Vec::new();
    let mut saw_end = false;

    while offset + 12 <= bytes.len() {
        let len = u32::from_be_bytes(bytes[offset..offset + 4].try_into().ok()?) as usize;
        let kind: [u8; 4] = bytes[offset + 4..offset + 8].try_into().ok()?;
        let data_start = offset + 8;
        let data_end = data_start.checked_add(len)?;
        let chunk_end = data_end.checked_add(4)?;
        if chunk_end > bytes.len() {
            return None;
        }
        let data = &bytes[data_start..data_end];
        offset = chunk_end;

        match &kind {
            b"IHDR" => {
                if header.is_some() || data.len() != 13 {
                    return None;
                }
                let width = u32::from_be_bytes(data[0..4].try_into().ok()?);
                let height = u32::from_be_bytes(data[4..8].try_into().ok()?);
                let bit_depth = data[8];
                let color_type = data[9];
                let compression = data[10];
                let filter = data[11];
                let interlace = data[12];
                if width == 0
                    || height == 0
                    || width > MAX_ICON_DIMENSION
                    || height > MAX_ICON_DIMENSION
                    || compression != 0
                    || filter != 0
                    || !matches!(interlace, 0 | 1)
                {
                    return None;
                }
                let valid_depth = match color_type {
                    0 => matches!(bit_depth, 1 | 2 | 4 | 8 | 16),
                    2 | 4 | 6 => matches!(bit_depth, 8 | 16),
                    3 => matches!(bit_depth, 1 | 2 | 4 | 8),
                    _ => false,
                };
                if !valid_depth {
                    return None;
                }
                header = Some(PngHeader {
                    width,
                    height,
                    bit_depth,
                    color_type,
                    interlace,
                });
            }
            b"PLTE" => {
                header?;
                if data.is_empty() || data.len() > 256 * 3 || data.len() % 3 != 0 {
                    return None;
                }
                palette = data
                    .chunks_exact(3)
                    .map(|rgb| [rgb[0], rgb[1], rgb[2]])
                    .collect();
            }
            b"tRNS" => {
                header?;
                trns_raw = Some(data);
            }
            b"IDAT" => {
                header?;
                if idat.len().checked_add(data.len())? > usize::try_from(MAX_ICON_FILE_BYTES).ok()? {
                    return None;
                }
                idat.extend_from_slice(data);
            }
            b"IEND" => {
                saw_end = true;
                break;
            }
            _ => {
                let _ = header?;
            }
        }
    }

    let header = header?;
    if !saw_end || idat.is_empty() {
        return None;
    }
    if header.color_type == 3 && palette.is_empty() {
        return None;
    }

    let channels = header.channels()?;
    let expected_len = expected_decompressed_len(header, channels)?;
    let mut decoder = ZlibDecoder::new(idat.as_slice()).take((expected_len + 1) as u64);
    let mut decompressed = Vec::with_capacity(expected_len);
    decoder.read_to_end(&mut decompressed).ok()?;
    if decompressed.len() != expected_len {
        return None;
    }

    let trns = parse_trns(header, trns_raw, palette.len())?;
    let side = header.width.max(header.height);
    let off_x = (side - header.width) / 2;
    let off_y = (side - header.height) / 2;
    let side_usize = usize::try_from(side).ok()?;
    let mut output = vec![0u8; side_usize.checked_mul(side_usize)?.checked_mul(4)?];

    let passes: &[(u32, u32, u32, u32)] = if header.interlace == 0 {
        &[(0, 0, 1, 1)]
    } else {
        &[
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        ]
    };

    let bpp = (channels * usize::from(header.bit_depth)).div_ceil(8).max(1);
    let mut cursor = 0usize;
    for &(x0, y0, dx, dy) in passes {
        if header.width <= x0 || header.height <= y0 {
            continue;
        }
        let pass_w = usize::try_from((header.width - x0).div_ceil(dx)).ok()?;
        let pass_h = usize::try_from((header.height - y0).div_ceil(dy)).ok()?;
        let row_bytes = (pass_w * channels * usize::from(header.bit_depth)).div_ceil(8);
        let mut prev_row = vec![0u8; row_bytes];
        let mut curr_row = vec![0u8; row_bytes];

        for py in 0..pass_h {
            let filter_type = *decompressed.get(cursor)?;
            cursor += 1;
            let raw = decompressed.get(cursor..cursor + row_bytes)?;
            cursor += row_bytes;
            unfilter_scanline(filter_type, raw, &prev_row, &mut curr_row, bpp)?;

            let dest_y = usize::try_from(y0).ok()? + py * usize::try_from(dy).ok()?;
            for px in 0..pass_w {
                let (r, g, b, a) =
                    sample_rgba(&curr_row, px, header, &palette, &trns)?;
                let dest_x = usize::try_from(x0).ok()? + px * usize::try_from(dx).ok()?;
                let out_idx = ((usize::try_from(off_y).ok()? + dest_y) * side_usize
                    + usize::try_from(off_x).ok()?
                    + dest_x)
                    * 4;
                let (pr, pg, pb) = premultiply(r, g, b, a);
                output[out_idx] = pb;
                output[out_idx + 1] = pg;
                output[out_idx + 2] = pr;
                output[out_idx + 3] = a;
            }
            std::mem::swap(&mut prev_row, &mut curr_row);
        }
    }

    Some((side, output))
}

#[derive(Default)]
struct Transparency<'a> {
    palette_alphas: &'a [u8],
    gray_key: Option<u16>,
    rgb_key: Option<[u16; 3]>,
}

fn parse_trns<'a>(
    header: PngHeader,
    raw: Option<&'a [u8]>,
    palette_len: usize,
) -> Option<Transparency<'a>> {
    let Some(data) = raw else {
        return Some(Transparency::default());
    };
    match header.color_type {
        0 => (data.len() == 2).then(|| Transparency {
            gray_key: Some(u16::from_be_bytes([data[0], data[1]])),
            ..Transparency::default()
        }),
        2 => (data.len() == 6).then(|| Transparency {
            rgb_key: Some([
                u16::from_be_bytes([data[0], data[1]]),
                u16::from_be_bytes([data[2], data[3]]),
                u16::from_be_bytes([data[4], data[5]]),
            ]),
            ..Transparency::default()
        }),
        3 => (data.len() <= palette_len).then(|| Transparency {
            palette_alphas: data,
            ..Transparency::default()
        }),
        _ => None,
    }
}

fn expected_decompressed_len(header: PngHeader, channels: usize) -> Option<usize> {
    let bits_per_pixel = channels.checked_mul(usize::from(header.bit_depth))?;
    if header.interlace == 0 {
        let row_bytes = usize::try_from(header.width)
            .ok()?
            .checked_mul(bits_per_pixel)?
            .div_ceil(8);
        return usize::try_from(header.height)
            .ok()?
            .checked_mul(1usize.checked_add(row_bytes)?);
    }
    let passes = [
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ];
    let mut total = 0usize;
    for (x0, y0, dx, dy) in passes {
        if header.width <= x0 || header.height <= y0 {
            continue;
        }
        let pass_w = usize::try_from((header.width - x0).div_ceil(dx)).ok()?;
        let pass_h = usize::try_from((header.height - y0).div_ceil(dy)).ok()?;
        let row_bytes = pass_w.checked_mul(bits_per_pixel)?.div_ceil(8);
        let pass_bytes = pass_h.checked_mul(1usize.checked_add(row_bytes)?)?;
        total = total.checked_add(pass_bytes)?;
    }
    Some(total)
}

fn unfilter_scanline(
    filter_type: u8,
    raw: &[u8],
    prev: &[u8],
    curr: &mut [u8],
    bpp: usize,
) -> Option<()> {
    match filter_type {
        0 => curr.copy_from_slice(raw),
        1 => {
            for (index, &byte) in raw.iter().enumerate() {
                let left = if index >= bpp { curr[index - bpp] } else { 0 };
                curr[index] = byte.wrapping_add(left);
            }
        }
        2 => {
            for (index, &byte) in raw.iter().enumerate() {
                curr[index] = byte.wrapping_add(prev[index]);
            }
        }
        3 => {
            for (index, &byte) in raw.iter().enumerate() {
                let left = if index >= bpp { curr[index - bpp] } else { 0 };
                let up = prev[index];
                let avg = u8::try_from((u16::from(left) + u16::from(up)) / 2).unwrap_or(0);
                curr[index] = byte.wrapping_add(avg);
            }
        }
        4 => {
            for (index, &byte) in raw.iter().enumerate() {
                let left = if index >= bpp { curr[index - bpp] } else { 0 };
                let up = prev[index];
                let up_left = if index >= bpp { prev[index - bpp] } else { 0 };
                curr[index] = byte.wrapping_add(paeth_predictor(left, up, up_left));
            }
        }
        _ => return None,
    }
    Some(())
}

fn paeth_predictor(a: u8, b: u8, c: u8) -> u8 {
    let ai = i32::from(a);
    let bi = i32::from(b);
    let ci = i32::from(c);
    let p = ai + bi - ci;
    let pa = (p - ai).abs();
    let pb = (p - bi).abs();
    let pc = (p - ci).abs();
    if pa <= pb && pa <= pc {
        a
    } else if pb <= pc {
        b
    } else {
        c
    }
}

fn sample_rgba(
    row: &[u8],
    x: usize,
    header: PngHeader,
    palette: &[[u8; 3]],
    trns: &Transparency<'_>,
) -> Option<(u8, u8, u8, u8)> {
    match (header.color_type, header.bit_depth) {
        (6, 8) => {
            let base = x * 4;
            Some((row[base], row[base + 1], row[base + 2], row[base + 3]))
        }
        (6, 16) => {
            let base = x * 8;
            Some((row[base], row[base + 2], row[base + 4], row[base + 6]))
        }
        (2, 8) => {
            let base = x * 3;
            let (r, g, b) = (row[base], row[base + 1], row[base + 2]);
            let a = if trns.rgb_key == Some([u16::from(r), u16::from(g), u16::from(b)]) {
                0
            } else {
                255
            };
            Some((r, g, b, a))
        }
        (2, 16) => {
            let base = x * 6;
            let r16 = u16::from_be_bytes([row[base], row[base + 1]]);
            let g16 = u16::from_be_bytes([row[base + 2], row[base + 3]]);
            let b16 = u16::from_be_bytes([row[base + 4], row[base + 5]]);
            let a = if trns.rgb_key == Some([r16, g16, b16]) {
                0
            } else {
                255
            };
            Some((row[base], row[base + 2], row[base + 4], a))
        }
        (4, 8) => {
            let base = x * 2;
            let v = row[base];
            Some((v, v, v, row[base + 1]))
        }
        (4, 16) => {
            let base = x * 4;
            let v = row[base];
            Some((v, v, v, row[base + 2]))
        }
        (0, 16) => {
            let base = x * 2;
            let raw = u16::from_be_bytes([row[base], row[base + 1]]);
            let v = row[base];
            let a = if trns.gray_key == Some(raw) { 0 } else { 255 };
            Some((v, v, v, a))
        }
        (0, depth @ (1 | 2 | 4 | 8)) => {
            let raw = packed_sample(row, x, depth);
            let max_val = (1u16 << depth) - 1;
            let v = u8::try_from((u16::from(raw) * 255) / max_val).unwrap_or(255);
            let a = if trns.gray_key == Some(u16::from(raw)) {
                0
            } else {
                255
            };
            Some((v, v, v, a))
        }
        (3, depth @ (1 | 2 | 4 | 8)) => {
            let idx = usize::from(packed_sample(row, x, depth));
            let [r, g, b] = *palette.get(idx)?;
            let a = trns.palette_alphas.get(idx).copied().unwrap_or(255);
            Some((r, g, b, a))
        }
        _ => None,
    }
}

fn packed_sample(row: &[u8], x: usize, bit_depth: u8) -> u8 {
    match bit_depth {
        8 => row[x],
        4 => {
            let byte = row[x / 2];
            if x % 2 == 0 { byte >> 4 } else { byte & 0x0f }
        }
        2 => {
            let shift = 6 - 2 * (x % 4);
            (row[x / 4] >> shift) & 0x03
        }
        1 => {
            let shift = 7 - (x % 8);
            (row[x / 8] >> shift) & 0x01
        }
        _ => 0,
    }
}

fn premultiply(r: u8, g: u8, b: u8, a: u8) -> (u8, u8, u8) {
    match a {
        0 => (0, 0, 0),
        255 => (r, g, b),
        alpha => (
            u8::try_from((u16::from(r) * u16::from(alpha) + 127) / 255).unwrap_or(255),
            u8::try_from((u16::from(g) * u16::from(alpha) + 127) / 255).unwrap_or(255),
            u8::try_from((u16::from(b) * u16::from(alpha) + 127) / 255).unwrap_or(255),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::Compression;
    use flate2::write::ZlibEncoder;

    fn encode_test_png(
        width: u32,
        height: u32,
        bit_depth: u8,
        color_type: u8,
        interlace: u8,
        palette: Option<&[[u8; 3]]>,
        trns: Option<&[u8]>,
        raw_scanlines: &[u8],
    ) -> Vec<u8> {
        let mut out = PNG_SIGNATURE.to_vec();
        let mut ihdr = Vec::new();
        ihdr.extend_from_slice(&width.to_be_bytes());
        ihdr.extend_from_slice(&height.to_be_bytes());
        ihdr.extend_from_slice(&[bit_depth, color_type, 0, 0, interlace]);
        push_chunk(&mut out, *b"IHDR", &ihdr);
        if let Some(plte) = palette {
            let mut bytes = Vec::with_capacity(plte.len() * 3);
            for rgb in plte {
                bytes.extend_from_slice(rgb);
            }
            push_chunk(&mut out, *b"PLTE", &bytes);
        }
        if let Some(trns) = trns {
            push_chunk(&mut out, *b"tRNS", trns);
        }
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(raw_scanlines).unwrap();
        let compressed = encoder.finish().unwrap();
        push_chunk(&mut out, *b"IDAT", &compressed);
        push_chunk(&mut out, *b"IEND", &[]);
        out
    }

    fn push_chunk(out: &mut Vec<u8>, kind: [u8; 4], data: &[u8]) {
        out.extend_from_slice(&u32::try_from(data.len()).unwrap().to_be_bytes());
        out.extend_from_slice(&kind);
        out.extend_from_slice(data);
        out.extend_from_slice(&0u32.to_be_bytes());
    }

    #[test]
    fn desktop_entry_lookup_reads_id_title_and_icon_deterministically() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("droidloom-zeta.desktop"),
            "[Desktop Entry]\nType=Application\nName=Zeta\nIcon=droidloom-zeta\nX-Droidloom-Package=org.example.multi\nX-Droidloom-User=10\n",
        )
        .unwrap();
        fs::write(
            dir.path().join("droidloom-alpha.desktop"),
            "[Desktop Entry]\nType=Application\nName=Alpha\\nApp\nIcon=droidloom-alpha\nX-Droidloom-Package=org.example.multi\nX-Droidloom-User=0\n\n[Desktop Action Extra]\nName=Wrong\nIcon=wrong\n",
        )
        .unwrap();
        fs::write(
            dir.path().join("droidloom-fdroid.desktop"),
            "[Desktop Entry]\nType=Application\nName=F-Droid\nIcon=droidloom-fdroid\nX-Droidloom-Package=org.fdroid.fdroid\n\n[Desktop Action Extra]\nName=Wrong\nIcon=wrong\n",
        )
        .unwrap();

        assert_eq!(
            desktop_entry_in_dir(dir.path(), "org.fdroid.fdroid"),
            Some(DesktopEntry {
                desktop_id: "droidloom-fdroid".into(),
                title: Some("F-Droid".into()),
                icon: Some("droidloom-fdroid".into()),
            })
        );
        assert_eq!(
            desktop_entry_in_dir(dir.path(), "org.example.multi"),
            Some(DesktopEntry {
                desktop_id: "droidloom-alpha".into(),
                title: Some("Alpha App".into()),
                icon: Some("droidloom-alpha".into()),
            })
        );
        assert_eq!(desktop_entry_in_dir(dir.path(), "com.example.missing"), None);
    }

    #[test]
    fn resolves_hicolor_png_icons_and_rejects_traversal() {
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("share");
        let icon = data.join("icons/hicolor/128x128/apps/droidloom-abc.png");
        fs::create_dir_all(icon.parent().unwrap()).unwrap();
        fs::write(&icon, b"png").unwrap();

        assert_eq!(
            resolve_icon_png_path(
                "droidloom-abc",
                Some(data.clone().into_os_string()),
                None,
                None,
            ),
            Some(icon)
        );
        assert_eq!(
            resolve_icon_png_path(
                "../escape",
                Some(data.into_os_string()),
                None,
                None,
            ),
            None
        );
    }

    #[test]
    fn decodes_rgba_png_with_sub_up_paeth_filters_and_premultiplies_alpha() {
        // 2x2 RGBA:
        // row 0 (filter Sub): (200, 100, 50, 128), (200, 100, 50, 255)
        // row 1 (filter Up):  (10, 20, 30, 0),     (255, 0, 128, 255)
        let raw = [
            1u8, 200, 100, 50, 128, 0, 0, 0, 127,
            2u8, 66, 176, 236, 128, 55, 156, 78, 0,
        ];
        let png = encode_test_png(2, 2, 8, 6, 0, None, None, &raw);
        let (size, pixels) = decode_png_argb8888_square(&png).unwrap();
        assert_eq!(size, 2);
        // Pixel (0,0): r=200, g=100, b=50, a=128 -> premult (100, 50, 25, 128) -> BGRA [25, 50, 100, 128]
        assert_eq!(&pixels[0..4], &[25, 50, 100, 128]);
        // Pixel (1,0): r=200, g=100, b=50, a=255 -> [50, 100, 200, 255]
        assert_eq!(&pixels[4..8], &[50, 100, 200, 255]);
        // Pixel (0,1): a=0 -> [0, 0, 0, 0]
        assert_eq!(&pixels[8..12], &[0, 0, 0, 0]);
        // Pixel (1,1): (255, 0, 128, 255) -> [128, 0, 255, 255]
        assert_eq!(&pixels[12..16], &[128, 0, 255, 255]);
    }

    #[test]
    fn decodes_indexed_and_non_square_png_padded_to_square() {
        // 3x1 indexed 8-bit, padded to 3x3 centered vertically at y=1.
        let palette = [[255, 0, 0], [0, 255, 0], [0, 0, 255]];
        let trns = [255, 0];
        let raw = [0u8, 0, 1, 2];
        let png = encode_test_png(3, 1, 8, 3, 0, Some(&palette), Some(&trns), &raw);
        let (size, pixels) = decode_png_argb8888_square(&png).unwrap();
        assert_eq!(size, 3);
        assert_eq!(pixels.len(), 3 * 3 * 4);
        assert_eq!(&pixels[0..12], &[0u8; 12]);
        // Row 1: red, transparent green, opaque blue in BGRA.
        assert_eq!(&pixels[12..16], &[0, 0, 255, 255]);
        assert_eq!(&pixels[16..20], &[0, 0, 0, 0]);
        assert_eq!(&pixels[20..24], &[255, 0, 0, 255]);
        assert_eq!(&pixels[24..36], &[0u8; 12]);
    }

    #[test]
    fn rejects_invalid_or_oversized_png_dimensions() {
        let mut bad = encode_test_png(1, 1, 8, 6, 0, None, None, &[0, 1, 2, 3, 4]);
        // Overwrite IHDR width to 513 (> MAX_ICON_DIMENSION).
        bad[16..20].copy_from_slice(&513u32.to_be_bytes());
        assert!(decode_png_argb8888_square(&bad).is_none());
        assert!(decode_png_argb8888_square(b"not a png").is_none());
    }

    #[test]
    fn decodes_adam7_interlaced_rgba_png() {
        // 2x2 RGBA interlaced:
        // pass 1 (x0=0,y0=0,dx=8,dy=8): (0,0) = red
        // pass 2 (x0=4,y0=0): empty
        // pass 3 (x0=0,y0=4): empty
        // pass 4 (x0=2,y0=0): empty
        // pass 5 (x0=0,y0=2): empty
        // pass 6 (x0=1,y0=0,dx=2,dy=2): (1,0) = green
        // pass 7 (x0=0,y0=1,dx=1,dy=2): (0,1) = blue, (1,1) = white
        let raw = [
            0, 255, 0, 0, 255, // pass 1
            0, 0, 255, 0, 255, // pass 6
            0, 0, 0, 255, 255, 255, 255, 255, 255, // pass 7
        ];
        let png = encode_test_png(2, 2, 8, 6, 1, None, None, &raw);
        let (size, pixels) = decode_png_argb8888_square(&png).unwrap();
        assert_eq!(size, 2);
        assert_eq!(&pixels[0..4], &[0, 0, 255, 255]);
        assert_eq!(&pixels[4..8], &[0, 255, 0, 255]);
        assert_eq!(&pixels[8..12], &[255, 0, 0, 255]);
        assert_eq!(&pixels[12..16], &[255, 255, 255, 255]);
    }
}
