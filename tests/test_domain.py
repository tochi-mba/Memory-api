"""The rules that hold whatever the storage is.

Everything here is pure: a model refusing a request that contradicts itself, the secret
detector refusing credential-shaped text, and the topic matcher deciding what belongs with
what. None of it touches a database, which is the point of it being a separate layer -- these
are the decisions that must stay the same if the store is ever replaced.
"""

from __future__ import annotations

import builtins

import pytest
from pydantic import ValidationError

from memory_api.domain import topics
from memory_api.domain.errors import ConflictError, MemoryFault, NotFoundError, SecretError
from memory_api.domain.models import BlockInput, Decision, MemoryInput, TopicUpdate
from memory_api.domain.secrets import refuse_secrets


class TestTheErrorVocabulary:
    def test_every_domain_error_is_one_kind_of_thing_the_api_can_render(self) -> None:
        # The API layer registers one handler for the base class and reads `status` and
        # `code` off whatever it caught, so an error outside this hierarchy becomes a 500.
        for error in (NotFoundError, ConflictError, SecretError):
            assert issubclass(error, MemoryFault)

    def test_the_base_class_shadows_no_builtin(self) -> None:
        # An `except MemoryError` anywhere in the process must still mean what Python means
        # by it. This is the whole reason the class is not called that, and the check is
        # against every builtin rather than that one name so the next error added here
        # cannot reintroduce the problem under a different spelling.
        assert MemoryFault.__name__ not in vars(builtins)
        assert not issubclass(MemoryFault, MemoryError)


class TestAMemoryMustAgreeWithItself:
    def test_a_profile_memory_without_a_profile_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="profile is required"):
            MemoryInput(title="x", scope="profile")

    def test_a_session_memory_without_a_session_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="session_id is required"):
            MemoryInput(title="x", scope="session", profile="work")

    def test_an_account_memory_cannot_name_a_profile(self) -> None:
        with pytest.raises(ValidationError, match="cannot name a profile"):
            MemoryInput(title="x", scope="account", profile="work")

    def test_a_session_id_belongs_only_to_a_session_memory(self) -> None:
        with pytest.raises(ValidationError, match="belongs only to session"):
            MemoryInput(title="x", scope="profile", profile="work", session_id="s1")

    def test_a_memory_cannot_expire_before_it_starts(self) -> None:
        with pytest.raises(ValidationError, match="must follow valid_from"):
            MemoryInput(title="x", valid_from=200.0, expires_at=100.0)

    def test_an_expiry_with_no_start_is_allowed_because_the_start_defaults_to_now(self) -> None:
        assert MemoryInput(title="x", expires_at=100.0).valid_from is None

    def test_a_field_the_schema_does_not_have_is_refused_rather_than_ignored(self) -> None:
        # The account id is the field that matters here: a request that tries to name one
        # must fail loudly, not have it quietly dropped and be answered for somebody else.
        with pytest.raises(ValidationError):
            MemoryInput.model_validate({"title": "x", "account_id": "acct_someone_else"})


class TestATopicUpdateMustChangeSomething:
    def test_an_update_with_neither_a_title_nor_a_summary_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must change the title"):
            TopicUpdate()

    def test_either_one_alone_is_enough(self) -> None:
        assert TopicUpdate(title="Tea").summary is None
        assert TopicUpdate(summary="About tea").title is None


class TestABlockMustFitItsOwnLimit:
    def test_a_body_longer_than_its_char_limit_is_refused_rather_than_truncated(self) -> None:
        with pytest.raises(ValidationError, match="exceeds char_limit"):
            BlockInput(body="x" * 20, char_limit=10)

    def test_a_body_exactly_at_the_limit_is_accepted(self) -> None:
        assert BlockInput(body="x" * 10, char_limit=10).char_limit == 10


class TestADecisionMustCarryWhatItActsOn:
    def test_adding_without_a_memory_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="require a memory"):
            Decision(action="ADD")

    def test_updating_without_an_id_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="require memory_id"):
            Decision(action="UPDATE", memory=MemoryInput(title="x"))

    def test_a_noop_needs_only_the_id_of_what_it_left_alone(self) -> None:
        assert Decision(action="NOOP", memory_id="mem_1").memory is None


class TestCredentialShapedContentIsNotMemory:
    # No literal credential is committed here: each is assembled from parts so the file
    # itself never contains a string a secret scanner would flag.
    @pytest.mark.parametrize(
        "text",
        [
            "Bearer " + "abc123",
            "-----BEGIN RSA PRIVATE KEY-----",
            "sk-" + "a" * 20,
            "ghp_" + "b" * 20,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        ],
    )
    def test_a_recognised_credential_shape_is_refused(self, text: str) -> None:
        with pytest.raises(SecretError, match="keyring"):
            refuse_secrets({"body": text})

    def test_a_long_high_entropy_run_is_refused_even_with_no_recognised_prefix(self) -> None:
        # The patterns only catch credentials whose shape somebody thought of. This catches
        # the rest by the one property every secret has and no sentence does.
        with pytest.raises(SecretError, match="High-entropy"):
            refuse_secrets({"value": "Zq7Z" + "xK9wPm2LbV4tYr8NcJ6hQa3sDf5gTu1eWi0oRy" + "Ab"})

    def test_a_long_but_repetitive_run_is_not_a_secret(self) -> None:
        # Forty identical characters trips the length pattern and nothing else, which is
        # what keeps the entropy floor from refusing ordinary text.
        refuse_secrets({"body": "a" * 60})

    def test_ordinary_prose_passes(self) -> None:
        refuse_secrets({"title": "Home city", "body": "Moved to Bristol last spring."})

    def test_a_credential_nested_inside_a_structured_value_is_still_found(self) -> None:
        # The check runs over the serialised request, not over its prose fields, because a
        # token hidden in a JSON `value` is stored just as durably as one in the body.
        with pytest.raises(SecretError):
            refuse_secrets({"value": {"nested": ["sk-" + "c" * 20]}})


class TestWhichWordsMakeATopicKey:
    def test_stopwords_are_dropped(self) -> None:
        assert topics.words("The home of my favourite tea") == ("home", "favourite", "tea")

    def test_a_title_made_entirely_of_stopwords_keeps_them(self) -> None:
        # Otherwise the key is empty and every such memory collapses into one topic.
        assert topics.words("how to") == ("how", "to")

    def test_a_key_is_order_independent_so_one_subject_is_one_topic(self) -> None:
        assert topics.key_for("tea preferences") == topics.key_for("preferences: tea")

    def test_a_key_drops_repeats_so_repetition_does_not_change_the_subject(self) -> None:
        assert topics.key_for("tea tea tea") == "tea"


class TestHowAlikeTwoSubjectsAre:
    def test_identical_keys_score_one(self) -> None:
        assert topics.similarity("home city", "home city") == 1.0

    def test_keys_with_nothing_in_common_score_zero(self) -> None:
        assert topics.similarity("home city", "favourite tea") == 0.0

    def test_an_empty_key_is_like_nothing_rather_than_like_everything(self) -> None:
        # Jaccard over two empty sets is undefined; treating it as 1.0 would merge every
        # keyless memory into whichever topic was asked about first.
        assert topics.similarity("", "home city") == 0.0
        assert topics.similarity("home city", "") == 0.0


class TestWhichTopicAMemoryJoins:
    def test_an_exact_key_wins_without_a_judgement_call(self) -> None:
        assert topics.choose("home city", {"home city": "t1", "city home town": "t2"}) == "t1"

    def test_a_close_enough_key_joins_the_closest_candidate(self) -> None:
        assert topics.choose("favourite tea", {"favourite tea drink": "t1"}) == "t1"

    def test_a_key_below_the_threshold_starts_its_own_topic(self) -> None:
        assert topics.choose("favourite tea", {"home city address": "t1"}) is None

    def test_no_candidates_at_all_starts_its_own_topic(self) -> None:
        assert topics.choose("favourite tea", {}) is None

    def test_a_tie_is_broken_by_the_key_so_two_runs_over_the_same_data_agree(self) -> None:
        # Both candidates score exactly the threshold against "tea". Without the tie-break
        # the winner would be whichever the dictionary happened to yield first.
        candidates = {"tea zebra": "t_zebra", "tea apple": "t_apple"}
        assert topics.choose("tea", candidates) == "t_apple"
        assert topics.choose("tea", dict(reversed(list(candidates.items())))) == "t_apple"


class TestThePlaceholderSummary:
    def test_the_first_sentence_of_the_body_is_the_summary(self) -> None:
        assert topics.summarise("Home city", "Moved to Bristol. Again.") == "Moved to Bristol"

    def test_a_bodyless_memory_falls_back_to_its_title(self) -> None:
        assert topics.summarise("Home city", "   ") == "Home city"


class TestTitlesAndSummariesAreFlattened:
    def test_a_newline_cannot_break_the_shape_of_the_block_a_model_reads(self) -> None:
        assert topics.clamp("Home\ncity\t ", 80) == "Home city"

    def test_an_overlong_title_is_cut_and_marked_rather_than_left_to_fill_the_budget(
        self,
    ) -> None:
        clamped = topics.clamp("x" * 100, 10)
        assert len(clamped) == 10
        assert clamped.endswith("…")
