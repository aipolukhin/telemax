"""Creating a bot: the one thing Bot API has no method for.

@BotFather is itself a bot, and bots cannot talk to bots, so automation means a
*user* session over MTProto — a second credential with the weight of a whole
account. The transport is thin and untestable here; what is tested is the part
that actually breaks, which is the prose: BotFather answers in sentences and
hides the token inside one of them.

The rule these tests protect is "stop rather than guess". A wrong guess leaves a
half-created bot and a token nobody wrote down.
"""

from __future__ import annotations

from enum import Enum

from bridge.provisioning.botfather import DELETE_CONFIRMATION, Reply, parse, redact
from bridge.provisioning.picker import DialogOption, is_personal, rank
from bridge.provisioning.selection import parse_callback, select_callback

TOKEN = "9000000003:AAHnQzZ5r4v6WcE2sTgP1kLm9xYbVdNfQwE"

NEW_BOT = "Alright, a new bot. How are we going to call it? Please choose a name for your bot."
ASK_USERNAME = (
    "Good. Now let's choose a username for your bot. It must end in `bot`. "
    "Like this, for example: TetrisBot or tetris_bot."
)
DONE = (
    "Done! Congratulations on your new bot. You will find it at t.me/telemax_bot. "
    f"Use this token to access the HTTP API:\n{TOKEN}\nKeep your token secure."
)
TAKEN = "Sorry, this username is already taken. Please try something different."
INVALID_NAME = "Sorry, the name is invalid. Please try again."
#: A rate limit, not a capacity one. @BotFather usually names the wait; this
#: wording, without a number, is the case the code must still survive.
TOO_SOON = "Sorry, too many attempts. Please try again later."
TOO_SOON_62 = "Sorry, too many attempts. Please try again in 62 seconds."
#: Measured live on this account after five `/newbot` walks in a row.
TOO_SOON_58000 = "Sorry, too many attempts. Please try again in 58000 seconds."
LIMIT = "Sorry, you can't add more than 40 bots. To create a new bot, delete one of your bots."

CHOOSE_DELETE = "Choose a bot to delete."
CONFIRM_DELETE = (
    "OK, you selected @telemax_bot. Are you sure? "
    "Send 'Yes, I am totally sure.' to confirm."
)
DELETED = "Done! The bot is gone."


def test_each_step_of_the_dialogue_is_recognised() -> None:
    assert parse(NEW_BOT).kind is Reply.ASK_NAME
    assert parse(ASK_USERNAME).kind is Reply.ASK_USERNAME
    assert parse(TAKEN).kind is Reply.USERNAME_TAKEN
    assert parse(INVALID_NAME).kind is Reply.NAME_INVALID
    assert parse(LIMIT).kind is Reply.LIMIT


def test_a_rate_limit_is_not_a_full_account() -> None:
    """The two used to be one reply kind, and the difference decides everything.

    Waiting fixes one; only deleting a bot fixes the other. Reported as the same
    thing, an owner with thirty-three free slots is told to go and delete
    something because @BotFather asked them to slow down for a minute.
    """
    assert parse(LIMIT).kind is Reply.LIMIT
    assert parse("You have too many bots.").kind is Reply.LIMIT
    for text in (TOO_SOON, TOO_SOON_62, TOO_SOON_58000):
        assert parse(text).kind is Reply.TOO_SOON


def test_the_wait_is_read_from_the_sentence_not_guessed() -> None:
    """62 seconds and 58000 seconds are both real answers from this account.

    No single cooldown is right for both, so the number is taken from what
    @BotFather actually said.
    """
    assert parse(TOO_SOON_62).retry_after == 62
    assert parse(TOO_SOON_58000).retry_after == 58000
    assert parse("Please try again in 5 minutes").retry_after == 300
    assert parse("Retry after 2 hours").retry_after == 7200
    assert parse(TOO_SOON).retry_after is None, "not every refusal names one"


def test_the_plural_does_not_hide_the_number() -> None:
    """`seconds` — with the word boundary after `second`, every real refusal
    parsed as "no wait stated", which is the only wording Telegram ever sends."""
    assert parse("Please try again in 7 second").retry_after == 7
    assert parse("Please try again in 7 seconds").retry_after == 7


def test_the_token_is_pulled_out_of_the_prose() -> None:
    parsed = parse(DONE)
    assert parsed.kind is Reply.TOKEN
    assert parsed.token == TOKEN


def test_anything_unfamiliar_is_not_guessed_at() -> None:
    """BotFather's wording changes; a guess would strand a half-made bot."""
    assert parse("Что-то новое, чего мы не видели").kind is Reply.UNKNOWN
    assert parse("").kind is Reply.UNKNOWN


def test_a_token_never_survives_into_a_log() -> None:
    cleaned = redact(DONE)
    assert TOKEN not in cleaned
    assert "<token>" in cleaned


def test_every_step_of_the_deletion_dialogue_is_recognised() -> None:
    """`/deletebot` has three steps, and each needs a different answer sent."""
    assert parse(CHOOSE_DELETE).kind is Reply.ASK_DELETE_TARGET
    assert parse(CONFIRM_DELETE).kind is Reply.CONFIRM_DELETE
    assert parse(DELETED).kind is Reply.DELETED
    assert parse("Invalid bot selected").kind is Reply.NO_SUCH_BOT


def test_the_confirmation_is_the_exact_phrase_botfather_wants() -> None:
    """Anything else and BotFather simply does not delete, silently."""
    assert DELETE_CONFIRMATION == "Yes, I am totally sure."
    assert DELETE_CONFIRMATION.lower() in CONFIRM_DELETE.lower()


def test_a_finished_deletion_is_never_mistaken_for_an_unknown_reply() -> None:
    """UNKNOWN means "stop", which for a deleted bot would be the wrong move."""
    assert parse(DELETED).kind is not Reply.UNKNOWN


def test_there_is_no_fallback_username_generator() -> None:
    """Determinism has no near-miss: a taken name is information, not a retry."""
    from bridge.provisioning import botfather

    assert not hasattr(botfather, "next_username")


# ------------------------------------------------------------------- picking


class FakeChat:
    def __init__(self, chat_id: int, kind: object, last: int) -> None:
        self.id = chat_id
        self.type = kind
        self.last_event_time = last


class ChatType(str, Enum):  # noqa: UP042 - deliberately the shape PyMax uses
    DIALOG = "DIALOG"
    CHAT = "CHAT"


def test_only_personal_dialogs_are_offered() -> None:
    """A group is not a bridge: one bot is one person."""
    assert is_personal(FakeChat(1, ChatType.DIALOG, 0))
    assert not is_personal(FakeChat(2, ChatType.CHAT, 0))
    # And a plain string works too, because raw events carry those.
    assert is_personal(FakeChat(3, "DIALOG", 0))


def test_the_newest_dialogs_come_first_and_the_list_is_capped() -> None:
    chats = [FakeChat(index, ChatType.DIALOG, index) for index in range(1, 21)]

    top = rank(chats, exclude=set(), limit=5)

    assert [chat.id for chat in top] == [20, 19, 18, 17, 16]


def test_already_bridged_dialogs_are_not_offered_again() -> None:
    chats = [FakeChat(index, ChatType.DIALOG, index) for index in range(1, 6)]

    top = rank(chats, exclude={5, 4}, limit=10)

    assert [chat.id for chat in top] == [3, 2, 1]


def test_the_button_round_trips_its_chat_id_and_its_generation() -> None:
    from bridge.provisioning.selection import SELECT

    data = select_callback(236856064, epoch=4)
    assert parse_callback(data, SELECT) == (4, 236856064)
    # A callback from another namespace, or nonsense, is recognised as neither.
    assert parse_callback("prov:create:1", SELECT) is None
    assert parse_callback("nonsense", SELECT) is None


def test_an_option_carries_what_the_button_needs() -> None:
    option = DialogOption(max_chat_id=1, title="Иван", last_activity=5, max_user_id=7)
    assert option.title == "Иван"
    assert select_callback(option.max_chat_id, epoch=0).endswith(":1")
