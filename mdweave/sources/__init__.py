"""Annotation source adapters.

Each adapter turns some external representation into `list[Annotation]`.
Register a new one here and every renderer picks it up.
"""

from . import obsidian_inline, sidecar

__all__ = ["sidecar", "obsidian_inline"]
