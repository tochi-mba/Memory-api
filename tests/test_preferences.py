"""Reading a person's importance floor from settings-api, and what happens when it cannot.

These hold the preference source on its own, against settings-client's fake, because the
rules are about one answer at a time: what a chosen value becomes, what an outage becomes,
and what a refusal becomes. ``test_importance_floor.py`` drives the same rules over HTTP.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr, ValidationError
from settings_client import HttpSettingsClient
from settings_client.models import Fallback, OnUnavailable, Value
from settings_client.testing import FakeSettingsClient

from conftest import build_settings
from memory_api.core.preferences import (
    DEPLOYMENT,
    FLOOR,
    NAMESPACE,
    NOT_GUESSED,
    UNKNOWN,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
)
from memory_api.domain.errors import BelowImportanceFloorError, PreferencesUnavailableError
from memory_api.domain.importance import LOWEST, UNASKED, FloorNotAskedError, hold, within_floor
from memory_api.domain.models import Decision, MemoryInput, Selection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from memory_api.store.sql import SQLStore

TOKEN = "user-token-for-settings"
ACCOUNT = "acct_example"
SETTINGS_API_URL = "http://settings.test"
SETTINGS_API_TOKEN = "s" * 32


def source(client: FakeSettingsClient) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client)


def floor_of(level: int) -> Callable[[], Awaitable[int]]:
    async def floor() -> int:
        return level

    return floor


class TestNobodyAsked:
    async def test_with_no_settings_api_everything_is_kept(self) -> None:
        """The bug, named: a deployment without settings-api must keep every write it kept
        before; any floor above one here would start refusing memories nobody chose to
        lose."""
        chosen = build_preference_source(build_settings())

        assert isinstance(chosen, DeploymentPreferences)
        assert await chosen.for_token(TOKEN) == DEPLOYMENT
        assert DEPLOYMENT.importance_floor() == LOWEST
        await chosen.aclose()

    async def test_a_configured_settings_api_gets_the_real_client(self) -> None:
        settings = build_settings(
            settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN
        )

        chosen = build_preference_source(settings)

        assert isinstance(chosen, SettingsApiPreferences)
        assert isinstance(chosen._client, HttpSettingsClient)
        await chosen.aclose()

    async def test_a_substituted_client_is_used_as_given(self) -> None:
        fake = FakeSettingsClient()

        chosen = build_preference_source(build_settings(), client=fake)

        assert isinstance(chosen, SettingsApiPreferences)
        assert chosen._client is fake
        await chosen.aclose()


class TestAChosenFloor:
    async def test_the_persons_own_floor_is_the_floor(self) -> None:
        """The bug, named: memory-api stored every memory whatever importance floor the
        person had chosen in settings-api, because nothing here read it."""
        fake = FakeSettingsClient({NAMESPACE: {FLOOR: 7}})

        preferences = await source(fake).for_token(TOKEN)

        assert preferences.importance_floor() == 7

    async def test_a_settings_api_without_the_key_keeps_everything(self) -> None:
        """The bug, named: an older settings-api that has never heard of the floor cannot
        have been told one, and refusing writes for it would be inventing a choice."""
        fake = FakeSettingsClient({NAMESPACE: {}})

        assert (await source(fake).for_token(TOKEN)).importance_floor() == LOWEST

    @pytest.mark.parametrize("value", ["seven", True, 0, 11, None])
    async def test_a_value_that_is_not_a_floor_is_not_guessed(
        self, value: Value, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bug, named: a value of the wrong shape would otherwise become a guess, and
        this key is one the catalogue says must never be guessed."""
        caplog.set_level(logging.WARNING, logger="memory_api.core.preferences")
        fake = FakeSettingsClient({NAMESPACE: {FLOOR: value}})

        preferences = await source(fake).for_token(TOKEN)

        assert preferences == UNKNOWN
        assert [record.getMessage() for record in caplog.records] == [
            f"setting_unusable namespace={NAMESPACE} key={FLOOR}"
        ]

    async def test_it_asks_for_the_memory_namespace_with_the_callers_token(self) -> None:
        fake = FakeSettingsClient({NAMESPACE: {FLOOR: 2}})

        await source(fake).for_token(TOKEN)

        assert fake.resolves == 1


class TestAnOutage:
    async def test_never_having_heard_from_settings_api_is_not_a_guess(self) -> None:
        """The bug, named: a settings-api that has never answered was treated as "keep
        everything", which writes down more than a person with a floor agreed to."""
        fake = FakeSettingsClient()
        fake.unavailable = True

        preferences = await source(fake).for_token(TOKEN)

        assert preferences == UNKNOWN
        with pytest.raises(PreferencesUnavailableError) as caught:
            preferences.importance_floor()
        assert caught.value.status == 503

    async def test_a_refused_key_is_not_guessed(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="memory_api.core.preferences")
        fake = FakeSettingsClient(
            fallbacks={NAMESPACE: {FLOOR: Fallback(default=3, on_unavailable=OnUnavailable.REFUSE)}}
        )
        fake.unavailable = True

        assert await source(fake).for_token(TOKEN) == UNKNOWN
        assert f"settings_refused namespace={NAMESPACE} key={FLOOR}" in caplog.text

    async def test_a_stale_answer_is_the_persons_own_and_is_used(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="memory_api.core.preferences")
        fake = FakeSettingsClient(
            fallbacks={
                NAMESPACE: {FLOOR: Fallback(default=4, on_unavailable=OnUnavailable.USE_DEFAULT)}
            }
        )
        fake.unavailable = True

        assert (await source(fake).for_token(TOKEN)).importance_floor() == 4
        assert f"settings_stale namespace={NAMESPACE}" in caplog.text

    async def test_a_refusal_of_this_service_fails_rather_than_degrades(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bug, named: a missing grant would hide behind behaviour that happens to work
        if a 403 were read as "settings-api is down"."""
        caplog.set_level(logging.WARNING, logger="memory_api.core.preferences")
        fake = FakeSettingsClient()
        fake.rejects[NAMESPACE] = (403, "grant memory-api: namespace memory not granted")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await source(fake).for_token(TOKEN)

        assert caught.value.status == 503
        assert "grant" not in str(caught.value)
        assert caplog.messages == [f"settings_rejected namespace={NAMESPACE} status_code=403"]


class TestTheFloorItself:
    def test_a_known_floor_is_read_back(self) -> None:
        assert Preferences(write_importance_floor=5).importance_floor() == 5


class TestConfiguration:
    def test_no_settings_api_is_the_default(self) -> None:
        assert build_settings().settings_api is None

    def test_a_base_url_and_a_token_together_turn_it_on(self) -> None:
        settings = build_settings(
            settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN
        )

        assert settings.settings_api is not None
        base_url, token = settings.settings_api
        assert base_url == SETTINGS_API_URL
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == SETTINGS_API_TOKEN

    @pytest.mark.parametrize(
        "half",
        [{"settings_api_base_url": SETTINGS_API_URL}, {"settings_api_token": SETTINGS_API_TOKEN}],
    )
    def test_half_a_configuration_refuses_to_start(self, half: dict[str, str]) -> None:
        with pytest.raises(ValueError, match="set together"):
            build_settings(**half)

    def test_a_blank_base_url_means_off(self) -> None:
        assert build_settings(settings_api_base_url="").settings_api is None

    def test_a_short_token_is_refused_without_being_echoed(self) -> None:
        with pytest.raises(ValidationError, match="32") as caught:
            build_settings(settings_api_base_url=SETTINGS_API_URL, settings_api_token="too-short")
        assert all("too-short" not in error["msg"] for error in caught.value.errors())

    def test_the_token_does_not_render_itself(self) -> None:
        settings = build_settings(
            settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN
        )

        assert SETTINGS_API_TOKEN not in repr(settings)


class TestWhatTheFloorAppliesTo:
    """The rules without HTTP: :mod:`memory_api.domain.importance`, applied by the store."""

    def test_an_addition_below_the_floor_names_its_position_and_nothing_else(
        self, store: SQLStore
    ) -> None:
        decisions = [
            Decision(action="ADD", memory=MemoryInput(title="Keep", importance=8)),
            Decision(action="ADD", memory=MemoryInput(title="Secret-ish trivia", importance=2)),
            Decision(action="ADD", memory=MemoryInput(title="Also trivia", importance=1)),
        ]

        with pytest.raises(BelowImportanceFloorError) as caught:
            store.batch(ACCOUNT, ACCOUNT, decisions, floor=3)

        message = str(caught.value)
        assert ": 1, 2." in message
        assert "trivia" not in message
        assert caught.value.status == 422
        assert caught.value.code == "below-importance-floor"
        assert store.listing(ACCOUNT, Selection()).data == []

    async def test_a_batch_that_adds_nothing_never_asks_for_the_floor(
        self, store: SQLStore
    ) -> None:
        """The bug, named: a batch of corrections and forgets failed during a settings-api
        outage for a floor it had no use for."""
        first, second, third = (
            store.write(ACCOUNT, ACCOUNT, MemoryInput(title=title)).id
            for title in ("One", "Two", "Three")
        )

        async def unreadable() -> int:
            raise PreferencesUnavailableError(NOT_GUESSED)

        async def apply(floor: int | None) -> int:
            decisions = [
                Decision(
                    action="UPDATE", memory_id=first, memory=MemoryInput(title="x", importance=1)
                ),
                Decision(action="DELETE", memory_id=second),
                Decision(action="NOOP", memory_id=third),
            ]
            return len(store.batch(ACCOUNT, ACCOUNT, decisions, floor=floor))

        assert await within_floor(apply, unreadable) == 3

    def test_an_unasked_floor_rolls_back_at_the_first_new_memory(self, store: SQLStore) -> None:
        """What makes asking lazily safe: the first attempt stops at a new memory, before
        the floor it is held to is known, and leaves nothing it did behind."""
        kept = store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Kept"))
        decisions = [
            Decision(action="DELETE", memory_id=kept.id),
            Decision(action="ADD", memory=MemoryInput(title="New")),
        ]

        with pytest.raises(FloorNotAskedError):
            store.batch(ACCOUNT, ACCOUNT, decisions, floor=UNASKED)

        assert [row.title for row in store.listing(ACCOUNT, Selection()).data] == ["Kept"]

    async def test_a_write_that_adds_a_memory_is_applied_again_held_to_the_floor(
        self, store: SQLStore
    ) -> None:
        attempts: list[int | None] = []

        async def apply(floor: int | None) -> str:
            attempts.append(floor)
            return store.write(ACCOUNT, ACCOUNT, MemoryInput(title="New"), floor=floor).title

        assert await within_floor(apply, floor_of(5)) == "New"
        assert attempts == [UNASKED, 5]

    def test_additions_at_the_floor_pass(self, store: SQLStore) -> None:
        at = MemoryInput(title="Exactly enough", importance=3)

        hold(at, 3)
        assert len(store.batch(ACCOUNT, ACCOUNT, [Decision(action="ADD", memory=at)], floor=3)) == 1
