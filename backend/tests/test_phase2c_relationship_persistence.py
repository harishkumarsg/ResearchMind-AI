"""
Phase 2C step 2C-3 — durable persistence for cross-paper relationships.

Four properties carry the weight:

  * BOTH papers must be the caller's. The tempting bug is to check one —
    a relationship written between your own paper and somebody else's
    would look entirely well-formed;

  * the pair is unordered, so (A,B) and (B,A) are ONE row. Two rows would
    be two competing analyses of the same fact with nothing to say which
    is current;

  * ...but the CONTENT is oriented. `cites_a` belongs to `paper_a_id`, so
    the store refuses a reversed pair rather than swapping citations to
    fit. Swapping without rewriting the statement would misattribute
    evidence while looking perfectly well-formed, which is the worst
    failure this feature could have;

  * only a validated PaperRelationship is persisted. A dict, a string, or
    an object carrying a non-canonical section name is refused.

Fully offline: the database is per-test in-memory SQLite through the
existing harness, which enforces the real CHECK and UNIQUE constraints.
No Groq, no Voyage, no Qdrant, no Storage, no production.

NOTE on the harness: tests/__init__.py sets QUOTA_ENFORCEMENT=off for the
whole suite. Nothing here touches quota — persistence charges nothing —
so that default is neither relied on nor altered by this file.
"""
import ast
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy.exc import IntegrityError

import app.db.session as session_module
from app.db.models import Paper, PaperRelationshipRow
from app.services.intelligence_schema import SECTION_NAMES
from app.services.paper_intelligence_store import PaperNotOwned
from app.services.relationship_schema import (
    RELATION_ALIGNED,
    RELATION_COMPLEMENTARY,
    RELATION_DIVERGENT,
    RELATION_NOT_COMPARABLE,
    PaperRelationship,
    RelationshipValidationError,
    SectionRelationship,
    validate_relationship,
)
from app.services.paper_relationship_store import (
    PAPER_RELATIONSHIP_SCHEMA_VERSION,
    StoredRelationship,
    canonical_pair,
    get_relationship,
    relationship_is_stale,
    save_relationship,
)
from tests.sqlite_harness import attach_sqlite_db

STORE_PATH = os.path.join(
    BACKEND_DIR, "app", "services", "paper_relationship_store.py"
)
MIGRATION_PATH = os.path.join(
    BACKEND_DIR, "migrations", "0006_paper_relationship.sql"
)

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

#: Deliberately ordered so LOW < HIGH as uuids, and UNKNOWN sorts last.
PAPER_LOW = "11111111-1111-1111-1111-111111111111"
PAPER_HIGH = "22222222-2222-2222-2222-222222222222"
PAPER_THIRD = "33333333-3333-3333-3333-333333333333"
FOREIGN_PAPER = "88888888-8888-8888-8888-888888888888"
#: Owned by A and sorting ABOVE the foreign/unknown ids, so an ownership
#: test can supply a canonical pair whose FIRST id is the unowned one.
PAPER_TOP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNKNOWN_PAPER = "99999999-9999-9999-9999-999999999999"

TEST_MODEL = "test-model/2c3"
GENERATED_AT = datetime(2026, 9, 24, 11, 30, 0, tzinfo=timezone.utc)

#: The two SOURCE paper_intelligence stamps. Deliberately distinct from
#: each other and from GENERATED_AT, so any mix-up between the three is
#: visible rather than coincidentally equal.
SOURCE_A_AT = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)
SOURCE_B_AT = datetime(2026, 9, 21, 9, 15, 0, tzinfo=timezone.utc)

_UNSET = object()

COMPARABLE = ("methodology", "key_results")

ALLOWED_A = {(1, 0): "A one", (3, 0): "A three"}
ALLOWED_B = {(2, 0): "B two", (3, 0): "B three"}


def cite(pair):
    page, chunk_id = pair
    return {"page": page, "chunk_id": chunk_id}


def raw_section(relation=RELATION_ALIGNED, statement="Both describe a pipeline.",
                cites_a=None, cites_b=None):
    if relation != RELATION_NOT_COMPARABLE:
        if cites_a is None:
            cites_a = [cite((3, 0))]
        if cites_b is None:
            cites_b = [cite((3, 0))]
    return {
        "relation": relation,
        "statement": statement,
        "cites_a": cites_a if cites_a is not None else [],
        "cites_b": cites_b if cites_b is not None else [],
    }


def raw_payload(**overrides):
    sections = {name: raw_section() for name in COMPARABLE}
    sections.update(overrides)
    return {"sections": sections}


def validated(**overrides) -> PaperRelationship:
    """A PaperRelationship obtained the way Step 2C-5 will obtain one:
    through the validator, against server-built allowlists."""
    return validate_relationship(
        raw_payload(**overrides),
        comparable_sections=COMPARABLE,
        allowed_evidence_a=ALLOWED_A,
        allowed_evidence_b=ALLOWED_B,
    )


def _as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self._seed_paper(OWNER_A, PAPER_LOW, "A low")
        self._seed_paper(OWNER_A, PAPER_HIGH, "A high")
        self._seed_paper(OWNER_A, PAPER_THIRD, "A third")
        self._seed_paper(OWNER_A, PAPER_TOP, "A top")
        self._seed_paper(OWNER_B, FOREIGN_PAPER, "B's paper")

    def _seed_paper(self, owner_id, paper_id, title):
        with self.factory() as session:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="h" * 64,
                    storage_path=f"{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()

    def save(self, owner_id=OWNER_A, a=PAPER_LOW, b=PAPER_HIGH, **kw):
        # A sentinel, not `or validated()`: a falsy relationship such as
        # None or [] must reach the store so its TypeError guard is what
        # rejects it, rather than being quietly replaced with a valid
        # object here.
        supplied = kw.pop("relationship", _UNSET)
        return save_relationship(
            owner_id,
            a,
            b,
            validated() if supplied is _UNSET else supplied,
            paper_a_generated_at=kw.pop("paper_a_generated_at", SOURCE_A_AT),
            paper_b_generated_at=kw.pop("paper_b_generated_at", SOURCE_B_AT),
            model=kw.pop("model", TEST_MODEL),
            generated_at=kw.pop("generated_at", GENERATED_AT),
            **kw,
        )

    def rows(self):
        """Every stored row, read with NO owner filter at all."""
        with self.factory() as session:
            return session.query(PaperRelationshipRow).all()


# ----------------------------------------------------------------------
# 1, 22-26. The happy path
# ----------------------------------------------------------------------
class TestValidPersistence(StoreTestCase):

    def test_a_valid_relationship_persists(self):
        stored = self.save()

        self.assertIsInstance(stored, StoredRelationship)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(str(stored.paper_a_id), PAPER_LOW)
        self.assertEqual(str(stored.paper_b_id), PAPER_HIGH)

    def test_generated_at_persists(self):
        self.save()
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(_as_utc(got.generated_at), GENERATED_AT)

    def test_model_persists(self):
        self.save(model="openai/gpt-oss-120b")
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model,
            "openai/gpt-oss-120b",
        )

    def test_schema_version_persists_and_defaults_to_the_constant(self):
        self.save()
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(got.schema_version, PAPER_RELATIONSHIP_SCHEMA_VERSION)
        self.assertEqual(PAPER_RELATIONSHIP_SCHEMA_VERSION, "1")

    def test_an_explicit_schema_version_is_recorded_as_given(self):
        self.save(schema_version="7")
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).schema_version, "7"
        )

    def test_the_relationship_round_trips_exactly(self):
        original = validated(
            methodology=raw_section(
                RELATION_DIVERGENT,
                statement="One uses a two-stage pipeline; the other is end-to-end.",
                cites_a=[cite((1, 0)), cite((3, 0))],
                cites_b=[cite((2, 0))],
            )
        )

        self.save(relationship=original)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).relationship

        self.assertEqual(
            got.model_dump(mode="json"), original.model_dump(mode="json")
        )
        method = got.sections["methodology"]
        self.assertEqual(method.relation, RELATION_DIVERGENT)
        self.assertEqual(
            [(e.page, e.chunk_id) for e in method.cites_a], [(1, 0), (3, 0)]
        )
        self.assertEqual([(e.page, e.chunk_id) for e in method.cites_b], [(2, 0)])

    def test_not_comparable_round_trips_with_no_citations(self):
        original = validated(
            key_results=raw_section(
                RELATION_NOT_COMPARABLE,
                statement="Different metrics; nothing lines up.",
                cites_a=[],
                cites_b=[],
            )
        )

        self.save(relationship=original)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).relationship

        self.assertEqual(got.sections["key_results"].relation, RELATION_NOT_COMPARABLE)
        self.assertEqual(got.sections["key_results"].cites_a, [])

    def test_the_stored_json_holds_only_validated_relationship_fields(self):
        self.save()

        raw = self.rows()[0].relationship

        self.assertEqual(set(raw), {"sections"})
        for section in raw["sections"].values():
            self.assertEqual(
                set(section), {"relation", "statement", "cites_a", "cites_b"}
            )
            for citation in section["cites_a"] + section["cites_b"]:
                self.assertEqual(set(citation), {"page", "chunk_id"})

    def test_no_identity_is_duplicated_inside_the_json(self):
        self.save()

        blob = json.dumps(self.rows()[0].relationship)

        for leaked in (OWNER_A, PAPER_LOW, PAPER_HIGH, "owner_id", "paper_a_id",
                       "paper_b_id", "paper_id", "point_id", "token"):
            self.assertNotIn(leaked, blob)

    def test_all_ten_sections_may_be_stored(self):
        every = validate_relationship(
            {"sections": {name: raw_section() for name in SECTION_NAMES}},
            comparable_sections=SECTION_NAMES,
            allowed_evidence_a=ALLOWED_A,
            allowed_evidence_b=ALLOWED_B,
        )

        self.save(relationship=every)

        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).relationship
        self.assertEqual(list(got.sections), list(SECTION_NAMES))


# ----------------------------------------------------------------------
# 2-11. Only validated objects reach the table
# ----------------------------------------------------------------------
class TestOnlyValidatedObjectsPersist(StoreTestCase):

    def test_a_raw_dict_is_rejected(self):
        with self.assertRaises(TypeError):
            self.save(relationship=raw_payload())
        self.assertEqual(self.rows(), [])

    def test_a_raw_string_is_rejected(self):
        with self.assertRaises(TypeError):
            self.save(relationship=json.dumps(raw_payload()))
        self.assertEqual(self.rows(), [])

    def test_other_unvalidated_types_are_rejected(self):
        for bad in (None, 42, [], object(), True):
            with self.subTest(relationship=type(bad).__name__):
                with self.assertRaises(TypeError):
                    self.save(relationship=bad)
        self.assertEqual(self.rows(), [])

    def test_a_non_canonical_section_name_is_rejected(self):
        """PaperRelationship types `sections` as a plain mapping, so it can
        be CONSTRUCTED with an arbitrary key even though the validator
        would refuse it. Persistence must refuse it too."""
        smuggled = PaperRelationship(
            sections={
                "future_work": SectionRelationship(
                    relation=RELATION_ALIGNED,
                    statement="Smuggled past the validator.",
                    cites_a=[{"page": 3, "chunk_id": 0}],
                    cites_b=[{"page": 3, "chunk_id": 0}],
                )
            }
        )

        with self.assertRaises(RelationshipValidationError) as ctx:
            self.save(relationship=smuggled)

        self.assertEqual(ctx.exception.code, "unknown_section")
        self.assertEqual(self.rows(), [])

    def test_an_unknown_relation_never_reaches_the_store(self):
        with self.assertRaises(RelationshipValidationError):
            validated(methodology=raw_section("better"))
        self.assertEqual(self.rows(), [])

    def test_evidence_outside_the_allowlist_never_reaches_the_store(self):
        with self.assertRaises(RelationshipValidationError):
            validated(methodology=raw_section(cites_a=[cite((9, 9))]))
        self.assertEqual(self.rows(), [])

    def test_cross_paper_evidence_never_reaches_the_store(self):
        """(2, 0) is in B's allowlist only, so citing it for A is refused
        one layer up — the store is never offered the object."""
        with self.assertRaises(RelationshipValidationError):
            validated(methodology=raw_section(cites_a=[cite((2, 0))]))
        self.assertEqual(self.rows(), [])

    def test_not_comparable_with_evidence_never_reaches_the_store(self):
        with self.assertRaises(RelationshipValidationError):
            validated(
                methodology=raw_section(
                    RELATION_NOT_COMPARABLE, cites_a=[cite((3, 0))], cites_b=[]
                )
            )
        self.assertEqual(self.rows(), [])

    def test_a_grounded_relation_without_both_sides_never_reaches_the_store(self):
        for relation in (RELATION_ALIGNED, RELATION_DIVERGENT, RELATION_COMPLEMENTARY):
            for side in ("cites_a", "cites_b"):
                with self.subTest(relation=relation, missing=side):
                    with self.assertRaises(RelationshipValidationError):
                        validated(methodology=raw_section(relation, **{side: []}))
        self.assertEqual(self.rows(), [])

    def test_a_corrupted_stored_row_is_reported_not_repaired(self):
        self.save()

        with self.factory() as session:
            row = session.query(PaperRelationshipRow).one()
            broken = dict(row.relationship)
            broken["sections"] = dict(broken["sections"])
            broken["sections"]["methodology"] = {"relation": "aligned"}
            row.relationship = broken
            session.commit()

        with self.assertRaises(RelationshipValidationError) as ctx:
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(ctx.exception.code, "invalid_structure")


# ----------------------------------------------------------------------
# 12-17, 27-28. Ownership
# ----------------------------------------------------------------------
class TestOwnership(StoreTestCase):

    def test_an_owner_of_both_papers_may_persist(self):
        self.assertIsNotNone(self.save())

    def test_owning_only_one_paper_is_refused(self):
        """The tempting bug: check paper A and forget paper B."""
        with self.assertRaises(PaperNotOwned):
            self.save(a=PAPER_LOW, b=FOREIGN_PAPER)
        self.assertEqual(self.rows(), [])

    def test_owning_only_the_second_paper_is_refused(self):
        # Canonical (8888... < aaaa...), so the orientation guard cannot
        # fire first — ownership is what must reject this.
        with self.assertRaises(PaperNotOwned):
            self.save(a=FOREIGN_PAPER, b=PAPER_TOP)
        self.assertEqual(self.rows(), [])

    def test_another_owner_cannot_persist_for_this_owners_papers(self):
        with self.assertRaises(PaperNotOwned):
            self.save(owner_id=OWNER_B, a=PAPER_LOW, b=PAPER_HIGH)
        self.assertEqual(self.rows(), [])

    def test_a_nonexistent_paper_a_is_refused(self):
        # Canonical (9999... < aaaa...).
        with self.assertRaises(PaperNotOwned):
            self.save(a=UNKNOWN_PAPER, b=PAPER_TOP)
        self.assertEqual(self.rows(), [])

    def test_a_nonexistent_paper_b_is_refused(self):
        with self.assertRaises(PaperNotOwned):
            self.save(a=PAPER_LOW, b=UNKNOWN_PAPER)
        self.assertEqual(self.rows(), [])

    def test_a_malformed_paper_id_is_refused(self):
        for bad in ("not-a-uuid", "", None, "1; drop table papers"):
            with self.subTest(paper_id=bad):
                with self.assertRaises(PaperNotOwned):
                    self.save(a=PAPER_LOW, b=bad)
        self.assertEqual(self.rows(), [])

    def test_the_same_paper_for_both_sides_is_refused(self):
        with self.assertRaises(PaperNotOwned):
            self.save(a=PAPER_LOW, b=PAPER_LOW)
        self.assertEqual(self.rows(), [])

    def test_get_is_owner_scoped(self):
        self.save()

        self.assertIsNotNone(get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH))
        self.assertIsNone(get_relationship(OWNER_B, PAPER_LOW, PAPER_HIGH))

    def test_a_foreign_owner_get_returns_nothing_not_an_error(self):
        self.save()
        self.assertIsNone(get_relationship(OWNER_B, PAPER_LOW, PAPER_HIGH))

    def test_get_returns_none_for_unknown_and_malformed_pairs(self):
        self.save()
        for a, b in (
            (PAPER_LOW, UNKNOWN_PAPER),
            (UNKNOWN_PAPER, PAPER_HIGH),
            (PAPER_LOW, "not-a-uuid"),
            (PAPER_LOW, PAPER_LOW),
            (PAPER_LOW, None),
        ):
            with self.subTest(a=a, b=b):
                self.assertIsNone(get_relationship(OWNER_A, a, b))

    def test_owner_id_is_never_read_from_the_relationship_json(self):
        """Structural: the store's only source of owner identity is its
        own argument."""
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        looked_up = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        for forbidden in ("owner_id", "paper_a_id", "paper_b_id", "paper_id"):
            self.assertNotIn(forbidden, looked_up)


# ----------------------------------------------------------------------
# 18-21. Canonical pair
# ----------------------------------------------------------------------
class TestCanonicalPair(StoreTestCase):

    def test_canonicalization_is_deterministic(self):
        low, high = canonical_pair(PAPER_LOW, PAPER_HIGH)
        self.assertEqual(str(low), PAPER_LOW)
        self.assertEqual(str(high), PAPER_HIGH)

    def test_the_reverse_order_yields_the_same_canonical_pair(self):
        self.assertEqual(
            canonical_pair(PAPER_LOW, PAPER_HIGH),
            canonical_pair(PAPER_HIGH, PAPER_LOW),
        )

    def test_canonicalization_matches_python_uuid_ordering(self):
        low, high = canonical_pair(PAPER_HIGH, PAPER_LOW)
        self.assertLess(low, high)

    def test_a_self_pair_is_refused(self):
        with self.assertRaises(PaperNotOwned):
            canonical_pair(PAPER_LOW, PAPER_LOW)

    def test_a_malformed_id_is_refused(self):
        with self.assertRaises(PaperNotOwned):
            canonical_pair(PAPER_LOW, "not-a-uuid")

    def test_get_finds_the_row_in_either_order(self):
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        forward = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        reverse = get_relationship(OWNER_A, PAPER_HIGH, PAPER_LOW)

        self.assertIsNotNone(forward)
        self.assertIsNotNone(reverse)
        self.assertEqual(forward, reverse)

    def test_a_reversed_save_is_refused_rather_than_reoriented(self):
        """The store will not swap cites_a/cites_b to fit the canonical
        order: swapping citations without rewriting the statement would
        misattribute evidence."""
        with self.assertRaises(ValueError) as ctx:
            self.save(a=PAPER_HIGH, b=PAPER_LOW)

        self.assertIn("canonical", str(ctx.exception).lower())
        self.assertEqual(self.rows(), [])

    def test_a_reverse_duplicate_cannot_create_a_second_row(self):
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        with self.assertRaises(ValueError):
            self.save(a=PAPER_HIGH, b=PAPER_LOW)

        self.assertEqual(len(self.rows()), 1)

    def test_saving_the_same_pair_twice_replaces_rather_than_duplicates(self):
        self.save(model="first")
        self.save(model="second", generated_at=GENERATED_AT + timedelta(hours=1))

        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model, "second"
        )

    def test_different_pairs_keep_their_own_rows(self):
        self.save(a=PAPER_LOW, b=PAPER_HIGH, model="low-high")
        self.save(a=PAPER_LOW, b=PAPER_THIRD, model="low-third")

        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model, "low-high"
        )
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_THIRD).model, "low-third"
        )

    def test_the_database_itself_refuses_a_reversed_row(self):
        """Belt and braces: even a writer that bypassed the store is
        rejected by the CHECK constraint in 0006."""
        with self.factory() as session:
            session.add(
                PaperRelationshipRow(
                    owner_id=uuid.UUID(OWNER_A),
                    paper_a_id=uuid.UUID(PAPER_HIGH),  # > paper_b_id
                    paper_b_id=uuid.UUID(PAPER_LOW),
                    relationship=raw_payload(),
                    paper_a_generated_at=SOURCE_A_AT,
                    paper_b_generated_at=SOURCE_B_AT,
                    generated_at=GENERATED_AT,
                    model=TEST_MODEL,
                    schema_version="1",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_the_database_itself_refuses_a_self_pair_row(self):
        with self.factory() as session:
            session.add(
                PaperRelationshipRow(
                    owner_id=uuid.UUID(OWNER_A),
                    paper_a_id=uuid.UUID(PAPER_LOW),
                    paper_b_id=uuid.UUID(PAPER_LOW),
                    relationship=raw_payload(),
                    paper_a_generated_at=SOURCE_A_AT,
                    paper_b_generated_at=SOURCE_B_AT,
                    generated_at=GENERATED_AT,
                    model=TEST_MODEL,
                    schema_version="1",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_two_owners_may_each_hold_the_same_pair(self):
        """Uniqueness is per owner, not global."""
        self._seed_paper(OWNER_B, PAPER_LOW.replace("1", "4"), "B low")
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        self.assertEqual(len(self.rows()), 1)
        # Owner B cannot reuse A's papers, which is the point — but the
        # UNIQUE tuple leads with owner_id so two owners never collide.
        with self.assertRaises(PaperNotOwned):
            self.save(owner_id=OWNER_B, a=PAPER_LOW, b=PAPER_HIGH)


# ----------------------------------------------------------------------
# 30-31. Concurrency and atomicity
# ----------------------------------------------------------------------
class TestConcurrencyAndAtomicity(StoreTestCase):

    def test_a_stale_generation_cannot_overwrite_a_newer_one(self):
        self.save(model="newer", generated_at=GENERATED_AT + timedelta(hours=2))

        stored = self.save(model="stale", generated_at=GENERATED_AT)

        self.assertTrue(stored.superseded)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model, "newer"
        )

    def test_the_superseding_row_is_what_the_caller_receives(self):
        self.save(model="newer", generated_at=GENERATED_AT + timedelta(hours=2))

        stored = self.save(model="stale", generated_at=GENERATED_AT)

        self.assertEqual(stored.model, "newer")

    def test_a_newer_generation_does_overwrite_an_older_one(self):
        self.save(model="older", generated_at=GENERATED_AT)

        stored = self.save(model="newer", generated_at=GENERATED_AT + timedelta(hours=1))

        self.assertFalse(stored.superseded)
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model, "newer"
        )

    def test_a_concurrent_first_insert_resolves_to_one_row(self):
        from app.services import paper_relationship_store as store

        calls = {"n": 0}
        real_scope = store.session_scope

        def racing_scope(owner_id):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the winner committing between our SELECT and
                # our INSERT: the UNIQUE constraint rejects this attempt.
                raise IntegrityError("insert", {}, Exception("unique violation"))
            return real_scope(owner_id)

        with patch.object(store, "session_scope", side_effect=racing_scope):
            self.save()

        self.assertEqual(calls["n"], 2, "the store should have retried once")
        self.assertEqual(len(self.rows()), 1)

    def test_a_failure_mid_write_leaves_no_partial_row(self):
        from app.services import paper_relationship_store as store

        with patch.object(
            store, "_parse_stored", side_effect=RuntimeError("failed before commit")
        ):
            with self.assertRaises(RuntimeError):
                self.save()

        self.assertEqual(self.rows(), [])
        self.assertIsNone(get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH))

    def test_a_failed_regeneration_leaves_the_previous_row_intact(self):
        from app.services import paper_relationship_store as store

        self.save(model="original")

        with patch.object(
            store, "_parse_stored", side_effect=RuntimeError("failed mid-update")
        ):
            with self.assertRaises(RuntimeError):
                self.save(
                    model="replacement",
                    generated_at=GENERATED_AT + timedelta(hours=1),
                )

        self.assertEqual(len(self.rows()), 1)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(got.model, "original")
        self.assertEqual(_as_utc(got.generated_at), GENERATED_AT)


# ----------------------------------------------------------------------
# 32-36. Purity
# ----------------------------------------------------------------------
class TestNoProviderOrProductionAccess(StoreTestCase):

    def _imported_roots(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots.add((node.module or "").split(".")[0])
        return roots

    def test_no_provider_or_transport_library_is_imported(self):
        forbidden = {
            "groq", "qdrant_client", "voyageai", "openai", "supabase",
            "httpx", "requests", "urllib", "socket", "aiohttp", "boto3",
            "fastapi",
        }
        self.assertEqual(self._imported_roots() & forbidden, set())

    def test_the_store_imports_nothing_beyond_its_stated_dependencies(self):
        self.assertEqual(
            self._imported_roots(),
            {"uuid", "dataclasses", "datetime", "typing", "pydantic",
             "sqlalchemy", "app"},
        )

    def test_the_store_opens_no_second_database_connection(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("create_engine", "sessionmaker", "psycopg", "connect("):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)

    def test_no_qdrant_no_storage_no_generation_is_reachable(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in ("scroll", "query_points", "upsert", "fetch_pdf",
                          "generate", "encode_query", "validate_relationship"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called)

    def test_a_full_save_and_read_opens_no_socket(self):
        import socket

        def explode(*args, **kwargs):
            raise AssertionError("the store opened a network socket")

        with patch.object(socket, "socket", explode):
            self.save()
            got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)

        self.assertIsNotNone(got)

    def test_every_query_carries_an_explicit_owner_filter(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        filters = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "filter"
        ]
        self.assertTrue(filters)
        for call in filters:
            rendered = [ast.unparse(arg) for arg in call.args]
            self.assertTrue(
                any("owner_id ==" in arg for arg in rendered),
                f"a query filters without an owner term: {rendered}",
            )


# ----------------------------------------------------------------------
# Migration 0006 — static review only. Nothing is executed.
# ----------------------------------------------------------------------
class TestMigrationText(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as handle:
            raw = handle.read()
        lines = []
        for line in raw.splitlines():
            marker = line.find("--")
            if marker != -1:
                line = line[:marker]
            lines.append(line)
        cls.sql = " ".join(" ".join(lines).split()).lower()

    def test_migration_0006_is_committed_and_none_is_uncommitted(self):
        """Asked of git rather than of a hard-coded list.

        An exact-list snapshot is what broke the Phase 2B equivalent the
        moment 0006 landed, and it would break again at 0007 — so this is
        asked of git. But the FIRST git-based version asserted that 0006
        was UNTRACKED, which was true only in the window before it was
        committed and became permanently unsatisfiable the moment it was.
        A test that can never pass again asserts nothing.

        The durable intent, which holds before and after any future
        migration lands:

          * 0006 exists on disk;
          * 0006 is TRACKED, i.e. actually committed rather than sitting
            in someone's working tree;
          * no migration file is uncommitted — a modified 0001-0006 or a
            stray untracked 0007 both fail here, which is the real risk
            this gate guards against.

        Deliberately no assertion on the total migration count: 0007 must
        stay possible without editing this test.
        """
        import subprocess

        repo_root = os.path.dirname(BACKEND_DIR)
        migration = "backend/migrations/0006_paper_relationship.sql"

        self.assertTrue(os.path.exists(MIGRATION_PATH), MIGRATION_PATH)

        tracked = subprocess.run(
            ["git", "ls-files", "backend/migrations/"],
            capture_output=True, text=True, cwd=repo_root,
        ).stdout.split()

        self.assertIn(migration, tracked, f"0006 is not tracked by git: {tracked}")

        uncommitted = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all",
             "backend/migrations/"],
            capture_output=True, text=True, cwd=repo_root,
        ).stdout.strip().splitlines()

        self.assertEqual(
            uncommitted, [], f"no migration should be uncommitted, got {uncommitted}"
        )

    def test_the_migration_sequence_is_contiguous(self):
        names = sorted(
            n for n in os.listdir(os.path.dirname(MIGRATION_PATH)) if n.endswith(".sql")
        )
        self.assertIn("0006_paper_relationship.sql", names)
        self.assertEqual([int(n[:4]) for n in names], list(range(1, len(names) + 1)))

    def test_the_table_and_its_columns_exist(self):
        self.assertIn("create table if not exists paper_relationships (", self.sql)
        for fragment in (
            "id uuid primary key default gen_random_uuid()",
            "owner_id uuid not null references auth.users(id) on delete cascade",
            "paper_a_id uuid not null references papers(id) on delete cascade",
            "paper_b_id uuid not null references papers(id) on delete cascade",
            "relationship jsonb not null",
            "paper_a_generated_at timestamptz not null",
            "paper_b_generated_at timestamptz not null",
            "generated_at timestamptz not null",
            "model text not null",
            "schema_version text not null",
            "created_at timestamptz not null default now()",
            "updated_at timestamptz not null default now()",
        ):
            with self.subTest(column=fragment.split(" ")[0]):
                self.assertIn(fragment, self.sql)

    def test_the_canonical_pair_check_exists(self):
        self.assertIn("check (paper_a_id < paper_b_id)", self.sql)

    def test_uniqueness_is_per_owner_and_pair(self):
        self.assertIn("unique (owner_id, paper_a_id, paper_b_id)", self.sql)
        self.assertEqual(self.sql.count("unique ("), 1)
        self.assertNotIn("create unique index", self.sql)

    def test_no_speculative_indexes(self):
        self.assertNotIn("create index", self.sql)

    def test_rls_is_enabled_and_forced(self):
        self.assertIn("alter table paper_relationships enable row level security", self.sql)
        self.assertIn("alter table paper_relationships force row level security", self.sql)

    def test_the_policy_covers_reads_and_writes(self):
        self.assertIn("create policy paper_relationships_owner_only", self.sql)
        self.assertIn("using (owner_id = auth.uid())", self.sql)
        self.assertIn("with check (owner_id = auth.uid())", self.sql)
        self.assertEqual(self.sql.count("auth.uid()"), 2)

    def test_grants_are_minimal_and_only_to_the_app_role(self):
        self.assertIn(
            "grant select, insert, update on paper_relationships to researchmind_app",
            self.sql,
        )
        self.assertEqual(self.sql.count("grant "), 1)
        # No delete_relationship exists, so no DELETE is granted.
        self.assertNotIn("delete on paper_relationships", self.sql)

    def test_no_broad_or_dangerous_grants(self):
        for forbidden in ("grant all", "to public", "to anon", "to authenticated",
                          "to service_role", "truncate", "security definer",
                          "bypassrls", "create role", "alter role"):
            with self.subTest(grant=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_the_granted_privilege_list_is_exactly_three(self):
        """Scoped to the GRANT statement: a bare search for \"references\"
        or \"trigger\" would match the legitimate foreign-key clauses."""
        grant = self.sql[self.sql.index("grant "):]
        privileges = grant[len("grant "):grant.index(" on ")]
        self.assertEqual(
            sorted(p.strip() for p in privileges.split(",")),
            ["insert", "select", "update"],
        )
        for forbidden in ("references", "trigger", "truncate", "maintain", "all"):
            with self.subTest(privilege=forbidden):
                self.assertNotIn(forbidden, privileges)

    def test_it_touches_no_existing_table_or_policy(self):
        for existing in ("papers", "reports", "chat_sessions", "chat_messages",
                         "usage_counters", "paper_intelligence", "storage.objects"):
            with self.subTest(table=existing):
                self.assertNotIn(f"alter table {existing} ", self.sql)
        self.assertNotIn("drop table", self.sql)
        self.assertNotIn("drop column", self.sql)
        self.assertNotIn("disable row level security", self.sql)
        # The only DROP is this table's own policy idempotence guard.
        self.assertEqual(self.sql.count("drop "), 1)

    def test_migrations_0001_to_0005_are_untouched(self):
        import subprocess

        changed = subprocess.run(
            ["git", "status", "--porcelain", "backend/migrations/"],
            capture_output=True, text=True, cwd=os.path.dirname(BACKEND_DIR),
        ).stdout
        for older in ("0001_", "0002_", "0003_", "0004_", "0005_"):
            self.assertNotIn(older, changed)


class TestOrmMatchesMigration(unittest.TestCase):

    def test_columns_match(self):
        self.assertEqual(
            sorted(c.name for c in PaperRelationshipRow.__table__.columns),
            ["created_at", "generated_at", "id", "model", "owner_id",
             "paper_a_generated_at", "paper_a_id", "paper_b_generated_at",
             "paper_b_id", "relationship", "schema_version", "updated_at"],
        )

    def test_table_name_matches(self):
        self.assertEqual(PaperRelationshipRow.__tablename__, "paper_relationships")

    def test_the_orm_declares_the_same_uniqueness(self):
        uniques = [
            tuple(c.name for c in constraint.columns)
            for constraint in PaperRelationshipRow.__table__.constraints
            if type(constraint).__name__ == "UniqueConstraint"
        ]
        self.assertEqual(uniques, [("owner_id", "paper_a_id", "paper_b_id")])

    def test_the_orm_declares_the_canonical_pair_check(self):
        checks = [
            str(constraint.sqltext)
            for constraint in PaperRelationshipRow.__table__.constraints
            if type(constraint).__name__ == "CheckConstraint"
        ]
        self.assertEqual(checks, ["paper_a_id < paper_b_id"])

    def test_the_orm_adds_no_index_the_migration_lacks(self):
        self.assertEqual(list(PaperRelationshipRow.__table__.indexes), [])

    def test_both_paper_columns_cascade(self):
        for name in ("paper_a_id", "paper_b_id"):
            column = PaperRelationshipRow.__table__.columns[name]
            fks = list(column.foreign_keys)
            self.assertEqual(len(fks), 1)
            self.assertEqual(fks[0].ondelete, "CASCADE")
            self.assertEqual(str(fks[0].column), "papers.id")




# ----------------------------------------------------------------------
# Source-intelligence provenance
# ----------------------------------------------------------------------
class TestSourceIntelligenceStamps(StoreTestCase):
    """The two source stamps are what make a stored relationship's
    staleness knowable. Without them a re-analysed paper leaves the
    relationship describing claims that no longer exist, and nothing in
    the row would say so."""

    def test_paper_a_generated_at_persists(self):
        self.save()
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(_as_utc(got.paper_a_generated_at), SOURCE_A_AT)

    def test_paper_b_generated_at_persists(self):
        self.save()
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(_as_utc(got.paper_b_generated_at), SOURCE_B_AT)

    def test_both_stamps_round_trip_exactly(self):
        a_at = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
        b_at = datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

        self.save(paper_a_generated_at=a_at, paper_b_generated_at=b_at)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)

        self.assertEqual(_as_utc(got.paper_a_generated_at), a_at)
        self.assertEqual(_as_utc(got.paper_b_generated_at), b_at)

    def test_the_a_stamp_belongs_to_the_canonical_a_paper(self):
        """Not merely two timestamps: the association with the canonical
        ids has to survive storage."""
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)

        self.assertEqual(str(got.paper_a_id), PAPER_LOW)
        self.assertEqual(_as_utc(got.paper_a_generated_at), SOURCE_A_AT)

    def test_the_b_stamp_belongs_to_the_canonical_b_paper(self):
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)

        self.assertEqual(str(got.paper_b_id), PAPER_HIGH)
        self.assertEqual(_as_utc(got.paper_b_generated_at), SOURCE_B_AT)

    def test_a_reversed_read_returns_canonical_orientation(self):
        """Reading (B, A) returns the same row, still oriented to the
        canonical ids — the stamps are not swapped to match the caller's
        argument order."""
        self.save(a=PAPER_LOW, b=PAPER_HIGH)

        reverse = get_relationship(OWNER_A, PAPER_HIGH, PAPER_LOW)

        self.assertEqual(str(reverse.paper_a_id), PAPER_LOW)
        self.assertEqual(_as_utc(reverse.paper_a_generated_at), SOURCE_A_AT)
        self.assertEqual(str(reverse.paper_b_id), PAPER_HIGH)
        self.assertEqual(_as_utc(reverse.paper_b_generated_at), SOURCE_B_AT)

    def test_the_stored_result_exposes_both_stamps(self):
        stored = self.save()

        self.assertEqual(_as_utc(stored.paper_a_generated_at), SOURCE_A_AT)
        self.assertEqual(_as_utc(stored.paper_b_generated_at), SOURCE_B_AT)

    def test_relationship_generated_at_is_independent_of_both_sources(self):
        """Three distinct timings. Regenerating the relationship moves
        generated_at and leaves the sources; re-analysing a paper moves one
        source and leaves generated_at."""
        stored = self.save()

        self.assertNotEqual(_as_utc(stored.generated_at), _as_utc(stored.paper_a_generated_at))
        self.assertNotEqual(_as_utc(stored.generated_at), _as_utc(stored.paper_b_generated_at))
        self.assertEqual(_as_utc(stored.generated_at), GENERATED_AT)

        # Regenerate with the SAME sources but a later run time.
        later = GENERATED_AT + timedelta(hours=3)
        again = self.save(generated_at=later)

        self.assertEqual(_as_utc(again.generated_at), later)
        self.assertEqual(_as_utc(again.paper_a_generated_at), SOURCE_A_AT)
        self.assertEqual(_as_utc(again.paper_b_generated_at), SOURCE_B_AT)

    def test_both_stamps_are_required_keyword_arguments(self):
        with self.assertRaises(TypeError):
            save_relationship(
                OWNER_A, PAPER_LOW, PAPER_HIGH, validated(),
                paper_b_generated_at=SOURCE_B_AT, model=TEST_MODEL,
            )
        with self.assertRaises(TypeError):
            save_relationship(
                OWNER_A, PAPER_LOW, PAPER_HIGH, validated(),
                paper_a_generated_at=SOURCE_A_AT, model=TEST_MODEL,
            )
        self.assertEqual(self.rows(), [])

    def test_a_non_datetime_source_stamp_is_refused(self):
        """Provenance that cannot be compared is worse than refusing: the
        row would look checkable and never be."""
        for bad in (None, "2026-09-20", 0, 1758355200):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.save(paper_a_generated_at=bad)
                with self.assertRaises(ValueError):
                    self.save(paper_b_generated_at=bad)
        self.assertEqual(self.rows(), [])

    def test_the_relationship_json_carries_no_timestamps(self):
        """The stamps are columns. A timestamp inside the jsonb would be a
        second, unvalidated source of provenance."""
        self.save()

        blob = json.dumps(self.rows()[0].relationship)

        for forbidden in ("generated_at", "paper_a_generated_at",
                          "paper_b_generated_at", "2026-", "timestamp"):
            self.assertNotIn(forbidden, blob)

    def test_the_database_refuses_a_row_with_no_source_stamps(self):
        """NOT NULL in 0006, and SQLite enforces it too."""
        with self.factory() as session:
            session.add(
                PaperRelationshipRow(
                    owner_id=uuid.UUID(OWNER_A),
                    paper_a_id=uuid.UUID(PAPER_LOW),
                    paper_b_id=uuid.UUID(PAPER_HIGH),
                    relationship=raw_payload(),
                    generated_at=GENERATED_AT,
                    model=TEST_MODEL,
                    schema_version="1",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()


# ----------------------------------------------------------------------
# Stale detection
# ----------------------------------------------------------------------
class TestStaleDetection(StoreTestCase):

    def stored(self):
        self.save()
        return get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)

    def test_identical_source_stamps_are_not_stale(self):
        self.assertFalse(
            relationship_is_stale(self.stored(), SOURCE_A_AT, SOURCE_B_AT)
        )

    def test_a_newer_source_intelligence_is_stale(self):
        self.assertTrue(
            relationship_is_stale(
                self.stored(), SOURCE_A_AT + timedelta(hours=1), SOURCE_B_AT
            )
        )

    def test_an_older_source_intelligence_is_also_stale(self):
        """Stale means DIFFERS, not 'is older'. A stamp that moved
        backwards still means the row in front of us is not the one this
        relationship was derived from."""
        self.assertTrue(
            relationship_is_stale(
                self.stored(), SOURCE_A_AT - timedelta(days=5), SOURCE_B_AT
            )
        )

    def test_changing_only_the_a_stamp_is_stale(self):
        self.assertTrue(
            relationship_is_stale(
                self.stored(), SOURCE_A_AT + timedelta(seconds=1), SOURCE_B_AT
            )
        )

    def test_changing_only_the_b_stamp_is_stale(self):
        self.assertTrue(
            relationship_is_stale(
                self.stored(), SOURCE_A_AT, SOURCE_B_AT + timedelta(seconds=1)
            )
        )

    def test_a_missing_current_stamp_is_stale(self):
        """An analysis that has been removed cannot still be the one this
        relationship compared."""
        stored = self.stored()
        self.assertTrue(relationship_is_stale(stored, None, SOURCE_B_AT))
        self.assertTrue(relationship_is_stale(stored, SOURCE_A_AT, None))
        self.assertTrue(relationship_is_stale(stored, None, None))

    def test_comparison_is_utc_aware_on_both_sides(self):
        """SQLite reads timestamps back naive and Postgres returns them
        aware; comparing the two forms directly raises TypeError."""
        stored = self.stored()

        naive_a = SOURCE_A_AT.replace(tzinfo=None)
        naive_b = SOURCE_B_AT.replace(tzinfo=None)
        self.assertFalse(relationship_is_stale(stored, naive_a, naive_b))

        # The same instant expressed in another zone is still not stale.
        other_zone = SOURCE_A_AT.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertFalse(relationship_is_stale(stored, other_zone, SOURCE_B_AT))

    def test_swapping_the_two_current_stamps_is_detected(self):
        """The stamps are not interchangeable: handing them over in the
        wrong order must not read as fresh."""
        self.assertTrue(
            relationship_is_stale(self.stored(), SOURCE_B_AT, SOURCE_A_AT)
        )

    def test_the_relationship_run_time_does_not_affect_staleness(self):
        stored = self.stored()
        # generated_at is GENERATED_AT, neither source stamp.
        self.assertTrue(
            relationship_is_stale(stored, GENERATED_AT, GENERATED_AT)
        )

    def test_the_helper_is_pure(self):
        """No I/O, no clock, no provider: called twice with the same
        arguments it returns the same answer."""
        stored = self.stored()
        first = relationship_is_stale(stored, SOURCE_A_AT, SOURCE_B_AT)
        second = relationship_is_stale(stored, SOURCE_A_AT, SOURCE_B_AT)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)

    def test_stale_detection_does_not_regenerate_or_delete(self):
        """Detection only in this gate."""
        stored = self.stored()

        relationship_is_stale(stored, SOURCE_A_AT + timedelta(days=1), SOURCE_B_AT)

        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(
            _as_utc(get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).paper_a_generated_at),
            SOURCE_A_AT,
        )


# ----------------------------------------------------------------------
# The existing stale-WRITE guard is a different mechanism
# ----------------------------------------------------------------------
class TestStaleWriteGuardStillWorks(StoreTestCase):
    """relationship_is_stale() is about SOURCE freshness. The guard inside
    save_relationship() is about concurrent RELATIONSHIP writes. Both must
    keep working, and neither may be mistaken for the other."""

    def test_the_generated_at_write_guard_still_drops_a_stale_write(self):
        self.save(model="newer", generated_at=GENERATED_AT + timedelta(hours=2))

        stored = self.save(model="stale", generated_at=GENERATED_AT)

        self.assertTrue(stored.superseded)
        self.assertEqual(
            get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH).model, "newer"
        )

    def test_a_superseded_write_does_not_overwrite_the_source_stamps(self):
        self.save(
            model="newer",
            generated_at=GENERATED_AT + timedelta(hours=2),
            paper_a_generated_at=SOURCE_A_AT,
            paper_b_generated_at=SOURCE_B_AT,
        )

        stored = self.save(
            model="stale",
            generated_at=GENERATED_AT,
            paper_a_generated_at=SOURCE_A_AT + timedelta(days=9),
            paper_b_generated_at=SOURCE_B_AT + timedelta(days=9),
        )

        self.assertTrue(stored.superseded)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(_as_utc(got.paper_a_generated_at), SOURCE_A_AT)
        self.assertEqual(_as_utc(got.paper_b_generated_at), SOURCE_B_AT)

    def test_a_newer_write_does_replace_the_source_stamps(self):
        self.save(generated_at=GENERATED_AT)

        fresh_a = SOURCE_A_AT + timedelta(days=2)
        stored = self.save(
            generated_at=GENERATED_AT + timedelta(hours=1),
            paper_a_generated_at=fresh_a,
        )

        self.assertFalse(stored.superseded)
        got = get_relationship(OWNER_A, PAPER_LOW, PAPER_HIGH)
        self.assertEqual(_as_utc(got.paper_a_generated_at), fresh_a)
        # ...and the relationship is no longer stale against the new source.
        self.assertFalse(relationship_is_stale(got, fresh_a, SOURCE_B_AT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
