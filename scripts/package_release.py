#!/usr/bin/env python3
"""Build deterministic PhotoGIMP archives for supported platforms."""

from __future__ import annotations

import argparse
import hashlib
import json
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
#   selection / Ctrl+Shift+Z redo / Ctrl+L levels / Ctrl+M curves /
#   Ctrl+1 100% zoom / Ctrl+I invert image (perceptual; Colors > Invert
#   is three GEGL ops and the plain Invert is filters-invert-perceptual) /
#   Ctrl+Alt+J Layer via Copy (user-defined action from
#   photogimp-actions.json, registered by the photogimp-actions plug-in).
#   Ctrl+]/Ctrl+[ raise/lower layer / Ctrl+Shift+]/Ctrl+Shift+[ to
#   top/bottom / Alt+]/Alt+[ select next/previous layer.
#   (Ctrl+Shift+[ / ] use the shifted keysyms braceleft/braceright:
#   GTK matches a real Ctrl+Shift+[ press as braceleft, so the
#   bracketleft spelling never fires -- see the fork shortcutsrc.)
#   Levels/Curves are the filters-* GEGL actions, which open the dialogs;
#   the tools-* actions only select the tool, so tools-levels/tools-curves
#   are asserted keyless below.
PHOTOSHOP_SHORTCUTS: dict[str, tuple[str, ...]] = {
    "layers-merge-down": ("<Primary>e",),
    "image-merge-layers": ("<Primary><Shift>e",),
    "select-invert": ("<Primary><Shift>i",),
    "edit-redo": ("<Primary><Shift>z",),
    "tools-curves": (),
    "tools-levels": (),
    "filters-curves": ("<Primary>m",),
    "filters-levels": ("<Primary>l",),
    "view-zoom-1-1": ("<Primary>1", "1", "KP_1"),
    "filters-invert-perceptual": ("<Primary>i",),
    "photogimp-layer-via-copy": ("<Primary><Alt>j",),
    "layers-raise": ("<Primary>bracketright",),
    "layers-lower": ("<Primary>bracketleft",),
    "layers-raise-to-top": ("<Primary><Shift>braceright",),
    "layers-lower-to-bottom": ("<Primary><Shift>braceleft",),
    "layers-select-next": ("<Alt>bracketright",),
    "layers-select-previous": ("<Alt>bracketleft",),
    "tools-transform-again": ("<Primary><Shift>t",),
    "tools-transform-duplicate": ("<Primary><Alt>t",),
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
        # Never ship editor/sync backups (e.g. shortcutsrc.bak-<timestamp>).
        if ".bak-" in file.name or file.name.endswith(".bak"):
            continue
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


def verify_actions_file(archive_path: Path, json_entry: str, loader_entry: str) -> None:
    """Fail unless the packaged user-defined actions are valid and wired up.

    Reads photogimp-actions.json back out of the finished archive, validates
    its schema, checks every action's shortcut agrees with the shortcuts
    gate (so the JSON, the shortcutsrc, and this file cannot drift apart),
    and checks the loader plug-in is present and executable (GIMP ignores
    a plug-in file without the executable bit).
    """
    with zipfile.ZipFile(archive_path) as archive:
        try:
            data = json.loads(archive.read(json_entry).decode("utf-8"))
        except KeyError:
            raise SystemExit(f"{archive_path.name}: missing {json_entry} in archive")
        try:
            info = archive.getinfo(loader_entry)
        except KeyError:
            raise SystemExit(f"{archive_path.name}: missing {loader_entry} in archive")
        loader = archive.read(loader_entry)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SystemExit(f"{archive_path.name}: {json_entry} has an unsupported format")
    actions = data.get("actions", [])
    if not actions:
        raise SystemExit(f"{archive_path.name}: {json_entry} defines no actions")
    for action in actions:
        if not isinstance(action, dict):
            raise SystemExit(f"{archive_path.name}: {json_entry} has a non-object action")
        action_id = action.get("id", "")
        if not re.fullmatch(r"[a-z0-9-]+", action_id):
            raise SystemExit(f"{archive_path.name}: invalid action id {action_id!r}")
        for field in ("label", "menu", "shortcut"):
            if not action.get(field):
                raise SystemExit(f"{archive_path.name}: action {action_id!r} has no {field}")
        steps = action.get("steps", [])
        if not steps:
            raise SystemExit(f"{archive_path.name}: action {action_id!r} has no steps")
        for step in steps:
            if not isinstance(step, dict) or not step.get("procedure") \
                    or not isinstance(step.get("args", {}), dict):
                raise SystemExit(f"{archive_path.name}: action {action_id!r} has a bad step")
        proc = f"photogimp-{action_id}"
        expected = (action["shortcut"],)
        if PHOTOSHOP_SHORTCUTS.get(proc) != expected:
            raise SystemExit(f"{archive_path.name}: action {proc} shortcut {expected} "
                             f"does not match the shortcuts gate {PHOTOSHOP_SHORTCUTS.get(proc)}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if not mode & 0o111:
        raise SystemExit(f"{archive_path.name}: {loader_entry} is not executable (mode {oct(mode)})")
    if not loader.startswith(b"#!/usr/bin/env python3"):
        raise SystemExit(f"{archive_path.name}: {loader_entry} is missing its python3 shebang")
    print(f"{archive_path.name}: user-defined actions OK ({len(actions)} action(s), loader executable)")


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
            sync_script = ROOT / "scripts" / "sync-actions.py"
            if sync_script.exists():
                add_file(archive, sync_script, f"{prefix}/scripts/sync-actions.py")
            shortcuts_entry = f"{prefix}/.config/GIMP/{version}/shortcutsrc"
            json_entry = f"{prefix}/.config/GIMP/{version}/photogimp-actions.json"
            loader_entry = f"{prefix}/.config/GIMP/{version}/plug-ins/photogimp-actions/photogimp-actions.py"
        else:
            add_tree(archive, config, version)
            shortcuts_entry = f"{version}/shortcutsrc"
            json_entry = f"{version}/photogimp-actions.json"
            loader_entry = f"{version}/plug-ins/photogimp-actions/photogimp-actions.py"
    verify_photoshop_shortcuts(output, shortcuts_entry)
    verify_actions_file(output, json_entry, loader_entry)


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
