"""Locate the active knowledge directory from ``VfmApp_deploy/config.yaml``.

The perception app (``percept``) declares which template scene is live via
``template.template_root`` in ``config.yaml``; the drug metadata KSQ needs sits
in the ``knowledge`` sub-directory of that scene::

    # VfmApp_deploy/config.yaml
    template:
      template_root: templates/pnp_percept/noematrix_0004_..._20260827

    -> <vfm_app>/model/templates/pnp_percept/noematrix_0004_..._20260827/knowledge

The scene directory is date-stamped and rotates, so hard-coding it makes KSQ
drift away from the data percept actually uses.

``template_root`` is resolved against ``<vfm_app>/model`` — that is where
``templates/`` and the sibling ``PNP/`` detector paths live.

The runtime image ships no YAML parser, so instead of adding a dependency this
module extracts just the one key it needs with an indentation-aware scan.  That
matters for correctness as much as for dependencies: ``config.yaml`` holds a
second ``template_root`` under ``template_for_validation``, and only the one
inside the top-level ``template:`` block describes the live scene.

Any failure (missing file, missing key, escaping path) returns ``None`` so
callers transparently keep their existing knowledge path — zero regression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ksq.runtime_logging import get_logger


LOGGER = get_logger("vfm_app")

CONFIG_FILE_NAME = "config.yaml"
# template_root is written relative to this sub-directory of VfmApp_deploy.
MODEL_SUBDIRECTORY = "model"
TEMPLATE_BLOCK_KEY = "template"
TEMPLATE_ROOT_KEY = "template_root"
KNOWLEDGE_SUBDIRECTORY = "knowledge"


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar_value(raw: str) -> str:
    """Strip surrounding quotes and any inline ``#`` comment from a YAML scalar."""
    value = raw.strip()
    if not value:
        return ""
    quote = value[0]
    if quote in "\"'":
        closing = value.find(quote, 1)
        if closing != -1:
            # Quoted scalar: everything after the closing quote is a comment.
            return value[1:closing].strip()
        # Unterminated quote — treat the remainder as an unquoted scalar.
        value = value[1:]
    return value.split("#", 1)[0].strip()


def read_template_root(config_file: Path) -> Optional[str]:
    """Return ``template.template_root`` from *config_file*, or ``None``.

    Only the mapping nested directly under the top-level ``template:`` key is
    searched, so ``template_for_validation.template_root`` is never picked up.
    """
    try:
        text = config_file.read_text(encoding="utf-8")
    except OSError as error:
        LOGGER.warning("读取 %s 失败：%s", config_file, error)
        return None

    inside_template_block = False
    block_indent: Optional[int] = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indentation(raw_line)
        if not inside_template_block:
            # Top-level "template:" with no inline value opens the block.
            if indent == 0 and stripped == f"{TEMPLATE_BLOCK_KEY}:":
                inside_template_block = True
            continue
        if block_indent is None:
            # First non-blank line after "template:" fixes the block depth.
            if indent == 0:
                # Empty block — "template:" was followed by another top key.
                return None
            block_indent = indent
        if indent < block_indent:
            # Dedented out of the block without finding the key.
            return None
        if indent > block_indent:
            # Deeper nesting inside the block; not our key.
            continue
        key, separator, value = stripped.partition(":")
        if separator and key.strip() == TEMPLATE_ROOT_KEY:
            return _scalar_value(value) or None
    return None


def knowledge_directory(vfm_app_dir: Path) -> Optional[Path]:
    """Return the live knowledge directory under *vfm_app_dir*, or ``None``.

    ``None`` means "could not determine" — the caller keeps its current path.
    A directory is only returned when it actually exists, so a stale
    ``template_root`` never replaces a working knowledge path.
    """
    config_file = vfm_app_dir / CONFIG_FILE_NAME
    if not config_file.is_file():
        LOGGER.warning("VfmApp 配置不存在：%s", config_file)
        return None
    template_root = read_template_root(config_file)
    if not template_root:
        LOGGER.warning("%s 中未找到 template.template_root", config_file)
        return None

    model_dir = (vfm_app_dir / MODEL_SUBDIRECTORY).resolve()
    candidate = (model_dir / template_root / KNOWLEDGE_SUBDIRECTORY).resolve()
    # Reject paths escaping the model directory (e.g. template_root: "../..").
    try:
        candidate.relative_to(model_dir)
    except ValueError:
        LOGGER.warning("template_root 越出 model 目录，已忽略：%s", template_root)
        return None
    if not candidate.is_dir():
        LOGGER.warning("template_root 对应的 knowledge 目录不存在：%s", candidate)
        return None
    return candidate
