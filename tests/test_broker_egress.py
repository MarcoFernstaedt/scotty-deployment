"""Where the privileged broker is allowed to send a request, and nowhere else.

Two defects met in this file. The unit permitted `AF_UNIX` only, while the code
inside it opens HTTPS connections — so on the installed host Trello, GHL and
RentCast could not work at all. Loosening that is necessary and is also the
moment to be careful, because the process being loosened is the one holding
every provider credential.

So the host is not a thing a caller influences. It comes from the operation
table, it is compared after the URL is built, redirects are refused rather than
followed, ambient proxy variables are ignored, and the response is bounded. A
request that resolves anywhere but its provider does not leave the process.
"""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from pathlib import Path

from assistant.scotty_broker.executor import (
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT,
    ExecutionError,
    Executor,
    _opener,
)
from assistant.scotty_broker.operations import PROVIDER_BASES


class Store:
    def read(self, provider, credential_class, actor="shared"):
        return "synthetic-material-value"


class UnitTests(unittest.TestCase):
    """The sandbox has to permit what the code inside it actually does."""

    def unit(self) -> str:
        return Path("broker/scotty-credential-broker.service").read_text(encoding="utf-8")

    def test_the_sandbox_permits_the_families_https_needs(self) -> None:
        unit = self.unit()
        families = next(
            line for line in unit.splitlines() if line.startswith("RestrictAddressFamilies=")
        )
        # AF_UNIX alone was the defect: the ingress worked and every provider
        # call failed on the installed host while passing in tests.
        for family in ("AF_UNIX", "AF_INET", "AF_INET6"):
            with self.subTest(family=family):
                self.assertIn(family, families)
        self.assertNotIn("AF_NETLINK", families)
        self.assertNotIn("AF_PACKET", families)

    def test_the_sandbox_still_refuses_everything_it_refused_before(self) -> None:
        unit = self.unit()
        for directive in (
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "PrivateDevices=true",
            "RestrictNamespaces=true",
            "MemoryDenyWriteExecute=true",
            "ReadWritePaths=/run/scotty /var/lib/scotty",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)

    def test_the_service_runs_as_root_and_says_so(self) -> None:
        self.assertIn("User=root", self.unit())

    def test_ambient_proxy_variables_are_not_inherited(self) -> None:
        unit = self.unit()
        # A proxy variable in the environment would route every credentialed
        # request through whatever it names.
        self.assertIn("Environment=", unit)
        self.assertIn("no_proxy", unit)


class RedirectTests(unittest.TestCase):
    def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        opener = _opener()
        handlers = [type(handler).__name__ for handler in opener.handlers]
        self.assertIn("NoRedirects", handlers)
        # And no proxy handler is installed at all, so the environment cannot
        # introduce one.
        self.assertNotIn("ProxyHandler", handlers)

    def test_the_redirect_handler_raises_instead_of_returning_a_request(self) -> None:
        from assistant.scotty_broker.executor import NoRedirects

        handler = NoRedirects()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                urllib.request.Request("https://api.trello.com/1/cards/x"),
                None,
                302,
                "Found",
                {},
                "https://elsewhere.example/",
            )


class TargetTests(unittest.TestCase):
    def executor(self) -> Executor:
        return Executor(Store())

    def test_every_declared_provider_base_is_https_and_fixed(self) -> None:
        for provider, base in sorted(PROVIDER_BASES.items()):
            with self.subTest(provider=provider):
                self.assertTrue(base.startswith("https://"))
                self.assertNotIn("{", base)

    def test_an_argument_cannot_move_the_request_off_its_provider(self) -> None:
        executor = self.executor()
        for endpoint in (
            "//evil.example/x",
            "https://evil.example/x",
            "..%2f..%2fevil",
            "\\evil.example",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ExecutionError):
                executor.run(
                    "rentcast.fetch",
                    {"endpoint": endpoint},
                    actor="main_operator",
                )

    def test_the_bounds_are_real_numbers_not_absent(self) -> None:
        self.assertLessEqual(MAX_TIMEOUT, 60)
        self.assertLessEqual(MAX_RESPONSE_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
