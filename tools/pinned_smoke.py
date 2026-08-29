from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.scotty_business.routing import (  # noqa: E402
    CLIENT_PROFILES,
    MAINTAINER_PROFILE,
    SERVED_PROFILES,
)
from assistant.scotty_business.setup import (  # noqa: E402
    SetupInputs,
    private_mapping,
    render_hermes_config,
    render_profile_config,
    runtime_environment,
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
import importlib
import inspect
import json
import os
import pathlib
import sys
import types

import yaml

failures = []
notes = {}


def check(label, condition):
    if not condition:
        failures.append(label)


def source(guild, chat, user, parent=None, is_bot=False, platform="discord", message_id=None):
    return types.SimpleNamespace(
        platform=types.SimpleNamespace(value=platform),
        guild_id=guild,
        scope_id=guild,
        chat_id=chat,
        user_id=user,
        parent_chat_id=parent,
        is_bot=is_bot,
        message_id=message_id,
    )


def event(text, src, message_id="800000000000000001"):
    return types.SimpleNamespace(text=text, message_id=message_id, source=src)


DATA = pathlib.Path("/opt/data")
IDS = json.loads(os.environ["SCOTTY_SMOKE_IDS"])
ROUTE_GUILD = IDS["route_guild"]
ROUTE_CHANNEL = IDS["route_channel"]
ROUTE_USER = IDS["route_user"]
CLIENT_GUILD = IDS["client_guild"]
OPERATOR_CHANNEL = IDS["operator_channel"]
OPERATOR_USER = IDS["operator_user"]
EMPLOYEE_CHANNEL = IDS["employee_channel"]
EMPLOYEE_USER = IDS["employee_user"]
WIZARD = IDS["wizard_command"]
UNKNOWN_USER = "999000000000000001"

# ---------------------------------------------------------------- tool boundary
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from model_tools import get_tool_definitions

discover_plugins(force=True)
manager = get_plugin_manager()
plugins = [item for item in manager.list_plugins() if item.get("name") == "scotty-business"]
definitions = get_tool_definitions(enabled_toolsets=["scotty"], quiet_mode=True)
names = {item["function"]["name"] for item in definitions}
expected_tools = {
    "scotty_read", "scotty_propose", "scotty_approval", "scotty_reminder", "scotty_calculate"
}
check("bounded toolset exposes exactly the five Scotty tools", names == expected_tools)
check("the bounded plugin is discovered", bool(plugins))

# ------------------------------------------------------- native routing contract
config = yaml.safe_load((DATA / "config.yaml").read_text())
gateway = config.get("gateway") or {}
routes = gateway.get("profile_routes") or []
allowlist = gateway.get("multiplex_profile_allowlist") or []
route_keys = sorted({key for route in routes for key in route})
check("gateway.multiplex_profiles is true", gateway.get("multiplex_profiles") is True)
check("exactly three native profile routes", len(routes) == 3)
check(
    "native route keys are exact",
    route_keys == ["chat_id", "guild_id", "name", "platform", "profile"],
)
check(
    "routed profiles match the served allowlist",
    sorted(allowlist) == sorted({route["profile"] for route in routes}),
)

profiles = {}
for name in allowlist:
    path = DATA / "profiles" / name / "config.yaml"
    profiles[name] = yaml.safe_load(path.read_text()) if path.is_file() else None
for name, profile in profiles.items():
    check(f"served profile has a home configuration: {name}", profile is not None)
    if profile is None:
        continue
    enabled = (profile.get("plugins") or {}).get("enabled")
    toolsets = (profile.get("platform_toolsets") or {}).get("discord")
    if name == "scotty-maintainer":
        check("the full profile enables only the guard", enabled == ["scotty-guard"])
        check("the full profile keeps the normal inventory", toolsets == ["*"])
    else:
        check(f"client profile enables only the bounded plugin: {name}",
              enabled == ["scotty-business"])
        check(f"client profile is bounded: {name}", toolsets == ["scotty"])

# ------------------------------------------------- profile-scoped model resolution
root_model = config.get("model") or {}
for name, profile in profiles.items():
    profile_model = (profile or {}).get("model") or {}
    check(
        f"{name} keeps the setup-selected provider and model",
        profile_model.get("provider") == root_model.get("provider")
        and profile_model.get("default") == root_model.get("default")
        and bool(root_model.get("provider")),
    )
notes["model"] = root_model

# --------------------------------------------------- gateway sender authorization
raw_allowed = os.environ.get("DISCORD_ALLOWED_USERS", "")
allowed_ids = [item.strip() for item in raw_allowed.split(",") if item.strip()]
check(
    "DISCORD_ALLOWED_USERS carries exactly the three configured senders",
    sorted(allowed_ids) == sorted([ROUTE_USER, OPERATOR_USER, EMPLOYEE_USER]),
)
check("no open sender policy is set", not os.environ.get("DISCORD_ALLOW_ALL_USERS"))

authz_decisions = {}
try:
    authz_module = importlib.import_module("gateway.authz_mixin")
except Exception as exc:  # noqa: BLE001 - the module name is part of the contract
    failures.append("gateway.authz_mixin is not importable: %s" % exc)
    authz_module = None

if authz_module is not None:
    owner = None
    for _, candidate in inspect.getmembers(authz_module, inspect.isclass):
        if hasattr(candidate, "_is_user_authorized"):
            owner = candidate
            break
    if owner is None:
        failures.append("no class in gateway.authz_mixin defines _is_user_authorized")
    else:
        method = owner._is_user_authorized
        notes["authz_signature"] = str(inspect.signature(method))
        instance = object.__new__(owner)
        # Give the mixin the allowlist under every attribute name it may read.
        for attribute in (
            "allowed_users", "_allowed_users", "allowed_user_ids", "_allowed_user_ids",
        ):
            try:
                setattr(instance, attribute, set(allowed_ids))
            except Exception:  # noqa: BLE001 - read-only attributes are fine
                pass
        for user in (ROUTE_USER, OPERATOR_USER, EMPLOYEE_USER, UNKNOWN_USER):
            try:
                authz_decisions[user] = bool(method(instance, user))
            except Exception as exc:  # noqa: BLE001
                authz_decisions[user] = "error: %s" % exc
        notes["authz_decisions"] = authz_decisions
        if all(isinstance(value, bool) for value in authz_decisions.values()):
            check("the runtime admits each configured sender",
                  all(authz_decisions[user] for user in
                      (ROUTE_USER, OPERATOR_USER, EMPLOYEE_USER)))
            check("the runtime denies an unknown sender", authz_decisions[UNKNOWN_USER] is False)
        else:
            failures.append(
                "the runtime authorization call could not be driven; signature was %s"
                % notes.get("authz_signature")
            )

# ------------------------------------------- exact tuple enforcement, pre-dispatch
root_hook = None
for hook in getattr(manager, "hooks", {}).get("pre_gateway_dispatch", []) or []:
    root_hook = hook
if root_hook is None:
    sys.path.insert(0, str(DATA / "plugins"))
    from scotty_business.ingress import IngressGuard
    from scotty_business.runtime import _load_private_config

    delivered = []
    root_hook = IngressGuard(
        _load_private_config(DATA), lambda channel, text: delivered.append((channel, text)),
        DATA / "scotty",
    )
    notes["root_hook"] = "constructed directly (manager exposed no hook registry)"
else:
    delivered = None
    notes["root_hook"] = "obtained from the pinned plugin manager"

admitted = {
    "operator exact tuple": source(CLIENT_GUILD, OPERATOR_CHANNEL, OPERATOR_USER),
    "employee exact tuple": source(CLIENT_GUILD, EMPLOYEE_CHANNEL, EMPLOYEE_USER),
    "maintainer exact tuple": source(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER),
}
for label, src in admitted.items():
    result = root_hook(event("status please", src))
    check("pre-dispatch admits %s" % label, result.get("action") == "allow")

denied = {
    "unknown sender": source(CLIENT_GUILD, OPERATOR_CHANNEL, UNKNOWN_USER),
    "maintainer in the operator channel": source(CLIENT_GUILD, OPERATOR_CHANNEL, ROUTE_USER),
    "maintainer in the employee channel": source(CLIENT_GUILD, EMPLOYEE_CHANNEL, ROUTE_USER),
    "operator in the maintainer channel": source(ROUTE_GUILD, ROUTE_CHANNEL, OPERATOR_USER),
    "employee in the maintainer channel": source(ROUTE_GUILD, ROUTE_CHANNEL, EMPLOYEE_USER),
    "operator in the employee channel": source(CLIENT_GUILD, EMPLOYEE_CHANNEL, OPERATOR_USER),
    "employee in the operator channel": source(CLIENT_GUILD, OPERATOR_CHANNEL, EMPLOYEE_USER),
    "wrong guild": source("999000000000000002", OPERATOR_CHANNEL, OPERATOR_USER),
    "wrong channel": source(CLIENT_GUILD, "900000000000000001", OPERATOR_USER),
    "wrong parent thread": source(
        CLIENT_GUILD, "900000000000000001", OPERATOR_USER, parent=EMPLOYEE_CHANNEL
    ),
    "bot author": source(CLIENT_GUILD, OPERATOR_CHANNEL, OPERATOR_USER, is_bot=True),
}
for label, src in denied.items():
    result = root_hook(event("status please", src))
    check("pre-dispatch denies %s before model execution" % label,
          result.get("action") == "skip")

# --------------------------------------------------------- maintainer guard path
sys.path.insert(0, str(DATA / "profiles" / "scotty-maintainer" / "plugins"))
import scotty_guard.guard as guard_module

guard_sent = []
guard_module.send_fixed_message = lambda channel, text: guard_sent.append((channel, text))
maintainer_guard = guard_module.MaintainerGuard()
check(
    "the profile guard admits the exact maintainer tuple",
    maintainer_guard(event("status please", source(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER)))
    == {"action": "allow"},
)
for label, user in (
    ("an unknown sender", UNKNOWN_USER),
    ("the operator", OPERATOR_USER),
    ("the employee", EMPLOYEE_USER),
):
    check(
        "the profile guard denies %s in the maintainer channel" % label,
        maintainer_guard(
            event("status please", source(ROUTE_GUILD, ROUTE_CHANNEL, user))
        ) == {"action": "skip", "reason": "unauthorized"},
    )
check("no guard denial replies or discloses", guard_sent == [])

# ------------------------------------------------------------- fixed wizard path
wizard_event = event(WIZARD, source(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER), "800000000000000009")
guard_result = maintainer_guard(wizard_event)
check("the wizard trigger is handled before model execution",
      guard_result == {"action": "skip", "reason": "fixed-wizard"})
check("the fixed wizard goes only to the main-operator channel",
      [channel for channel, _ in guard_sent] == [OPERATOR_CHANNEL])
maintainer_guard(wizard_event)
check("one inbound message delivers the wizard exactly once", len(guard_sent) == 1)
for user in (OPERATOR_USER, EMPLOYEE_USER, UNKNOWN_USER):
    maintainer_guard(event(WIZARD, source(ROUTE_GUILD, ROUTE_CHANNEL, user), "80000000000000001"))
check("no wrong sender can trigger the wizard", len(guard_sent) == 1)

print(json.dumps({"version": "0.20.6", "tools": sorted(names), "notes": notes}, indent=1))
if failures:
    raise SystemExit("pinned smoke failures:\n  - " + "\n  - ".join(failures))
print("pinned smoke: PASS")
"""


_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _stage(home: Path) -> None:
    """Lay out a synthetic deployment exactly as the installer and setup do."""

    shutil.copytree(
        ROOT / "assistant" / "scotty_business",
        home / "plugins" / "scotty_business",
        ignore=_IGNORE,
    )
    (home / "config.yaml").write_text(render_hermes_config(SYNTHETIC_INPUTS), encoding="utf-8")

    scotty = home / "scotty"
    scotty.mkdir()
    (scotty / "private.json").write_text(
        json.dumps(private_mapping(SYNTHETIC_INPUTS), indent=2), encoding="utf-8"
    )

    for profile in SERVED_PROFILES:
        profile_home = home / "profiles" / profile
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            render_profile_config(profile, SYNTHETIC_INPUTS), encoding="utf-8"
        )
        if profile == MAINTAINER_PROFILE:
            shutil.copytree(
                ROOT / "assistant" / "scotty_guard",
                profile_home / "plugins" / "scotty_guard",
                ignore=_IGNORE,
            )
        else:
            shutil.copytree(
                ROOT / "assistant" / "scotty_business",
                profile_home / "plugins" / "scotty_business",
                ignore=_IGNORE,
            )
    assert set(CLIENT_PROFILES.values()) < set(SERVED_PROFILES)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scotty-pinned-smoke-") as directory:
        home = Path(directory)
        _stage(home)
        environment = os.environ.copy()
        environment.pop("DISCORD_BOT_TOKEN", None)
        runtime = runtime_environment(SYNTHETIC_INPUTS)
        identifiers = json.dumps(
            {
                "route_guild": SYNTHETIC_INPUTS.route_guild_id,
                "route_channel": SYNTHETIC_INPUTS.route_channel_id,
                "route_user": SYNTHETIC_INPUTS.route_user_id,
                "client_guild": SYNTHETIC_INPUTS.guild_id,
                "operator_channel": SYNTHETIC_INPUTS.operator_channel_id,
                "operator_user": SYNTHETIC_INPUTS.operator_user_id,
                "employee_channel": SYNTHETIC_INPUTS.employee_channel_id,
                "employee_user": SYNTHETIC_INPUTS.employee_user_id,
                "wizard_command": "Scotty, send Trent the setup wizard.",
            }
        )
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
            "--env",
            f"DISCORD_ALLOWED_USERS={runtime['DISCORD_ALLOWED_USERS']}",
            "--env",
            "SCOTTY_PRIVATE_CONFIG=/opt/data/scotty/private.json",
            "--env",
            f"SCOTTY_SMOKE_IDS={identifiers}",
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
            timeout=300,
            env=environment,
        )
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pinned-image smoke failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
