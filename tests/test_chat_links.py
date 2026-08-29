"""Where "open the chat" points, and why it is still a username.

A bot deleted and rebuilt at the same deterministic name leaves every `t.me/`
tap opening the dead peer. That is a real client-side defect, read out of the
clients' own source:

* tdesktop — `SessionNavigation::resolveUsername` returns
  `_session->data().peerByUsername(username)` and returns *without asking the
  server*; `peerByUsername` checks nothing but `isLoaded()`.
* TDLib — `USERNAME_CACHE_EXPIRE_TIME = 86400`, and `resolve_dialog_username`
  returns the cached `dialog_id` **even when expired**, firing the refresh into
  a discarded promise.

`tg://user?id=` bypasses all of it — `LinkManager::get_link_user_id` parses it
into a `MentionName` entity carrying a `UserId`, so the username resolver is
never reached. It was tried, for one deploy, and Telegram refused it: the Bot
API allows that form to "mention a **user**", a bot is not one, and the whole
keyboard is rejected with the button — which took the guardian's «Мосты» screen
off the air entirely.

So the link stays a username, the client-side staleness stays Telegram's, and
these tests pin the shape that actually works.
"""

from __future__ import annotations

from bridge.provisioning.selection import bot_link


def test_the_link_is_a_username_even_when_the_id_is_known() -> None:
    """`tg://user?id=` in a button is refused for a bot, and the refusal takes
    the whole keyboard with it."""
    assert bot_link("example_contact_max_bot", 9000000005) == "https://t.me/example_contact_max_bot"
    assert bot_link("example_contact_max_bot") == "https://t.me/example_contact_max_bot"


def test_the_id_stays_in_the_signature_on_purpose() -> None:
    """Every caller has it. The day Telegram accepts an id-shaped button, this
    is the one line that changes."""
    import inspect

    assert "bot_id" in inspect.signature(bot_link).parameters


def test_the_bridge_card_opens_the_chat() -> None:
    from bridge.onboarding import screens

    item = type("Card", (), {
        "max_chat_id": 1,
        "title": "Наталья",
        "username": "example_contact_max_bot",
        "bot_id": 9000000005,
    })()

    from tests.fake_provisioning import fake_bridge_view

    rendered = str(screens.bridge_screen(fake_bridge_view(item))[1])

    assert "https://t.me/example_contact_max_bot" in rendered
    assert "tg://user" not in rendered, "a button Telegram refuses draws no screen at all"


def test_the_bridge_list_opens_the_card_and_the_card_opens_the_chat() -> None:
    """The list no longer carries a link column; the card carries the link."""
    from bridge.onboarding import screens
    from bridge.provisioning.flow import BridgeSummary
    from tests.fake_provisioning import fake_bridge_view

    item = BridgeSummary(
        title="Наталья", username="example_contact_max_bot", bridge_name="qbn",
        max_chat_id=1, bot_id=9000000005,
    )

    listed = str(screens.bridges_screen([fake_bridge_view(item)])[1])
    carded = str(screens.bridge_screen(fake_bridge_view(item))[1])

    assert screens.bridge_callback(1) in listed
    assert "https://t.me/example_contact_max_bot" in carded
    assert "tg://user" not in listed + carded


def test_every_button_url_is_one_telegram_accepts() -> None:
    """The regression in one assertion: a keyboard is rejected whole.

    One refused button does not degrade to a screen with one button missing —
    the edit fails, the post fails, and the owner taps «Мосты» and gets nothing.
    """
    from bridge.onboarding import screens
    from bridge.provisioning.flow import BridgeSummary
    from bridge.provisioning.journal import ItemState, JournalEntry
    from bridge.provisioning.selection import result_markup

    item = type("Card", (), {
        "max_chat_id": 1, "title": "Н", "username": "example_contact_max_bot", "bot_id": 42,
    })()
    entry = JournalEntry(
        max_chat_id=1, expected_username="example_contact_max_bot", title="Н",
        state=ItemState.HEALTHY, telegram_bot_id=42,
    )
    summary = BridgeSummary(
        title="Н", username="example_contact_max_bot", bridge_name="qbn", max_chat_id=1, bot_id=42
    )

    from tests.fake_provisioning import fake_bridge_view

    keyboards = [
        screens.bridge_screen(fake_bridge_view(item))[1],
        screens.bridges_screen([fake_bridge_view(summary)])[1],
        result_markup([entry], epoch=1),
        screens.free_slot(item, can_delete=True, can_wipe=True, revision=1)[1],
    ]

    for markup in keyboards:
        for row in markup.inline_keyboard:
            for button in row:
                if button.url is None:
                    continue
                assert button.url.startswith("https://t.me/"), button.url
