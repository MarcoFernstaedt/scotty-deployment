from __future__ import annotations

import json
import unittest
from pathlib import Path

from assistant.scotty_business.config import RuntimeConfig


class FixtureTests(unittest.TestCase):
    def test_private_example_is_valid_and_synthetic(self) -> None:
        root = Path("fixtures")
        raw = json.loads((root / "scotty.private.example.json").read_text(encoding="utf-8"))
        config = RuntimeConfig.from_mapping(raw)
        self.assertEqual(config.addons, ("discord", "trello", "ghl", "rentcast"))
        all_text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(root.glob("*.json"))
        )
        self.assertIn("Synthetic", all_text)
        self.assertNotRegex(all_text, r"(?i)(api[_ -]?key|token|secret)\s*[:=]\s*[^\"\s]{8,}")

    def test_provider_fixtures_have_explicit_source_identity(self) -> None:
        for name in ("trello.cards.json", "ghl.contacts.json", "rentcast.property.json"):
            with self.subTest(name=name):
                data = json.loads((Path("fixtures") / name).read_text(encoding="utf-8"))
                self.assertTrue(data)
                serialized = json.dumps(data)
                self.assertRegex(serialized, r"(?:id|Id|source_id)")


if __name__ == "__main__":
    unittest.main()
