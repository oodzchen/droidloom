# NativeBridge ABI Compatibility & Per-App ABI Alignment

- **Status**: Deferred design note. Revisit if important ARM64-only APKs without upstream `x86_64` builds fail due to Java-vs-native ABI mismatches.

## Problem Statement

On `x86_64` Droidloom with the Teto ARM64 NativeBridge enabled (`ro.product.cpu.abilist=x86_64,arm64-v8a`), apps installed as `arm64-v8a` run their native JNI/ELF libraries under Berberis (`ApplicationInfo.primaryCpuAbi == "arm64-v8a"`), while the ART JVM runs natively on `x86_64`.

As a result, Java APIs such as `android.os.Build.CPU_ABI` and `android.os.Build.SUPPORTED_ABIS[0]` still report `"x86_64"` inside an `arm64-v8a` app process. Apps or cross-platform engines that read `Build.CPU_ABI` / `SUPPORTED_ABIS[0]` to locate ABI-specific assets or fetch architecture-matched plugins then look for `x86_64` resources inside an `arm64-v8a`-only package or environment.

### Observed Case: Fennec (`org.mozilla.fennec_fdroid` 160.0.1)

1. F-Droid publishes both `arm64-v8a` (`versionCode=1600120`) and `x86_64` (`versionCode=1600110`) for `org.mozilla.fennec_fdroid` 160.0.1. Because F-Droid picks the highest compatible `versionCode` rather than `SUPPORTED_ABIS` preference order, it installed `1600120` (`arm64-v8a`).
2. `GeckoLoader` sets `MOZ_ANDROID_CPU_ABI` from `android.os.Build.CPU_ABI` (`"x86_64"`), then loads `libxul.so` (`arm64-v8a`) under Berberis.
3. During startup, `Preferences::Init()` reads `getenv("MOZ_ANDROID_CPU_ABI")` and attempts to load `x86_64/greprefs.js` from `assets/omni.ja`. However, the `arm64-v8a` APK only packages `arm64-v8a/greprefs.js` and `defaults/pref/arm64-v8a/geckoview-prefs.js`.
4. `Preferences::Init()` fails, `Preferences::GetInstanceForService()` returns `nullptr` (mapped by XPCOM to `NS_ERROR_OUT_OF_MEMORY` / `0x8007000e`), leaving `sPreferences == nullptr` and causing a deterministic `SIGSEGV` (`si_addr=0x8`) in `Preferences::InitializeUserPrefs()`.

## Current Recommended Practice (Option 1)

Whenever an upstream application publishes a native `x86_64` APK (including Fennec on F-Droid), install the native `x86_64` build instead of running the `arm64-v8a` build under translation.

- In F-Droid, select the `x86_64` version (`1600110` for Fennec `160.0.1`) and enable **Ignore updates** if the `arm64-v8a` variant carries a higher `versionCode`.
- Native `x86_64` avoids binary translation overhead and double-JIT issues (e.g., SpiderMonkey ARM64 JIT translated by Berberis) for browsers and heavy runtime engines.

## Deferred System-Level Design (Option 2: Per-App ARM64 ABI View Alignment)

### Goal

Improve out-of-the-box compatibility for **ARM64-only apps** that have no `x86_64` release and rely on `Build.CPU_ABI` / `Build.SUPPORTED_ABIS[0]` to load internal assets or download plugins/hotfix `.so` modules.

### Strict Scoping Requirement

Per-app ABI alignment **must apply only** inside app processes whose bound `ApplicationInfo.primaryCpuAbi` is `"arm64-v8a"`.

It **must never** affect:
- `system_server` or platform services
- App stores / package installers (F-Droid, Aurora, Play, etc.) — otherwise stores would stop offering native `x86_64` builds
- Pure-Java/Kotlin apps (`primaryCpuAbi == null`)
- Native `x86_64` apps (`primaryCpuAbi == "x86_64"`)

### AOSP Implementation Notes & Zygote Pitfall

In AOSP `frameworks/base/core/java/android/os/Build.java`, `Build.CPU_ABI`, `Build.CPU_ABI2`, `Build.SUPPORTED_ABIS`, and `Build.SUPPORTED_64_BIT_ABIS` are `public static final` fields initialized in `Build`'s static initializer during **Zygote preload** (before `fork()`).

Implementing per-app alignment therefore requires:
1. Routing or updating those fields after process specialization (e.g., during `Zygote.specializeAppProcess` or `ActivityThread.handleBindApplication` when `primaryCpuAbi` is `"arm64-v8a"`).
2. Ensuring `Build.CPU_ABI` / `SUPPORTED_ABIS` are not treated as compile-time or ART-preloaded immutable constants across app processes, and optionally evaluating `System.getProperty("os.arch")` consistency.
3. Rebuilding and shipping updated `framework.jar` / boot image patches in Droidloom's Android tree.

### Limitations & Trade-offs

- **Scope of fix**: Resolves only Java-vs-native ABI view mismatches (`Build.CPU_ABI`, `SUPPORTED_ABIS`). It does **not** fix Berberis instruction translation gaps, self-modifying/JIT `W^X` faults (`SEGV_ACCERR`), or anti-cheat / integrity checks.
- **Maintenance cost**: Adds ongoing AOSP `frameworks/base` patch surface and boot-image rebuild/testing burden, so it should remain deferred until concrete ARM64-only apps without `x86_64` builds require it.
