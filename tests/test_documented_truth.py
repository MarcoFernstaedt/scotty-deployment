"""The documents say what the code does, checked against the code.

Prose drifts. A capability lands, the document that said it was unavailable
stays as it was, and the next reader believes whichever one they opened. The
review found several of these at once: Discord administration implemented while
a contract said it was withheld, an add-on count that disagreed with itself,
Google described as guidance-only next to a working adapter, and static checks
described as runtime acceptance.

So the facts in these tests are derived from the code -- the add-on list from
the configuration setup writes, the gates from the Makefile, the evidence class
of each gate from what that gate actually executes -- and the documents are
checked against them. A document that falls behind fails here rather than
misleading somebody later.

Where a claim cannot be derived, the test says so instead of pretending: the
classification below is the contract, and moving a gate between classes has to
be a deliberate edit here as well as there.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.setup import SetupInputs, private_mapping

#: Every document that describes what this deployment does.
DOCUMENTS: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "docs/scotty-basic-operations.md",
    "docs/scotty-basic-release-commands.md",
    "docs/scotty-basic-release-engineering-contract.md",
    "docs/scotty-google-oauth.md",
    "docs/discord-permissions.md",
    "docs/white-label-rename-plan.md",
    "docs/claude-app-handoff.md",
)

#: What each governing gate actually proves, and therefore what a document may
#: claim it proves. Promoting one class into another is how "the tests pass"
#: becomes "it works against Google", which is the specific dishonesty this
#: classification exists to prevent.
#:
#:   static          -- reads bytes; executes none of the product
#:   unit            -- executes product code in this process, synthetic inputs
#:   synthetic       -- executes product code against recorded provider shapes
#:   pinned-runtime  -- needs the pinned image; proves what that image does
#:   installed-host  -- needs a real installed host; proves what systemd/Docker did
#:   live-provider   -- talks to a real provider account. Never run by any gate here.
EVIDENCE_CLASSES: dict[str, str] = {
    "format-check": "static",
    "lint": "static",
    "shellcheck": "static",
    "typecheck": "static",
    "scan": "static",
    "checksums": "static",
    "package": "static",
    "test": "unit",
    "acceptance": "synthetic",
    "smoke": "pinned-runtime",
    "oauth-probe": "pinned-runtime",
}


def read(name: str) -> str:
    return Path(name).read_text(encoding="utf-8")


#: A document that opens by declaring itself a record of an earlier release is
#: not a contradiction: it says so, and says not to act on it. Its counts and
#: its branch belong to the release it describes, and correcting them would
#: falsify the record rather than fix anything.
HISTORICAL_MARKER = "historical record. not current instructions"


def prose(body: str) -> str:
    """One line of readable text: no blockquote markers, no wrapping.

    The marker sentences these tests look for are inside a blockquote and wrap
    across lines, so matching them on the raw bytes finds nothing and quietly
    reports the marker missing.
    """

    stripped = (line.lstrip("> ").rstrip() for line in body.casefold().splitlines())
    return " ".join(" ".join(stripped).split())


def is_historical(body: str) -> bool:
    return HISTORICAL_MARKER in prose(body)


def every_document() -> dict[str, str]:
    """Every document that describes the deployment as it is now."""

    live = {}
    for name in DOCUMENTS:
        body = read(name)
        if not is_historical(body):
            live[name] = body
    return live


def provisioned() -> SetupInputs:
    """The inputs a completed setup holds, so what it writes can be read back.

    Built from the real dataclass rather than from a hand-written dict, so a
    field that setup starts requiring shows up here as a failure rather than as
    a document that quietly stops matching.
    """

    return SetupInputs(  # type: ignore[arg-type]
        model_provider="openrouter",
        model_name="synthetic/model",
        guild_id=synthetic.CLIENT_GUILD,
        operator_channel_id=synthetic.OPERATOR_CHANNEL,
        operator_user_id=synthetic.OPERATOR_USER,
        employee_channel_id=synthetic.EMPLOYEE_CHANNEL,
        employee_user_id=synthetic.EMPLOYEE_USER,
        route_guild_id=synthetic.ROUTE_GUILD,
        route_channel_id=synthetic.ROUTE_CHANNEL,
        route_user_id=synthetic.ROUTE_USER,
        trello_board_id="board-1",
        ghl_location_id="location-1",
        google_account_email="operator.synthetic@example.invalid",
        employee_google_account_email="employee.synthetic@example.invalid",
        secrets={
            "DISCORD_BOT_TOKEN": "synthetic-discord",
            "OPENROUTER_API_KEY": "synthetic-model-key",
            "SCOTTY_RENTCAST_API_KEY": "synthetic-rentcast",
        },
    )


class InventoryTests(unittest.TestCase):
    """Counts and cardinalities, taken from what setup writes."""

    def addons(self) -> list[str]:
        return [str(name) for name in private_mapping(provisioned())["addons"]]  # type: ignore[index]

    def test_the_installed_add_ons_are_the_ones_the_documents_name(self) -> None:
        installed = self.addons()
        self.assertEqual(installed, ["discord", "trello", "ghl", "rentcast", "google_workspace"])
        # Five, not four: Google Workspace is an installed add-on with a
        # working adapter, not a slot still held open.
        self.assertEqual(len(installed), 5)

    def test_no_document_still_counts_four_installed_add_ons(self) -> None:
        installed = len(self.addons())
        cap = 6
        free = cap - installed
        for name, body in every_document().items():
            for line_number, line in enumerate(body.splitlines(), start=1):
                lowered = line.casefold()
                if "add-on" not in lowered and "add on" not in lowered:
                    continue
                with self.subTest(document=name, line=line_number):
                    self.assertNotIn("four installed", lowered, line.strip())
                    self.assertNotIn("fifth add-on", lowered, line.strip())
                    if "slot" in lowered:
                        # Digits or words, but the right number either way.
                        spelled = ("zero", "one", "two", "three", "four", "five", "six")[free]
                        self.assertTrue(
                            str(free) in lowered or spelled in lowered,
                            f"{name}:{line_number} names neither {free} nor {spelled}: {line.strip()}",
                        )

    def test_no_document_calls_google_workspace_guidance_only(self) -> None:
        for name, body in every_document().items():
            with self.subTest(document=name):
                self.assertNotIn("guidance only", body.casefold())

    def test_each_client_user_has_exactly_one_google_account(self) -> None:
        mapping = private_mapping(provisioned())
        accounts = mapping.get("google_workspace") or {}
        self.assertIsInstance(accounts, dict)
        for role, scope in accounts.items():  # type: ignore[union-attr]
            with self.subTest(role=role):
                self.assertIn("account_email", scope)
        # One per client user, never one for the deployment. A document that
        # says "the configured Workspace account", singular, is describing an
        # arrangement this code does not have.
        for name, body in every_document().items():
            with self.subTest(document=name):
                self.assertNotIn("the single google workspace account", body.casefold())


class CapabilityTests(unittest.TestCase):
    """What a document may say is unavailable."""

    def test_no_document_says_discord_administration_does_not_exist(self) -> None:
        from assistant.scotty_business.discord_permissions import required_permissions

        # It exists, it runs on named permissions, and it never asks for the
        # Administrator bit. A document that withholds it is out of date.
        self.assertTrue(required_permissions())
        for name, body in every_document().items():
            lowered = body.casefold()
            with self.subTest(document=name):
                self.assertNotIn("discord administration is unavailable", lowered)
                self.assertNotIn("no discord administration", lowered)

    def test_the_administrator_bit_stays_forbidden_everywhere(self) -> None:
        from assistant.scotty_business.discord_permissions import (
            ADMINISTRATOR,
            PERMISSION_BITS,
            required_permissions,
        )

        self.assertFalse(required_permissions() & ADMINISTRATOR)
        self.assertNotIn("ADMINISTRATOR", PERMISSION_BITS)


class GateTests(unittest.TestCase):
    """The gates, their prerequisites, and what each one is evidence of."""

    def gates(self) -> set[str]:
        makefile = read("Makefile")
        declared = re.search(r"^\.PHONY:(.*)$", makefile, re.MULTILINE)
        assert declared is not None
        return {name for name in declared.group(1).split() if name != "verify"}

    def test_every_gate_has_a_declared_evidence_class(self) -> None:
        # A new gate with no class is a new claim nobody has characterised.
        self.assertEqual(self.gates(), set(EVIDENCE_CLASSES))

    def test_verify_runs_every_gate_that_has_a_class(self) -> None:
        makefile = read("Makefile")
        line = re.search(r"^verify:(.*)$", makefile, re.MULTILINE)
        assert line is not None
        ran = set(line.group(1).split())
        # shellcheck is reached through lint rather than named again.
        self.assertEqual(ran | {"shellcheck"}, set(EVIDENCE_CLASSES))

    def test_only_the_pinned_runtime_gates_need_docker(self) -> None:
        """Which gates need a daemon, taken from what they import and run."""

        needs_docker = {gate for gate, kind in EVIDENCE_CLASSES.items() if kind == "pinned-runtime"}
        self.assertEqual(needs_docker, {"smoke", "oauth-probe"})
        # "Needs Docker" means it runs the daemon, not that it mentions the
        # word: `acceptance` names docker in a list of brands a client-visible
        # string may never contain, which is the opposite of a dependency.
        invokes = re.compile(r"""which\(\s*["']docker|\[\s*["']docker""")
        for gate, script in (
            ("smoke", "tools/pinned_smoke.py"),
            ("oauth-probe", "tools/pinned_oauth_probe.py"),
        ):
            with self.subTest(gate=gate):
                self.assertRegex(read(script), invokes)
        # And no gate outside that set runs it, so a machine without a daemon
        # can still run everything else and know what it proved.
        for gate, script in (
            ("acceptance", "tools/synthetic_acceptance.py"),
            ("scan", "tools/scan_repository.py"),
            ("package", "tools/build_package.py"),
            ("checksums", "tools/generate_checksums.py"),
        ):
            with self.subTest(gate=gate):
                self.assertIsNone(invokes.search(read(script)))

    def test_no_document_calls_a_static_or_synthetic_gate_a_live_one(self) -> None:
        promotions = (
            "verified against google",
            "verified against trello",
            "against the live provider",
            "live provider evidence",
            "proves the deployment works",
        )
        for name, body in every_document().items():
            lowered = body.casefold()
            for phrase in promotions:
                with self.subTest(document=name, phrase=phrase):
                    self.assertNotIn(phrase, lowered)

    def test_the_evidence_classes_are_documented_where_a_reader_looks(self) -> None:
        contract = read("docs/scotty-basic-release-engineering-contract.md").casefold()
        for kind in sorted(set(EVIDENCE_CLASSES.values())):
            with self.subTest(kind=kind):
                self.assertIn(kind, contract)


class CommandSurfaceTests(unittest.TestCase):
    """The commands a document promises are the commands that exist."""

    def supervisor_commands(self) -> set[str]:
        source = read("assistant/scotty_supervisor/cli.py")
        return set(re.findall(r'command == "([a-z-]+)"', source))

    def test_the_lifecycle_is_not_described_as_one_command(self) -> None:
        """`scotty-start` brings it up; it does not operate it.

        Releases, acceptance, activation, backup, restore, rollback and
        uninstall all live in the supervisor. A document that calls starting
        "the single lifecycle command" is describing a smaller product than the
        one that ships, and an operator who believes it has no way to roll back.
        """

        commands = self.supervisor_commands()
        self.assertTrue({"publish", "stage", "accept", "activate", "uninstall"} <= commands)
        for name, body in every_document().items():
            lowered = prose(body)
            with self.subTest(document=name):
                self.assertNotIn("the single root-only lifecycle command", lowered)
                self.assertNotIn("the only lifecycle command is", lowered)

    def test_the_readme_names_the_supervisor_beside_the_start_command(self) -> None:
        readme = prose(read("README.md"))
        self.assertIn("scotty-start", readme)
        self.assertIn("scotty-supervisor", readme)

    def test_no_document_calls_the_oauth_probe_both_run_and_unavailable(self) -> None:
        """It runs in CI on every push. Saying otherwise is the contradiction.

        The evidence it produces is `pinned-runtime`: real, and obtainable only
        where the image is. A machine without a Docker daemon is missing the
        gate; the release is not missing the evidence.
        """

        workflow = read(".github/workflows/verify.yml")
        self.assertIn("make oauth-probe", workflow)
        self.assertIn("docker pull", workflow)
        for name, body in every_document().items():
            lowered = prose(body)
            with self.subTest(document=name):
                self.assertNotIn(
                    "the exact subcommand has not been captured in this repository", lowered
                )


class EvidencePromotionTests(unittest.TestCase):
    """A static or synthetic check is never described as a runtime one."""

    def test_acceptance_is_described_as_synthetic_wherever_it_is_named(self) -> None:
        for name, body in every_document().items():
            for line_number, line in enumerate(body.splitlines(), start=1):
                if "make acceptance" not in line and "`acceptance`" not in line:
                    continue
                lowered = line.casefold()
                with self.subTest(document=name, line=line_number):
                    # It runs product code against recorded provider shapes. It
                    # does not touch a provider, a container, or a host.
                    self.assertNotIn("runtime acceptance", lowered, line.strip())
                    self.assertNotIn("live acceptance", lowered, line.strip())

    def test_the_word_synthetic_is_attached_to_the_synthetic_gate(self) -> None:
        commands = read("docs/scotty-basic-release-commands.md").casefold()
        self.assertIn("synthetic acceptance", commands)


class BranchTests(unittest.TestCase):
    """No document points at a branch or an artefact that is not this one."""

    BRANCH = "feature/scotty-google-one-command"

    def test_a_document_that_keeps_an_old_release_s_facts_says_so_at_the_top(self) -> None:
        """The one exemption, and the words that earn it.

        `claude-app-handoff.md` names a superseded branch, an old commit, and
        an add-on count from an earlier release. That is fine, because it opens
        by saying it is a record and not to act on it -- and this checks that
        the marker is really there rather than assumed.
        """

        marked = [name for name in DOCUMENTS if is_historical(read(name))]
        self.assertEqual(marked, ["docs/claude-app-handoff.md"])
        body = prose(read("docs/claude-app-handoff.md"))
        self.assertIn("do not act on them", body)
        # And it points the reader at what is current.
        self.assertIn("claude.md", body)
        self.assertIn("readme.md", body)

    def test_no_document_names_a_superseded_branch(self) -> None:
        stale = (
            "feature/scotty-basic-assistant",
            "feature/scotty-discord-admin",
            "feature/scotty-broker",
            "main-candidate",
        )
        for name, body in every_document().items():
            for branch in stale:
                with self.subTest(document=name, branch=branch):
                    self.assertNotIn(branch, body)

    def test_the_contract_names_the_branch_this_work_is_on(self) -> None:
        self.assertIn(self.BRANCH, read("CLAUDE.md"))


if __name__ == "__main__":
    unittest.main()
