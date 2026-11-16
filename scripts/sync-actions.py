#!/usr/bin/env python3
"""Sync photogimp-actions.json shortcuts into a GIMP shortcutsrc.

The JSON file is the source of truth for user-defined actions; this script
writes each action's shortcut into shortcutsrc as an active binding for the
procedure photogimp-<id>. It never steals a key: if the chord is already
bound to a different action, it fails and reports the conflict. Idempotent:
running it twice changes nothing the second time. Use --dry-run first.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

ACTION_RE = re.compile(r'^\((action) "([^"]+)"((?: "[^"]*")*)\)\s*$')
COMMENTED_RE = re.compile(r'^#\s*\(action "([^"]+)"((?: "[^"]*")*)\)\s*$')


def load_actions(json_path: Path) -> list[dict]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"sync-actions: no such file: {json_path}")
    except ValueError as exc:
        sys.exit(f"sync-actions: invalid JSON in {json_path}: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1:
        sys.exit(f"sync-actions: unsupported format in {json_path}")
    actions = []
    for action in data.get("actions", []):
        if not isinstance(action, dict):
            sys.exit("sync-actions: action entry is not an object")
        action_id = action.get("id", "")
        if not re.fullmatch(r"[a-z0-9-]+", action_id):
            sys.exit(f"sync-actions: invalid action id {action_id!r}")
        if not action.get("steps"):
            sys.exit(f"sync-actions: action {action_id!r} has no steps")
        if not action.get("shortcut"):
            sys.exit(f"sync-actions: action {action_id!r} has no shortcut")
        actions.append(action)
    return actions


def keys_of(line: str) -> tuple[str, ...] | None:
    match = ACTION_RE.match(line.strip())
    if not match:
        return None
    return tuple(re.findall(r'"([^"]*)"', match.group(3)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True,
                        help="GIMP config dir holding photogimp-actions.json and shortcutsrc")
    parser.add_argument("--json", type=Path, default=None,
                        help="path to photogimp-actions.json (default: <config>/photogimp-actions.json)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    json_path = args.json or (args.config / "photogimp-actions.json")
    shortcuts_path = args.config / "shortcutsrc"
    actions = load_actions(json_path)
    if not shortcuts_path.is_file():
        sys.exit(f"sync-actions: no shortcutsrc at {shortcuts_path}")

    lines = shortcuts_path.read_text(encoding="utf-8").splitlines()

    # Active bindings per chord, for the conflict check.
    bound: dict[tuple[str, ...], str] = {}
    for line in lines:
        match = ACTION_RE.match(line.strip())
        if match:
            bound[tuple(re.findall(r'"([^"]*)"', match.group(3)))] = match.group(2)

    changes: list[str] = []
    for action in actions:
        proc = f"photogimp-{action['id']}"
        wanted = (action["shortcut"],)
        # Conflict: chord already bound to a different action.
        if wanted in bound and bound[wanted] != proc:
            sys.exit(f"sync-actions: CONFLICT: {action['shortcut']} is already bound to "
                     f"{bound[wanted]!r}; refusing to steal it for {proc!r}")
        # Existing line for this procedure (active or commented)?
        target = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            for pattern in (ACTION_RE, COMMENTED_RE):
                match = pattern.match(stripped)
                if match and match.group(2) == proc:
                    target = index
                    break
            if target is not None:
                break
        wanted_line = f'(action "{proc}" "{action["shortcut"]}")'
        if target is not None:
            if lines[target].strip() == wanted_line:
                changes.append(f"ok: {proc} already bound to {action['shortcut']}")
            else:
                changes.append(f"update: {proc} -> {action['shortcut']} (was: {lines[target].strip()})")
                if not args.dry_run:
                    lines[target] = wanted_line
        else:
            changes.append(f"add: {proc} -> {action['shortcut']}")
            if not args.dry_run:
                lines.append(wanted_line)

    for change in changes:
        print(change)
    if args.dry_run:
        print("dry_run: nothing written.")
        return
    if all(change.startswith("ok:") for change in changes):
        print("sync-actions: already in sync, nothing written.")
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = shortcuts_path.with_name(f"shortcutsrc.bak-{stamp}")
    shutil.copyfile(shortcuts_path, backup)
    shortcuts_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sync-actions: wrote {shortcuts_path} (backup {backup})")


if __name__ == "__main__":
    main()
