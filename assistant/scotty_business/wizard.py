"""Single-delivery dispatch for the fixed Trent onboarding wizard.

The trigger is handled before model execution, the destination is chosen by
code, and the text is fixed and non-secret. Nothing is ever sent automatically
after installation.

Two `pre_gateway_dispatch` hooks can observe the same inbound Discord message:
the bounded plugin registered at the gateway root, and the profile-local guard
installed in the full maintainer profile. Whichever runs first delivers; the
other no-ops. Delivery therefore happens once per trigger, and a genuinely
repeated trigger is a new message with a new key.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

MARKER_DIRNAME = "wizard"
_MAX_MARKERS = 512
_HEX = frozenset("0123456789abcdef")


def message_key(event: object, text: str) -> str:
    """A stable per-message key, from the most specific identity available."""

    source = getattr(event, "source", None)
    for holder in (event, source):
        for attribute in ("message_id", "id"):
            value = getattr(holder, attribute, None)
            if type(value) is str and value:
                return hashlib.sha256(value.encode("utf-8")).hexdigest()
    # No message identity is exposed. Fall back to the acting user, the channel,
    # the exact text, and a coarse clock, so two hooks handling the same message
    # agree while a later repeat of the trigger gets a new key.
    parts = (
        str(getattr(source, "user_id", "")),
        str(getattr(source, "chat_id", "")),
        text,
        str(int(time.time())),
    )
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()


def claim_delivery(marker_root: Path, key: str) -> bool:
    """Claim one delivery for `key`. Returns False when already claimed."""

    if not key or any(character not in _HEX for character in key):
        return False
    directory = marker_root / MARKER_DIRNAME
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            return False
        marker = directory / key
        if marker.is_symlink():
            return False
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        # Without a durable claim there is no safe way to guarantee one delivery.
        return False
    os.close(descriptor)
    _trim(directory)
    return True


def _trim(directory: Path) -> None:
    try:
        markers = sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime)
    except OSError:
        return
    for stale in markers[:-_MAX_MARKERS]:
        try:
            stale.unlink()
        except OSError:
            return


def deliver_once(
    marker_root: Path,
    key: str,
    destination: str,
    text: str,
    send: Callable[[str, str], object],
) -> bool:
    """Deliver the fixed wizard exactly once for this inbound message."""

    if not claim_delivery(marker_root, key):
        return False
    send(destination, text)
    return True
