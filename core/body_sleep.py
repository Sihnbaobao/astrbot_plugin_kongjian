"""Read the sleep state published by astrbot_plugin_body_needs.

Sleep belongs to the body plugin: it decides when the persona falls asleep,
when she wakes up, and whether she takes a daytime nap. This plugin only reads
the contract, either by asking the running body plugin for a fresh snapshot or
by reading the shared sleep.json file when the plugin is not loaded.

Contract (data/plugin_data/astrbot_plugin_body_needs/sleep.json)::

    {
      "current": "<persona id>",
      "personas": {
        "<persona id>": {
          "asleep": true,
          "kind": "night" | "nap",
          "since": <unix seconds>,
          "wake_at": <unix seconds, 0 when unknown>,
          "line": "已经睡下了",
          "updated_at": "2026-11-21T03:00:00+08:00"
        }
      }
    }

An entry whose wake_at is already in the past counts as awake: the file may be
a few minutes old, and nobody should keep posting after she woke up.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BODY_PLUGIN = "astrbot_plugin_body_needs"
SLEEP_FILE = Path(BODY_PLUGIN) / "sleep.json"


def _awake() -> dict[str, Any]:
    """Return the neutral awake state used whenever nothing can be read."""
    return {
        "asleep": False,
        "enabled": False,
        "persona": "",
        "kind": "",
        "since": 0.0,
        "wake_at": 0.0,
        "bedtime": 0.0,
        "line": "",
    }


def normalise(entry: Any, now: float) -> dict[str, Any]:
    """Turn one contract entry into a small, checked state dict.

    Args:
        entry: One persona object from sleep.json or sleep_snapshot().
        now: Unix timestamp used to decide whether a wake time has passed.

    Returns:
        The same keys as the neutral awake state, with asleep resolved.
    """
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        return _awake()
    try:
        wake_at = max(0.0, float(entry.get("wake_at") or 0.0))
    except (TypeError, ValueError):
        wake_at = 0.0
    asleep = bool(entry.get("asleep")) and (wake_at <= 0.0 or now < wake_at)
    return {
        "asleep": asleep,
        "enabled": True,
        "persona": str(entry.get("persona_id") or entry.get("persona") or ""),
        "kind": str(entry.get("kind") or ""),
        "since": float(entry.get("since") or 0.0),
        "wake_at": wake_at if asleep else 0.0,
        "bedtime": float(entry.get("bedtime") or 0.0),
        "line": str(entry.get("line") or ""),
    }


def from_file(persona_id: str = "", now: float | None = None) -> dict[str, Any]:
    """Read the sleep state from disk without the body plugin being loaded.

    Args:
        persona_id: Persona to look up; empty follows the file's current entry.
        now: Unix timestamp, defaults to the current time.

    Returns:
        The normalised state, or awake when the file is missing or unreadable.
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
    except Exception:
        return _awake()
    try:
        path = Path(get_astrbot_plugin_data_path()) / SLEEP_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _awake()
    personas = payload.get("personas") if isinstance(payload, dict) else None
    if not isinstance(personas, dict):
        return _awake()
    entry = personas.get(persona_id) if persona_id else None
    if entry is None:
        entry = personas.get(str(payload.get("current") or ""))
    if entry is None and len(personas) == 1:
        entry = next(iter(personas.values()))
    return normalise(entry, time.time() if now is None else now)


def read(
    context: Any = None,
    persona_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Return the current sleep state, preferring a live snapshot.

    Args:
        context: AstrBot context; when given, the running body plugin is asked
            first so the answer is never stale.
        persona_id: Persona to look up in the file fallback.
        now: Unix timestamp, defaults to the current time.

    Returns:
        The normalised sleep state; awake when the body plugin is absent.
    """
    stamp = time.time() if now is None else now
    if context is not None:
        try:
            star = context.get_registered_star(BODY_PLUGIN)
            plugin = getattr(star, "star_cls", None) if star else None
            getter = getattr(plugin, "sleep_snapshot", None)
            if callable(getter):
                snapshot = getter()
                if isinstance(snapshot, dict):
                    return normalise(snapshot, stamp)
        except Exception:
            pass
    return from_file(persona_id, stamp)
