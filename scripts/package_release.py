#!/usr/bin/env python3
"""Build deterministic PhotoGIMP archives for supported platforms."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / ".config" / "GIMP"
LINUX_DESKTOP = ROOT / ".local" / "share" / "applications" / "org.gimp.GIMP.desktop"
LINUX_ICONS = ROOT / ".local" / "share" / "icons"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)

# Photoshop-alignment shortcuts (branch photoshop-alignment). These are the
# bindings the fork promises over stock GIMP/PhotoGIMP, and the build FAILS
# if any of them is missing or changed in the packaged shortcutsrc, so a
# bad merge can never ship a zip without them. Keys are the exact quoted
# accelerator strings from the (action ...) line, in order.
#   Ctrl+E merge down / Ctrl+Shift+E merge visible / Ctrl+Shift+I invert
#   selection / Ctrl+Shift+Z redo / Ctrl+M curves / Ctrl+1 100% zoom /
#   Ctrl+I invert image (perceptual; Colors > Invert is three GEGL ops and
#   the plain Invert is filters-invert-perceptual).
PHOTOSHOP_SHORTCUTS: dict[str, tuple[str, ...]] = {
    "layers-merge-down": ("<Primary>e",),
    "image-merge-layers": ("<Primary><Shift>e",),
    "select-invert": ("<Primary><Shift>i",),
    "edit-redo": ("<Primary><Shift>z",),
    "tools-curves": ("<Primary>m",),
    "view-zoom-1-1": ("<Primary>1", "1", "KP_1"),
    "filters-invert-perceptual": ("<Primary>i",),
}

_ACTION_RE = re.compile(r'^\(action "([^"]+)"((?: "[^"]*")*)\)\s*$')


def discover_config() -> tuple[str, Path]:
    versions = sorted(path for path in CONFIG_ROOT.iterdir() if path.is_dir())
    if len(versions) != 1:
        found = ", ".join(path.name for path in versions) or "none"
        raise SystemExit(f"Expected exactly one GIMP config version in {CONFIG_ROOT}; found: {found}")
    return versions[0].name, versions[0]


def iter_files(path: Path):
    yield from sorted((item for item in path.rglob("*") if item.is_file()), key=lambda p: p.as_posix())


def add_file(archive: zipfile.ZipFile, source: Path, destination: str) -> None:
    info = zipfile.ZipInfo(destination, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = stat.S_IMODE(source.stat().st_mode)
    info.external_attr = (mode & 0xFFFF) << 16
    archive.writestr(info, source.read_bytes())


def add_tree(archive: zipfile.ZipFile, source: Path, destination: str) -> None:
    for file in iter_files(source):
        add_file(archive, file, f"{destination}/{file.relative_to(source).as_posix()}")


def parse_shortcuts(text: str) -> dict[str, tuple[str, ...]]:
    """Active (uncommented) bindings from a shortcutsrc file."""
    found: dict[str, tuple[str, ...]] = {}
    for line in text.splitlines():
        match = _ACTION_RE.match(line.strip())
        if match:
            keys = tuple(re.findall(r'"([^"]*)"', match.group(2)))
            found[match.group(1)] = keys
    return found


def verify_photoshop_shortcuts(archive_path: Path, shortcuts_entry: str) -> None:
    """Fail unless the packaged shortcutsrc carries every Photoshop binding.

    Reads the shortcutsrc back out of the finished archive, so this gates
    the actual build output, not just the source tree.
    """
    with zipfile.ZipFile(archive_path) as archive:
        try:
            text = archive.read(shortcuts_entry).decode("utf-8")
        except KeyError:
            raise SystemExit(f"{archive_path.name}: missing {shortcuts_entry} in archive")
    found = parse_shortcuts(text)
    problems = []
    for action, expected in PHOTOSHOP_SHORTCUTS.items():
        if action not in found:
            problems.append(f"{action}: missing (expected {list(expected)})")
        elif found[action] != expected:
            problems.append(f"{action}: got {list(found[action])}, expected {list(expected)}")
    if problems:
        detail = "; ".join(problems)
        raise SystemExit(f"{archive_path.name}: Photoshop shortcuts check FAILED: {detail}")
    print(f"{archive_path.name}: Photoshop shortcuts OK ({len(PHOTOSHOP_SHORTCUTS)} bindings)")


def build_archive(output: Path, platform: str, version: str, config: Path) -> None:
    with zipfile.ZipFile(output, "w") as archive:
        if platform == "linux":
            prefix = "PhotoGIMP-linux"
            add_tree(archive, config, f"{prefix}/.config/GIMP/{version}")
            add_file(archive, LINUX_DESKTOP, f"{prefix}/.local/share/applications/{LINUX_DESKTOP.name}")
            add_tree(archive, LINUX_ICONS, f"{prefix}/.local/share/icons")
            installer = ROOT / "install.sh"
            if installer.exists():
                add_file(archive, installer, f"{prefix}/install.sh")
            shortcuts_entry = f"{prefix}/.config/GIMP/{version}/shortcutsrc"
        else:
            add_tree(archive, config, version)
            shortcuts_entry = f"{version}/shortcutsrc"
    verify_photoshop_shortcuts(output, shortcuts_entry)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    version, config = discover_config()

    outputs = {
        "linux": args.output / "PhotoGIMP-linux.zip",
        "windows": args.output / "PhotoGIMP-windows.zip",
        "macos": args.output / "PhotoGIMP-macos.zip",
    }
    for platform, output in outputs.items():
        build_archive(output, platform, version, config)

    checksum = args.output / "SHA256SUMS.txt"
    checksum.write_text(
        "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in outputs.values()),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
