"""The store, one call at a time.

These sit below HTTP because what they pin is not a request shape: cursor paging, the
ranking blend, the sweep that finally erases, and the several ways a correction can be
refused. Driving them through fifteen requests would test the router as much as the store
and say less about either.

The account id is passed explicitly here, which is exactly what the HTTP layer never does.
That is the seam being tested: the store will happily answer for any account, and it is the
absence of any way to *say* an account over HTTP that keeps one person's memories from
another's.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from memory_api.domain.errors import ConflictError, MemoryFault, NotFoundError
from memory_api.domain.models import BlockInput, Decision, MemoryInput, Selection
from memory_api.store.sql import DATABASE_FILE_MODE, SQLStore

if TYPE_CHECKING:
    from conftest import FakeClock

ACCOUNT = "acct_example"
OTHER = "acct_someone_else"
AUTHOR = "acct_example"


def remember(store: SQLStore, **fields: object) -> str:
    """Write a memory and return its id, so a test reads as what it is checking."""
    request = MemoryInput.model_validate({"title": "Home city", **fields})
    return store.write(ACCOUNT, AUTHOR, request).id


class TestOpeningAnExistingDatabase:
    def test_a_database_written_by_an_earlier_release_gains_the_columns_it_lacks(
        self, tmp_path_factory: pytest.TempPathFactory, clock: FakeClock
    ) -> None:
        # `CREATE TABLE IF NOT EXISTS` does nothing to a table that exists, so a column
        # added later would never appear on an upgraded install.
        path = str(tmp_path_factory.mktemp("upgrade") / "memory.db")
        first = SQLStore(path, clock)
        remember(first, body="Lived in London")
        first.close()

        second = SQLStore(path, clock)
        try:
            # Re-opening must be a no-op the second time, not an attempt to add the column
            # again -- which would fail and take the process down on every restart.
            assert second.listing(ACCOUNT, Selection()).data[0].body == "Lived in London"
        finally:
            second.close()

    def test_memories_written_before_topics_existed_are_given_one(
        self, tmp_path_factory: pytest.TempPathFactory, clock: FakeClock
    ) -> None:
        """Otherwise the index is empty on every upgraded install, and stays empty.

        The index joins topics to memories, so rows whose `topic_id` is still null simply
        do not appear -- and would not until every one of them happened to be rewritten.
        What the person reads is an assistant that has forgotten everything about them.
        """
        path = str(tmp_path_factory.mktemp("backfill") / "memory.db")
        first = SQLStore(path, clock)
        remembered = remember(first, body="Lived in London")
        # Put the database back the way a release without the topic layer left it.
        first.db.execute("UPDATE memories SET topic_id=NULL")
        first.db.execute("DELETE FROM topics")
        first.db.commit()
        assert first.topics(ACCOUNT).total == 0
        first.close()

        second = SQLStore(path, clock)
        try:
            index = second.topics(ACCOUNT)
            assert index.total == 1
            assert index.data[0].memory_count == 1
            assert second.get(ACCOUNT, remembered).topic_id == index.data[0].id
        finally:
            second.close()

    def test_the_backfill_does_not_run_again_once_every_memory_has_a_topic(
        self, tmp_path_factory: pytest.TempPathFactory, clock: FakeClock
    ) -> None:
        path = str(tmp_path_factory.mktemp("backfill-once") / "memory.db")
        first = SQLStore(path, clock)
        remember(first, body="Lived in London")
        before = first.get(ACCOUNT, first.listing(ACCOUNT, Selection()).data[0].id).revision
        first.close()

        second = SQLStore(path, clock)
        try:
            after = second.listing(ACCOUNT, Selection()).data[0].revision
            assert after == before, "a restart must not rewrite every memory it finds"
        finally:
            second.close()


class TestWritingTheSameThingTwice:
    def test_an_identical_memory_returns_the_one_that_already_exists(self, store: SQLStore) -> None:
        # Extraction passes re-derive the same fact constantly. Storing each one would bury
        # the useful memories under copies of themselves.
        first = remember(store, body="Lived in London")
        assert remember(store, body="Lived in London") == first

    def test_a_memory_that_differs_only_in_trust_is_a_different_memory(
        self, store: SQLStore
    ) -> None:
        # Otherwise an untrusted claim from a web page would be silently deduplicated into
        # the person's own stated fact and inherit its trust.
        first = remember(store, body="Lived in London")
        assert remember(store, body="Lived in London", trust="untrusted") != first

    def test_a_forgotten_memory_does_not_absorb_a_later_identical_write(
        self, store: SQLStore
    ) -> None:
        first = remember(store, body="Lived in London")
        store.transition(ACCOUNT, first, "forget")
        assert remember(store, body="Lived in London") != first

    def test_a_repeated_write_carrying_a_revised_assessment_keeps_the_revision(
        self, store: SQLStore
    ) -> None:
        """The claim is the same; what the caller thinks about it is not.

        Returning the stored row unchanged threw the revision away silently, so a caller
        setting an expiry on something it had written before got a memory that never
        expires and no indication of that.
        """
        first = remember(store, body="Lived in London", importance=2)
        again = remember(store, body="Lived in London", importance=9, expires_at=1_500_000.0)

        assert again == first, "still one memory, not a near-duplicate"
        stored = store.get(ACCOUNT, first)
        assert stored.importance == 9
        assert stored.expires_at == 1_500_000.0
        assert stored.revision == 2

    def test_a_repeated_write_that_revises_nothing_leaves_the_memory_alone(
        self, store: SQLStore
    ) -> None:
        first = remember(store, body="Lived in London", importance=2)
        before = store.get(ACCOUNT, first)
        remember(store, body="Lived in London", importance=2)
        after = store.get(ACCOUNT, first)

        assert (after.revision, after.updated_at) == (before.revision, before.updated_at)


class TestCorrections:
    def test_a_correction_retires_what_it_replaces_and_links_the_two(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        old = remember(store, body="Lived in London")
        clock.advance(60)
        new = store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Bristol"), old)
        retired = store.get(ACCOUNT, old)
        assert retired.superseded_by_id == new.id
        assert retired.valid_to == new.valid_from
        assert new.supersedes_id == old

    def test_a_memory_that_was_already_corrected_cannot_be_corrected_again(
        self, store: SQLStore
    ) -> None:
        # The chain has one head. Correcting the middle of it would produce two current
        # answers to the same question and no way to tell which is meant.
        old = remember(store, body="Lived in London")
        store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Bristol"), old)
        with pytest.raises(ConflictError, match="current, remembered"):
            store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Leeds"), old)

    def test_a_forgotten_memory_cannot_be_corrected(self, store: SQLStore) -> None:
        old = remember(store, body="Lived in London")
        store.transition(ACCOUNT, old, "forget")
        with pytest.raises(ConflictError, match="current, remembered"):
            store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Bristol"), old)

    def test_a_correction_cannot_move_a_memory_into_another_scope(self, store: SQLStore) -> None:
        # A correction that changed scope would quietly move a fact out of the profile or
        # session that could see it, which reads to the person as the memory disappearing.
        old = remember(store, body="Lived in London")
        replacement = MemoryInput(title="Home city", body="Bristol", scope="profile", profile="w")
        with pytest.raises(ConflictError, match="preserve the original memory scope"):
            store.write(ACCOUNT, AUTHOR, replacement, old)

    def test_a_correction_cannot_start_before_the_memory_it_replaces(self, store: SQLStore) -> None:
        # Otherwise the two would overlap and `as_of` would have two answers for one instant.
        old = remember(store, body="Lived in London", valid_from=500.0)
        replacement = MemoryInput(title="Home city", body="Bristol", valid_from=100.0)
        with pytest.raises(ConflictError, match="cannot start before"):
            store.write(ACCOUNT, AUTHOR, replacement, old)

    def test_correcting_a_memory_that_is_not_yours_is_not_found(self, store: SQLStore) -> None:
        mine = remember(store, body="Lived in London")
        with pytest.raises(NotFoundError):
            store.write(OTHER, OTHER, MemoryInput(title="Home city", body="Bristol"), mine)


class TestTransitions:
    def test_forgetting_twice_keeps_the_first_moment_it_was_forgotten(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        # The grace period is measured from that moment. Re-stamping it on every retry
        # would let a client keep something alive forever without meaning to.
        memory_id = remember(store)
        first = store.transition(ACCOUNT, memory_id, "forget").forgotten_at
        clock.advance(600)
        assert store.transition(ACCOUNT, memory_id, "forget").forgotten_at == first

    def test_restoring_brings_a_forgotten_memory_back(self, store: SQLStore) -> None:
        memory_id = remember(store)
        store.transition(ACCOUNT, memory_id, "forget")
        assert store.transition(ACCOUNT, memory_id, "restore").forgotten_at is None

    def test_confirming_records_the_vouching_without_rewriting_where_it_came_from(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        """Confirmation is somebody saying "yes, that is right". It is not a new origin.

        Overwriting `trust` here used to relabel a memory scraped from a page as something
        the person had stated, which is the one thing this service's provenance split
        exists to prevent. Retrieval gates on `confirmed_at` instead, so the memory becomes
        usable and still remembers what it was.
        """
        memory_id = remember(store, trust="untrusted")
        confirmed = store.transition(ACCOUNT, memory_id, "confirm")
        assert confirmed.confirmed_at == clock.now
        assert confirmed.trust == "untrusted", "where it came from has not changed"

    def test_confirming_an_inferred_memory_leaves_it_inferred(self, store: SQLStore) -> None:
        memory_id = remember(store, trust="inferred")
        assert store.transition(ACCOUNT, memory_id, "confirm").trust == "inferred"


class TestWhatRetrievalIsFor:
    def test_a_summary_crosses_sessions_because_it_is_semantic_memory(
        self, store: SQLStore
    ) -> None:
        """A summary is what a consolidation pass produces. Excluding it wasted the pass.

        The filter admitted only facts and procedures, so the distilled form of everything
        an assistant had learned was the one kind it could never retrieve.
        """
        remember(store, title="What matters to them", body="Small venues", kind="summary")
        found = store.listing(ACCOUNT, Selection(q="venues"), retrieval=True)
        assert [memory.kind for memory in found.data] == ["summary"]

    def test_an_episode_from_elsewhere_does_not_cross_sessions(self, store: SQLStore) -> None:
        remember(store, title="What we discussed", body="Small venues", kind="episode")
        assert store.listing(ACCOUNT, Selection(q="venues"), retrieval=True).data == []

    def test_recency_halves_at_the_half_life_the_constant_names(self, store: SQLStore) -> None:
        """`exp(-dt/tau)` would halve at about twenty-one days, not the thirty it claims.

        The ranking blend normalises its terms, so this checks the term itself rather than
        an ordering: a name that lies about its own arithmetic is the kind of thing nobody
        notices until they are tuning it.
        """
        import math

        from memory_api.store.sql import HALF_LIFE_SECONDS

        weight = math.exp(-math.log(2) * HALF_LIFE_SECONDS / HALF_LIFE_SECONDS)
        assert weight == pytest.approx(0.5)

    def test_paging_a_ranked_answer_is_refused_rather_than_quietly_wrong(
        self, store: SQLStore
    ) -> None:
        """Cursors bound by write order; retrieval presents by rank.

        Allowing both would page through one ordering while showing another, and a caller
        would believe it had seen everything above a threshold when it had seen everything
        written after a row.
        """
        first = remember(store, body="Lived in London")
        with pytest.raises(MemoryFault, match="write order"):
            store.listing(ACCOUNT, Selection(after=first), retrieval=True)

    def test_the_listing_view_still_pages(self, store: SQLStore) -> None:
        first = remember(store, body="Lived in London")
        remember(store, title="Tea", body="Earl Grey")
        assert len(store.listing(ACCOUNT, Selection(after=first)).data) == 1


class TestBatches:
    def test_every_decision_produces_a_row_in_the_order_it_was_asked_for(
        self, store: SQLStore
    ) -> None:
        # Including the NOOPs: the result is a complete account of what the pass considered,
        # not only of what it changed.
        existing = remember(store, body="Lived in London")
        doomed = remember(store, title="Old note", body="Delete me")
        decisions = [
            Decision(action="ADD", memory=MemoryInput(title="Favourite tea", body="Earl Grey")),
            Decision(
                action="UPDATE",
                memory_id=existing,
                memory=MemoryInput(title="Home city", body="Bristol"),
            ),
            Decision(action="DELETE", memory_id=doomed),
            Decision(action="NOOP", memory_id=existing),
        ]
        results = store.batch(ACCOUNT, AUTHOR, decisions)
        assert [memory.body for memory in results] == [
            "Earl Grey",
            "Bristol",
            "Delete me",
            "Lived in London",
        ]
        assert results[2].forgotten_at is not None

    def test_a_batch_that_fails_halfway_leaves_nothing_behind(self, store: SQLStore) -> None:
        # The whole reason reconciliation is one call: a correction stored without its
        # predecessor being retired is two current answers to the same question.
        before = len(store.listing(ACCOUNT, Selection()).data)
        decisions = [
            Decision(action="ADD", memory=MemoryInput(title="Favourite tea", body="Earl Grey")),
            Decision(action="NOOP", memory_id="mem_does_not_exist"),
        ]
        with pytest.raises(NotFoundError):
            store.batch(ACCOUNT, AUTHOR, decisions)
        assert len(store.listing(ACCOUNT, Selection()).data) == before


class TestWhatAListingIncludes:
    def test_forgotten_memories_are_hidden_unless_asked_for(self, store: SQLStore) -> None:
        memory_id = remember(store)
        store.transition(ACCOUNT, memory_id, "forget")
        assert store.listing(ACCOUNT, Selection()).data == []
        assert len(store.listing(ACCOUNT, Selection(include_forgotten=True)).data) == 1

    def test_superseded_versions_are_hidden_unless_history_is_asked_for(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        old = remember(store, body="Lived in London")
        clock.advance(60)
        store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Bristol"), old)
        assert [row.body for row in store.listing(ACCOUNT, Selection()).data] == ["Bristol"]
        with_history = store.listing(ACCOUNT, Selection(include_history=True)).data
        assert [row.body for row in with_history] == ["Lived in London", "Bristol"]

    def test_inferred_memories_can_be_excluded(self, store: SQLStore) -> None:
        # What a model worked out about somebody is not what they told it, and a person
        # auditing what is held should be able to look at one without the other.
        remember(store, body="Stated", trust="stated")
        remember(store, title="Guess", body="Inferred", trust="inferred")
        kept = store.listing(ACCOUNT, Selection(include_inferred=False)).data
        assert [row.body for row in kept] == ["Stated"]

    def test_stated_memories_can_be_excluded(self, store: SQLStore) -> None:
        remember(store, body="Stated", trust="stated")
        remember(store, title="Guess", body="Inferred", trust="inferred")
        kept = store.listing(ACCOUNT, Selection(include_stated=False)).data
        assert [row.body for row in kept] == ["Inferred"]

    def test_narrowing_to_a_profile_still_includes_what_belongs_to_the_whole_account(
        self, store: SQLStore
    ) -> None:
        remember(store, body="Account wide")
        store.write(
            ACCOUNT,
            AUTHOR,
            MemoryInput(title="Work note", body="Work only", scope="profile", profile="work"),
        )
        store.write(
            ACCOUNT,
            AUTHOR,
            MemoryInput(title="Home note", body="Home only", scope="profile", profile="home"),
        )
        kept = store.listing(ACCOUNT, Selection(profile="work")).data
        assert [row.body for row in kept] == ["Account wide", "Work only"]

    def test_another_account_sees_none_of_it(self, store: SQLStore) -> None:
        remember(store, body="Lived in London")
        assert store.listing(OTHER, Selection()).data == []


class TestPagingThroughAListing:
    def test_a_page_reports_its_own_ends_and_whether_more_follows(self, store: SQLStore) -> None:
        # `first_id` and `last_id` are what the caller pages with; `has_more` is what stops
        # it concluding it has seen everything when it has seen twenty of forty.
        ids = [remember(store, title=f"Note {index}") for index in range(3)]
        page = store.listing(ACCOUNT, Selection(limit=2))
        assert [row.id for row in page.data] == ids[:2]
        assert (page.first_id, page.last_id, page.has_more) == (ids[0], ids[1], True)

    def test_an_empty_page_names_no_ends_rather_than_inventing_them(self, store: SQLStore) -> None:
        page = store.listing(ACCOUNT, Selection())
        assert (page.data, page.first_id, page.last_id, page.has_more) == ([], None, None, False)

    def test_after_a_cursor_returns_what_follows_it(self, store: SQLStore) -> None:
        ids = [remember(store, title=f"Note {index}") for index in range(3)]
        page = store.listing(ACCOUNT, Selection(after=ids[0]))
        assert [row.id for row in page.data] == ids[1:]

    def test_before_a_cursor_returns_what_precedes_it(self, store: SQLStore) -> None:
        ids = [remember(store, title=f"Note {index}") for index in range(3)]
        page = store.listing(ACCOUNT, Selection(before=ids[2]))
        assert [row.id for row in page.data] == ids[:2]

    def test_a_cursor_means_the_same_direction_when_the_order_is_reversed(
        self, store: SQLStore
    ) -> None:
        # "After" means later in the page the caller is reading, not higher in the table.
        # Without this a client that flipped `order` would silently page backwards.
        ids = [remember(store, title=f"Note {index}") for index in range(3)]
        page = store.listing(ACCOUNT, Selection(after=ids[2], order="desc"))
        assert [row.id for row in page.data] == list(reversed(ids[:2]))

    def test_a_cursor_naming_somebody_elses_memory_is_not_found(self, store: SQLStore) -> None:
        # The cursor is resolved against the caller's own rows first, so a borrowed id
        # cannot be used to probe whether it exists.
        mine = remember(store)
        with pytest.raises(NotFoundError):
            store.listing(OTHER, Selection(after=mine))


class TestSearch:
    def test_a_query_of_only_punctuation_is_refused_rather_than_run(self, store: SQLStore) -> None:
        # Every term is quoted before it reaches FTS, so punctuation is data. A query that
        # quotes to nothing would otherwise be a syntax error from sqlite.
        remember(store, body="Lived in London")
        with pytest.raises(MemoryFault, match="at least one word"):
            store.listing(ACCOUNT, Selection(q="   "))

    def test_fts_operators_in_a_query_are_words_and_not_a_query_language(
        self, store: SQLStore
    ) -> None:
        remember(store, body="Lived in London")
        assert store.listing(ACCOUNT, Selection(q='London OR "')).data != []

    def test_a_query_matching_nothing_returns_nothing_rather_than_everything(
        self, store: SQLStore
    ) -> None:
        # The failure being guarded against is an empty match set collapsing to "no filter".
        remember(store, body="Lived in London")
        assert store.listing(ACCOUNT, Selection(q="zebra")).data == []

    def test_a_match_belonging_to_another_account_is_not_returned(self, store: SQLStore) -> None:
        remember(store, body="Lived in London")
        assert store.listing(OTHER, Selection(q="London")).data == []

    def test_re_indexing_a_corrected_memory_does_not_leave_the_old_text_searchable(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        old = remember(store, body="Lived in London")
        clock.advance(60)
        store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Bristol"), old)
        found = store.listing(ACCOUNT, Selection(q="London", include_history=True)).data
        assert [row.body for row in found] == ["Lived in London"]


class TestRetrieval:
    def test_retrieval_records_that_a_memory_was_used(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        # Recency of *use* is half the ranking. A memory that is never retrieved decays out
        # of the top of the list, which is what keeps stale facts from crowding it.
        remember(store, body="Lived in London")
        clock.advance(120)
        retrieved = store.listing(ACCOUNT, Selection(q="London"), retrieval=True).data[0]
        assert retrieved.access_count == 1
        assert retrieved.last_accessed_at == clock.now

    def test_ranking_prefers_the_important_and_recently_used(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        remember(store, title="Tea note one", body="tea", importance=1)
        clock.advance(86_400 * 120)
        remember(store, title="Tea note two", body="tea", importance=10)
        found = store.listing(ACCOUNT, Selection(q="tea"), retrieval=True).data
        assert [row.title for row in found] == ["Tea note two", "Tea note one"]

    def test_a_single_result_ranks_without_dividing_by_a_zero_spread(self, store: SQLStore) -> None:
        # Every component normalises against the spread of the candidates. With one
        # candidate that spread is zero, which is the arithmetic this guards.
        remember(store, body="Lived in London")
        assert len(store.listing(ACCOUNT, Selection(q="London"), retrieval=True).data) == 1

    def test_ranking_an_empty_result_set_does_nothing(self, store: SQLStore) -> None:
        assert store.listing(ACCOUNT, Selection(), retrieval=True).data == []

    def test_an_expired_memory_is_not_retrieved(self, store: SQLStore, clock: FakeClock) -> None:
        remember(store, body="Parking permit", expires_at=clock.now + 60)
        clock.advance(120)
        assert store.listing(ACCOUNT, Selection(q="permit"), retrieval=True).data == []

    def test_an_episode_from_another_session_is_not_retrieved(self, store: SQLStore) -> None:
        # A conversation's own turns are not facts about a life, and carrying one session's
        # episodes into another is how an assistant starts answering the wrong question.
        store.write(
            ACCOUNT,
            AUTHOR,
            MemoryInput(
                title="Said hello",
                body="hello",
                kind="episode",
                scope="session",
                profile="work",
                session_id="s1",
            ),
        )
        here = Selection(profile="work", session_id="s1", q="hello")
        there = Selection(profile="work", session_id="s2", q="hello")
        assert len(store.listing(ACCOUNT, here, retrieval=True).data) == 1
        assert store.listing(ACCOUNT, there, retrieval=True).data == []


class TestBlocks:
    def test_a_block_is_created_then_replaced_wholesale(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        store.block(ACCOUNT, "persona", BlockInput(body="Warm and brief"))
        clock.advance(60)
        rewritten = store.block(ACCOUNT, "persona", BlockInput(body="Warm, brief, direct"))
        assert rewritten.body == "Warm, brief, direct"
        assert rewritten.updated_at == clock.now
        assert [block.label for block in store.blocks(ACCOUNT)] == ["persona"]

    def test_reading_a_block_that_does_not_exist_is_not_found(self, store: SQLStore) -> None:
        with pytest.raises(NotFoundError, match="block"):
            store.block(ACCOUNT, "persona")

    def test_another_accounts_block_is_not_found_rather_than_refused(self, store: SQLStore) -> None:
        store.block(ACCOUNT, "persona", BlockInput(body="Warm and brief"))
        with pytest.raises(NotFoundError):
            store.block(OTHER, "persona")

    def test_deleting_a_block_that_is_not_there_succeeds(self, store: SQLStore) -> None:
        store.delete_block(ACCOUNT, "persona")
        assert store.blocks(ACCOUNT) == []

    def test_blocks_are_listed_in_a_stable_order(self, store: SQLStore) -> None:
        for label in ("persona", "human", "context"):
            store.block(ACCOUNT, label, BlockInput(body="x"))
        assert [block.label for block in store.blocks(ACCOUNT)] == ["context", "human", "persona"]


class TestForgettingEverything:
    def test_it_forgets_the_memories_and_removes_the_blocks(self, store: SQLStore) -> None:
        remember(store, body="Lived in London")
        remember(store, title="Favourite tea", body="Earl Grey")
        store.block(ACCOUNT, "persona", BlockInput(body="Warm and brief"))
        assert store.forget_all(ACCOUNT) == 2
        assert store.listing(ACCOUNT, Selection()).data == []
        assert store.blocks(ACCOUNT) == []

    def test_it_counts_only_what_was_still_remembered(self, store: SQLStore) -> None:
        # A count that included the already-forgotten would tell somebody they had just
        # erased more than they had.
        memory_id = remember(store, body="Lived in London")
        store.transition(ACCOUNT, memory_id, "forget")
        assert store.forget_all(ACCOUNT) == 0

    def test_it_leaves_another_account_untouched(self, store: SQLStore) -> None:
        remember(store, body="Lived in London")
        assert store.forget_all(OTHER) == 0
        assert len(store.listing(ACCOUNT, Selection()).data) == 1


class TestForgettingEverythingCounts:
    def test_the_count_is_what_was_still_believed_not_every_row_ever_written(
        self, store: SQLStore
    ) -> None:
        """A correction leaves history behind, and history was being counted.

        Telling somebody they had three memories when they had told the assistant one thing
        twice makes the number worse than useless: it is the one figure they have for how
        much is held about them.
        """
        original = remember(store, body="Lived in London")
        store.write(ACCOUNT, AUTHOR, MemoryInput(title="Home city", body="Moved"), original)
        remember(store, title="Tea", body="Earl Grey")

        assert store.forget_all(ACCOUNT) == 2, "the superseded row was history, not a memory"


class TestTheSweep:
    def test_a_memory_forgotten_longer_ago_than_the_grace_period_is_erased(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        memory_id = remember(store, body="Lived in London")
        store.transition(ACCOUNT, memory_id, "forget")
        clock.advance(86_400)
        assert store.sweep(grace_seconds=3_600) == 1
        with pytest.raises(NotFoundError):
            store.get(ACCOUNT, memory_id)

    def test_erasure_takes_the_search_index_with_it(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        # A row erased from `memories` but left in the full-text index is the whole erasure
        # promise broken: the text is still on disk and still findable.
        memory_id = remember(store, body="Lived in London")
        store.transition(ACCOUNT, memory_id, "forget")
        clock.advance(86_400)
        store.sweep(grace_seconds=3_600)
        assert store.listing(ACCOUNT, Selection(q="London", include_forgotten=True)).data == []

    def test_a_memory_still_inside_the_grace_period_survives(
        self, store: SQLStore, clock: FakeClock
    ) -> None:
        memory_id = remember(store, body="Lived in London")
        store.transition(ACCOUNT, memory_id, "forget")
        clock.advance(60)
        assert store.sweep(grace_seconds=3_600) == 0
        assert store.get(ACCOUNT, memory_id).forgotten_at is not None

    def test_a_remembered_memory_is_never_swept(self, store: SQLStore, clock: FakeClock) -> None:
        remember(store, body="Lived in London")
        clock.advance(86_400 * 365)
        assert store.sweep(grace_seconds=1) == 0


class TestTheFileOnDisk:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows uses ACLs")
    def test_the_database_is_readable_only_by_the_account_that_runs_us(
        self, tmp_path_factory: pytest.TempPathFactory, clock: FakeClock
    ) -> None:
        """It holds a person's own words about themselves, in plaintext.

        It was being created at whatever the process umask happened to be, which on a great
        many machines is world-readable.
        """
        path = tmp_path_factory.mktemp("private") / "memory.db"
        store = SQLStore(str(path), clock)
        try:
            remember(store, body="Lived in London")
            assert stat.S_IMODE(path.stat().st_mode) == DATABASE_FILE_MODE
        finally:
            store.close()

    def test_an_in_memory_database_has_no_file_to_protect(self, clock: FakeClock) -> None:
        store = SQLStore(":memory:", clock)
        try:
            assert remember(store, body="Lived in London")
        finally:
            store.close()
