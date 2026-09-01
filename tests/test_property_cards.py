"""The canonical property card: one truth, explainable, never silently overwritten.

Wholesaling data arrives from conversation, from Trello, from RentCast and from
GHL, in different spellings and different orders. The card is the one canonical
record, so the rules that matter are: which source may write a field, when two
records are the same property, and what happens when they disagree.
"""

from __future__ import annotations

import unittest

from assistant.scotty_business.property_cards import (
    CARD_SCHEMA_VERSION,
    Authority,
    ConflictError,
    FieldValue,
    PropertyCard,
    compare,
    duplicate_score,
    merge_preview,
    normalize_address,
    parse_card,
)


def value(text, source="operator", authority=Authority.VERIFIED, at="2026-09-01T00:00:00Z"):
    return FieldValue(value=text, source=source, source_id="", retrieved_at=at, authority=authority)


class AddressNormalizationTests(unittest.TestCase):
    def test_the_same_address_written_differently_normalizes_the_same(self) -> None:
        spellings = (
            "1234 North West Elm Street, Apartment 5B, Springfield, IL 62704",
            "1234 NW Elm St Apt 5B, Springfield, Illinois 62704",
            "  1234 nw elm st. apt 5b springfield il 62704  ",
        )
        normalized = {normalize_address(text).key() for text in spellings}
        self.assertEqual(len(normalized), 1)

    def test_the_parts_are_kept_separately_not_just_a_flattened_string(self) -> None:
        address = normalize_address("88 Maple Ave Unit 2, Dayton, OH 45402")
        self.assertEqual(address.number, "88")
        self.assertEqual(address.street, "MAPLE AVE")
        self.assertEqual(address.unit, "2")
        self.assertEqual(address.city, "DAYTON")
        self.assertEqual(address.state, "OH")
        self.assertEqual(address.postal_code, "45402")

    def test_a_different_unit_is_a_different_address(self) -> None:
        first = normalize_address("88 Maple Ave Unit 2, Dayton, OH 45402")
        second = normalize_address("88 Maple Ave Unit 3, Dayton, OH 45402")
        self.assertNotEqual(first.key(), second.key())

    def test_an_unparseable_address_is_reported_not_guessed(self) -> None:
        address = normalize_address("send me the thing")
        self.assertFalse(address.complete)
        self.assertTrue(address.problems)


class FieldAuthorityTests(unittest.TestCase):
    def test_a_weaker_source_never_overwrites_a_verified_value(self) -> None:
        card = PropertyCard.new("card-1")
        card = card.with_field("asking_price", value("125000"))
        weaker = FieldValue(
            value="130000",
            source="rentcast",
            source_id="rc-1",
            retrieved_at="2026-09-02T00:00:00Z",
            authority=Authority.PROVIDER,
        )
        updated, rejected = card.apply(("asking_price", weaker))
        self.assertEqual(updated.fields["asking_price"].value, "125000")
        self.assertEqual([name for name, _ in rejected], ["asking_price"])

    def test_a_model_inference_is_the_weakest_source_of_all(self) -> None:
        card = PropertyCard.new("card-1").with_field(
            "arv",
            FieldValue(
                value="200000",
                source="rentcast",
                source_id="rc-1",
                retrieved_at="2026-09-01T00:00:00Z",
                authority=Authority.PROVIDER,
            ),
        )
        guess = FieldValue(
            value="210000",
            source="assistant",
            source_id="",
            retrieved_at="2026-09-02T00:00:00Z",
            authority=Authority.INFERRED,
        )
        updated, rejected = card.apply(("arv", guess))
        self.assertEqual(updated.fields["arv"].value, "200000")
        self.assertTrue(rejected)

    def test_a_newer_value_from_the_same_authority_wins_and_keeps_provenance(self) -> None:
        card = PropertyCard.new("card-1").with_field("asking_price", value("125000"))
        newer = value("120000", at="2026-09-05T00:00:00Z")
        updated, rejected = card.apply(("asking_price", newer))
        self.assertEqual(updated.fields["asking_price"].value, "120000")
        self.assertEqual(updated.fields["asking_price"].retrieved_at, "2026-09-05T00:00:00Z")
        self.assertEqual(rejected, ())

    def test_an_unknown_field_is_refused_rather_than_stored(self) -> None:
        card = PropertyCard.new("card-1")
        with self.assertRaises(ValueError):
            card.with_field("wire_transfer_instructions", value("nope"))


class DuplicateDetectionTests(unittest.TestCase):
    def card(self, **fields) -> PropertyCard:
        card = PropertyCard.new(fields.pop("card_id", "card-1"))
        for name, text in fields.items():
            card = card.with_field(name, value(text))
        return card

    def test_the_same_normalized_address_scores_as_a_duplicate_with_reasons(self) -> None:
        left = self.card(address="1234 NW Elm St, Springfield, IL 62704")
        right = self.card(
            card_id="card-2", address="1234 North West Elm Street, Springfield, Illinois 62704"
        )
        result = duplicate_score(left, right)
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertTrue(result.duplicate)
        self.assertTrue(any("address" in reason for reason in result.reasons))

    def test_a_shared_parcel_identifier_is_strong_evidence_on_its_own(self) -> None:
        left = self.card(address="1234 NW Elm St, Springfield, IL 62704", parcel_id="14-22-333")
        right = self.card(card_id="card-2", address="unknown", parcel_id="14-22-333")
        result = duplicate_score(left, right)
        self.assertTrue(result.duplicate)
        self.assertTrue(any("parcel" in reason for reason in result.reasons))

    def test_different_properties_are_not_duplicates_and_say_why(self) -> None:
        left = self.card(address="1234 NW Elm St, Springfield, IL 62704")
        right = self.card(card_id="card-2", address="7 Oak Ln, Dayton, OH 45402")
        result = duplicate_score(left, right)
        self.assertFalse(result.duplicate)
        self.assertLess(result.confidence, 0.5)
        self.assertTrue(result.reasons)

    def test_scoring_is_symmetric_and_deterministic(self) -> None:
        left = self.card(address="88 Maple Ave Unit 2, Dayton, OH 45402", parcel_id="p-1")
        right = self.card(card_id="card-2", address="88 Maple Avenue #2, Dayton OH 45402")
        first = duplicate_score(left, right)
        self.assertEqual(first.confidence, duplicate_score(right, left).confidence)
        self.assertEqual(first.reasons, duplicate_score(left, right).reasons)

    def test_a_confidence_threshold_is_configurable_not_hidden(self) -> None:
        left = self.card(address="88 Maple Ave, Dayton, OH 45402")
        right = self.card(card_id="card-2", address="88 Maple Ave, Dayton, OH 45403")
        self.assertFalse(duplicate_score(left, right).duplicate)
        self.assertTrue(duplicate_score(left, right, threshold=0.1).duplicate)


class ComparisonAndMergeTests(unittest.TestCase):
    def pair(self):
        left = PropertyCard.new("card-1")
        left = left.with_field("address", value("88 Maple Ave, Dayton, OH 45402"))
        left = left.with_field("asking_price", value("125000"))
        left = left.with_field("seller_name", value("A. Synthetic"))
        right = PropertyCard.new("card-2")
        right = right.with_field("address", value("88 Maple Avenue, Dayton OH 45402"))
        right = right.with_field("asking_price", value("130000"))
        right = right.with_field("arv", value("210000"))
        return left, right

    def test_a_comparison_names_every_agreement_conflict_and_addition(self) -> None:
        left, right = self.pair()
        diff = compare(left, right)
        self.assertIn("asking_price", diff.conflicts)
        self.assertIn("arv", diff.additions)
        self.assertIn("seller_name", diff.only_left)
        self.assertIn("address", diff.agreements)

    def test_a_merge_preview_is_deterministic_and_changes_nothing_yet(self) -> None:
        left, right = self.pair()
        first = merge_preview(left, right)
        second = merge_preview(left, right)
        self.assertEqual(first.result.fields.keys(), second.result.fields.keys())
        self.assertEqual(first.payload_hash, second.payload_hash)
        # The preview is a proposal: neither input card is touched.
        self.assertEqual(left.fields["asking_price"].value, "125000")

    def test_a_conflict_is_preserved_and_must_be_chosen_explicitly(self) -> None:
        left, right = self.pair()
        preview = merge_preview(left, right)
        self.assertIn("asking_price", preview.unresolved)
        with self.assertRaises(ConflictError):
            preview.commit()

    def test_choosing_a_side_resolves_exactly_that_conflict(self) -> None:
        left, right = self.pair()
        preview = merge_preview(left, right).choose("asking_price", "right")
        self.assertEqual(preview.unresolved, ())
        merged = preview.commit()
        self.assertEqual(merged.fields["asking_price"].value, "130000")
        # Nothing that only one side knew is lost in the merge.
        self.assertEqual(merged.fields["seller_name"].value, "A. Synthetic")
        self.assertEqual(merged.fields["arv"].value, "210000")

    def test_a_choice_for_a_field_that_is_not_in_conflict_is_refused(self) -> None:
        left, right = self.pair()
        with self.assertRaises(ConflictError):
            merge_preview(left, right).choose("arv", "left")

    def test_merging_never_silently_overwrites_a_verified_value(self) -> None:
        left, right = self.pair()
        preview = merge_preview(left, right).choose("asking_price", "left")
        merged = preview.commit()
        self.assertEqual(merged.fields["asking_price"].value, "125000")
        self.assertEqual(merged.fields["asking_price"].source, "operator")


class SerializationTests(unittest.TestCase):
    def test_a_card_round_trips_through_its_stored_form_with_provenance(self) -> None:
        card = PropertyCard.new("card-1").with_field("asking_price", value("125000"))
        restored = parse_card(card.as_json())
        self.assertEqual(restored.card_id, "card-1")
        self.assertEqual(restored.fields["asking_price"].value, "125000")
        self.assertEqual(restored.fields["asking_price"].source, "operator")
        self.assertEqual(restored.schema_version, CARD_SCHEMA_VERSION)

    def test_a_card_from_an_older_schema_is_migrated_not_rejected(self) -> None:
        card = PropertyCard.new("card-1").with_field("asking_price", value("125000"))
        stored = card.as_json()
        stored["schema_version"] = 1
        restored = parse_card(stored)
        self.assertEqual(restored.schema_version, CARD_SCHEMA_VERSION)
        self.assertEqual(restored.fields["asking_price"].value, "125000")

    def test_a_malformed_stored_card_is_refused(self) -> None:
        for bad in ({}, {"card_id": ""}, {"card_id": "c", "fields": []}, "text"):
            with self.subTest(stored=bad), self.assertRaises(ValueError):
                parse_card(bad)


if __name__ == "__main__":
    unittest.main()
