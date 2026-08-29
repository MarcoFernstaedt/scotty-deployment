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
EXPECTED = (
    "scotty_read",
    "scotty_propose",
    "scotty_approval",
    "scotty_reminder",
    "scotty_calculate",
)
WIZARD_COMMAND = "Scotty, send Trent the setup wizard."

#: Two passes, each with the profile home the pinned runtime would actually use.
ROLE_ROOT = "root"
ROLE_MAINTAINER = "maintainer"

PROBE = r'''
"""In-container acceptance probe. Runs against the pinned Hermes 0.20.6 image.

Nothing here mocks the runtime. Sender authorization is decided by the pinned
gateway method, and the pre-dispatch hook is invoked through the pinned plugin
lifecycle dispatcher. If either interface cannot be driven, the probe fails and
prints what it actually found.
"""

import asyncio
import importlib
import inspect
import json
import os
import pathlib
import re
import types

import yaml

failures = []
notes = {}


def check(label, condition):
    if not condition:
        failures.append(label)
    return bool(condition)


def fail(label):
    failures.append(label)
    return False


DATA = pathlib.Path("/opt/data")
IDS = json.loads(os.environ["SCOTTY_SMOKE_IDS"])
ROLE = os.environ["SCOTTY_SMOKE_ROLE"]
ROUTE_GUILD = IDS["route_guild"]
ROUTE_CHANNEL = IDS["route_channel"]
ROUTE_USER = IDS["route_user"]
CLIENT_GUILD = IDS["client_guild"]
OPERATOR_CHANNEL = IDS["operator_channel"]
OPERATOR_USER = IDS["operator_user"]
EMPLOYEE_CHANNEL = IDS["employee_channel"]
EMPLOYEE_USER = IDS["employee_user"]
WIZARD = IDS["wizard_command"]
EXPECTED_TOOLS = set(IDS["expected_tools"])
UNKNOWN_USER = "999000000000000001"
CONFIGURED_USERS = (ROUTE_USER, OPERATOR_USER, EMPLOYEE_USER)

# ------------------------------------------------------------ environment hygiene
# The probe must decide on its staged synthetic state alone. Any inherited host
# allowlist or credential would silently change the result.
discord_env = sorted(name for name in os.environ if name.startswith("DISCORD_"))
notes["discord_env"] = discord_env
check(
    "no open sender policy is inherited or set",
    not os.environ.get("DISCORD_ALLOW_ALL_USERS")
    and not os.environ.get("DISCORD_ALLOWED_ROLES"),
)
check(
    "the container carries no unexpected Discord environment",
    set(discord_env) <= {"DISCORD_ALLOWED_USERS", "DISCORD_BOT_TOKEN"},
)
check(
    "the probe uses its staged profile home",
    os.environ.get("HERMES_HOME") == IDS["home"][ROLE],
)

raw_allowed = os.environ.get("DISCORD_ALLOWED_USERS", "")
allowed_ids = [item.strip() for item in raw_allowed.split(",") if item.strip()]
check(
    "DISCORD_ALLOWED_USERS carries exactly the three configured senders",
    sorted(allowed_ids) == sorted(CONFIGURED_USERS),
)


# --------------------------------------------------------- real SessionSource build
def resolve_platform():
    """The runtime's own Discord platform value, never a stand-in string."""

    for module_name in ("gateway.session", "gateway.run", "gateway.platforms.base"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for attribute in ("Platform", "PlatformType", "SessionPlatform"):
            enum = getattr(module, attribute, None)
            member = getattr(enum, "DISCORD", None) if enum is not None else None
            if member is not None:
                notes["platform"] = "%s.%s.DISCORD" % (module_name, attribute)
                return member
    return None


session_module = None
SessionSource = None
try:
    session_module = importlib.import_module("gateway.session")
    SessionSource = getattr(session_module, "SessionSource", None)
except Exception as exc:
    fail("gateway.session is not importable: %s" % exc)

if SessionSource is None:
    fail("gateway.session.SessionSource is unavailable")

DISCORD_PLATFORM = resolve_platform()
if DISCORD_PLATFORM is None:
    fail("the runtime Discord platform value could not be resolved")


def build_session_source(guild, chat, user, parent=None, is_bot=False):
    """Construct a real SessionSource from the runtime's own signature."""

    known = {
        "platform": DISCORD_PLATFORM,
        "scope_id": guild,
        "guild_id": guild,
        "server_id": guild,
        "chat_id": chat,
        "channel_id": chat,
        "user_id": user,
        "author_id": user,
        "sender_id": user,
        "parent_chat_id": parent,
        "parent_channel_id": parent,
        "thread_parent_id": parent,
        "is_bot": is_bot,
    }
    signature = inspect.signature(SessionSource)
    kwargs = {}
    missing = []
    for name, parameter in signature.parameters.items():
        if name in ("self", "args", "kwargs"):
            continue
        if name in known:
            kwargs[name] = known[name]
        elif parameter.default is not inspect.Parameter.empty:
            continue
        else:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "SessionSource needs unmapped required fields %s; signature is %s"
            % (missing, signature)
        )
    return SessionSource(**kwargs)


notes["session_source_signature"] = (
    str(inspect.signature(SessionSource)) if SessionSource is not None else None
)


# --------------------------------------------------- gateway sender authorization
def authorization_instance(owner, method):
    """A minimal instance of the real mixin owner, fed the staged allowlist."""

    instance = object.__new__(owner)
    try:
        body = inspect.getsource(method)
    except (OSError, TypeError):
        body = ""
    notes["authz_source"] = body
    assigned = []
    discovered = sorted(set(re.findall(r"self\.([A-Za-z_][A-Za-z0-9_]*)", body)))
    for attribute in discovered:
        if callable(getattr(owner, attribute, None)):
            continue
        value = None
        lowered = attribute.lower()
        if "user" in lowered and ("allow" in lowered or "authorized" in lowered):
            value = set(allowed_ids)
        elif "allow_all" in lowered or "allowall" in lowered:
            value = False
        elif "role" in lowered and "allow" in lowered:
            value = set()
        elif "adapter" in lowered or "platform" in lowered:
            value = None
        try:
            setattr(instance, attribute, value)
            assigned.append("%s=%r" % (attribute, value))
        except Exception:
            continue
    notes["authz_attributes"] = assigned
    return instance


authz_decisions = {}
authz_module = None
try:
    authz_module = importlib.import_module("gateway.authz_mixin")
except Exception as exc:
    fail("gateway.authz_mixin is not importable: %s" % exc)

if authz_module is not None and SessionSource is not None and DISCORD_PLATFORM is not None:
    owner = getattr(authz_module, "GatewayAuthorizationMixin", None)
    if owner is None:
        for _, candidate in inspect.getmembers(authz_module, inspect.isclass):
            if "_is_user_authorized" in vars(candidate):
                owner = candidate
                break
    if owner is None:
        fail("no class in gateway.authz_mixin defines _is_user_authorized")
    else:
        method = owner._is_user_authorized
        signature = inspect.signature(method)
        notes["authz_owner"] = "%s.%s" % (owner.__module__, owner.__qualname__)
        notes["authz_signature"] = str(signature)
        check(
            "the pinned authorization method takes a SessionSource, not a user string",
            "source" in signature.parameters,
        )
        instance = authorization_instance(owner, method)
        keywords = {}
        if "allow_adapter_delegation" in signature.parameters:
            # Decide on the configured allowlist alone. Adapter delegation would
            # ask a live platform adapter, which does not exist in this probe.
            keywords["allow_adapter_delegation"] = False
        for label, user in (
            ("route", ROUTE_USER),
            ("operator", OPERATOR_USER),
            ("employee", EMPLOYEE_USER),
            ("unknown", UNKNOWN_USER),
        ):
            channel = {
                "route": ROUTE_CHANNEL,
                "operator": OPERATOR_CHANNEL,
                "employee": EMPLOYEE_CHANNEL,
                "unknown": OPERATOR_CHANNEL,
            }[label]
            guild = ROUTE_GUILD if label == "route" else CLIENT_GUILD
            try:
                session_source = build_session_source(guild, channel, user)
                notes.setdefault("authz_source_type", type(session_source).__name__)
                authz_decisions[label] = bool(method(instance, session_source, **keywords))
            except Exception as exc:
                authz_decisions[label] = "error: %s: %s" % (type(exc).__name__, exc)
        notes["authz_decisions"] = authz_decisions
        if all(isinstance(value, bool) for value in authz_decisions.values()):
            check(
                "the pinned runtime admits each configured sender",
                all(authz_decisions[label] for label in ("route", "operator", "employee")),
            )
            check(
                "the pinned runtime denies an unknown sender",
                authz_decisions["unknown"] is False,
            )
        else:
            fail(
                "the pinned authorization method could not be driven; signature %s, "
                "decisions %s" % (signature, authz_decisions)
            )


# -------------------------------------------------- pinned plugin lifecycle dispatch
from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402
from model_tools import get_tool_definitions  # noqa: E402

discover_plugins(force=True)
manager = get_plugin_manager()
loaded = [item.get("name") for item in manager.list_plugins()]
notes["loaded_plugins"] = loaded
notes["manager_type"] = type(manager).__name__

_DISPATCH_NAMES = (
    "dispatch_hook",
    "run_hook",
    "call_hook",
    "invoke_hook",
    "emit_hook",
    "fire_hook",
    "trigger_hook",
    "run_hooks",
    "call_hooks",
    "dispatch",
    "emit",
)


def _resolve(value):
    if inspect.isawaitable(value):
        return asyncio.run(_await(value))
    return value


async def _await(value):
    return await value


def lifecycle_dispatch(hook_name, event):
    """Invoke a hook through the pinned lifecycle dispatcher.

    Returns (result, description). Raises RuntimeError when no supported
    dispatch entry point can be driven; there is deliberately no fallback that
    constructs the plugin's hook object directly.
    """

    attempts = []
    for name in _DISPATCH_NAMES:
        candidate = getattr(manager, name, None)
        if candidate is None or not callable(candidate):
            continue
        for shape, call in (
            ("(hook, event)", lambda c=candidate: c(hook_name, event)),
            ("(hook, event=event)", lambda c=candidate: c(hook_name, event=event)),
            ("(hook_name=..., event=...)", lambda c=candidate: c(hook_name=hook_name, event=event)),
        ):
            try:
                result = _resolve(call())
            except Exception as exc:
                attempts.append("%s%s -> %s: %s" % (name, shape, type(exc).__name__, exc))
                continue
            return result, "%s.%s%s" % (type(manager).__name__, name, shape)
    raise RuntimeError(
        "no pinned lifecycle dispatch entry point could be driven. "
        "manager=%s attributes=%s attempts=%s"
        % (type(manager).__name__, sorted(dir(manager)), attempts)
    )


def hook_decision(result):
    """Extract the registered hook's own decision from a dispatch result."""

    if isinstance(result, dict) and "action" in result:
        return result
    if isinstance(result, (list, tuple)):
        for item in result:
            found = hook_decision(item)
            if found is not None:
                return found
    return None


def source_of(guild, chat, user, parent=None, is_bot=False):
    return build_session_source(guild, chat, user, parent=parent, is_bot=is_bot)


def event_of(text, session_source, message_id):
    return types.SimpleNamespace(text=text, message_id=message_id, source=session_source)


def dispatch_decision(label, text, session_source, message_id):
    result, description = lifecycle_dispatch(
        "pre_gateway_dispatch", event_of(text, session_source, message_id)
    )
    notes["lifecycle_dispatch"] = description
    decision = hook_decision(result)
    if decision is None:
        raise RuntimeError(
            "lifecycle dispatch for %s returned %r, so the registered hook did not decide"
            % (label, result)
        )
    return decision


if ROLE == "root":
    definitions = get_tool_definitions(enabled_toolsets=["scotty"], quiet_mode=True)
    names = {item["function"]["name"] for item in definitions}
    check("bounded toolset exposes exactly the five Scotty tools", names == EXPECTED_TOOLS)
    check("the bounded plugin is loaded", "scotty-business" in loaded)
    check("the maintainer guard is absent from the client surface", "scotty-guard" not in loaded)

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
        check("served profile has a home configuration: %s" % name, profile is not None)
        if profile is None:
            continue
        enabled = (profile.get("plugins") or {}).get("enabled")
        toolsets = (profile.get("platform_toolsets") or {}).get("discord")
        if name == "scotty-maintainer":
            check("the full profile enables only the guard", enabled == ["scotty-guard"])
            check("the full profile keeps the normal inventory", toolsets == ["*"])
        else:
            check(
                "client profile enables only the bounded plugin: %s" % name,
                enabled == ["scotty-business"],
            )
            check("client profile is bounded: %s" % name, toolsets == ["scotty"])

    root_model = config.get("model") or {}
    for name, profile in profiles.items():
        profile_model = (profile or {}).get("model") or {}
        check(
            "%s keeps the setup-selected provider and model" % name,
            profile_model.get("provider") == root_model.get("provider")
            and profile_model.get("default") == root_model.get("default")
            and bool(root_model.get("provider")),
        )
    notes["model"] = root_model

    try:
        admitted = {
            "operator exact tuple": source_of(CLIENT_GUILD, OPERATOR_CHANNEL, OPERATOR_USER),
            "employee exact tuple": source_of(CLIENT_GUILD, EMPLOYEE_CHANNEL, EMPLOYEE_USER),
            "maintainer exact tuple": source_of(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER),
        }
        for label, session_source in admitted.items():
            decision = dispatch_decision(label, "status please", session_source, "900000000000000101")
            check(
                "registered pre_gateway_dispatch admits %s" % label,
                decision.get("action") == "allow",
            )

        denied = {
            "unknown sender": source_of(CLIENT_GUILD, OPERATOR_CHANNEL, UNKNOWN_USER),
            "maintainer in the operator channel": source_of(
                CLIENT_GUILD, OPERATOR_CHANNEL, ROUTE_USER
            ),
            "maintainer in the employee channel": source_of(
                CLIENT_GUILD, EMPLOYEE_CHANNEL, ROUTE_USER
            ),
            "operator in the maintainer channel": source_of(
                ROUTE_GUILD, ROUTE_CHANNEL, OPERATOR_USER
            ),
            "employee in the maintainer channel": source_of(
                ROUTE_GUILD, ROUTE_CHANNEL, EMPLOYEE_USER
            ),
            "operator in the employee channel": source_of(
                CLIENT_GUILD, EMPLOYEE_CHANNEL, OPERATOR_USER
            ),
            "employee in the operator channel": source_of(
                CLIENT_GUILD, OPERATOR_CHANNEL, EMPLOYEE_USER
            ),
            "wrong guild": source_of("999000000000000002", OPERATOR_CHANNEL, OPERATOR_USER),
            "wrong channel": source_of(CLIENT_GUILD, "900000000000000001", OPERATOR_USER),
            "wrong parent thread": source_of(
                CLIENT_GUILD, "900000000000000001", OPERATOR_USER, parent=EMPLOYEE_CHANNEL
            ),
            "bot author": source_of(
                CLIENT_GUILD, OPERATOR_CHANNEL, OPERATOR_USER, is_bot=True
            ),
        }
        for label, session_source in denied.items():
            decision = dispatch_decision(label, "status please", session_source, "900000000000000102")
            check(
                "registered pre_gateway_dispatch denies %s before model execution" % label,
                decision.get("action") == "skip",
            )

        wizard_decision = dispatch_decision(
            "wizard trigger",
            WIZARD,
            source_of(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER),
            "900000000000000103",
        )
        check(
            "the wizard trigger is intercepted before model execution",
            wizard_decision == {"action": "skip", "reason": "fixed-wizard"},
        )
        for label, session_source in (
            ("operator", source_of(CLIENT_GUILD, OPERATOR_CHANNEL, OPERATOR_USER)),
            ("employee", source_of(CLIENT_GUILD, EMPLOYEE_CHANNEL, EMPLOYEE_USER)),
        ):
            decision = dispatch_decision(
                "wizard from %s" % label, WIZARD, session_source, "900000000000000104"
            )
            check(
                "no client principal can trigger the wizard (%s)" % label,
                decision == {"action": "skip", "reason": "fixed-wizard"},
            )
    except RuntimeError as exc:
        fail(str(exc))

elif ROLE == "maintainer":
    definitions = get_tool_definitions(enabled_toolsets=["scotty"], quiet_mode=True)
    names = {item["function"]["name"] for item in definitions}
    check("the full profile exposes no bounded Scotty tool", not (names & EXPECTED_TOOLS))
    check("the maintainer guard is loaded in its own profile home", "scotty-guard" in loaded)
    check("the bounded plugin is absent from the full profile", "scotty-business" not in loaded)

    guard_module = importlib.import_module("scotty_guard.guard")
    delivered = []
    guard_module.send_fixed_message = lambda channel, text: delivered.append((channel, text))

    try:
        decision = dispatch_decision(
            "maintainer exact tuple",
            "status please",
            source_of(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER),
            "900000000000000201",
        )
        check("the registered guard admits the exact maintainer tuple",
              decision == {"action": "allow"})

        for label, session_source in (
            ("an unknown sender", source_of(ROUTE_GUILD, ROUTE_CHANNEL, UNKNOWN_USER)),
            ("the operator", source_of(ROUTE_GUILD, ROUTE_CHANNEL, OPERATOR_USER)),
            ("the employee", source_of(ROUTE_GUILD, ROUTE_CHANNEL, EMPLOYEE_USER)),
            ("the maintainer in a client channel",
             source_of(CLIENT_GUILD, OPERATOR_CHANNEL, ROUTE_USER)),
            ("a wrong-parent thread",
             source_of(ROUTE_GUILD, "900000000000000001", ROUTE_USER, parent=OPERATOR_CHANNEL)),
            ("a bot author",
             source_of(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER, is_bot=True)),
        ):
            decision = dispatch_decision(label, "status please", session_source, "900000000000000202")
            check(
                "the registered guard denies %s before model execution" % label,
                decision == {"action": "skip", "reason": "unauthorized"},
            )
        check("no guard denial replies or discloses", delivered == [])

        wizard_source = source_of(ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER)
        decision = dispatch_decision(
            "wizard trigger", WIZARD, wizard_source, "900000000000000203"
        )
        check(
            "the wizard trigger is intercepted before model execution",
            decision == {"action": "skip", "reason": "fixed-wizard"},
        )
        check(
            "the fixed wizard goes only to the main-operator channel",
            [channel for channel, _ in delivered] == [OPERATOR_CHANNEL],
        )
        dispatch_decision("wizard repeat", WIZARD, wizard_source, "900000000000000203")
        check("one inbound message delivers the wizard exactly once", len(delivered) == 1)
        for user in (OPERATOR_USER, EMPLOYEE_USER, UNKNOWN_USER):
            dispatch_decision(
                "wizard from a wrong sender",
                WIZARD,
                source_of(ROUTE_GUILD, ROUTE_CHANNEL, user),
                "900000000000000204",
            )
        check("no wrong sender can trigger the wizard", len(delivered) == 1)
    except RuntimeError as exc:
        fail(str(exc))

else:
    fail("unknown probe role: %s" % ROLE)

print(json.dumps({"version": "0.20.6", "role": ROLE, "notes": notes}, indent=1, default=str))
if failures:
    raise SystemExit("pinned smoke failures (%s):\n  - %s" % (ROLE, "\n  - ".join(failures)))
print("pinned smoke (%s): PASS" % ROLE)
'''

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


def _identifiers() -> str:
    maintainer_home = f"/opt/data/profiles/{MAINTAINER_PROFILE}"
    return json.dumps(
        {
            "route_guild": SYNTHETIC_INPUTS.route_guild_id,
            "route_channel": SYNTHETIC_INPUTS.route_channel_id,
            "route_user": SYNTHETIC_INPUTS.route_user_id,
            "client_guild": SYNTHETIC_INPUTS.guild_id,
            "operator_channel": SYNTHETIC_INPUTS.operator_channel_id,
            "operator_user": SYNTHETIC_INPUTS.operator_user_id,
            "employee_channel": SYNTHETIC_INPUTS.employee_channel_id,
            "employee_user": SYNTHETIC_INPUTS.employee_user_id,
            "wizard_command": WIZARD_COMMAND,
            "expected_tools": list(EXPECTED),
            "home": {ROLE_ROOT: "/opt/data", ROLE_MAINTAINER: maintainer_home},
        }
    )


def _command(home: Path, role: str, hermes_home: str, allowed: str, identifiers: str) -> list[str]:
    return [
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
        f"HERMES_HOME={hermes_home}",
        "--env",
        f"DISCORD_ALLOWED_USERS={allowed}",
        # A synthetic token only. The container has no network, so nothing can
        # leave it; the bounded plugin needs a token present to build at all.
        "--env",
        f"DISCORD_BOT_TOKEN={SYNTHETIC_INPUTS.secrets['DISCORD_BOT_TOKEN']}",
        "--env",
        "SCOTTY_PRIVATE_CONFIG=/opt/data/scotty/private.json",
        "--env",
        f"SCOTTY_SMOKE_ROLE={role}",
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scotty-pinned-smoke-") as directory:
        home = Path(directory)
        _stage(home)
        # A clean environment for the docker CLI itself; container environment is
        # supplied explicitly so no host allowlist or credential can leak in.
        environment = os.environ.copy()
        for name in list(environment):
            if name.startswith("DISCORD_"):
                environment.pop(name)
        allowed = runtime_environment(SYNTHETIC_INPUTS)["DISCORD_ALLOWED_USERS"]
        identifiers = _identifiers()
        maintainer_home = f"/opt/data/profiles/{MAINTAINER_PROFILE}"
        for role, hermes_home in (
            (ROLE_ROOT, "/opt/data"),
            (ROLE_MAINTAINER, maintainer_home),
        ):
            result = subprocess.run(
                _command(home, role, hermes_home, allowed, identifiers),
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                env=environment,
            )
            if result.stdout:
                print(result.stdout.strip())
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or f"pinned-image smoke failed for role {role}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
