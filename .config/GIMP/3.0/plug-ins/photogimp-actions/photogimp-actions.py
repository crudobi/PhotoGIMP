#!/usr/bin/env python3
# PhotoGIMP user-defined actions.
#
# Reads photogimp-actions.json from the GIMP user directory at startup and
# registers one GIMP procedure per action (named photogimp-<id>). Each
# procedure runs its steps in order inside a single undo group, so a
# multi-step action behaves like one built-in command. The procedures appear
# in the menus and in Edit > Keyboard Shortcuts, where they can be bound to
# any key; scripts/sync-actions.py writes the shortcuts from the JSON into
# shortcutsrc.
#
# The JSON file is the source of truth and is never written by GIMP: edit it
# by hand, version it, back it up, share it. After editing, re-run
# scripts/sync-actions.py and restart GIMP.

import gi
gi.require_version('Gimp', '3.0')
from gi.repository import Gimp
from gi.repository import GLib
import json
import os
import re
import sys

ACTIONS_FILE = "photogimp-actions.json"

_actions = None


def _load_actions():
    global _actions
    if _actions is not None:
        return _actions
    _actions = []
    try:
        path = os.path.join(Gimp.directory(), ACTIONS_FILE)
    except Exception as exc:
        sys.stderr.write(f"photogimp-actions: cannot locate user directory: {exc}\n")
        return _actions
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return _actions
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"photogimp-actions: cannot read {path}: {exc}\n")
        return _actions
    if not isinstance(data, dict) or data.get("version") != 1:
        sys.stderr.write(f"photogimp-actions: unsupported format in {path}\n")
        return _actions
    for action in data.get("actions", []):
        if not isinstance(action, dict):
            continue
        action_id = action.get("id", "")
        steps = action.get("steps", [])
        if not re.fullmatch(r"[a-z0-9-]+", action_id) or not steps:
            sys.stderr.write(f"photogimp-actions: skipping invalid action {action_id!r}\n")
            continue
        _actions.append(action)
    return _actions


def _get_action(action_id):
    for action in _load_actions():
        if action.get("id") == action_id:
            return action
    return None


_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?$")


def _resolve(value, context):
    """Resolve one step argument: $refs against the run context, else literal."""
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    match = _REF_RE.match(value)
    if not match:
        raise ValueError(f"bad reference {value!r}")
    name, index = match.group(1), match.group(2)
    if name not in context:
        raise ValueError(f"unknown reference ${name}")
    resolved = context[name]
    if index is not None:
        resolved = resolved[int(index)]
    return resolved


def _call_pdb(proc_name, args):
    """Call a PDB procedure by name; return its return values (excluding status).

    Uses the config API (lookup_procedure + create_config + run), the same
    pattern GIMP's own Python plug-ins use (see file-openraster.py).
    """
    pdb = Gimp.get_pdb()
    procedure = pdb.lookup_procedure(proc_name)
    if procedure is None:
        raise RuntimeError(f"{proc_name}: no such procedure")
    config = procedure.create_config()
    if config.find_property("run-mode") is not None:
        config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    for key, value in args.items():
        if isinstance(value, list):
            # Object arrays (e.g. drawables) need the dedicated setter;
            # set_property cannot convert a Python list to GimpCoreObjectArray.
            config.set_core_object_array(key, value)
        else:
            config.set_property(key, value)
    result = procedure.run(config)
    if result.index(0) != Gimp.PDBStatusType.SUCCESS:
        raise RuntimeError(f"{proc_name}: failed with status {result.index(0)}")
    # The run result is [status, return-value...]. Its length comes from
    # ValueArray.length(): probing index() past the end does NOT raise, it
    # logs a C assertion and returns nothing usable, so an unbounded probe
    # loop spins forever. Object-array returns come back from index() as a
    # bare GBoxed; unwrap those with get_core_object_array, and keep every
    # other value as-is.
    returns = []
    for i in range(1, result.length()):
        value = result.index(i)
        if type(value).__name__ == "GBoxed":
            value = result.get_core_object_array(i)
        returns.append(value)
    return returns


def _run_action(procedure, run_mode, image, drawables, config, data):
    action_id = procedure.get_name().removeprefix("photogimp-")
    action = _get_action(action_id)
    if action is None:
        sys.stderr.write(f"photogimp-actions: unknown action {action_id!r}\n")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error())
    context = {"image": image, "drawables": list(drawables)}
    try:
        image.undo_group_start()
        for step in action["steps"]:
            args = {}
            for key, raw in step.get("args", {}).items():
                args[key] = _resolve(raw, context)
            returns = _call_pdb(step["procedure"], args)
            if "result" in step:
                if len(returns) == 1:
                    context[step["result"]] = returns[0]
                else:
                    context[step["result"]] = returns
        image.undo_group_end()
    except Exception as exc:
        try:
            image.undo_group_end()
        except Exception:
            pass
        sys.stderr.write(f"photogimp-actions: {action_id} failed: {exc}\n")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error())
    Gimp.displays_flush()
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


class PhotoGIMPActions(Gimp.PlugIn):
    def do_set_i18n(self, procname):
        return True, 'gimp30-python', None

    def do_query_procedures(self):
        return [f"photogimp-{action['id']}" for action in _load_actions()]

    def do_create_procedure(self, name):
        action_id = name.removeprefix("photogimp-")
        action = _get_action(action_id)
        if action is None:
            return None
        procedure = Gimp.ImageProcedure.new(self, name,
                                            Gimp.PDBProcType.PLUGIN,
                                            _run_action, None)
        procedure.set_image_types("*")
        procedure.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.DRAWABLE |
            Gimp.ProcedureSensitivityMask.DRAWABLES)
        procedure.set_documentation(action.get("label", action_id), "", name)
        procedure.set_menu_label(action.get("label", action_id))
        procedure.add_menu_path(action.get("menu", "<Image>/Layer/New"))
        procedure.set_attribution("PhotoGIMP", "PhotoGIMP", "2026")
        return procedure


Gimp.main(PhotoGIMPActions.__gtype__, sys.argv)
