# Fedora Linux Packaging & Hardware Adaptation

Packaging definitions and host adaptations validated on **Fedora Linux 45 (`aarch64`)**
with KDE Plasma Wayland (`kwin_wayland`), SELinux (`Enforcing`), `firewalld`,
and Qualcomm Snapdragon 8 Elite (`SM8750` / Adreno 830 `/dev/dri/renderD128` via `msm_dpu`).

## Contents

- [droidloom-image.spec](droidloom-image.spec): RPM spec packaging `/usr/lib/droidloom/{images,runtime,storage,current}`.
- [droidloom-runtime.spec](droidloom-runtime.spec): RPM spec packaging host binaries, systemd units, SELinux module, `firewalld` rules, and Android cell overrides.
- [selinux/droidloom_fedora.te](selinux/droidloom_fedora.te): Type Enforcement policy allowing `binder` IPC, `mac_admin`/`mac_override`, container entrypoint labels, and tmpfs/pseudofs association under `unconfined_t` / `unconfined_service_t`.
- [firewalld/zones/droidloom.xml](firewalld/zones/droidloom.xml) & [firewalld/policies/droidloom-egress.xml](firewalld/policies/droidloom-egress.xml): Outbound NAT/forwarding zone and policy for Droidloom veth interfaces (`dlh+`, `10.177.0.0/30`).
- [density-desktop.json](density-desktop.json): Sample `$XDG_CONFIG_HOME/droidloom/density-v1.json` setting `dpiPerScale: 160` so `1 Android dp` matches `1 Wayland logical pixel`.
- [braun/](braun/): `libcamera`-backed `org.braun.Camera1` system service (`droidloom-braun`) bridging host cameras to `droidloom-devices` and Android's camera HAL.

## Host Adaptations Required on Fedora + Adreno 830

### 1. SELinux Policy Module
Compile [selinux/droidloom_fedora.te](selinux/droidloom_fedora.te) into `/usr/share/selinux/packages/droidloom_fedora.pp` before packaging `droidloom-runtime`:

```sh
checkmodule -M -m -o droidloom_fedora.mod packaging/fedora/selinux/droidloom_fedora.te
semodule_package -o droidloom_fedora.pp -m droidloom_fedora.mod
```

The `%post` scriptlet in `droidloom-runtime.spec` installs the module with `semodule -i`.

### 2. Fedora `bpffs` SELinux Context Compatibility (`ClatCoordinator`)
On Fedora with SELinux enforcing on the shared host kernel, `/sys/fs/bpf` nodes carry `u:object_r:bpf_t:s0` rather than Android's `u:object_r:fs_bpf:s0`. Android's `libservice-connectivity.so` (`verifyPerms` in `ClatCoordinator`) originally tolerated missing `security.selinux` xattrs (`EOPNOTSUPP`/`ENODATA`) under `DROIDLOOM_CELL`, but aborted `system_server` when an xattr existed with `bpf_t != fs_bpf`.

Staging `/usr/lib/droidloom/fedora-classpath-compat` (derived from `com.android.tethering` `classpath-compat` with `ClatCoordinator` context-mismatch checks relaxed under `DROIDLOOM_CELL`) and overriding `/droidloom/classpath-compat` avoids `system_server` boot crash loops.

### 3. Adreno 830 (`A8XX`) `minigbm` UBWC Mitigation
Android's prebuilt `libminigbm_gralloc.so` (`backend_msm`) computes `DRM_FORMAT_MOD_QCOM_COMPRESSED` (UBWC) buffer sizes using Adreno 6xx `64x16` macrotile alignment (`32,563,200` bytes at `3392x2400`), whereas Adreno 830 Turnip writes `96x32` UBWC (`33,177,600` bytes), causing an Adreno SMMU fault and kernel GPU recovery deadlock.

Do **not** switch to `vendor.minigbm.allocator=dma_heap_images`, because `dma_heap_images` allocates unpadded-height buffers (for example `2,244,608` bytes at `548x974`) that are 4 KiB smaller than Turnip's overrun guard-page requirement (`2,248,704` bytes), crashing `SurfaceFlinger` `RenderEngine` on multi-layer dialogs/overlays.

Instead, keep the default `backend_msm` allocator (`/dev/dri/renderD128` linear GEM BOs with `64x64` width/height alignment) and disable UBWC compression in the staged `/usr/lib/droidloom/fedora-init.rc` (`early-init`) and `/usr/lib/droidloom/fedora-vendor-build.prop`:

- In `fedora-init.rc`:
  ```rc
  export DROIDLOOM_CELL 1
  export vendor.minigbm.debug nocompression
  ```
- In `fedora-vendor-build.prop`:
  ```ini
  vendor.minigbm.debug=nocompression
  ```

### 4. `firewalld` Outbound Forwarding
Place [droidloom.xml](firewalld/zones/droidloom.xml) under `/usr/lib/firewalld/zones/` and [droidloom-egress.xml](firewalld/policies/droidloom-egress.xml) under `/usr/lib/firewalld/policies/`, then reload `firewalld` (`firewall-cmd --reload`, handled by `%post`).

## Building `droidloom-wayland` & RPMs (Rootless Podman)

```sh
podman build -t localhost/droidloom-wayland-builder:fc45 - <<'EOF'
FROM registry.fedoraproject.org/fedora:45
RUN dnf install -y --setopt=install_weak_deps=False \
    rust cargo gcc pkgconf-pkg-config \
    wayland-devel libxkbcommon-devel mesa-libgbm-devel libdrm-devel && \
    dnf clean all
EOF

podman run --rm -v "$PWD:/src:Z" -w /src localhost/droidloom-wayland-builder:fc45 \
  cargo build --locked --release -j 6 -p droidloom-wayland --bin droidloom-wayland

rpmbuild --define "_topdir $PWD/.work/fedora/rpmbuild" \
         --define "stage_dir $PWD/.work/fedora/stage-runtime" \
         -bb packaging/fedora/droidloom-runtime.spec

rpmbuild --define "_topdir $PWD/.work/fedora/rpmbuild" \
         --define "stage_dir $PWD/.work/fedora/stage-image" \
         -bb packaging/fedora/droidloom-image.spec
```

Install both packages on the host with `rpm -Uvh --nosignature`, then configure desktop-mode sizing:

```sh
mkdir -p ~/.config/droidloom
cp packaging/fedora/density-desktop.json ~/.config/droidloom/density-v1.json
droidloomctl window-mode windowed --width 960 --height 680
droidloomctl start
```

To start Droidloom automatically with your Plasma Wayland session after reboot, also run:

```sh
systemctl --user enable droidloom.service
```
