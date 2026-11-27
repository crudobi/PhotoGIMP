#!/usr/bin/env python3
"""Build deterministic PhotoGIMP archives for supported platforms."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
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
#   u cycles the layer scope (Current / All visible / Selected).
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
    "tools-layer-scope-cycle": ("u",),
}

_ACTION_RE = re.compile(r'^\(action "([^"]+)"((?: "[^"]*")*)\)\s*$')

# Headless PDB verification (see verify_pdb_procedures): the dev GIMP is
# probed with python-fu-eval, which only exists in GIMP 3. A missing
# binary warns and skips; any other failure fails the build (fail-closed).
# Runs measured on kaw at ~1-2s wall; the timeout only bounds a hung GIMP.
PDB_CHECK_TIMEOUT_SECONDS = 120


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


def find_shortcut_collisions(found: dict[str, tuple[str, ...]]) -> list[str]:
    """Chords bound by more than one action.

    `found` is the parsed shortcutsrc (see parse_shortcuts): every active
    line, not just the Photoshop gate entries, so a stock binding that
    steals a gate chord fails the build too. Decision, documented:
    a chord repeated within ONE action's own line is NOT a collision --
    the action still owns that chord exactly once (deduped per line).
    A chord bound by two DIFFERENT actions is a collision. Keyless
    actions bind no chords and cannot collide. Commented-out lines
    never reach here: parse_shortcuts only matches lines starting with
    `(action`. Returns one message per colliding chord, naming the
    chord and both actions, in file order.
    """
    owner: dict[str, str] = {}
    collisions: list[str] = []
    for action, keys in found.items():
        for chord in dict.fromkeys(keys):
            previous = owner.setdefault(chord, action)
            if previous != action:
                collisions.append(f"{chord!r} bound by both {previous} and {action}")
    return collisions


def verify_photoshop_shortcuts(archive_path: Path, shortcuts_entry: str) -> None:
    """Fail unless the packaged shortcutsrc carries every Photoshop binding.

    Reads the shortcutsrc back out of the finished archive, so this gates
    the actual build output, not just the source tree. Also fails if any
    chord is bound by more than one action (see find_shortcut_collisions).
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
    problems.extend(
        f"chord collision: {message}" for message in find_shortcut_collisions(found)
    )
    if problems:
        detail = "; ".join(problems)
        raise SystemExit(f"{archive_path.name}: Photoshop shortcuts check FAILED: {detail}")
    print(f"{archive_path.name}: Photoshop shortcuts OK ({len(PHOTOSHOP_SHORTCUTS)} bindings)")


def _pdb_probe_source(procedures: list[str]) -> str:
    """Python source for the headless PDB existence probe.

    Run inside GIMP 3 via python-fu-eval (see verify_pdb_procedures).
    Prints one `PDB-CHECK <name> FOUND|MISSING` line per procedure to
    stdout. `Gimp.get_pdb().lookup_procedure` returns None for an
    unknown procedure, so a missing procedure is a real MISSING line,
    not an exception -- and the gate treats a probe that prints no (or
    incomplete) lines as unverifiable, never as a pass.
    """
    wanted = json.dumps(sorted(set(procedures)))
    return (
        "from gi.repository import Gimp\n"
        "pdb = Gimp.get_pdb()\n"
        f"for _name in {wanted}:\n"
        "    _proc = pdb.lookup_procedure(_name)\n"
        "    print('PDB-CHECK %s %s' % (_name, 'FOUND' if _proc is not None else 'MISSING'), flush=True)\n"
    )


def verify_pdb_procedures(archive_path: Path, procedures: list[str]) -> None:
    """Fail unless every PDB procedure named by the actions exists.

    Runs headless GIMP 3 (gimp-dev on this box) with python-fu-eval and
    looks each procedure up in the PDB. Proven reliable on kaw
    (2026-11-27): the lookup prints FOUND for real procedures and
    MISSING for a bogus name, exits 0 in ~1-2s, and repeats
    deterministically. Two flags are load-bearing: --quit (without it
    GIMP backgrounds itself after the batch and never exits -- a hung
    build) and --new-instance (without it the batch could be routed
    into the user's running GUI GIMP). A timeout bounds a hung GIMP.
    The Script-Fu route was tried first and rejected:
    gimp-procedural-db-query is unbound in GIMP 3.2, and a failing
    Script-Fu batch command leaves GIMP hung until killed.
    If no GIMP binary is present at all, this WARNS and skips: the
    gate must not fail a build on a box without GIMP. Any other
    failure (timeout, non-zero exit, incomplete output) FAILS the
    build: a verification that could not run must never silently pass.
    """
    if not procedures:
        return
    wanted = sorted(set(procedures))
    gimp = shutil.which("gimp-dev") or shutil.which("gimp")
    if gimp is None:
        print(f"{archive_path.name}: PDB procedures check SKIPPED (no GIMP binary found)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".py", prefix="photogimp-pdb-probe-", delete=False) as probe_file:
        probe_file.write(_pdb_probe_source(wanted))
        probe_path = probe_file.name
    try:
        batch = "exec(open(%s).read())" % json.dumps(probe_path)
        result = subprocess.run(
            [gimp, "--no-interface", "--new-instance", "--quit",
             "--batch-interpreter", "python-fu-eval", "-b", batch],
            capture_output=True, text=True, timeout=PDB_CHECK_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        print(f"{archive_path.name}: PDB procedures check SKIPPED (GIMP binary vanished: {gimp})")
        return
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"{archive_path.name}: PDB procedures check FAILED: GIMP timed out "
            f"after {PDB_CHECK_TIMEOUT_SECONDS}s while looking up {wanted}"
        )
    finally:
        Path(probe_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(
            f"{archive_path.name}: PDB procedures check FAILED: GIMP exited "
            f"{result.returncode} while looking up {wanted}"
        )
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("PDB-CHECK "):
            parts = line.split()
            if len(parts) == 3:
                found[parts[1]] = parts[2]
    if set(found) != set(wanted):
        raise SystemExit(
            f"{archive_path.name}: PDB procedures check FAILED: incomplete probe "
            f"output (got {sorted(found)} for {wanted})"
        )
    missing = [name for name in wanted if found[name] != "FOUND"]
    if missing:
        raise SystemExit(
            f"{archive_path.name}: PDB procedures check FAILED: unknown procedure(s): {', '.join(missing)}"
        )
    print(f"{archive_path.name}: PDB procedures OK ({len(wanted)} procedure(s))")


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
    procedures: list[str] = []
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
            procedures.append(step["procedure"])
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
    verify_pdb_procedures(archive_path, procedures)
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
