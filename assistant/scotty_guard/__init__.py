"""Profile-local authorization guard for the full maintainer profile.

This plugin registers exactly one hook and nothing else: no model tools, no
system-prompt section, no client identity, and no bounded Scotty behaviour. Its
only job is to enforce the exact maintainer tuple before model dispatch, because
native profile routing matches guild and channel but not the acting user.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .guard import MaintainerGuard

__all__ = ["__version__", "register"]
__version__ = "1.0.0"


class PluginContext(Protocol):
    def register_hook(self, hook_name: str, callback: Callable[..., object]) -> object: ...


def register(ctx: PluginContext) -> None:
    ctx.register_hook("pre_gateway_dispatch", MaintainerGuard())
