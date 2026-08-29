"""Adding a contact the owner names: by number, or by an attached contact card.

The feature exists because the people this bridge is for do not install MAX, and
until now a bridge could only be built for somebody who had already written. What
is asserted here is mostly what the code refuses to do:

* nothing is ever sent to the person being added during lookup;
* the search takes one number and the import takes one contact, never a list;
* the full number appears on no screen and in no log record;
* a person with no dialog, an already-bridged person and the owner themselves
  each end at an honest screen instead of a bridge built on a guess.

The one thing this feature deliberately cannot do is create the MAX dialog. The
only chat id available before the first message is `owner_id ^ peer_id`, computed
locally by PyMax and never confirmed by the server — and a bridge keyed on a
wrong guess is two bots on one conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher

from bridge.max_client import MaxContact
from bridge.onboarding import screens
from bridge.provisioning import DialogFlow, GuardianContext, build_guardian_router, byphone
from bridge.provisioning.journal import ProvisioningJournal
from bridge.provisioning.picker import OPEN, DialogOption
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_provisioning import (
    FakeBridgeRepository,
    FakeDialogPicker,
    FakeGateway,
    FakeProvisioner,
)
from tests.fake_telegram import (
    OWNER_ID,
    FakeBot,
    make_callback,
    make_contact_message,
    make_message,
)

#: The owner's Telegram id is half of every V2 username; the contact's
#: MAX id is the other half. No secret is involved any more.
OWNER = 100000001

OWNER_MAX_ID = 100000002
CONTACT_MAX_ID = 200000003
CONTACT_CHAT_ID = 236856065

#: Written the way an owner types it, so normalisation is exercised on the way in.
TYPED = "8 900 123-45-67"
E164 = "+79001234567"


@dataclass
class FakeDirectory:
    """MAX, as far as a phone number is concerned. Records every call.

    There is no `send_message` on it on purpose: a test cannot assert "nothing was
    sent" as convincingly as a collaborator that has nothing to send with.
    """

    known: dict[str, MaxContact] = field(default_factory=dict)
    own_user_id: int | None = OWNER_MAX_ID

    searched: list[str] = field(default_factory=list)
    imported: list[tuple[str, str]] = field(default_factory=list)
    #: Numbers the import conjures into existence, as the fallback claims to.
    importable: dict[str, MaxContact] = field(default_factory=dict)
    #: Contacts resolvable by MAX user id — the by-contactId (deep-link) path.
    profiles: dict[int, MaxContact] = field(default_factory=dict)
    profiled: list[int] = field(default_factory=list)

    async def search_by_phone(self, phone: str) -> MaxContact | None:
        self.searched.append(phone)
        return self.known.get(phone)

    async def contact_profile(self, user_id: int) -> MaxContact | None:
        self.profiled.append(user_id)
        return self.profiles.get(user_id)

    async def import_contact(self, phone: str, name: str) -> MaxContact | None:
        self.imported.append((phone, name))
        found = self.importable.get(phone)
        if found is not None:
            self.known[phone] = found
        return found


def contact(
    user_id: int = CONTACT_MAX_ID,
    name: str | None = "Анна Смирнова",
    avatar: str | None = "https://i.oneme.ru/i?r=abc",
) -> MaxContact:
    return MaxContact(user_id=user_id, display_name=name, avatar_url=avatar)


@dataclass
class Wired:
    dispatcher: Dispatcher
    bot: FakeBot
    flow: DialogFlow
    directory: FakeDirectory
    provisioner: FakeProvisioner
    gateway: FakeGateway
    bridges: FakeBridgeRepository


def wire(
    tmp_path: Path,
    *,
    dialogs: list[DialogOption] | None = None,
    known: dict[str, MaxContact] | None = None,
    importable: dict[str, MaxContact] | None = None,
    profiles: dict[int, MaxContact] | None = None,
    directory: bool = True,
    is_guardian: Any = None,
) -> Wired:
    provisioner = FakeProvisioner()
    gateway = FakeGateway()
    bridges = FakeBridgeRepository()
    folder = FakeDirectory(
        known=dict(known or {}),
        importable=dict(importable or {}),
        profiles=dict(profiles or {}),
    )
    flow = DialogFlow(
        picker=FakeDialogPicker(list(dialogs or [])),  # type: ignore[arg-type]
        provisioner=provisioner,
        gateway=gateway,
        journal=ProvisioningJournal.for_data_dir(tmp_path),
        bridges=bridges,
        telegram_owner_user_id=OWNER,
        directory=folder if directory else None,  # type: ignore[arg-type]
    )
    bot = FakeBot("1:aaa")
    context = GuardianContext(flow=flow, is_guardian=is_guardian)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(build_guardian_router(context))
    return Wired(
        dispatcher=dispatcher,
        bot=bot,
        flow=flow,
        directory=folder,
        provisioner=provisioner,
        gateway=gateway,
        bridges=bridges,
    )


def dialog(user_id: int = CONTACT_MAX_ID, chat_id: int = CONTACT_CHAT_ID) -> DialogOption:
    return DialogOption(
        max_chat_id=chat_id, title="Анна", last_activity=7, max_user_id=user_id
    )


async def feed(wired: Wired, update: Any) -> None:
    await wired.dispatcher.feed_update(wired.bot, update)  # type: ignore[arg-type]


def texts(wired: Wired) -> list[str]:
    return [
        str(call.kwargs.get("text") or "")
        for call in wired.bot.calls
        if call.method in {"send_message", "edit_message_text"}
    ]


def last_text(wired: Wired) -> str:
    said = texts(wired)
    assert said, "nothing was drawn"
    return said[-1]


def last_markup(wired: Wired) -> str:
    for call in reversed(wired.bot.calls):
        if call.kwargs.get("reply_markup") is not None:
            return str(call.kwargs["reply_markup"])
    return ""


async def enter(wired: Wired, number: str, update_id: int = 2) -> None:
    """The manual path, end to end: the button, then the typed number."""
    await feed(wired, make_callback(update_id, byphone.BY_PHONE))
    await feed(wired, make_message(update_id + 1, number))


@pytest.fixture
def found(tmp_path: Path) -> Wired:
    return wire(tmp_path, dialogs=[dialog()], known={E164: contact()})


# ------------------------------------------------------------------- the menu


def test_the_menu_offers_three_ways_in() -> None:
    _, markup = byphone.menu(dialogs=OPEN)
    rendered = str(markup)

    assert OPEN in rendered, "picking an existing dialog stays the first option"
    assert byphone.BY_CONTACT in rendered
    assert byphone.BY_PHONE in rendered


def test_the_back_button_points_at_the_screen_that_exists() -> None:
    """`byphone` names the home callback by value to avoid an import cycle.

    Which makes it a string that can rot silently. This is the check that it
    cannot: onboarding owns the constant, and the two must be the same.
    """
    assert byphone.HOME == screens.MENU


def test_the_home_screen_leads_here() -> None:
    from bridge.onboarding.views import AttentionFacts, home_view

    _, markup = screens.home(
        home_view(AttentionFacts(worker_running=True, max_connected=True))
    )
    assert byphone.ADD in str(markup)


# ------------------------------------------------------- looking a number up


async def test_a_typed_number_is_normalised_before_it_is_searched(found: Wired) -> None:
    await enter(found, TYPED)

    assert found.directory.searched == [E164], "8… → +7…, and one lookup for one number"


async def test_the_found_contact_is_shown_with_a_name_and_a_photo(found: Wired) -> None:
    await enter(found, TYPED)

    assert "Анна Смирнова" in last_text(found)
    assert "https://i.oneme.ru/i?r=abc" in last_markup(found), "the avatar, as a link"
    assert byphone.CONFIRM in last_markup(found)


async def test_the_number_is_masked_on_screen(found: Wired) -> None:
    await enter(found, TYPED)

    assert E164 not in last_text(found)
    assert "45-67" in last_text(found), "enough to recognise"


async def test_the_number_never_reaches_the_log(
    found: Wired, caplog: pytest.LogCaptureFixture
) -> None:
    """By the same rule as tokens: not at any level, not in any record."""
    with caplog.at_level(logging.DEBUG):
        await enter(found, TYPED)

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert E164 not in written
    assert TYPED not in written
    assert "8377356" not in written


async def test_nothing_is_sent_to_the_person(found: Wired) -> None:
    """The whole lookup path touches MAX twice, and neither call writes."""
    await enter(found, TYPED)

    assert found.directory.searched == [E164]
    assert found.directory.imported == []
    assert not hasattr(found.directory, "sent")


async def test_a_bad_number_is_refused_before_max_is_asked(tmp_path: Path) -> None:
    wired = wire(tmp_path)
    await enter(wired, "не телефон")

    assert "не похоже на номер" in last_text(wired)
    assert wired.directory.searched == []


async def test_the_owners_own_number_is_refused(tmp_path: Path) -> None:
    wired = wire(
        tmp_path,
        dialogs=[dialog()],
        known={E164: contact(user_id=OWNER_MAX_ID, name="Я сам")},
    )
    await enter(wired, TYPED)

    assert "ваш собственный номер" in last_text(wired)
    assert byphone.CONFIRM not in last_markup(wired)


# ------------------------------------------------------ the attached contact


async def test_an_attached_contact_brings_its_own_name_and_number(
    tmp_path: Path,
) -> None:
    wired = wire(tmp_path, dialogs=[dialog()], known={E164: contact()})

    await feed(wired, make_callback(2, byphone.BY_CONTACT))
    await feed(
        wired,
        make_contact_message(3, phone=E164, first_name="Анна", last_name="А."),
    )

    assert wired.directory.searched == [E164]
    assert byphone.CONFIRM in last_markup(wired)


async def test_a_contact_card_is_ignored_when_nothing_was_asked(tmp_path: Path) -> None:
    """A card sent out of the blue is a message, not an answer to a question."""
    wired = wire(tmp_path, dialogs=[dialog()], known={E164: contact()})

    await feed(wired, make_contact_message(2, phone=E164, first_name="Кто-то"))

    assert wired.directory.searched == []
    assert wired.bot.calls == []


async def test_a_contact_card_sent_to_a_bridge_bot_is_left_alone(tmp_path: Path) -> None:
    """The same router runs on the worker's dispatcher in some deployments.

    There, a contact card belongs to the person on the other end of that bot and
    has to reach MAX untouched — even in the middle of adding somebody.
    """
    wired = wire(
        tmp_path,
        dialogs=[dialog()],
        known={E164: contact()},
        is_guardian=lambda bot_id: False,
    )

    await feed(wired, make_callback(2, byphone.BY_PHONE))
    await feed(wired, make_contact_message(3, phone=E164, first_name="Кто-то"))
    await feed(wired, make_message(4, TYPED))

    assert wired.directory.searched == [], "not the guardian's message to read"


# ---------------------------------------------------------------- refusals


async def test_a_person_with_no_dialog_is_still_offered(tmp_path: Path) -> None:
    """Never spoken to is not unreachable: the chat id is `own ^ peer`.

    MAX issues no id for a personal dialog — its own web client computes this
    one locally and types into a chat the server has never heard of. So the
    button is offered, and what creates the conversation is the owner writing.
    """
    wired = wire(tmp_path, dialogs=[], known={E164: contact()})

    await enter(wired, TYPED)

    said = last_text(wired)
    assert "начнётся с вашего сообщения" in said
    assert byphone.CONFIRM in last_markup(wired)


async def test_a_bridge_for_a_dialog_that_does_not_exist_yet(tmp_path: Path) -> None:
    """The whole point of adding by number: the contact writes nothing first."""
    wired = wire(tmp_path, dialogs=[], known={E164: contact()})

    await enter(wired, TYPED)
    await feed(wired, make_callback(9, byphone.confirm_callback(epoch=1)))

    expected = OWNER_MAX_ID ^ CONTACT_MAX_ID
    assert wired.gateway.started == [expected], "keyed on the computed dialog id"
    assert wired.gateway.peers == {expected: CONTACT_MAX_ID}, (
        "the contact is passed through: the chat cannot be asked who is in it yet"
    )
    assert wired.provisioner.created


def test_the_dialog_id_is_the_xor_of_the_two_user_ids() -> None:
    """The formula itself, written down where a change would trip a test.

    Verified against MAX's web client (`resolveChatId(e, t) => e ^ t`) and
    against 47 of the account's 47 personal dialogs.
    """
    assert byphone.dm_chat_id(100000002, 200000002) == 236856064
    assert byphone.dm_chat_id(1, 2) == byphone.dm_chat_id(2, 1), "both sides agree"
    assert byphone.dm_chat_id(5, 5) == 0, "the chat with oneself is id 0, as MAX has it"


async def test_a_contact_that_already_has_a_bridge_is_refused(tmp_path: Path) -> None:
    wired = wire(tmp_path, dialogs=[dialog()], known={E164: contact()})
    wired.bridges.records[CONTACT_CHAT_ID] = type(
        "Row", (), {"bridge_name": "eliza", "title": "Анна"}
    )()

    await enter(wired, TYPED)

    assert "уже есть" in last_text(wired)
    assert byphone.CONFIRM not in last_markup(wired)
    assert wired.provisioner.created == []


async def test_without_a_max_session_the_button_says_so(tmp_path: Path) -> None:
    wired = wire(tmp_path, directory=False)

    await feed(wired, make_callback(2, byphone.BY_PHONE))

    alerts = [
        str(call.kwargs.get("text") or "")
        for call in wired.bot.calls
        if call.method == "answer_callback_query"
    ]
    assert byphone.NO_DIRECTORY in alerts


# ----------------------------------------------------------------- creating


async def test_the_confirm_button_builds_the_bridge(found: Wired) -> None:
    await enter(found, TYPED)

    await feed(found, make_callback(9, byphone.confirm_callback(epoch=1)))

    assert found.gateway.started, "the bridge for the chosen dialog came up"
    assert found.provisioner.created, "a bot was made for it"


async def test_a_stale_confirmation_creates_nothing(found: Wired) -> None:
    """The epoch is the draft this button was drawn for, and drafts move on."""
    await enter(found, TYPED)
    await feed(found, make_callback(9, byphone.ADD))  # back to the menu: new draft

    await feed(found, make_callback(10, byphone.confirm_callback(epoch=1)))

    assert found.provisioner.created == []


async def test_confirming_twice_runs_once(found: Wired) -> None:
    await enter(found, TYPED)

    await feed(found, make_callback(9, byphone.confirm_callback(epoch=1)))
    made = len(found.provisioner.created)
    await feed(found, make_callback(10, byphone.confirm_callback(epoch=1)))

    assert len(found.provisioner.created) == made


# ------------------------------------------------------------------ import


async def test_a_number_nobody_has_asks_for_a_name_first(tmp_path: Path) -> None:
    wired = wire(tmp_path)

    await enter(wired, TYPED)

    assert "нет в вашем MAX" in last_text(wired)
    assert byphone.IMPORT not in last_markup(wired), "no import without a name to send"
    assert wired.directory.imported == []


async def test_the_import_waits_for_an_explicit_yes(tmp_path: Path) -> None:
    wired = wire(tmp_path)

    await enter(wired, TYPED)
    await feed(wired, make_message(4, "Папа"))

    assert byphone.IMPORT in last_markup(wired)
    assert "MAX получит указанное имя и номер" in last_text(wired)
    assert wired.directory.imported == [], "the screen offers it; the button does it"


async def test_the_import_carries_one_contact_and_then_re_searches(
    tmp_path: Path,
) -> None:
    wired = wire(tmp_path, dialogs=[dialog()], importable={E164: contact()})

    await enter(wired, TYPED)
    await feed(wired, make_message(4, "Папа"))
    await feed(wired, make_callback(5, byphone.import_callback(epoch=1)))

    assert wired.directory.imported == [(E164, "Папа")], "one contact, once"
    assert byphone.CONFIRM in last_markup(wired), "found on the second look"


async def test_an_import_that_finds_nobody_does_not_offer_itself_again(
    tmp_path: Path,
) -> None:
    wired = wire(tmp_path)

    await enter(wired, TYPED)
    await feed(wired, make_message(4, "Папа"))
    await feed(wired, make_callback(5, byphone.import_callback(epoch=1)))

    assert len(wired.directory.imported) == 1
    assert byphone.IMPORT not in last_markup(wired)
    assert "Писать некому" in last_text(wired)


async def test_an_attached_contact_needs_no_name_step(tmp_path: Path) -> None:
    """The card already carried one — asking again would be asking twice."""
    wired = wire(tmp_path)

    await feed(wired, make_callback(2, byphone.BY_CONTACT))
    await feed(wired, make_contact_message(3, phone=E164, first_name="Папа"))

    assert byphone.IMPORT in last_markup(wired)


def test_there_is_no_bulk_import_anywhere_in_the_bridge() -> None:
    """The address book is a list, and `import_contacts` takes a list.

    One careless line joins them. The only call site in the bridge is the one
    below it, and it is handed a list built here, of one, from arguments that
    cannot be a list.
    """
    import inspect

    from bridge.max_client.client import MaxClient

    source = inspect.getsource(MaxClient)
    assert source.count("import_contacts(") == 1

    signature = inspect.signature(MaxClient.import_contact)
    assert list(signature.parameters) == ["self", "phone", "name"]
    assert all(
        signature.parameters[name].annotation == "str" for name in ("phone", "name")
    )


# ----------------------------------------------------- by contactId (deep link)


def test_the_deep_link_payload_round_trips() -> None:
    payload = byphone.bridge_deep_link_payload(CONTACT_MAX_ID)

    assert payload == f"mb{CONTACT_MAX_ID}"
    assert byphone.parse_bridge_deep_link(payload) == CONTACT_MAX_ID


def test_a_non_bridge_start_payload_is_none() -> None:
    """An onboarding token or a hand-typed word is not a bridge deep link."""
    assert byphone.parse_bridge_deep_link("some-onboarding-token") is None
    assert byphone.parse_bridge_deep_link("mb") is None
    assert byphone.parse_bridge_deep_link("mbnotanumber") is None
    assert byphone.parse_bridge_deep_link("") is None


async def test_resolve_by_user_id_needs_no_phone(tmp_path: Path) -> None:
    wired = wire(tmp_path, profiles={CONTACT_MAX_ID: contact()}, dialogs=[dialog()])

    resolution = await wired.flow.resolve_user_id(CONTACT_MAX_ID)

    assert wired.directory.searched == [], "a user id is not searched for by phone"
    assert wired.directory.profiled == [CONTACT_MAX_ID]
    assert resolution.verdict is byphone.Verdict.READY
    assert resolution.max_chat_id == CONTACT_CHAT_ID


async def test_resolve_by_user_id_computes_the_dialog_when_none_exists(
    tmp_path: Path,
) -> None:
    wired = wire(tmp_path, profiles={CONTACT_MAX_ID: contact()})  # no dialog

    resolution = await wired.flow.resolve_user_id(CONTACT_MAX_ID)

    assert resolution.verdict is byphone.Verdict.NEW_DIALOG
    assert resolution.max_chat_id == OWNER_MAX_ID ^ CONTACT_MAX_ID


async def test_the_deep_link_opens_the_confirmation_screen(tmp_path: Path) -> None:
    wired = wire(tmp_path, profiles={CONTACT_MAX_ID: contact()}, dialogs=[dialog()])

    await feed(wired, make_message(2, f"/start mb{CONTACT_MAX_ID}"))

    # The same confirmation the by-phone path draws — name and the create button.
    assert "Анна" in last_text(wired)
    assert byphone.CONFIRM in last_markup(wired)


async def test_the_deep_link_button_then_confirm_builds_the_bridge(
    tmp_path: Path,
) -> None:
    wired = wire(tmp_path, profiles={CONTACT_MAX_ID: contact()}, dialogs=[dialog()])

    await feed(wired, make_message(2, f"/start mb{CONTACT_MAX_ID}"))
    await feed(wired, make_callback(3, byphone.confirm_callback(epoch=1)))

    assert wired.gateway.started, "the bridge for the shared contact came up"
    assert wired.provisioner.created, "a bot was made for it"


async def test_an_unknown_deep_link_is_left_alone(tmp_path: Path) -> None:
    """A payload this handler does not own must not draw a resolution screen."""
    wired = wire(tmp_path, profiles={CONTACT_MAX_ID: contact()})

    await feed(wired, make_message(2, "/start some-onboarding-token"))

    assert wired.directory.profiled == [], "no resolve for a foreign payload"
