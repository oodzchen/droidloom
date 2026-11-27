# Host Isolation and Desktop Boundaries

Droidloom was originally designed around integrated mobile/converged Linux hosts (such as Denial and Phonewave), where Android acts as an extension of a single-purpose device shell. When running on a general-purpose multi-tasking Linux desktop (such as Fedora with KDE Plasma), treating Android as an owner of global host resources introduces privacy, stability, and UX conflicts.

This document records the architectural assessment of Droidloom's host boundaries, known host-impact surfaces, and the phased remediation plan with explicit impacts and trade-offs.

## Architectural Strengths

Droidloom's core architecture is well-suited for desktop integration and does not require a rewrite:

- **Independent Wayland surfaces**: Each Android task maps to a standard `xdg_toplevel` using DMA-BUFs and explicit sync (`linux-drm-syncobj`), without compositor plugins or a monolithic container window.
- **Least-privilege broker split**: Privileged `droidloomd` handles namespaces, mounts, cgroups, and veth/nftables plumbing only; presentation, audio, devices, and desktop launchers run as unprivileged per-user brokers with `SO_PEERCRED` authentication.
- **Targeted filesystem protection**: `filesystem_guard.rs` installs an inherited seccomp filter blocking destructive superblock/filesystem ioctls (`FS_IOC_FREEZE`, non-bind remounts, `quotactl`) across shared mounts.

## Host-Impact Audit Matrix

### P0 — Direct Host Stability, Privacy, and UX Conflicts

1. **Clipboard synchronization (`ext-data-control`)**:
   - *Multi-pipe AB-BA deadlock (fixed)*: Requesting multiple MIME offers (`text/html` + `image/png`) while draining pipes sequentially deadlocked GTK3/Firefox main threads on blocking `write()` calls (`>64 KiB`). Fixed by concurrent `poll()` draining in `graphics/droidloom-wayland/src/clipboard/mod.rs`.
   - *Password-manager secret leakage*: Background clipboard monitoring automatically copies desktop clipboard entries carrying `x-kde-passwordManagerHint` or Qt's `ExcludeClipboardContentFromMonitorProcessing` hint into Android.
   - *Eager tmpfs file duplication*: Copying files in a host file manager (`text/uri-list`) or large images eagerly copies up to 256 MiB / 64 files into `/run/user/<uid>/droidloom/clipboard-*` (RAM-backed `tmpfs`) even when no Android window is focused or pasted into.
2. **Unbounded memory and no-op LMKD**:
   - `droidloom-lmkd-compat` acknowledges Android process-score updates without killing processes, while `droidloomd.service` runs with `OOMPolicy=continue` and no `MemoryHigh`/`MemoryMax` cgroup cap. Memory leaks or heavy games inside Android can exhaust host RAM and trigger the host kernel OOM killer against desktop applications.
3. **Global host hardware takeover (Audio master volume & Backlight)**:
   - `droidloom-audio` (`volume.rs` / `volume.c`) binds Android's `STREAM_MUSIC` to the host PipeWire default sink's master volume/mute with `SPA_PARAM_ROUTE_save = true`, allowing Android apps to alter global host volume.
   - `droidloom-devices` (`brightness.rs`) exposes `/sys/class/backlight` and `logind` `SetBrightness` to Android SystemUI, competing with desktop power managers (e.g., KDE PowerDevil).

### P1 — Isolation & System-Wide Configuration Surfaces

4. **Preview container isolation gaps**:
   - `cell.json` reserves `subordinate_uids`/`subordinate_gids`, but full container User Namespaces are not yet enabled: Android `init` runs as host UID 0 with full capabilities, and `system_server` overlaps host UID 1000.
   - `packaging/fedora/selinux/droidloom_fedora.te` grants `mac_admin`, `mac_override`, and `entrypoint` permissions broadly on `unconfined_t` and `unconfined_service_t` instead of a dedicated Droidloom SELinux domain.
5. **Broad default user-folder sharing**:
   - `user_folders.rs` shares `~/Downloads`, `~/Pictures`, `~/Documents`, `~/Music`, and `~/Videos` by default and sets the `setgid` bit on those host directories. Any Android app granted external storage permission can read/write host files in those directories (including executables stored under `~/Documents`).
6. **Global networking and session environment state**:
   - `development_network.rs` toggles global `/proc/sys/net/ipv4/ip_forward` on start/teardown, which can race with Docker, Podman, libvirt, or WireGuard.
   - `packaging/environment.d/60-droidloom.conf` exports hardcoded `DROIDLOOM_HOST_PEER_UID=1000` and `DROIDLOOM_RENDER_NODE=/dev/dri/renderD128` into the user's graphical session.

### P2 — Desktop Companion Helpers (`tools/droidloom-settings` & Fedora Packaging)

7. **Accelerometer sync on sensorless desktops**:
   - `droidloom-orientation-sync.service` historically looped on `ClaimAccelerometer failed: ServiceUnknown` when `iio-sensor-proxy` was absent (fixed in commit `45af99f` to exit cleanly with status `0`). On sensorless desktops, keeping the unit enabled still spawns an unnecessary Python process on every start.
8. **Python sidecars bypassing core RPC boundaries**:
   - `mpris_bridge.py` previously passed raw `file://` `art_uri` strings from Android into `mpris:artUrl` and fetched arbitrary `http://`/`https://` URLs from the host via `urllib.request.urlopen()` (potential SSRF / local-file disclosure) while maintaining a persistent `adb shell` process.
   - `mime_assoc.py` deletes `~/.cache/icon-cache.kcache` and triggers `kbuildsycoca6` during icon updates.
   - `packaging/fedora/braun/org.braun.conf` allows `context="default"` D-Bus callers to `org.braun.Camera1`.

---

## Phased Remediation Roadmap (Impacts & Trade-offs)

### Phase 1 — Immediate Desktop Hardening (Implemented)

1. **Clipboard desktop hardening (`droidloom-wayland`)**:
   - *Change*: Ignore offers with `x-kde-passwordManagerHint` or `ExcludeClipboardContentFromMonitorProcessing` unless `DROIDLOOM_CLIPBOARD_ALLOW_SECRETS=1`. Move temporary clipboard storage from `/run/user/<uid>` to `$XDG_CACHE_HOME/droidloom/clipboard` (or `DROIDLOOM_CLIPBOARD_CACHE_DIR`). Default `DROIDLOOM_MODE=desktop` to focus-gated lazy sync (`DROIDLOOM_CLIPBOARD_SYNC=focus`), deferring `offer.receive()` until an Android window gains focus (`DROIDLOOM_CLIPBOARD_SYNC=eager` restores immediate sync).
   - *Impact*: Prevents password-manager secrets from leaking into Android, eliminates `/run/user/<uid>` tmpfs exhaustion, and avoids copying host files/images unless switching to an Android window.
   - *Trade-off*: If a user copies content on Linux, **closes** the source Linux app before ever focusing an Android window, that clipboard entry will no longer be available in Android when Android is later focused. Moving temp files to disk-backed `$XDG_CACHE_HOME` trades tmpfs RAM safety for slightly higher disk I/O on large transfers.
2. **Desktop backlight decoupling (`droidloom-devices`)**:
   - *Change*: Added `--disable-brightness` and `DROIDLOOM_HOST_BRIGHTNESS` override; `DROIDLOOM_MODE=desktop` defaults host backlight control to disabled (`BrightnessUnavailable`), while `mobile` keeps it enabled.
   - *Impact*: Android apps (such as video players) can no longer alter the host monitor/laptop backlight in desktop mode.
   - *Trade-off*: Android brightness sliders and in-app per-window brightness boosts stay local to Android's virtual state unless `DROIDLOOM_HOST_BRIGHTNESS=1` is set.
3. **MPRIS artwork SSRF & `file://` hardening (`droidloom-settings`)**:
   - *Change*: Stopped passing raw `file://` `art_uri` values from Android into `mpris:artUrl` (only cache-managed `file://` paths from decoded `art_png_b64` are exposed). Disabled host-side remote HTTP(S) fetching by default (`DROIDLOOM_MPRIS_FETCH_REMOTE_ART=1` opts in, with global-IP validation and unsafe-redirect blocking).
   - *Impact*: Eliminates Android-to-host SSRF and local `file://` path injection via MediaSession metadata.
   - *Trade-off*: Android media players that only supply a remote HTTP(S) `art_uri` without inline `Bitmap` artwork (`art_png_b64`) will not show album art in Plasma unless `DROIDLOOM_MPRIS_FETCH_REMOTE_ART=1` is enabled.
4. **Disable `droidloom-orientation-sync.service` on sensorless desktop hosts**:
   - *Change*: Disabled the user unit on this host while keeping the unit file intact.
   - *Impact*: Eliminates unnecessary Python startup on every `droidloom.service` launch.
   - *Trade-off*: If a hardware accelerometer is later connected, the user must re-enable `droidloom-orientation-sync.service`.

### Phase 2 — Resource & Hardware Containment (Planned)

1. **Per-stream audio volume in desktop mode (`droidloom-audio`)**:
   - *Plan*: In `DROIDLOOM_MODE=desktop`, return `VolumeUnavailable` on `volume.sock` (or apply gain only to Droidloom's own PipeWire streams) instead of modifying the host default sink's master volume and `SPA_PARAM_ROUTE_save`.
   - *Trade-off*: Android's system volume slider will no longer control the host's master speaker volume; users adjust Android volume independently via the desktop mixer (e.g., Plasma Audio applet) or inside Android.
2. **Cgroup memory cap & PSI-driven container LMKD**:
   - *Plan*: Configure `MemoryHigh` / `MemoryMax` on `droidloomd.service` (e.g., 40–50% of host RAM) and upgrade `droidloom-lmkd-compat` to monitor the cell's own `memory.pressure` (PSI) and `memory.current` to reclaim background Android processes inside the cell.
   - *Trade-off*: Heavy Android games or simultaneous apps may be killed earlier by Android's LMKD when approaching the cell cap, in exchange for guaranteeing host desktop stability.
3. **Narrow default shared folders**:
   - *Plan*: Replace automatic sharing of all 5 XDG folders (especially `~/Documents`) with a dedicated exchange folder (e.g., `~/Downloads/Droidloom`) or per-file `xdg-desktop-portal` FileChooser grants, avoiding `setgid` changes on standard host directories.
   - *Trade-off*: Android apps will only see files placed in the dedicated shared folder (or explicitly picked via portal) rather than the user's entire `~/Documents` or `~/Pictures` tree.

### Phase 3 — Security & Sidecar Consolidation (Long-Term)

1. **Full container User Namespace & dedicated SELinux domain**:
   - *Plan*: Enable full subordinate UID/GID mapping across the entire cell (eliminating host UID 0 capabilities and UID 1000 overlap) and confine Fedora SELinux rules to dedicated Droidloom domains instead of `unconfined_t`.
   - *Trade-off*: Requires updating `SO_PEERCRED` UID translation across host brokers and regenerating/migrating existing Android `/data` ownership.
2. **Replace ADB-based Python daemons with typed Unix-socket brokers**:
   - *Plan*: Replace `mpris_bridge.py`'s persistent `adb shell` link with a native Rust broker over a dedicated `HostChannel` socket, matching `notifications` and `clipboard`.
   - *Trade-off*: Higher initial implementation effort in SystemUI/framework and Rust, in exchange for removing ADB runtime dependencies and unifying lifecycle/security enforcement.
