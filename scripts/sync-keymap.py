#!/usr/bin/env python3
"""Canonical PhotoGIMP keymap sync: sync the Photoshop-alignment bindings
into ONE live GIMP profile.

Replaces the three per-gap installers (gap1-sync.py, gap4-sync.py,
scope-sync.py) with a single script driven by the gate dict
PHOTOSHOP_SHORTCUTS in package_release.py (same directory). The dict is
the single source of the wanted bindings; this script duplicates none of
it. Importing package_release.py is side-effect free (module-level code
is only defs/constants; main() is guarded).

Run once per user, AS that user, so ownership stays theirs
(e.g. `sudo -n -u astorite python3 scripts/sync-keymap.py --apply
--profile /home/astorite/.config/GIMP/3.2/shortcutsrc`).

Rules (same as the per-gap scripts):
- Dry-run is the default (no flag, or --dry-run): prints exactly what
  would change, writes nothing.
- Refuses to steal a bound chord (a wanted chord already bound to a
  different action), refuses if a wanted action is already actively
  bound to something else, refuses on --apply if a GIMP using THIS
  profile is running (it would rewrite shortcutsrc on exit; a GIMP on
  another display with a scratch profile does not block the sync).
- Idempotent: re-running after a successful apply changes nothing and
  does not overwrite the pre-change backup.
- Snapshot-before-mutate: exact bytes + SHA-256 go to
  shortcutsrc-backup-20261127-keymap beside the live file, written once.
- Merge modes per action:
    KEEP           already bound exactly as wanted
    REPLACE-STOCK  a commented line for the action exists in the TARGET
                   profile -- replace the first such line in place
                   (derived from the target itself, not from the fork
                   file: live profiles may carry commented stock lines
                   the fork file does not)
    REPLACE-ACTIVE the action currently carries the superseded
                   <Primary><Shift>bracket* spelling for
                   layers-raise-to-top / layers-lower-to-bottom --
                   upgrade in place to the brace* spelling
    APPEND         otherwise, at end of file
  Keyless entries (tools-curves, tools-levels) use the same modes;
  their line is `(action "name")` with no accels.
  Never leaves duplicate active lines.
- Deliberately NOT managed: dialogs-layers is an active keyless line in
  the fork shortcutsrc but is NOT in PHOTOSHOP_SHORTCUTS, so this script
  never adds, removes, or rewrites it.
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_release import PHOTOSHOP_SHORTCUTS, parse_shortcuts

BINDINGS = PHOTOSHOP_SHORTCUTS

# Superseded 2026-11-26 spec strings for the two Ctrl+Shift chords:
# <Primary><Shift>bracket* never fires in GTK (a real Ctrl+Shift+[ press
# is keysym braceleft). A profile carrying them is upgraded in place.
OLD_BINDINGS = {
    "layers-raise-to-top": ("<Primary><Shift>bracketright",),
    "layers-lower-to-bottom": ("<Primary><Shift>bracketleft",),
}

ACTION_RE = re.compile(r'^\(action "([^"]+)"((?: "[^"]*")*)\)\s*$')
COMMENT_RE = re.compile(r'^\s*#\s*\(action "([^"]+)"((?: "[^"]*")*)\)\s*$')
BACKUP_NAME = "shortcutsrc-backup-20261127-keymap"


def active_map(text):
    return parse_shortcuts(text)


def commented_lines(text):
    """First commented line per action in the target profile.

    Maps action -> exact line (without trailing newline). Only the first
    commented line for an action is recorded; REPLACE-STOCK replaces
    that one line in place.
    """
    found = {}
    for line in text.splitlines():
        m = COMMENT_RE.match(line)
        if m and m.group(1) not in found:
            found[m.group(1)] = line
    return found


def gimp_using_profile_for_current_user(profile_dir):
    """GIMP processes owned by the current user whose effective profile dir
    is profile_dir. A GIMP on another display with a scratch profile (e.g.
    another wave worker's GIMP3_DIRECTORY) never rewrites this file, so it
    must not block the sync; a GIMP using THIS profile would rewrite
    shortcutsrc on exit and must block it. Returns [(pid, cmd)] hits."""
    me = os.getuid()
    target = os.path.realpath(profile_dir)
    home = os.path.expanduser("~")
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            st = os.stat(f"/proc/{pid}")
            if st.st_uid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read().split(b"\x00")
            tokens = [t.decode("utf-8", "replace") for t in raw if t]
            if not tokens:
                continue
            # Only the GIMP binary itself (argv[0] is the gimp executable),
            # not a shell wrapper whose command string merely mentions it,
            # and not plug-in children (script-fu etc.).
            if os.path.basename(tokens[0]) not in (
                    "gimp-3.2", "gimp-3", "gimp", "gimp-console-3.2"):
                continue
            cmd = " ".join(tokens)
            env = {}
            with open(f"/proc/{pid}/environ", "rb") as f:
                for item in f.read().split(b"\x00"):
                    if b"=" in item:
                        k, v = item.split(b"=", 1)
                        env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
            if "GIMP3_DIRECTORY" in env:
                prof = env["GIMP3_DIRECTORY"]
            elif "XDG_CONFIG_HOME" in env:
                prof = os.path.join(env["XDG_CONFIG_HOME"], "GIMP", "3.2")
            else:
                prof = os.path.join(home, ".config", "GIMP", "3.2")
            if os.path.realpath(prof) == target:
                hits.append((pid, cmd.strip()))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return hits


def active_line(action, keys=None):
    if keys is None:
        keys = BINDINGS[action]
    if keys:
        return '(action "{}" {})'.format(action, " ".join(f'"{k}"' for k in keys))
    return f'(action "{action}")'


def plan(text):
    """Return (actions, problems, stock_lines).

    actions: list of (action, mode) with mode in KEEP / REPLACE-STOCK /
    REPLACE-ACTIVE / APPEND. problems: list of refusal reasons.
    stock_lines: action -> exact commented line to replace (REPLACE-STOCK
    actions only), derived from the target profile itself.
    """
    active = active_map(text)
    stock = commented_lines(text)
    wanted_chords = {c for v in BINDINGS.values() for c in v}
    problems = []
    for action, keys in active.items():
        for k in keys:
            if k in wanted_chords and action not in BINDINGS:
                problems.append(f"chord {k!r} already bound to {action}")
    for action, exp in BINDINGS.items():
        if action in active and active[action] != exp:
            if OLD_BINDINGS.get(action) == active[action]:
                continue  # upgradeable below, not a refusal
            problems.append(
                f"{action} already bound to {list(active[action])}, expected {list(exp)}")
    if problems:
        return [], problems, {}
    actions = []
    stock_lines = {}
    for action in BINDINGS:
        if action in active and active[action] == BINDINGS[action]:
            actions.append((action, "KEEP"))
        elif action in active and OLD_BINDINGS.get(action) == active[action]:
            actions.append((action, "REPLACE-ACTIVE"))
        elif action in stock:
            actions.append((action, "REPLACE-STOCK"))
            stock_lines[action] = stock[action]
        else:
            actions.append((action, "APPEND"))
    return actions, problems, stock_lines


def apply_plan(text, actions, stock_lines):
    """Apply the plan line-wise, preserving every other byte of the file.

    REPLACE-STOCK replaces the first commented line for the action in
    place (keeping that line's ending); REPLACE-ACTIVE replaces the old
    active line in place; APPEND adds at end of file.
    """
    wanted = {a: m for a, m in actions if m != "KEEP"}
    if not wanted:
        return text
    lines = text.splitlines(keepends=True)
    replaced_stock = set()
    replaced_active = set()
    out = []
    for line in lines:
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):] or "\n"
        # REPLACE-STOCK: first commented line for the action.
        cm = COMMENT_RE.match(stripped)
        if cm and cm.group(1) in wanted and wanted[cm.group(1)] == "REPLACE-STOCK" \
                and cm.group(1) not in replaced_stock \
                and stripped == stock_lines[cm.group(1)]:
            out.append(active_line(cm.group(1)) + ending)
            replaced_stock.add(cm.group(1))
            continue
        # REPLACE-ACTIVE: the old active line for the action.
        am = ACTION_RE.match(stripped)
        if am and am.group(1) in wanted and wanted[am.group(1)] == "REPLACE-ACTIVE" \
                and am.group(1) not in replaced_active \
                and stripped == active_line(am.group(1), OLD_BINDINGS[am.group(1)]):
            out.append(active_line(am.group(1)) + ending)
            replaced_active.add(am.group(1))
            continue
        out.append(line)
    text = "".join(out)
    for action, mode in actions:
        if mode == "REPLACE-STOCK":
            assert action in replaced_stock, f"{action}: stock line not found during apply"
        elif mode == "REPLACE-ACTIVE":
            assert action in replaced_active, f"{action}: old active line not found during apply"
    for action, mode in actions:
        if mode == "APPEND":
            if not text.endswith("\n"):
                text += "\n"
            text += active_line(action) + "\n"
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--profile", required=True,
                    help="path to the live shortcutsrc to sync")
    args = ap.parse_args()
    if args.dry_run and args.apply:
        print("specify exactly one of --dry-run / --apply (no flag = dry-run)")
        return 2
    path = args.profile
    try:
        before = open(path, "rb").read()
    except OSError as exc:
        print(f"cannot read profile {path}: {exc}")
        return 2
    before_sha = hashlib.sha256(before).hexdigest()
    text = before.decode("utf-8")
    print(f"profile: {path}")
    print(f"before sha256: {before_sha} ({len(before)} bytes, "
          f"{len(active_map(text))} active actions)")
    actions, problems, stock_lines = plan(text)
    if problems:
        print("REFUSE:")
        for p in problems:
            print(f"  {p}")
        return 2
    changed = 0
    for action, mode in actions:
        print(f"  {action}: {mode} -> {active_line(action)}")
        if mode != "KEEP":
            changed += 1
    print(f"changed: {changed} of {len(BINDINGS)} bindings")
    if not args.apply:
        print("DRY-RUN: no files written.")
        return 0
    running = gimp_using_profile_for_current_user(os.path.dirname(path))
    if running:
        print("REFUSE: GIMP using this profile is running:")
        for pid, cmd in running:
            print(f"  pid {pid}: {cmd}")
        return 3
    backup = os.path.join(os.path.dirname(path), BACKUP_NAME)
    if os.path.exists(backup):
        bak_sha = hashlib.sha256(open(backup, "rb").read()).hexdigest()
        print(f"backup already exists: {backup} sha256 {bak_sha} (kept, not overwritten)")
    else:
        with open(backup, "wb") as f:
            f.write(before)
        bak_sha = hashlib.sha256(open(backup, "rb").read()).hexdigest()
        assert bak_sha == before_sha, "backup bytes differ from live file!"
        print(f"backup: {backup} sha256 {bak_sha}")
    new_text = apply_plan(text, actions, stock_lines)
    # Post-merge verification before writing.
    after = active_map(new_text)
    for action, exp in BINDINGS.items():
        assert after.get(action) == exp, f"{action}: got {after.get(action)}"
    counts = {}
    for line in new_text.splitlines():
        m = ACTION_RE.match(line.strip())
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    dupes = {a: c for a, c in counts.items() if c > 1}
    assert not dupes, f"duplicate active lines: {dupes}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    new_sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    print(f"after sha256: {new_sha} ({len(new_text.encode())} bytes, "
          f"{len(after)} active actions)")
    print("SYNC OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
