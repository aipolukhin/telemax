"""A shared contact card carries every number on it, not just the first.

Telegram's `Contact` has one structured `phone_number`; a person routinely has
three, and the other two are in the vCard. Reading only the first is how a
contact who *is* in MAX under their work number was reported as not being in
MAX at all.
"""

from __future__ import annotations

import pytest

from bridge.provisioning.byphone import (
    ContactDraft,
    Resolution,
    Verdict,
    parse_pick,
    phones_from_card,
    pick_callback,
    several,
)

pytestmark = pytest.mark.asyncio

THREE = """BEGIN:VCARD
VERSION:3.0
N:Иванов;Пётр
TEL;TYPE=CELL:+7 999 111-22-33
TEL;TYPE=WORK:+7 495 123-45-67
item1.TEL;TYPE=HOME:+7 (999) 444 55 66
EMAIL:x@y.z
END:VCARD"""


# ---------------------------------------------------------------- the parsing


async def test_every_number_on_the_card_is_read() -> None:
    assert phones_from_card("+79991112233", THREE) == [
        "+79991112233",
        "+74951234567",
        "+79994445566",
    ]


async def test_the_structured_number_comes_first() -> None:
    """Telegram calls it primary, and when several are in MAX it decides the
    order the owner is offered them in."""
    assert phones_from_card("+74951234567", THREE)[0] == "+74951234567"


async def test_the_same_number_twice_is_one_number() -> None:
    card = "BEGIN:VCARD\nTEL:+7 999 111 22 33\nTEL;TYPE=CELL:+79991112233\nEND:VCARD"
    assert phones_from_card("+79991112233", card) == ["+79991112233"]


async def test_a_folded_line_is_joined_before_it_is_read() -> None:
    """A vCard may wrap a long value onto the next line with a leading space;
    half a number normalises to nothing."""
    assert phones_from_card(None, "BEGIN:VCARD\nTEL:+7 999 111\n 22 33\nEND:VCARD") == [
        "+79991112233"
    ]


async def test_things_that_are_not_numbers_are_not_taken() -> None:
    card = "BEGIN:VCARD\nEMAIL:+notaphone\nNOTE:TEL:+79990000000\nTEL:мусор\nEND:VCARD"
    assert phones_from_card(None, card) == []


async def test_a_card_with_no_vcard_still_works() -> None:
    assert phones_from_card("+79991112233", None) == ["+79991112233"]
    assert phones_from_card(None, None) == []


# ----------------------------------------------------------------- the picker


def _found(phone: str, name: str) -> Resolution:
    from bridge.max_client import MaxContact

    return Resolution(
        verdict=Verdict.READY,
        phone=phone,
        contact=MaxContact(user_id=abs(hash(phone)) % 10_000, display_name=name),
    )


async def test_the_picker_names_people_not_numbers() -> None:
    """The resolved MAX profile name is what the owner recognises. The masked
    number is only there to tell two entries apart."""
    text, markup = several(
        [_found("+79991112233", "Пётр Иванов"), _found("+74951234567", "Пётр (работа)")],
        epoch=3,
    )

    assert "2 номера в MAX" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Пётр Иванов" in labels[0]
    assert "Пётр (работа)" in labels[1]
    assert "1112233" not in str(markup), "a full number never reaches a button"


async def test_a_tap_carries_a_position_never_a_number() -> None:
    """A phone number in a callback is a phone number in Telegram's servers, in
    the client's cache, and in every log line that prints the tap."""
    data = pick_callback(1, epoch=7)

    assert parse_pick(data) == (7, 1)
    assert "+" not in data, "no phone number in the callback"


@pytest.mark.parametrize("data", ["prov:add:pick:7", "prov:add:pick:a:1", "prov:add", ""])
async def test_a_malformed_tap_is_not_guessed_at(data: str) -> None:
    assert parse_pick(data) is None


# ------------------------------------------------------------------ the draft


async def test_choosing_promotes_one_candidate_and_drops_the_rest() -> None:
    draft = ContactDraft()
    draft.candidates = [_found("+79991112233", "Пётр"), _found("+74951234567", "Пётр рабочий")]

    chosen = draft.choose(1)

    assert chosen is not None
    assert draft.resolution is chosen
    assert draft.phone == "+74951234567"
    assert draft.candidates == [], "the others are not kept a moment longer"


async def test_an_index_out_of_range_chooses_nothing() -> None:
    draft = ContactDraft()
    draft.candidates = [_found("+79991112233", "Пётр")]

    assert draft.choose(5) is None
    assert draft.resolution is None


async def test_a_restart_forgets_the_candidates() -> None:
    """They are phone numbers. They live exactly as long as the screen does."""
    draft = ContactDraft()
    draft.candidates = [_found("+79991112233", "Пётр")]

    draft.restart()

    assert draft.candidates == []


# --------------------------------------------------- how many people it reaches


async def test_one_hit_among_three_numbers_costs_no_extra_tap() -> None:
    """Only a card that reaches more than one person is worth a question."""
    from bridge.provisioning.flow import DialogFlow

    hits = {"+74951234567": _found("+74951234567", "Пётр")}

    class Flow:
        async def resolve_phone(self, raw: str) -> Resolution:
            return hits.get(raw, Resolution(verdict=Verdict.NOT_FOUND, phone=raw))

    flow = DialogFlow.__new__(DialogFlow)
    flow.resolve_phone = Flow().resolve_phone  # type: ignore[method-assign]

    results = await flow.resolve_card(["+79991112233", "+74951234567", "+79994445566"])
    reachable = [r for r in results if r.verdict is not Verdict.NOT_FOUND]

    assert len(results) == 3, "every number was searched"
    assert len(reachable) == 1
    assert reachable[0].phone == "+74951234567"


async def test_no_hits_still_reports_against_the_first_number() -> None:
    """The one Telegram calls primary, and the one worth retyping."""
    from bridge.provisioning.flow import DialogFlow

    class Flow:
        async def resolve_phone(self, raw: str) -> Resolution:
            return Resolution(verdict=Verdict.NOT_FOUND, phone=raw)

    flow = DialogFlow.__new__(DialogFlow)
    flow.resolve_phone = Flow().resolve_phone  # type: ignore[method-assign]

    results = await flow.resolve_card(["+79991112233", "+74951234567"])

    assert all(r.verdict is Verdict.NOT_FOUND for r in results)
    assert results[0].phone == "+79991112233"
