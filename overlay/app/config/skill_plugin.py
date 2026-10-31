"""Startup-only loading of trusted administrator Python; no sandbox or file uploads."""

import importlib

from app.config.settings import Settings
from app.protocols.skill_plugin_protocol import SkillPlugin


def load_skill_plugin(config: Settings) -> SkillPlugin | None:
    reference = config.SKILL_WRITE_PLUGIN.strip()
    if not reference:
        return None
    module_name, separator, object_name = reference.partition(":")
    if (not separator or not object_name.isidentifier()
            or not all(part.isidentifier() for part in module_name.split("."))):
        raise ValueError("SKILL_WRITE_PLUGIN must be an importable module:object reference")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"SKILL_WRITE_PLUGIN import failed for {module_name}: {type(exc).__name__}",
        ) from None
    try:
        plugin = getattr(module, object_name)
    except Exception as exc:
        raise ValueError(
            f"SKILL_WRITE_PLUGIN attribute lookup failed for {reference}: {type(exc).__name__}",
        ) from None
    if not isinstance(plugin, SkillPlugin):
        raise ValueError("SKILL_WRITE_PLUGIN must reference a SkillPlugin registration")
    return plugin
