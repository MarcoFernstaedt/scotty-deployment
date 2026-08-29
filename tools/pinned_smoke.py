from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.scotty_business.routing import SERVED_PROFILES  # noqa: E402
from assistant.scotty_business.setup import (  # noqa: E402
    SetupInputs,
    render_hermes_config,
    render_profile_config,
)

#: Synthetic identifiers only. The smoke never touches a real deployment.
SYNTHETIC_INPUTS = SetupInputs(
    model_provider="openai-codex",
    model_name="synthetic/codex",
    guild_id="100000000000000001",
    operator_channel_id="201000000000000001",
    operator_user_id="301000000000000001",
    employee_channel_id="202000000000000001",
    employee_user_id="302000000000000001",
    route_guild_id="110000000000000001",
    route_channel_id="220000000000000001",
    route_user_id="320000000000000001",
    secrets={"DISCORD_BOT_TOKEN": "synthetic-smoke-token"},
)
IMAGE = "nousresearch/hermes-agent@sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517"
EXPECTED = {
    "scotty_read",
    "scotty_propose",
    "scotty_approval",
    "scotty_reminder",
    "scotty_calculate",
}
PROBE = r"""
import json
import pathlib

import yaml

from hermes_cli.plugins import discover_plugins, get_plugin_manager
from model_tools import get_tool_definitions

discover_plugins(force=True)
manager = get_plugin_manager()
plugins = [item for item in manager.list_plugins() if item.get("name") == "scotty-business"]
definitions = get_tool_definitions(enabled_toolsets=["scotty"], quiet_mode=True)
names = {item["function"]["name"] for item in definitions}
expected = {"scotty_read", "scotty_propose", "scotty_approval", "scotty_reminder", "scotty_calculate"}

# The generated configuration must satisfy the native routing contract as the
# runtime's own YAML loader sees it, not only as our renderer emits it.
config = yaml.safe_load(pathlib.Path("/opt/data/config.yaml").read_text())
gateway = config.get("gateway") or {}
routes = gateway.get("profile_routes") or []
allowlist = gateway.get("multiplex_profile_allowlist") or []
route_keys = sorted({key for route in routes for key in route})
profiles = {}
for name in allowlist:
    path = pathlib.Path("/opt/data/profiles") / name / "config.yaml"
    profiles[name] = yaml.safe_load(path.read_text()) if path.is_file() else None

print(
    json.dumps(
        {
            "version": "0.20.6",
            "tools": sorted(names),
            "plugins": plugins,
            "multiplex_profiles": gateway.get("multiplex_profiles"),
            "routes": [route.get("profile") for route in routes],
            "allowlist": allowlist,
            "profiles": profiles,
        }
    )
)

if names != expected:
    raise SystemExit(f"forbidden or missing model tools: {sorted(names)}")
if gateway.get("multiplex_profiles") is not True:
    raise SystemExit("gateway.multiplex_profiles is not enabled")
if len(routes) != 3:
    raise SystemExit(f"expected exactly three native profile routes, saw {len(routes)}")
if route_keys != ["chat_id", "guild_id", "name", "platform", "profile"]:
    raise SystemExit(f"native route keys are wrong: {route_keys}")
if sorted(allowlist) != sorted({route["profile"] for route in routes}):
    raise SystemExit("routed profiles and the served allowlist disagree")
for name, profile in profiles.items():
    if profile is None:
        raise SystemExit(f"served profile has no home configuration: {name}")
    enabled = (profile.get("plugins") or {}).get("enabled")
    toolsets = (profile.get("platform_toolsets") or {}).get("discord")
    if name == "scotty-maintainer":
        if enabled != [] or toolsets != ["*"]:
            raise SystemExit("the full profile is not a normal unbounded profile")
    elif enabled != ["scotty-business"] or toolsets != ["scotty"]:
        raise SystemExit(f"client profile is not bounded: {name}")
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scotty-pinned-smoke-") as directory:
        home = Path(directory)
        plugin_root = home / "plugins" / "scotty_business"
        shutil.copytree(ROOT / "assistant" / "scotty_business", plugin_root)
        (home / "config.yaml").write_text(render_hermes_config(SYNTHETIC_INPUTS), encoding="utf-8")
        for profile in SERVED_PROFILES:
            profile_home = home / "profiles" / profile
            profile_home.mkdir(parents=True)
            (profile_home / "config.yaml").write_text(
                render_profile_config(profile), encoding="utf-8"
            )
        environment = os.environ.copy()
        environment.pop("DISCORD_BOT_TOKEN", None)
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",  # noqa: S108 - isolated container tmpfs, not a host temp path
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HERMES_HOME=/opt/data",
            "--volume",
            f"{home}:/opt/data",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            PROBE,
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pinned-image smoke failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
