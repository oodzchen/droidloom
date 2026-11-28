"""Host MPRIS 2 D-Bus bridge exposing Android MediaSessions to KDE Plasma 6."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .android_bridge import DEFAULT_SELECTOR, adb_shell, is_adb_connected

MPRIS_BUS_PREFIX = "org.mpris.MediaPlayer2.droidloom"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_IFACE_ROOT = "org.mpris.MediaPlayer2"
MPRIS_IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

MPRIS_INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg name="Offset" type="x" direction="in"/>
    </method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri">
      <arg name="Uri" type="s" direction="in"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_MAX_ART_DOWNLOAD_BYTES = 2 * 1024 * 1024
_MAX_ART_CACHE_FILES = 60
_MAX_NATIVE_LINE_BYTES = 4 * 1024 * 1024


def _allowed_peer_uids() -> set[int]:
    return {os.getuid(), 0, 1000}


def _acquire_media_listen_socket(
    socket_path: Path | None = None,
) -> tuple[socket.socket | None, Path | None]:
    if socket_path is None:
        try:
            listen_pid = int(os.environ.get("LISTEN_PID", "0") or "0")
            listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
        except ValueError:
            listen_pid, listen_fds = 0, 0
        if listen_pid == os.getpid() and listen_fds >= 1:
            try:
                sock = socket.socket(fileno=3)
                os.set_inheritable(sock.fileno(), False)
                sock.settimeout(1.0)
                return sock, None
            except OSError:
                pass

    target = socket_path
    if target is None:
        rt = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        base = Path(rt) if rt and Path(rt).is_absolute() else Path(f"/run/user/{os.getuid()}")
        target = base / "droidloom/media.sock"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        target.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(target))
        os.chmod(target, 0o600)
        sock.listen(4)
        sock.settimeout(1.0)
        return sock, target
    except OSError:
        return None, None


def _is_safe_remote_art_url(url: str) -> bool:
    """Allow only public HTTP(S) URLs, blocking loopback/private/link-local SSRF targets."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        ip_obj = ipaddress.ip_address(host)
        return ip_obj.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0].split("%", 1)[0]
        try:
            if not ipaddress.ip_address(addr).is_global:
                return False
        except ValueError:
            return False
    return True


class _NoUnsafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_remote_art_url(newurl):
            raise urllib.error.URLError("blocked unsafe artwork redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def sanitize_bus_segment(package: str) -> str:
    """Convert an Android package name into a valid D-Bus bus-name segment."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", package.strip())
    if not cleaned:
        return "app"
    if cleaned[0].isdigit():
        cleaned = f"app_{cleaned}"
    return cleaned


def scan_desktop_entries(apps_dir: Path | None = None) -> dict[str, tuple[str, str, str]]:
    """Map package -> (desktop_entry_stem, display_name, icon_name) from Droidloom .desktop files."""
    if apps_dir is None:
        base = os.environ.get("XDG_DATA_HOME")
        data_home = (
            Path(base) if (base and Path(base).is_absolute()) else Path.home() / ".local/share"
        )
        apps_dir = data_home / "applications"
    if not apps_dir.is_dir():
        return {}
    out: dict[str, tuple[str, str, str]] = {}
    for entry in sorted(apps_dir.glob("droidloom-*.desktop")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        pkg = ""
        name = ""
        icon = ""
        for line in text.splitlines():
            if line.startswith("X-Droidloom-Package="):
                pkg = line.split("=", 1)[1].strip()
            elif line.startswith("Name="):
                name = line.split("=", 1)[1].strip()
            elif line.startswith("Icon="):
                icon = line.split("=", 1)[1].strip()
        if pkg and pkg not in out:
            out[pkg] = (entry.stem, name or pkg, icon)
    return out


class MediaArtCache:
    """Disk cache for Android MediaSession album art PNG files."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        if cache_dir is None:
            base = os.environ.get("XDG_CACHE_HOME")
            cache_home = (
                Path(base)
                if (base and Path(base).is_absolute())
                else Path.home() / ".cache"
            )
            cache_dir = cache_home / "droidloom-settings" / "media-art"
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._inflight_urls: set[str] = set()

    def lookup(self, key: str) -> str:
        if not key:
            return ""
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        target = self.cache_dir / f"{digest}.png"
        if target.is_file() and target.stat().st_size > 0:
            return target.as_uri()
        return ""

    def store_bytes(self, key: str, data: bytes) -> str:
        if not key or not data:
            return ""
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{digest}.png"
        if target.is_file() and target.stat().st_size > 0:
            return target.as_uri()
        tmp = self.cache_dir / f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError:
            tmp.unlink(missing_ok=True)
            return ""
        self._prune_old()
        return target.as_uri()

    def store_base64(self, art_hash: str, b64_str: str) -> str:
        if not art_hash or not b64_str:
            return ""
        existing = self.lookup(art_hash)
        if existing:
            return existing
        try:
            raw = base64.b64decode(b64_str)
        except Exception:
            return ""
        return self.store_bytes(art_hash, raw)

    def fetch_remote_async(self, url: str, on_ready: Callable[[str, str], None]) -> None:
        if os.environ.get("DROIDLOOM_MPRIS_FETCH_REMOTE_ART") != "1":
            return
        if not _is_safe_remote_art_url(url):
            return
        cached = self.lookup(f"url:{url}")
        if cached:
            on_ready(url, cached)
            return
        with self._lock:
            if url in self._inflight_urls:
                return
            self._inflight_urls.add(url)

        def worker() -> None:
            uri_out = ""
            try:
                opener = urllib.request.build_opener(_NoUnsafeRedirectHandler)
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Droidloom-MPRIS/1.0"},
                )
                with opener.open(req, timeout=4.0) as resp:
                    data = resp.read(_MAX_ART_DOWNLOAD_BYTES + 1)
                    if 0 < len(data) <= _MAX_ART_DOWNLOAD_BYTES:
                        uri_out = self.store_bytes(f"url:{url}", data)
            except Exception:
                uri_out = ""
            finally:
                with self._lock:
                    self._inflight_urls.discard(url)
            if uri_out:
                on_ready(url, uri_out)

        threading.Thread(target=worker, daemon=True, name="dl-art-fetch").start()

    def _prune_old(self) -> None:
        try:
            files = [p for p in self.cache_dir.glob("*.png") if p.is_file()]
            if len(files) <= _MAX_ART_CACHE_FILES:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            for stale in files[: len(files) - _MAX_ART_CACHE_FILES]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


def parse_dumpsys_media_session(raw_text: str) -> list[dict[str, Any]]:
    """Parse `dumpsys media_session` fallback snapshot when Java helper is unavailable."""
    sessions: list[dict[str, Any]] = []
    seen_pkgs: set[str] = set()
    cur: dict[str, Any] | None = None
    in_stack = False

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            pkg = str(cur.get("package") or "")
            if (
                pkg
                and pkg not in seen_pkgs
                and not (
                    pkg == "com.android.server.telecom"
                    and not cur.get("title")
                    and cur.get("state", 0) == 0
                )
            ):
                seen_pkgs.add(pkg)
                sessions.append(cur)
        cur = None

    for raw_line in raw_text.splitlines():
        s = raw_line.strip()
        if s.startswith("Sessions Stack"):
            in_stack = True
            continue
        if in_stack and (s.startswith("Audio playback") or s.startswith("Media session config")):
            flush()
            break
        if not in_stack:
            continue
        if s.startswith("MediaSession "):
            flush()
            m_tag = re.match(r"MediaSession\s+([^/]+)/([^\s]+)", s)
            pkg = m_tag.group(1) if m_tag else ""
            tag = m_tag.group(2) if m_tag else "MediaSession"
            cur = {
                "package": pkg,
                "tag": tag,
                "status": "Stopped",
                "state": 0,
                "position_ms": 0,
                "updated_elapsed_ms": 0,
                "speed": 1.0,
                "actions": 0,
                "can_play": True,
                "can_pause": True,
                "can_stop": False,
                "can_next": True,
                "can_previous": True,
                "can_seek": False,
                "can_set_speed": False,
                "title": "",
                "artist": "",
                "album": "",
                "album_artist": "",
                "duration_ms": 0,
                "track_number": 0,
                "media_id": "",
                "art_hash": "",
                "art_uri": "",
                "volume": 1.0,
                "compat_mode_supported": False,
                "repeat_mode": -1,
                "shuffle_mode": -1,
                "mode_action": "",
            }
            continue
        if cur is None:
            continue
        if s.startswith("package="):
            cur["package"] = s.split("=", 1)[1].strip()
        elif s.startswith("state=PlaybackState"):
            m_st = re.search(r"state=([A-Z_]+)\((\d+)\)", s)
            if m_st:
                st_name = m_st.group(1)
                st_code = int(m_st.group(2))
                cur["state"] = st_code
                if st_name in ("PLAYING", "BUFFERING", "FAST_FORWARDING", "REWINDING"):
                    cur["status"] = "Playing"
                elif st_name in ("PAUSED", "CONNECTING"):
                    cur["status"] = "Paused"
                else:
                    cur["status"] = "Stopped"
            m_pos = re.search(r"position=(-?\d+)", s)
            if m_pos:
                cur["position_ms"] = max(0, int(m_pos.group(1)))
            m_spd = re.search(r"speed=([0-9.]+)", s)
            if m_spd:
                try:
                    cur["speed"] = max(0.1, float(m_spd.group(1)))
                except ValueError:
                    pass
            m_upd = re.search(r"updated=(\d+)", s)
            if m_upd:
                cur["updated_elapsed_ms"] = int(m_upd.group(1))
            m_act = re.search(r"actions=(\d+)", s)
            if m_act:
                actions = int(m_act.group(1))
                cur["actions"] = actions
                cur["can_play"] = bool(actions & ((1 << 2) | (1 << 9)))
                cur["can_pause"] = bool(actions & ((1 << 1) | (1 << 9)))
                cur["can_stop"] = bool(actions & (1 << 0))
                cur["can_next"] = bool(actions & (1 << 5))
                cur["can_previous"] = bool(actions & (1 << 4))
                cur["can_seek"] = bool(actions & (1 << 8))
        elif s.startswith("metadata:"):
            m_desc = re.search(r"description=(.+)$", s)
            if m_desc:
                parts = [p.strip() for p in m_desc.group(1).split(",")]
                if parts and parts[0] != "null":
                    cur["title"] = parts[0]
                if len(parts) > 1 and parts[1] != "null":
                    cur["artist"] = parts[1]
                if len(parts) > 2 and parts[2] != "null":
                    cur["album"] = parts[2]
    flush()
    return sessions


def _loop_status_from_compat(repeat_mode: int, fallback: str) -> str:
    if repeat_mode == 0:
        return "None"
    if repeat_mode == 1:
        return "Track"
    if repeat_mode in (2, 3):
        return "Playlist"
    return fallback


def _shuffle_from_compat(shuffle_mode: int, fallback: bool) -> bool:
    if shuffle_mode == 0:
        return False
    if shuffle_mode in (1, 2):
        return True
    return fallback


@dataclass
class _MprisState:
    package: str
    identity: str
    desktop_entry: str
    bus_segment: str
    playback_status: str = "Stopped"
    loop_status: str = "None"
    shuffle: bool = False
    rate: float = 1.0
    volume: float = 1.0
    can_go_next: bool = False
    can_go_previous: bool = False
    can_play: bool = False
    can_pause: bool = False
    can_stop: bool = False
    can_seek: bool = False
    can_control: bool = True
    minimum_rate: float = 0.25
    maximum_rate: float = 4.0
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    duration_ms: int = 0
    track_number: int = 0
    media_id: str = ""
    art_url: str = ""
    base_pos_ms: int = 0
    base_monotonic: float = field(default_factory=time.monotonic)
    track_seq: int = 1
    track_fingerprint: tuple[str, str, str, int, str] = ("", "", "", 0, "")
    last_loop_fallback: str = "None"
    last_shuffle_fallback: bool = False

    def track_id(self) -> str:
        return f"/org/droidloom/MediaPlayer/{self.bus_segment}/track/{self.track_seq}"

    def live_position_us(self, now_mono: float | None = None) -> int:
        now = time.monotonic() if now_mono is None else now_mono
        pos_ms = float(self.base_pos_ms)
        if self.playback_status == "Playing":
            elapsed_ms = max(0.0, (now - self.base_monotonic) * 1000.0)
            pos_ms += elapsed_ms * (self.rate if self.rate > 0 else 1.0)
        if self.duration_ms > 0:
            pos_ms = min(pos_ms, float(self.duration_ms))
        return max(0, int(round(pos_ms * 1000.0)))


class MprisPlayerInstance:
    """Single MPRIS 2 D-Bus player object exported for one Android app."""

    def __init__(
        self,
        package: str,
        identity: str,
        desktop_entry: str,
        on_command: Callable[[dict[str, Any]], None],
        on_raise: Callable[[str], None] | None = None,
        on_quit: Callable[[str], None] | None = None,
        art_cache: MediaArtCache | None = None,
        bus_prefix: str = MPRIS_BUS_PREFIX,
        connection: Any | None = None,
        bus_name_override: str | None = None,
    ) -> None:
        from gi.repository import Gio, GLib

        self._Gio = Gio
        self._GLib = GLib
        self.package = package
        self._on_command = on_command
        self._on_raise = on_raise
        self._on_quit = on_quit
        self._art_cache = art_cache or MediaArtCache()

        seg = sanitize_bus_segment(package)
        self.bus_name = bus_name_override or f"{bus_prefix}.{seg}"
        self.state = _MprisState(
            package=package,
            identity=identity or package,
            desktop_entry=desktop_entry or "",
            bus_segment=seg,
        )

        self._owns_connection = connection is None
        if connection is None:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.connection = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
        else:
            self.connection = connection

        self._node_info = Gio.DBusNodeInfo.new_for_xml(MPRIS_INTROSPECTION_XML)
        self._reg_ids: list[int] = []
        for iface in self._node_info.interfaces:
            reg_id = self.connection.register_object(
                MPRIS_OBJECT_PATH,
                iface,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property,
            )
            if reg_id:
                self._reg_ids.append(reg_id)

        self._owner_id = Gio.bus_own_name_on_connection(
            self.connection,
            self.bus_name,
            Gio.BusNameOwnerFlags.REPLACE | Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT,
            None,
            None,
        )
        self._closed = False
        self._last_root_props: dict[str, Any] = {}
        self._last_player_props: dict[str, Any] = {}
        self._snapshot_current_props()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for reg_id in self._reg_ids:
            try:
                self.connection.unregister_object(reg_id)
            except Exception:
                pass
        self._reg_ids.clear()
        if self._owner_id:
            try:
                self._Gio.bus_unown_name(self._owner_id)
            except Exception:
                pass
            self._owner_id = 0
        if self._owns_connection:
            try:
                self.connection.close_sync(None)
            except Exception:
                pass

    def _metadata_dict(self) -> dict[str, Any]:
        GLib = self._GLib
        st = self.state
        md: dict[str, Any] = {
            "mpris:trackid": GLib.Variant("o", st.track_id()),
        }
        if st.duration_ms > 0:
            md["mpris:length"] = GLib.Variant("x", int(st.duration_ms) * 1000)
        if st.art_url:
            md["mpris:artUrl"] = GLib.Variant("s", st.art_url)
        if st.title:
            md["xesam:title"] = GLib.Variant("s", st.title)
        if st.artist:
            md["xesam:artist"] = GLib.Variant("as", [st.artist])
        if st.album:
            md["xesam:album"] = GLib.Variant("s", st.album)
        if st.album_artist:
            md["xesam:albumArtist"] = GLib.Variant("as", [st.album_artist])
        if st.track_number > 0:
            md["xesam:trackNumber"] = GLib.Variant("i", int(st.track_number))
        return md

    def _root_props_variants(self) -> dict[str, Any]:
        GLib = self._GLib
        st = self.state
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", st.identity),
            "DesktopEntry": GLib.Variant("s", st.desktop_entry),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
        }

    def _player_props_variants(self, include_position: bool = False) -> dict[str, Any]:
        GLib = self._GLib
        st = self.state
        props: dict[str, Any] = {
            "PlaybackStatus": GLib.Variant("s", st.playback_status),
            "LoopStatus": GLib.Variant("s", st.loop_status),
            "Rate": GLib.Variant("d", float(st.rate)),
            "Shuffle": GLib.Variant("b", bool(st.shuffle)),
            "Metadata": GLib.Variant("a{sv}", self._metadata_dict()),
            "Volume": GLib.Variant("d", float(st.volume)),
            "MinimumRate": GLib.Variant("d", float(st.minimum_rate)),
            "MaximumRate": GLib.Variant("d", float(st.maximum_rate)),
            "CanGoNext": GLib.Variant("b", bool(st.can_go_next)),
            "CanGoPrevious": GLib.Variant("b", bool(st.can_go_previous)),
            "CanPlay": GLib.Variant("b", bool(st.can_play)),
            "CanPause": GLib.Variant("b", bool(st.can_pause)),
            "CanSeek": GLib.Variant("b", bool(st.can_seek)),
            "CanControl": GLib.Variant("b", bool(st.can_control)),
        }
        if include_position:
            props["Position"] = GLib.Variant("x", st.live_position_us())
        return props

    def _snapshot_current_props(self) -> None:
        self._last_root_props = {k: v.unpack() for k, v in self._root_props_variants().items()}
        self._last_player_props = {
            k: v.unpack() for k, v in self._player_props_variants(include_position=False).items()
        }

    def _emit_properties_changed(self, iface_name: str, changed: dict[str, Any]) -> None:
        if self._closed or not changed:
            return
        try:
            self.connection.emit_signal(
                None,
                MPRIS_OBJECT_PATH,
                DBUS_PROPS_IFACE,
                "PropertiesChanged",
                self._GLib.Variant("(sa{sv}as)", (iface_name, changed, [])),
            )
        except Exception:
            pass

    def _emit_seeked(self, pos_us: int) -> None:
        if self._closed:
            return
        try:
            self.connection.emit_signal(
                None,
                MPRIS_OBJECT_PATH,
                MPRIS_IFACE_PLAYER,
                "Seeked",
                self._GLib.Variant("(x)", (max(0, int(pos_us)),)),
            )
        except Exception:
            pass

    def update_from_session(self, data: dict[str, Any], now_elapsed_ms: int | None = None) -> None:
        if self._closed:
            return
        st = self.state
        now_mono = time.monotonic()
        prev_expected_us = st.live_position_us(now_mono)

        title = str(data.get("title") or "")
        artist = str(data.get("artist") or "")
        album = str(data.get("album") or "")
        album_artist = str(data.get("album_artist") or "")
        duration_ms = max(0, int(data.get("duration_ms") or 0))
        track_number = max(0, int(data.get("track_number") or 0))
        media_id = str(data.get("media_id") or "")

        fp = (title, artist, album, duration_ms, media_id)
        track_changed = fp != st.track_fingerprint
        if track_changed:
            st.track_seq += 1
            st.track_fingerprint = fp

        st.title = title
        st.artist = artist
        st.album = album
        st.album_artist = album_artist
        st.duration_ms = duration_ms
        st.track_number = track_number
        st.media_id = media_id

        # Art resolution: accept inline PNG base64 or cached hash; never expose raw
        # Android file:// paths, and only fetch remote HTTP(S) URIs when opted in.
        art_hash = str(data.get("art_hash") or "")
        art_b64 = str(data.get("art_png_b64") or "")
        art_uri = str(data.get("art_uri") or "")
        if art_b64 and art_hash:
            st.art_url = self._art_cache.store_base64(art_hash, art_b64)
        elif art_hash:
            cached = self._art_cache.lookup(art_hash)
            if cached:
                st.art_url = cached
        elif (
            art_uri
            and os.environ.get("DROIDLOOM_MPRIS_FETCH_REMOTE_ART") == "1"
            and _is_safe_remote_art_url(art_uri)
        ):
            cached_remote = self._art_cache.lookup(f"url:{art_uri}")
            if cached_remote:
                st.art_url = cached_remote
            else:
                def _on_art(_url: str, local_uri: str) -> None:
                    self._GLib.idle_add(self._apply_async_art, local_uri)

                self._art_cache.fetch_remote_async(art_uri, _on_art)
        else:
            st.art_url = ""

        status = str(data.get("status") or "Stopped")
        if status not in ("Playing", "Paused", "Stopped"):
            status = "Stopped"
        st.playback_status = status

        speed = float(data.get("speed") or 1.0)
        st.rate = speed if speed > 0 else 1.0
        st.volume = max(0.0, min(1.0, float(data.get("volume", st.volume))))

        pos_ms = max(0, int(data.get("position_ms") or 0))
        updated_elapsed_ms = int(data.get("updated_elapsed_ms") or 0)
        if status == "Playing" and now_elapsed_ms and updated_elapsed_ms > 0:
            delta_ms = max(0, int(now_elapsed_ms) - updated_elapsed_ms)
            pos_ms += int(round(delta_ms * st.rate))
        if st.duration_ms > 0:
            pos_ms = min(pos_ms, st.duration_ms)
        st.base_pos_ms = pos_ms
        st.base_monotonic = now_mono

        st.can_play = bool(data.get("can_play", True))
        st.can_pause = bool(data.get("can_pause", True))
        st.can_stop = bool(data.get("can_stop", False))
        st.can_go_next = bool(data.get("can_next", False))
        st.can_go_previous = bool(data.get("can_previous", False))
        st.can_seek = bool(data.get("can_seek", False))
        if data.get("can_set_speed"):
            st.minimum_rate = 0.25
            st.maximum_rate = 4.0
        else:
            st.minimum_rate = 1.0
            st.maximum_rate = 1.0

        compat_ok = bool(data.get("compat_mode_supported", False))
        if compat_ok:
            st.loop_status = _loop_status_from_compat(
                int(data.get("repeat_mode", -1)), st.last_loop_fallback
            )
            st.shuffle = _shuffle_from_compat(
                int(data.get("shuffle_mode", -1)), st.last_shuffle_fallback
            )
        else:
            st.loop_status = st.last_loop_fallback
            st.shuffle = st.last_shuffle_fallback

        self._emit_diffs()

        new_pos_us = st.live_position_us(now_mono)
        if track_changed or abs(new_pos_us - prev_expected_us) > 1_500_000:
            self._emit_seeked(new_pos_us)

    def _apply_async_art(self, local_uri: str) -> bool:
        if self._closed or not local_uri:
            return False
        if self.state.art_url != local_uri:
            self.state.art_url = local_uri
            self._emit_diffs()
        return False

    def _emit_diffs(self) -> None:
        root_vars = self._root_props_variants()
        root_changed = {
            k: v for k, v in root_vars.items() if self._last_root_props.get(k) != v.unpack()
        }
        if root_changed:
            for k, v in root_changed.items():
                self._last_root_props[k] = v.unpack()
            self._emit_properties_changed(MPRIS_IFACE_ROOT, root_changed)

        player_vars = self._player_props_variants(include_position=False)
        player_changed = {
            k: v for k, v in player_vars.items() if self._last_player_props.get(k) != v.unpack()
        }
        if player_changed:
            for k, v in player_changed.items():
                self._last_player_props[k] = v.unpack()
            self._emit_properties_changed(MPRIS_IFACE_PLAYER, player_changed)

    def _handle_get_property(
        self,
        _conn: Any,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
    ) -> Any:
        if interface_name == MPRIS_IFACE_ROOT:
            return self._root_props_variants().get(property_name)
        if interface_name == MPRIS_IFACE_PLAYER:
            return self._player_props_variants(include_position=True).get(property_name)
        return None

    def _handle_set_property(
        self,
        _conn: Any,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
        value: Any,
    ) -> bool:
        if interface_name != MPRIS_IFACE_PLAYER:
            return False
        val = value.unpack()
        if property_name == "LoopStatus":
            ls = str(val)
            if ls not in ("None", "Track", "Playlist"):
                return False
            self.state.last_loop_fallback = ls
            self.state.loop_status = ls
            self._emit_diffs()
            self._on_command(
                {"cmd": "set_loop_status", "package": self.package, "loop_status": ls}
            )
            return True
        if property_name == "Shuffle":
            shuf = bool(val)
            self.state.last_shuffle_fallback = shuf
            self.state.shuffle = shuf
            self._emit_diffs()
            self._on_command(
                {"cmd": "set_shuffle", "package": self.package, "shuffle": shuf}
            )
            return True
        if property_name == "Rate":
            rate = max(self.state.minimum_rate, min(self.state.maximum_rate, float(val)))
            self.state.rate = rate
            self._emit_diffs()
            self._on_command({"cmd": "set_rate", "package": self.package, "rate": rate})
            return True
        if property_name == "Volume":
            vol = max(0.0, min(1.0, float(val)))
            self.state.volume = vol
            self._emit_diffs()
            self._on_command({"cmd": "set_volume", "package": self.package, "volume": vol})
            return True
        return False

    def _handle_method_call(
        self,
        _conn: Any,
        _sender: str,
        _object_path: str,
        interface_name: str,
        method_name: str,
        parameters: Any,
        invocation: Any,
    ) -> None:
        args = parameters.unpack() if parameters is not None else ()
        if interface_name == MPRIS_IFACE_ROOT:
            if method_name == "Raise":
                if self._on_raise is not None:
                    self._on_raise(self.package)
                invocation.return_value(None)
                return
            if method_name == "Quit":
                if self._on_quit is not None:
                    self._on_quit(self.package)
                invocation.return_value(None)
                return

        if interface_name == MPRIS_IFACE_PLAYER:
            simple_map = {
                "Play": "play",
                "Pause": "pause",
                "PlayPause": "play_pause",
                "Stop": "stop",
                "Next": "next",
                "Previous": "previous",
            }
            if method_name in simple_map:
                self._on_command({"cmd": simple_map[method_name], "package": self.package})
                invocation.return_value(None)
                return
            if method_name == "Seek":
                offset_us = int(args[0]) if args else 0
                cur_us = self.state.live_position_us()
                target_us = max(0, cur_us + offset_us)
                if self.state.duration_ms > 0:
                    target_us = min(target_us, self.state.duration_ms * 1000)
                self._apply_local_seek(target_us)
                invocation.return_value(None)
                return
            if method_name == "SetPosition":
                req_track = str(args[0]) if len(args) >= 1 else ""
                pos_us = int(args[1]) if len(args) >= 2 else 0
                if req_track == self.state.track_id() and pos_us >= 0:
                    if self.state.duration_ms > 0:
                        pos_us = min(pos_us, self.state.duration_ms * 1000)
                    self._apply_local_seek(pos_us)
                invocation.return_value(None)
                return
            if method_name == "OpenUri":
                invocation.return_value(None)
                return

        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod",
            f"Unknown method {interface_name}.{method_name}",
        )

    def _apply_local_seek(self, target_us: int) -> None:
        pos_ms = max(0, int(round(target_us / 1000.0)))
        self.state.base_pos_ms = pos_ms
        self.state.base_monotonic = time.monotonic()
        self._emit_seeked(pos_ms * 1000)
        self._on_command(
            {"cmd": "seek_to", "package": self.package, "position_ms": pos_ms}
        )


def _find_dex_path() -> Path | None:
    here = Path(__file__).resolve()
    xdg_data = os.environ.get("XDG_DATA_HOME")
    data_home = (
        Path(xdg_data) if (xdg_data and Path(xdg_data).is_absolute()) else Path.home() / ".local/share"
    )
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    cache_home = (
        Path(xdg_cache) if (xdg_cache and Path(xdg_cache).is_absolute()) else Path.home() / ".cache"
    )
    candidates = [
        data_home / "droidloom-settings/assets/droidloom-media-bridge.dex",
        here.parents[2] / "assets/droidloom-media-bridge.dex",
        cache_home / "droidloom-settings/droidloom-media-bridge.dex",
    ]
    for p in candidates:
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


class AndroidMprisBridgeManager:
    """Supervises Android MediaSession bridge and dynamic per-app MPRIS 2 D-Bus players."""

    def __init__(self, controller: Any, *, media_socket_path: Path | None = None) -> None:
        from gi.repository import GLib

        self._GLib = GLib
        self._controller = controller
        self._media_socket_path = media_socket_path
        self._art_cache = MediaArtCache()
        self._players: dict[str, MprisPlayerInstance] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._native_thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self._native_conn: socket.socket | None = None
        self._native_lock = threading.Lock()
        self._native_connected = threading.Event()
        self._listen_sock: socket.socket | None = None
        self._owned_sock_path: Path | None = None
        self._pushed_key: tuple[str, str] | None = None
        self._fallback_mode = False

    def start(self) -> None:
        self._stop_event.clear()
        if self._native_thread is None or not self._native_thread.is_alive():
            self._native_thread = threading.Thread(
                target=self._native_socket_loop, daemon=True, name="dl-mpris-native"
            )
            self._native_thread.start()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._supervisor_loop, daemon=True, name="dl-mpris-bridge"
            )
            self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._send_bridge_command({"cmd": "quit"})
        with self._native_lock:
            nconn = self._native_conn
            self._native_conn = None
        self._native_connected.clear()
        if nconn is not None:
            try:
                nconn.close()
            except OSError:
                pass
        lsock = self._listen_sock
        self._listen_sock = None
        if lsock is not None:
            try:
                lsock.close()
            except OSError:
                pass
        if self._owned_sock_path is not None:
            self._owned_sock_path.unlink(missing_ok=True)
            self._owned_sock_path = None
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._GLib.idle_add(self._clear_all_players)

    def _clear_all_players(self) -> bool:
        for player in list(self._players.values()):
            player.close()
        self._players.clear()
        return False

    def _terminate_adb_proc(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _native_socket_loop(self) -> None:
        listen_sock, owned_path = _acquire_media_listen_socket(self._media_socket_path)
        if listen_sock is None:
            return
        self._listen_sock = listen_sock
        self._owned_sock_path = owned_path
        allowed_uids = _allowed_peer_uids()
        ucred_size = struct.calcsize("3i")
        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = listen_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                try:
                    raw_cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, ucred_size)
                    _pid, peer_uid, _gid = struct.unpack("3i", raw_cred)
                    if peer_uid not in allowed_uids:
                        conn.close()
                        continue
                except OSError:
                    conn.close()
                    continue

                conn.settimeout(None)
                with self._native_lock:
                    prev = self._native_conn
                    self._native_conn = conn
                if prev is not None and prev is not conn:
                    try:
                        prev.close()
                    except OSError:
                        pass
                self._native_connected.set()
                self._terminate_adb_proc()

                try:
                    reader = conn.makefile("rb")
                    while not self._stop_event.is_set():
                        raw_line = reader.readline(_MAX_NATIVE_LINE_BYTES + 1)
                        if not raw_line or len(raw_line) > _MAX_NATIVE_LINE_BYTES:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if payload.get("event") == "sessions":
                            self._GLib.idle_add(self._apply_sessions_payload, payload)
                except OSError:
                    pass
                finally:
                    with self._native_lock:
                        if self._native_conn is conn:
                            self._native_conn = None
                            self._native_connected.clear()
                    try:
                        conn.close()
                    except OSError:
                        pass
                    self._GLib.idle_add(self._clear_all_players)
        finally:
            if self._listen_sock is listen_sock:
                self._listen_sock = None
                try:
                    listen_sock.close()
                except OSError:
                    pass
            if self._owned_sock_path is not None:
                self._owned_sock_path.unlink(missing_ok=True)
                self._owned_sock_path = None

    def _supervisor_loop(self) -> None:
        helper_failures = 0
        while not self._stop_event.is_set():
            try:
                if (
                    self._native_connected.is_set()
                    or os.environ.get("DROIDLOOM_MPRIS_ADB_FALLBACK") == "0"
                ):
                    self._stop_event.wait(2.0)
                    continue
                status_resp = self._controller.client.status()
                if status_resp.state != "running":
                    self._GLib.idle_add(self._clear_all_players)
                    self._stop_event.wait(3.0)
                    continue
                cell_ip = self._controller._cell_ip()
                ok_adb, target = is_adb_connected(cell_ip)
                if not ok_adb:
                    self._GLib.idle_add(self._clear_all_players)
                    self._stop_event.wait(3.0)
                    continue

                selector = target or DEFAULT_SELECTOR
                dex_path = _find_dex_path()
                if dex_path is None or helper_failures >= 3:
                    self._fallback_mode = True
                    self._poll_fallback_once(selector)
                    self._stop_event.wait(3.0)
                    continue

                self._fallback_mode = False
                if not self._ensure_dex_pushed(dex_path, selector):
                    helper_failures += 1
                    self._poll_fallback_once(selector)
                    self._stop_event.wait(3.0)
                    continue

                ok_run = self._run_dex_session(selector)
                if ok_run:
                    helper_failures = 0
                else:
                    helper_failures += 1
                self._GLib.idle_add(self._clear_all_players)
                self._stop_event.wait(2.0)
            except Exception:
                self._stop_event.wait(3.0)

    def _ensure_dex_pushed(self, dex_path: Path, selector: str) -> bool:
        try:
            digest = hashlib.sha256(dex_path.read_bytes()).hexdigest()[:16]
            key = (selector, digest)
            if self._pushed_key == key:
                return True
            res = subprocess.run(
                ["adb", "-s", selector, "push", str(dex_path), "/data/local/tmp/droidloom-media-bridge.dex"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if res.returncode == 0:
                self._pushed_key = key
                return True
        except Exception:
            pass
        return False

    def _run_dex_session(self, selector: str) -> bool:
        cmd = [
            "adb",
            "-s",
            selector,
            "shell",
            "CLASSPATH=/data/local/tmp/droidloom-media-bridge.dex",
            "app_process",
            "/system/bin",
            "org.droidloom.mediabridge.Main",
        ]
        saw_ready = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception:
            return False

        with self._proc_lock:
            self._proc = proc
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if self._stop_event.is_set():
                    break
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = payload.get("event")
                if ev == "ready":
                    saw_ready = True
                elif ev == "sessions":
                    saw_ready = True
                    self._GLib.idle_add(self._apply_sessions_payload, payload)
        except Exception:
            pass
        finally:
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return saw_ready

    def _poll_fallback_once(self, selector: str) -> None:
        ok, out = adb_shell("dumpsys media_session", selector=selector, timeout=4.0)
        if not ok:
            return
        sessions = parse_dumpsys_media_session(out)
        self._GLib.idle_add(
            self._apply_sessions_payload,
            {"event": "sessions", "now_elapsed_ms": 0, "sessions": sessions},
        )

    def _apply_sessions_payload(self, payload: dict[str, Any]) -> bool:
        if self._stop_event.is_set():
            return False
        sessions = payload.get("sessions") or []
        now_elapsed_ms = payload.get("now_elapsed_ms")
        desktop_map = scan_desktop_entries(getattr(self._controller, "apps_dir", None))

        desired_pkgs: list[str] = []
        by_pkg: dict[str, dict[str, Any]] = {}
        for s in sessions:
            pkg = str(s.get("package") or "").strip()
            if not pkg or pkg in by_pkg:
                continue
            by_pkg[pkg] = s
            desired_pkgs.append(pkg)

        # Remove vanished players
        for stale_pkg in list(self._players.keys()):
            if stale_pkg not in by_pkg:
                player = self._players.pop(stale_pkg)
                player.close()

        # Create/update active players
        for pkg in desired_pkgs:
            s = by_pkg[pkg]
            player = self._players.get(pkg)
            if player is None:
                desk_stem, desk_name, _ = desktop_map.get(pkg, ("", "", ""))
                app_label, _, _ = self._controller._app_label_and_icon(pkg)
                identity = app_label if app_label and app_label != pkg else (desk_name or pkg)
                player = MprisPlayerInstance(
                    package=pkg,
                    identity=identity,
                    desktop_entry=desk_stem,
                    on_command=self._handle_player_command,
                    on_raise=self._handle_player_raise,
                    on_quit=self._handle_player_quit,
                    art_cache=self._art_cache,
                )
                self._players[pkg] = player
            player.update_from_session(s, now_elapsed_ms=now_elapsed_ms)
        return False

    def _send_bridge_command(self, cmd: dict[str, Any]) -> bool:
        data = (json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8")
        with self._native_lock:
            nconn = self._native_conn
            if nconn is not None:
                try:
                    nconn.sendall(data)
                    return True
                except OSError:
                    pass
        with self._proc_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                return False
            try:
                proc.stdin.write(data.decode("utf-8"))
                proc.stdin.flush()
                return True
            except Exception:
                return False

    def _handle_player_command(self, cmd: dict[str, Any]) -> None:
        if self._send_bridge_command(cmd):
            return
        if not self._fallback_mode:
            return
        op = cmd.get("cmd")
        key_map = {
            "play": "play",
            "pause": "pause",
            "play_pause": "play-pause",
            "stop": "stop",
            "next": "next",
            "previous": "previous",
        }
        key = key_map.get(str(op))
        if key:
            def _run() -> None:
                ok, target = is_adb_connected(self._controller._cell_ip())
                if ok:
                    adb_shell(f"cmd media_session dispatch {key}", selector=target, timeout=3.0)

            threading.Thread(target=_run, daemon=True).start()

    def _handle_player_raise(self, package: str) -> None:
        threading.Thread(
            target=lambda: self._controller.launch_app(package), daemon=True
        ).start()

    def _handle_player_quit(self, package: str) -> None:
        def _stop() -> None:
            self._send_bridge_command({"cmd": "stop", "package": package})
            self._controller.stop_app(package)

        threading.Thread(target=_stop, daemon=True).start()
