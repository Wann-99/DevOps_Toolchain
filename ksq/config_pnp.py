"""Parse ``config_pnp/config.py`` via AST to locate KSQ data files.

The device-side ``config_pnp/config.py`` uses ``config.scene.<key> =
config_pnp_path("<filename>")`` assignments to point at data files whose names
may change (e.g. date-stamped CSVs).  Instead of executing that file — which
carries sandbox-escape risk via ``__builtins__`` access — this module parses it
with :mod:`ast` and extracts only the four keys KSQ cares about: ``shelves``,
``tool_mapping``, ``unavailable``, ``pick_strategy``.

The source code is **never executed** (no ``exec`` / ``eval`` / ``compile``),
so arbitrary code in ``config.py`` cannot run.  Any failure (missing file,
syntax error, etc.) returns an empty dict so callers transparently fall back
to the existing hard-coded defaults — zero regression.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict

# Maps ``config.scene.*`` attribute names to KSQ internal result keys.
SCENE_KEY_MAP: Dict[str, str] = {
    "sku_shelf_export_csv": "shelves",
    "obj_tool_mapping": "tool_mapping",
    "unavailable_obj": "unavailable",
    "pick_strategy_obj": "pick_strategy",
}


def _scene_attr_key(node: ast.AST) -> str | None:
    """Return the attribute name if *node* is ``config.scene.<key>``.

    The target must be a two-level attribute chain: an ``ast.Attribute``
    whose ``value`` is itself an ``ast.Attribute`` with ``attr == "scene"``
    and a ``value`` of ``ast.Name(id == "config")``.  Returns ``None`` for
    anything else (top-level ``config.x``, bare names, tuples, etc.).
    """
    if not isinstance(node, ast.Attribute):
        return None
    scene = node.value
    if not isinstance(scene, ast.Attribute) or scene.attr != "scene":
        return None
    if not isinstance(scene.value, ast.Name) or scene.value.id != "config":
        return None
    return node.attr


def _config_pnp_path_literal(node: ast.AST) -> str | None:
    """Return the filename string if *node* is a ``*_pnp_path("<str>")`` call.

    Recognised call forms (single string-literal argument required):

    * Bare function: ``config_pnp_path("xxx")``
    * Attribute style: ``config.pnp_path("xxx")``, ``cfg.config_pnp_path("xxx")``
      — any receiver object whose attribute name is ``pnp_path`` or
      ``config_pnp_path``.

    Anything else — bare names, numbers, multi-arg calls, other functions —
    returns ``None`` so the caller can safely skip the assignment.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        # Bare function call: config_pnp_path("xxx")
        if func.id != "config_pnp_path":
            return None
    elif isinstance(func, ast.Attribute):
        # Attribute-style call: config.pnp_path("xxx"), cfg.config_pnp_path("xxx")
        if func.attr not in ("config_pnp_path", "pnp_path"):
            return None
    else:
        return None
    if len(node.args) != 1:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def load_config_pnp_paths(config_pnp_dir: Path) -> Dict[str, Path]:
    """Parse *config_pnp_dir* / ``config.py`` and return resolved KSQ paths.

    Returns a dict whose keys are among ``shelves``, ``tool_mapping``,
    ``unavailable``, ``pick_strategy``.  Only keys actually configured in
    ``config.py`` are included; absent keys are omitted so callers fall back
    to their existing defaults.

    The source is parsed with :mod:`ast` — **never executed** — so arbitrary
    code in ``config.py`` cannot run.  Any error (missing file, syntax error,
    etc.) prints a warning to stderr and returns an empty dict to guarantee
    zero regression.
    """
    config_py = config_pnp_dir / "config.py"
    if not config_py.is_file():
        return {}

    try:
        source = config_py.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(config_py))
    except Exception as exc:
        print(f"警告：解析 config.py 失败：{exc}", file=sys.stderr)
        return {}

    resolved_dir = config_pnp_dir.resolve()
    dir_prefix = str(resolved_dir)
    result: Dict[str, Path] = {}

    for node in tree.body:
        # Only top-level simple assignments are considered.
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        scene_key = _scene_attr_key(node.targets[0])
        if scene_key is None:
            continue
        result_key = SCENE_KEY_MAP.get(scene_key)
        if result_key is None:
            continue
        filename = _config_pnp_path_literal(node.value)
        if not filename:
            continue
        path = (config_pnp_dir / filename).resolve()
        # Reject paths that escape config_pnp_dir (e.g. "../../etc/passwd").
        if not str(path).startswith(dir_prefix):
            continue
        result[result_key] = path

    return result
