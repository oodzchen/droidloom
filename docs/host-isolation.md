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

### Phase 2 — Resource & Hardware Containment (Implemented)

1. **Per-stream audio volume in desktop mode (`droidloom-audio`)**:
   - *Change*: Added `--volume-mode` (`stream` | `host` | `off`) and `DROIDLOOM_HOST_VOLUME` override. In `DROIDLOOM_MODE=desktop`, `droidloom-audio` defaults to `stream` (`StreamVolumeHub`), scaling only Droidloom's own PipeWire playback streams (`pw_stream_set_control`) and reflecting per-stream mixer adjustments back to Android without touching the host default sink's master volume or `SPA_PARAM_ROUTE_save`. `mobile` retains `host` mode.
   - *Impact*: Android apps and volume sliders can no longer alter the host's global speaker volume or mute state while still preserving independent Android volume control and KDE Plasma per-app stream mixer sync.
   - *Trade-off*: Android's system volume slider no longer controls the host's master speaker volume; if an app expects hardware master-volume behavior on desktop, users must set `DROIDLOOM_HOST_VOLUME=host`.
2. **Cgroup memory cap & container-local LMKD (`droidloomd.service` + `droidloom-lmkd-compat`)**:
   - *Change*: Added `MemoryHigh=40%`, `MemoryMax=50%`, and `TasksMax=8192` to `droidloomd.service`. Upgraded `droidloom-lmkd-compat` to track Android 17 `LMK_PROCPRIO` (16/20/24-byte) and batch `LMK_PROCS_PRIO` (cmd 11) updates in memory, monitor the cell root `/sys/fs/cgroup` (`memory.current` and PSI `memory.pressure`), and reclaim background/cached Android processes (`oom_score_adj >= 200`) inside the cell before host-wide OOM occurs. Also bind-mounted `/vendor/bin/droidloom-lmkd-compat` in `cell-spec-*.json` so containers run the updated binary instead of the baked-in vendor copy. Because `droidloom-supervisor` mounts `/proc` read-only (`bind,ro`, leaving only `/proc/sys/net` writable) for host kernel safety, `/proc/<pid>/oom_score_adj` remains `0` in kernel sysfs/procfs while `droidloom-lmkd-compat` enforces priority-ordered background reclaim directly.
   - *Impact*: Guarantees runaway Android games or memory leaks are contained and reclaimed inside the cell rather than triggering the Linux OOM killer against desktop applications, without weakening read-only `/proc` host isolation.
   - *Trade-off*: Heavy Android workloads approaching 40–50% of host RAM (or sustained PSI pressure under parent `MemoryHigh`) will have background Android apps terminated earlier, and host tools reading `/proc/<pid>/oom_score_adj` still see `0` while `/proc` stays read-only; hosts with custom budgets should override via `systemctl edit droidloomd.service`.
3. **Narrow default shared folders (`cell-spec-*.json`)**:
   - *Change*: Defaulted `share_user_folders` to `false` in `packaging/cell-spec-arm64.json` and `packaging/cell-spec-x86_64-u1000.json`, and added a safety notice in `droidloom-settings` Storage UI recommending a dedicated exchange folder (such as `~/Downloads/Droidloom`) instead of exposing `~/Documents`.
   - *Impact*: Prevents Android apps with storage permission from reading or modifying host files/executables in `~/Documents`, `~/Pictures`, `~/Music`, `~/Videos`, and `~/Downloads`, and avoids mutating `setgid` bits on standard host XDG folders.
   - *Trade-off*: Android apps can only access files explicitly placed into configured `shared_storage_directories` (e.g., `~/Downloads/Droidloom`) unless the user opts back into `share_user_folders`.

### Phase 3 — Security & Sidecar Consolidation (In Progress)

1. **Host D-Bus & KDE global cache hardening (Phase 3a — Implemented)**:
   - *Change*: Restricted `packaging/fedora/braun/org.braun.conf` from `context="default"` to `at_console="true"`, and updated `mime_assoc.py` / `install-user.sh` to refresh KDE caches incrementally without deleting `~/.cache/icon-cache.kcache`.
   - *Impact*: Prevents non-console background services from probing `org.braun.Camera1` and avoids wiping the host desktop's shared KDE icon cache during `.apk` MIME updates.
   - *Trade-off*: Headless SSH/remote sessions without a local console seat cannot invoke `org.braun.Camera1` over system D-Bus; KDE icon cache refreshes rely on `gtk-update-icon-cache` and incremental `kbuildsycoca6`.
2. **Native Unix-socket media bridge foundation (Phase 3b-1 — Implemented)**:
   - *Change*: Added `droidloom-panel.socket` (`%t/droidloom/media.sock`, `0600`), wired `droidloom.service` ordering and `droidloom-supervisor` bind-mounting into `/dev/socket/droidloom-framework-host/media`, and upgraded `mpris_bridge.py` (`AndroidMprisBridgeManager`) to accept systemd `LISTEN_FDS` / `AF_UNIX` connections with `SO_PEERCRED` UID verification and a 4 MiB line cap (retiring `adb shell` automatically once connected).
   - *Impact*: Establishes the authenticated, systemd-owned Unix-socket transport for MPRIS2 media metadata/control without needing ADB once Android-side `HostChannel.MEDIA` is wired.
   - *Trade-off*: Until Phase 3b-2 adds the Android-side `HostChannel.MEDIA` + `DroidloomSystemUI` bridge, `mpris_bridge.py` keeps the existing `adb shell` session as fallback (`DROIDLOOM_MPRIS_ADB_FALLBACK=0` disables fallback).
3. **Android-side `MediaSessionBridge` over `HostChannel.MEDIA` (Phase 3b-2 — Implemented)**:
   - *Change*: Added `HostChannel.MEDIA = 6` in `HostChannel.java` and authorized it exclusively for `com.android.systemui` (`SOCK_STREAM`) in `DroidloomHostChannels.java`; added `MEDIA_CONTENT_CONTROL` permission and `MediaSessionBridge` inside `DroidloomSystemUI` (`SystemUIService`), running MediaSession tracking, cover art encoding, and `MediaSessionCompat` repeat/shuffle handling on a background `HandlerThread`; switched `mpris_bridge.py` legacy ADB `app_process` fallback to off by default (opt-in via `DROIDLOOM_MPRIS_ADB_FALLBACK=1`).
   - *Impact*: Eliminates persistent `adb push` + `adb shell app_process` injection in normal operation while keeping live MPRIS2 metadata, album art, and playback control working natively over `/run/user/1000/droidloom/media.sock`.
   - *Trade-off*: Requires `services.jar` (plus recompiled `oat/x86_64`) and `SystemUI.apk` overrides; older Android images without `HostChannel.MEDIA` require `DROIDLOOM_MPRIS_ADB_FALLBACK=1`.
4. **Shared-kernel cell capability, network & SELinux hardening (Phase 3c-1 — Implemented)**:
   - *Change*: Confined `droidloomd` and the Android cell to a dedicated Fedora SELinux domain `droidloom_t` (`packaging/fedora/selinux/droidloom_fedora.te` v2.0 + `droidloom_fedora.fc` on `/usr/bin/droidloomd`), revoking `binder`, `mac_admin`, `mac_override`, and `unlabeled_t` `entrypoint` grants from `unconfined_t` and `unconfined_service_t` while excluding kernel module/SELinux policy loading attributes from `droidloom_t`. Stripped 10 host-dangerous Linux capabilities (`CAP_SYS_MODULE`, `CAP_SYS_RAWIO`, `CAP_SYS_BOOT`, `CAP_SYS_TIME`, `CAP_PERFMON`, `CAP_CHECKPOINT_RESTORE`, `CAP_AUDIT_READ`, `CAP_LINUX_IMMUTABLE`, `CAP_SYS_PACCT`, `CAP_LEASE`) from the cell's `CapBnd` before `exec("/init")` (retaining `CAP_AUDIT_CONTROL` for `logd`, `CAP_SYSLOG` for `logd`/`bpfloader`, and `CAP_WAKE_ALARM` for `audioserver` as requested by stock Android `.rc` files). Guarded `development_network.rs` `ip_forward` teardown/recovery so it never disables IPv4 forwarding while Docker, Podman, libvirt, WireGuard, or another Droidloom cell is active (and Fedora keeps runtime env in `/etc/droidloom/runtime.env` without global `/usr/lib/environment.d/60-droidloom.conf`).
   - *Impact*: Eliminates broad host `unconfined_t`/`unconfined_service_t` SELinux privilege expansion, prevents Android root inside the cell from loading kernel modules, rebooting the host, modifying host clock/dmesg/perf/immutable flags, and prevents stopping Droidloom from breaking concurrent VPN/container forwarding.
   - *Trade-off*: Relies on `/usr/bin/droidloomd` carrying `droidloom_exec_t` (via `restorecon -F`) and keeps `CAP_SYS_ADMIN` / `CAP_NET_ADMIN` / `CAP_MAC_ADMIN` inside `droidloom_t` for `init_user_ns` mounts and `bpffs`.
5. **Full container User Namespace (Phase 3c-2 — Deferred / Upstream Architecture)**:
   - *Blocker & Plan*: On Linux, `apexd` ext4/loop mounts, `netbpfload` `bpffs`/eBPF loading, and `power` HAL `CAP_SYS_NICE` uclamp require `init_user_ns` unless brokered out to the host supervisor. Full subordinate UID/GID mapping (`CLONE_NEWUSER`) remains a larger upstream architecture milestone requiring `SO_PEERCRED` UID translation across all host brokers and migrating existing `/var/lib/droidloom` `/data` ownership.
