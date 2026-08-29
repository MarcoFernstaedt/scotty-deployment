from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from model_tools import get_tool_definitions

discover_plugins(force=True)
manager = get_plugin_manager()
plugins = [item for item in manager.list_plugins() if item.get("name") == "scotty-business"]
definitions = get_tool_definitions(enabled_toolsets=["scotty"], quiet_mode=True)
names = {item["function"]["name"] for item in definitions}
expected = {"scotty_read", "scotty_propose", "scotty_approval", "scotty_reminder", "scotty_calculate"}
print(json.dumps({"version": "0.20.6", "tools": sorted(names), "plugins": plugins}))
if names != expected:
    raise SystemExit(f"forbidden or missing model tools: {sorted(names)}")
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scotty-pinned-smoke-") as directory:
        home = Path(directory)
        plugin_root = home / "plugins" / "scotty_business"
        shutil.copytree(ROOT / "assistant" / "scotty_business", plugin_root)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [scotty-business]\n"
            "platform_toolsets:\n  discord: [scotty]\n"
            "tools:\n  tool_search:\n    enabled: off\n",
            encoding="utf-8",
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
