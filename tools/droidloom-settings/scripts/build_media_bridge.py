#!/usr/bin/env python3
"""Compile the Android MediaSession Java bridge into a Dalvik DEX file."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
MAIN_JAVA = ROOT_DIR / "android-media-bridge/src/org/droidloom/mediabridge/Main.java"


def _find_javac() -> str | None:
    exe = shutil.which("javac")
    if exe:
        return exe
    for cand in sorted(Path("/usr/lib/jvm").glob("*/bin/javac"), reverse=True):
        if cand.is_file():
            return str(cand)
    return None


def _sdk_roots() -> list[Path]:
    roots: list[Path] = []
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env_var, "").strip()
        if val:
            roots.append(Path(val))
    roots.append(Path.home() / "Android/Sdk")
    return [r for r in roots if r.is_dir()]


def _find_d8() -> str | None:
    exe = shutil.which("d8")
    if exe:
        return exe
    for root in _sdk_roots():
        for cand in sorted(root.glob("build-tools/*/d8"), reverse=True):
            if cand.is_file():
                return str(cand)
        latest = root / "cmdline-tools/latest/bin/d8"
        if latest.is_file():
            return str(latest)
    return None


def _find_android_jar() -> Path | None:
    def _api_key(p: Path) -> tuple[int, int]:
        name = p.parent.name
        ver = name.removeprefix("android-")
        parts = ver.split(".", 1)
        try:
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            return (major, minor)
        except ValueError:
            return (0, 0)

    cands: list[Path] = []
    for root in _sdk_roots():
        cands.extend(root.glob("platforms/android-*/android.jar"))
    cands = [p for p in cands if p.is_file()]
    if not cands:
        return None
    cands.sort(key=_api_key, reverse=True)
    return cands[0]


def build_dex(output: Path, force: bool = False) -> int:
    if not MAIN_JAVA.is_file():
        print(f"Missing Java source: {MAIN_JAVA}", file=sys.stderr)
        return 1

    if not force and output.is_file() and output.stat().st_size > 0:
        if output.stat().st_mtime >= MAIN_JAVA.stat().st_mtime:
            print(f"Up to date: {output}")
            return 0

    javac = _find_javac()
    d8 = _find_d8()
    android_jar = _find_android_jar()
    if not javac or not d8 or android_jar is None:
        print(
            f"Android build tools unavailable (javac={javac}, d8={d8}, android.jar={android_jar})",
            file=sys.stderr,
        )
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dl-media-bridge-") as tmp:
        tmp_path = Path(tmp)
        classes_dir = tmp_path / "classes"
        dex_dir = tmp_path / "dex"
        classes_dir.mkdir()
        dex_dir.mkdir()

        subprocess.run(
            [
                javac,
                "-source",
                "11",
                "-target",
                "11",
                "-Xlint:-options",
                "-classpath",
                str(android_jar),
                "-d",
                str(classes_dir),
                str(MAIN_JAVA),
            ],
            check=True,
        )

        class_files = sorted(str(p) for p in classes_dir.rglob("*.class"))
        if not class_files:
            print("javac produced no .class files", file=sys.stderr)
            return 1

        subprocess.run(
            [d8, "--min-api", "30", "--output", str(dex_dir), *class_files],
            check=True,
        )
        built_dex = dex_dir / "classes.dex"
        if not built_dex.is_file():
            print("d8 did not produce classes.dex", file=sys.stderr)
            return 1

        tmp_out = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        shutil.copy2(built_dex, tmp_out)
        tmp_out.replace(output)

    print(f"Built {output} ({output.stat().st_size} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Droidloom Android MediaSession DEX bridge")
    parser.add_argument("--output", required=True, help="Destination .dex file path")
    parser.add_argument("--force", action="store_true", help="Rebuild even if output is newer")
    args = parser.parse_args(argv)
    return build_dex(Path(args.output), force=bool(args.force))


if __name__ == "__main__":
    raise SystemExit(main())
