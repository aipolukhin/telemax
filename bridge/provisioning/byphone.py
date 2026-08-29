"""Adding a contact the owner names, instead of waiting to be written to.

Telemax exists for people who do not install MAX. An account registered through
web/QR arrives empty — no address book, no dialogs — and until now a bridge could
only be made *after* the other person wrote first. For "get my father into this"
that is not a path at all: he does not know he has to write, and may have nowhere
to write from.

So there are three ways into the same confirmation screen, and they differ only
in where the contact comes from:

    ➕ Добавить контакт
    ├─ выбрать из диалогов MAX      — the picker, unchanged
    ├─ прикрепить контакт Telegram  — `message.contact`
    └─ ввести номер вручную         — a line of text

Rules that are conditions, not preferences (`docs/architecture/proactive-contact-onboarding.md`):

* **nothing is sent to the person.** No "test message", not once, under no
  setting. A tool that writes to a relative to check the wiring will one day
  write to the wrong relative.
* **one number at a time.** The search takes a single number and the import — the
  fallback, behind its own separate yes — takes a single contact. There is no
  bulk form anywhere in this file, and that is deliberate.
* **the number is masked everywhere it is shown**, and it is never stored: it
  lives in the draft below until the screen it belongs to is answered.
* **an ambiguous answer creates nothing.** Not-found, no-dialog and
  already-bridged each get their own honest screen rather than a guess.

**A dialog that does not exist yet is still addressable**, which is what makes
this feature worth having at all. The id of a personal MAX dialog is not issued
by the server: it is `viewer_id ^ peer_id`, computed on the client. MAX's own web
client does exactly that —

    static resolveChatId(e, t) { return R.resolveId(e ^ t) }
    get chatId() { return e.resolveChatId(V.profile.viewer.id, this.id) }

— and when that chat is not in its cache it *synthesises* one (`onNotFound`,
participants `{chatId ^ viewer.id, viewer.id}`) so the owner can type into it.
The first message is an ordinary `MSG_SEND` (opcode 64) to that id; there is no
create-chat call anywhere in the send path. Checked against the live account as
well: `own ^ peer == chat.id` on 47 personal dialogs out of 47.

So the owner adds their father by number, gets a bot, and writes to it. That
message creates the dialog in MAX. Nobody has to be asked to write first, and
still nothing is sent without the owner typing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bridge.max_client import MaxContact
from bridge.phone import mask as mask_phone
from bridge.phone import normalize as normalize_phone
from bridge.telegram.design import esc, title

#: The three-way entry screen, and the two paths that need something typed or
#: attached. One namespace with the rest of provisioning, because the guardian's
#: catch-all `prov:` handler is what these have to be recognised *before*.
ADD = "prov:add"
BY_PHONE = "prov:add:phone"
BY_CONTACT = "prov:add:contact"

#: The `/start` deep-link payload that opens the guardian straight on "bridge
#: this person", carrying the MAX user id. Telegram allows `[A-Za-z0-9_-]{0,64}`
#: in a start payload, and a decimal id fits with room to spare. The prefix is
#: what tells this deep link apart from onboarding's own token, which shares the
#: parameter.
BRIDGE_DEEP_LINK_PREFIX = "mb"


def bridge_deep_link_payload(user_id: int) -> str:
    return f"{BRIDGE_DEEP_LINK_PREFIX}{int(user_id)}"


def parse_bridge_deep_link(payload: str) -> int | None:
    """The MAX user id inside a bridge deep link, or None if it is not one.

    None for anything that is not `mb<ascii digits>` — an onboarding token, an
    empty start, a hand-typed word. The caller then treats it as an ordinary
    start.

    ASCII specifically. `str.isdigit()` is true of `²` and of `١`, and `int()`
    refuses the first while silently accepting the second: `/start mb²` used to
    raise a `ValueError` inside the handler, which is a tap that does nothing
    and says nothing, and `/start mb١٢` resolved to the user id 12.
    """
    text = (payload or "").strip()
    if not text.startswith(BRIDGE_DEEP_LINK_PREFIX):
        return None
    digits = text[len(BRIDGE_DEEP_LINK_PREFIX):]
    if not ascii_digits(digits):
        return None
    return int(digits)
#: `<CONFIRM>:<epoch>` — the epoch is the draft this tap belongs to, so a button
#: from a screen the owner has already moved past does nothing.
CONFIRM = "prov:add:ok"
IMPORT = "prov:add:import"
#: One row of the picker shown when a shared card turns out to hold several
#: people. Carries the index into the draft's candidates, not a phone number.
PICK = "prov:add:pick"
# `CANCEL = "prov:add:no"` was here. It was defined, never rendered by anything,
# and would have landed on the `prov:` catch-all and answered «Не понял кнопку.»
# if it ever had been. Every cancel in this flow goes to `ADD`, which is a real
# screen; a constant nothing draws is a promise the code does not keep.

#: `onboarding.screens.MENU`, by value rather than by import: `screens` imports
#: this module for the button that opens it, and importing back would be a cycle.
#: `test_add_contact.py` asserts the two are the same string, so this cannot rot
#: quietly.
HOME = "onb:menu"

BACK = "← Назад"

HEADING = "Добавить контакт"

ASK_PHONE_LINES = (
    "Пришлите номер телефона одним сообщением.",
    "",
    "Можно как угодно: <code>+7 902 837-73-56</code>, <code>89028377356</code>.",
    "",
    "Я только найду этого человека в MAX и покажу вам, кто это. "
    "Ничего ему не отправлю.",
)

ASK_CONTACT_LINES = (
    "Прикрепите контакт: скрепка → <b>Контакт</b> → выберите человека.",
    "",
    "Возьму из него имя и номер, найду в MAX и покажу, кто нашёлся. "
    "Сообщение ему не уйдёт.",
)

ASK_NAME_LINES = (
    "Этого номера нет в вашем MAX.",
    "",
    "Могу добавить <b>только этот один контакт</b> в ваш аккаунт MAX и поискать снова.",
    "MAX получит указанное имя и номер.",
    "",
    "Пришлите имя одним сообщением — или отмените.",
)

BAD_PHONE_LINES = (
    "Это не похоже на номер телефона.",
    "",
    "Нужен международный формат: <code>+7…</code>. Российские <code>8…</code> тоже пойму.",
)

#: Said when the number resolves to the owner. Not an error worth a screen full
#: of explanation — but creating that bridge would be a bot talking to itself.
SELF_LINES = ("Это ваш собственный номер.",)

NOT_RUNNING = "Мост ещё не запущен — сначала подключите MAX."
NO_DIRECTORY = "Поиск по номеру недоступен: нет сессии MAX."

STALE_ALERT = "Экран уже обновился — начните добавление заново."


class ContactDirectory(Protocol):
    """The MAX side of adding somebody by number.

    Two methods and a property, so the flow can be tested without a MAX session
    and — more to the point — so the *only* write in this feature is a method
    with a name that says it writes.
    """

    @property
    def own_user_id(self) -> int | None: ...

    async def search_by_phone(self, phone: str) -> MaxContact | None: ...

    async def import_contact(self, phone: str, name: str) -> MaxContact | None: ...

    async def contact_profile(self, user_id: int) -> MaxContact | None: ...


class Verdict(Enum):
    """What the search made of a number. One screen each, no overlaps."""

    BAD_PHONE = "bad_phone"
    SELF = "self"
    NOT_FOUND = "not_found"
    ALREADY_BRIDGED = "already_bridged"
    #: Found in MAX with no dialog yet. Still bridgeable: the chat id is a
    #: function of the two user ids, and the owner's first message creates the
    #: conversation on the server.
    NEW_DIALOG = "new_dialog"
    #: Found, dialog exists, no bridge on it.
    READY = "ready"


@dataclass(frozen=True, slots=True)
class Resolution:
    """The answer to one number, and everything a screen needs to draw it."""

    verdict: Verdict
    phone: str | None = None
    contact: MaxContact | None = None
    max_chat_id: int | None = None
    #: The name of the bridge that already carries this person, for the refusal.
    bridged_as: str | None = None

    @property
    def display_name(self) -> str:
        if self.contact is not None and self.contact.display_name:
            return self.contact.display_name
        return "без имени"


class Awaiting(Enum):
    NOTHING = "nothing"
    PHONE = "phone"
    #: A name for the single-contact import, asked only after a search found
    #: nobody and only on the manual path — an attached Telegram contact
    #: brought one with it.
    NAME = "name"


@dataclass
class ContactDraft:
    """The contact being added right now, and nothing that outlives the screen.

    Deliberately in memory: the plan says the number is kept only while it is
    needed, and "needed" ends when the owner answers the screen it is on. A
    restart loses the draft, which is the correct amount of loss — it costs one
    retyped number and stores no phone number anywhere.

    The epoch is what makes a stale tap harmless. Every new draft bumps it, and
    the confirm button carries the number it was drawn with.
    """

    epoch: int = 0
    awaiting: Awaiting = Awaiting.NOTHING
    phone: str | None = None
    name: str | None = None
    resolution: Resolution | None = None
    #: Everyone a shared card turned out to reach, when it reached more than one.
    #: Held only while the picker is on screen, like the number itself.
    candidates: list[Resolution] = field(default_factory=list)

    def restart(self, *, awaiting: Awaiting = Awaiting.NOTHING) -> int:
        self.epoch += 1
        self.awaiting = awaiting
        self.phone = None
        self.name = None
        self.resolution = None
        self.candidates = []
        return self.epoch

    def choose(self, index: int) -> Resolution | None:
        """Promote one candidate to *the* resolution. None when out of range."""
        if not 0 <= index < len(self.candidates):
            return None
        chosen = self.candidates[index]
        self.resolution = chosen
        self.phone = chosen.phone
        self.candidates = []
        return chosen

    def clear(self) -> None:
        self.restart()

    def holds(self, epoch: int) -> bool:
        return epoch == self.epoch and self.resolution is not None


# ------------------------------------------------------------------ callbacks


def confirm_callback(*, epoch: int) -> str:
    return f"{CONFIRM}:{epoch}"


def pick_callback(index: int, *, epoch: int) -> str:
    """Which candidate, by position — never by number.

    A phone number in a callback is a phone number in Telegram's servers, in the
    client's cache and in every log line that prints the tap. The index means
    nothing to anybody who does not already hold the draft.
    """
    return f"{PICK}:{epoch}:{index}"


def import_callback(*, epoch: int) -> str:
    return f"{IMPORT}:{epoch}"


def ascii_digits(text: str, *, signed: bool = False) -> bool:
    """Whether `int()` will accept this and mean what the caller thinks.

    `str.isdigit()` is not that test: it is true of superscripts, which `int()`
    refuses, and of other scripts' digits, which it accepts.
    """
    body = text[1:] if signed and text.startswith("-") else text
    return bool(body) and body.isascii() and body.isdigit()


def parse_pick(data: str) -> tuple[int, int] | None:
    """`prov:add:pick:<epoch>:<index>` — or None when it is not one."""
    if not data.startswith(f"{PICK}:"):
        return None
    parts = data[len(PICK) + 1 :].split(":")
    if len(parts) != 2 or not all(ascii_digits(part) for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def parse_epoch(data: str, prefix: str) -> int | None:
    if not data.startswith(f"{prefix}:"):
        return None
    tail = data[len(prefix) + 1 :]
    return int(tail) if ascii_digits(tail, signed=True) else None


def is_personal_chat(max_chat_id: int) -> bool:
    """Whether a MAX chat id can be a personal dialog at all.

    A personal dialog's id is `own ^ peer` of two positive user ids, so it is
    positive; MAX gives groups and channels negative ids. Observed on the live
    account: forty-seven personal dialogs, all positive, and a negative group id
    that the guardian offered a «Создать» button for — a button
    that could only ever answer «личных диалогов не нашлось», because the picker
    filters on the chat's own type and never sees it.

    Kept as arithmetic rather than a type lookup on purpose: the announcement
    path has a chat id and no chat object, and asking MAX for one to refuse a
    group would be a round trip to say no.
    """
    return max_chat_id > 0


def dm_chat_id(own_user_id: int, peer_user_id: int) -> int:
    """The id of the personal dialog between two people — with or without one.

    Not a guess and not PyMax's invention: MAX's own web client computes it the
    same way (`resolveChatId(e, t) => e ^ t`), and it matches every personal
    dialog on the live account. Symmetric, so both sides address the same chat,
    which is what keeps one conversation on one bridge.
    """
    return own_user_id ^ peer_user_id


def normalize(value: str) -> str | None:
    """The one definition of "that is a phone number", shared with setup."""
    return normalize_phone(value)


def phones_from_card(phone_number: str | None, vcard: str | None) -> list[str]:
    """Every number a shared contact card carries, normalised and deduplicated.

    A Telegram `Contact` has exactly one `phone_number`, and a person routinely
    has three. The others are in the vCard, in `TEL` lines the flow never read —
    so a card with a mobile, a work number and a landline was searched for on
    whichever one Telegram put in the structured field, and the owner was told
    the contact is not in MAX when they are, under another number.

    `phone_number` comes first: it is the one Telegram itself considers primary,
    and when only one of the three is in MAX the order decides nothing, but when
    several are it decides which is offered at the top.

    Folded lines are joined first — a vCard may wrap a long value onto the next
    line with a leading space, and a number split in half normalises to nothing.
    """
    numbers: list[str] = []
    seen: set[str] = set()

    def take(raw: str) -> None:
        normalised = normalize(raw)
        if normalised is not None and normalised not in seen:
            seen.add(normalised)
            numbers.append(normalised)

    if phone_number:
        take(phone_number)

    unfolded = (vcard or "").replace("\r\n ", "").replace("\n ", "").replace("\r\n\t", "")
    for line in unfolded.splitlines():
        name, _, value = line.partition(":")
        # `TEL`, `TEL;TYPE=CELL`, `item1.TEL;TYPE=WORK` — the property name is
        # what matters, and it is whatever precedes the first `;` after any
        # grouping prefix.
        prop = name.split(";", 1)[0].strip().upper()
        if prop.endswith("TEL") and value.strip():
            take(value)
    return numbers


def mask(value: str) -> str:
    return mask_phone(value)


# -------------------------------------------------------------------- screens


def _keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


Screen = tuple[str, InlineKeyboardMarkup | None]


def refusal(text: str, *, retry: str | None = None) -> Screen:
    """A refusal with a way out of it.

    Eight of these were drawn into the anchor with `reply_markup=None`: «Мост ещё
    не запущен», «Поиск по номеру недоступен», «В MAX не нашлось личных
    диалогов», and the rest. Each one replaced the owner's whole control surface
    with a sentence and nothing to tap, and the only escape was typing `/menu` —
    which none of them mentioned.
    """
    rows = [[("Попробовать снова", retry)]] if retry else []
    return (
        title(HEADING, esc(text)),
        _keyboard(*rows, [(BACK, HOME)]),
    )


def menu(*, dialogs: str) -> Screen:
    """The three ways in. `dialogs` is the picker's own callback, passed in.

    Passed rather than imported for the same reason `HOME` is a literal: this
    module is imported by the screen that offers it, and the picker is imported
    by that screen too.
    """
    return (
        title(
            HEADING,
            esc("Кого добавляем?"),
            "",
            esc("Никому ничего не отправлю — только найду и покажу."),
        ),
        _keyboard(
            [("Из диалогов MAX", dialogs)],
            [("Прикрепить контакт Telegram", BY_CONTACT)],
            [("Ввести номер вручную", BY_PHONE)],
            [(BACK, HOME)],
        ),
    )


def ask_phone() -> Screen:
    return (
        title(HEADING, *ASK_PHONE_LINES),
        _keyboard([(BACK, ADD)]),
    )


def ask_contact() -> Screen:
    return (
        title(HEADING, *ASK_CONTACT_LINES),
        _keyboard([(BACK, ADD)]),
    )


def bad_phone() -> Screen:
    return (
        title(HEADING, *BAD_PHONE_LINES),
        _keyboard([("Ввести ещё раз", BY_PHONE)], [(BACK, ADD)]),
    )


def _photo_row(contact: MaxContact | None) -> list[InlineKeyboardButton]:
    """The avatar as a link, not as a second message.

    The guardian owns exactly one message and edits it; sending a photo beside it
    would leave two screens in the chat, which is the thing the anchor exists to
    prevent. A URL button costs nothing and opens the same picture.
    """
    url = contact.avatar_url if contact is not None else None
    return [InlineKeyboardButton(text="Фото контакта ↗", url=url)] if url else []


def several(candidates: list[Resolution], *, epoch: int) -> Screen:
    """A shared card reached more than one person. Ask which one.

    Nothing is guessed. Before this the flow searched whichever number Telegram
    put in the structured field, so a card with a mobile, a work number and a
    landline could resolve to the wrong person entirely — or, worse, resolve to
    nobody and be reported as "not in MAX" while the contact was there under
    another number.

    Named, not numbered. The resolved MAX profile name is what the owner
    recognises; the masked number is there only to tell two entries apart when
    the same person appears twice.
    """
    lines = [
        esc(f"На этой карточке {len(candidates)} номера в MAX." if len(candidates) < 5
            else f"На этой карточке {len(candidates)} номеров в MAX."),
        "",
        esc("Выберите, с кем поднять мост."),
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item.display_name} · {mask(item.phone or '')}"[:64],
                callback_data=pick_callback(index, epoch=epoch),
            )
        ]
        for index, item in enumerate(candidates)
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=ADD)])
    return title("Несколько номеров", *lines), InlineKeyboardMarkup(inline_keyboard=rows)


def found(resolution: Resolution, *, epoch: int) -> Screen:
    """Who was found, and — only when there is something to bridge — the button."""
    contact = resolution.contact
    phone = resolution.phone or ""
    lines = [
        f"<b>{esc(resolution.display_name)}</b>",
        esc(mask(phone)) if phone else "",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    photo = _photo_row(contact)
    if photo:
        rows.append(photo)

    if resolution.verdict is Verdict.ALREADY_BRIDGED:
        lines.append(esc("Мост с этим человеком уже есть — второй не нужен."))
    else:
        if resolution.verdict is Verdict.READY:
            lines.append(esc("Диалог в MAX есть. Сделать для него отдельного бота?"))
        else:
            lines += [
                esc("Переписки с ним у вас пока нет — она начнётся с вашего сообщения."),
                "",
                esc(
                    "Сделаю бота. Первое, что вы напишете этому боту, придёт ему в MAX; "
                    "до этого он ничего не получит и не узнает."
                ),
            ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="Создать мост", callback_data=confirm_callback(epoch=epoch)
                )
            ]
        )

    rows.append([InlineKeyboardButton(text=BACK, callback_data=ADD)])
    return title(HEADING, *lines), InlineKeyboardMarkup(inline_keyboard=rows)


def not_found(phone: str, *, epoch: int, name: str | None) -> Screen:
    """Nobody by that number — offer the one import, or ask for a name first.

    Two screens in one function because they are the same moment: the import is
    only ever offered with the name it is about to send.
    """
    masked = esc(mask(phone))
    if name is None:
        return (
            title(HEADING, masked, "", *ASK_NAME_LINES),
            _keyboard([("Отмена", ADD)]),
        )
    return (
        title(
            HEADING,
            f"<b>{esc(name)}</b>",
            masked,
            "",
            esc(
                "Этого номера нет в вашем MAX. Могу импортировать только этот один "
                "контакт в ваш аккаунт MAX и повторить поиск. MAX получит указанное "
                "имя и номер."
            ),
        ),
        _keyboard(
            [("Импортировать один контакт", import_callback(epoch=epoch))],
            [("Отмена", ADD)],
        ),
    )


def import_failed(phone: str, name: str) -> Screen:
    """The end of the road: the import went through and MAX still knows nobody.

    A terminal screen rather than the same offer again — a button that has just
    failed and is redrawn identically reads as "press it harder".
    """
    return (
        title(
            HEADING,
            f"<b>{esc(name)}</b>",
            esc(mask(phone)),
            "",
            esc(
                "Контакт добавлен в ваш MAX, но самого человека в MAX нет — "
                "аккаунта с этим номером не существует."
            ),
            "",
            esc(
                "Писать некому: без аккаунта MAX сообщение доставить нельзя. "
                "Проверьте номер или попробуйте позже."
            ),
        ),
        _keyboard([(BACK, ADD)]),
    )


def self_number() -> Screen:
    return (
        title(HEADING, *SELF_LINES),
        _keyboard([(BACK, ADD)]),
    )


def screen_for(resolution: Resolution, *, epoch: int, name: str | None = None) -> Screen:
    """One verdict, one screen. The router does not choose; it draws this."""
    if resolution.verdict is Verdict.BAD_PHONE:
        return bad_phone()
    if resolution.verdict is Verdict.SELF:
        return self_number()
    if resolution.verdict is Verdict.NOT_FOUND:
        return not_found(resolution.phone or "", epoch=epoch, name=name)
    return found(resolution, epoch=epoch)
