"""Pulling the old conversation in, once, without duplicates.

Two things make asking twice safe, and they are different mechanisms for a
reason. Delivery already dedups on the MAX message id, so a repeated import
writes nothing even if the cursor is lost. The cursor is what makes
«Импортировать только новые» a *different* operation rather than the same work
done again — and it is what lets an import interrupted halfway carry on.

The last property is isolation: a contact whose history MAX refuses to serve
gets its own line and the rest of the import continues.
"""

from __future__ import annotations

from bridge.provisioning.history import (
    DEFAULT_LIMIT,
    HistoryImporter,
    ImportReport,
    ImportTarget,
    progress_text,
    summary_text,
)
from tests.fake_provisioning import FakeBridgeRepository, FakeHistorySource


def targets(*names: str) -> list[ImportTarget]:
    return [
        ImportTarget(max_chat_id=100 + index, title=name, bridge_name=f"c{index}")
        for index, name in enumerate(names)
    ]


def source(**counts: int) -> FakeHistorySource:
    made = FakeHistorySource()
    for index, count in enumerate(counts.values()):
        made.messages[100 + index] = list(range(1, count + 1))
    return made


async def test_only_healthy_bridges_are_imported() -> None:
    """The offer does not exist before a bridge answers; nor does the import."""
    import tempfile
    from pathlib import Path

    from bridge.provisioning.flow import DialogFlow
    from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
    from tests.fake_provisioning import FakeDialogPicker, FakeGateway, FakeProvisioner

    with tempfile.TemporaryDirectory() as directory:
        journal = ProvisioningJournal.for_data_dir(Path(directory))
        journal.begin(
            [
                JournalEntry(1, "a_max_bot", "Мама"),
                JournalEntry(2, "b_max_bot", "Папа"),
            ]
        )
        journal.note(1, state=ItemState.HEALTHY, bridge_name="c1")
        journal.note(2, state=ItemState.FAILED_RETRYABLE)

        flow = DialogFlow(
            picker=FakeDialogPicker([]),  # type: ignore[arg-type]
            provisioner=FakeProvisioner(),
            gateway=FakeGateway(),
            journal=journal,
            bridges=FakeBridgeRepository(),
            telegram_owner_user_id=100000001,
        )
        assert [item.title for item in flow.healthy_targets()] == ["Мама"]


async def test_fifty_messages_is_the_default_depth() -> None:
    feed = source(mama=200)
    importer = HistoryImporter(source=feed, cursors=FakeBridgeRepository())

    reports = await importer.run(targets("Мама"))

    assert DEFAULT_LIMIT == 50
    assert reports[0].delivered == 50


async def test_the_order_is_the_order_it_happened() -> None:
    """Oldest first: a conversation replayed backwards is not a conversation."""
    feed = source(mama=5)
    importer = HistoryImporter(source=feed, cursors=FakeBridgeRepository())

    await importer.run(targets("Мама"))

    # The fake records the tail it was handed; the source slices it oldest-first.
    assert feed.messages[100] == [1, 2, 3, 4, 5]


async def test_the_cursor_is_stored_and_only_moves_forward() -> None:
    cursors = FakeBridgeRepository()
    importer = HistoryImporter(source=source(mama=5), cursors=cursors)

    await importer.run(targets("Мама"))
    assert cursors.cursors["c0"] == 5

    await cursors.set_history_cursor("c0", 3)
    assert cursors.cursors["c0"] == 5, "an older tail must not rewind the watermark"


async def test_a_second_import_delivers_nothing_new() -> None:
    cursors = FakeBridgeRepository()
    feed = source(mama=5)
    importer = HistoryImporter(source=feed, cursors=cursors)

    await importer.run(targets("Мама"))
    again = await importer.run(targets("Мама"), only_new=True)

    assert again[0].delivered == 0
    assert feed.calls[-1] == (100, 5), "it asked only for what came after the cursor"


async def test_only_new_messages_can_be_asked_for() -> None:
    cursors = FakeBridgeRepository()
    feed = source(mama=5)
    importer = HistoryImporter(source=feed, cursors=cursors)
    await importer.run(targets("Мама"))

    feed.messages[100] = list(range(1, 9))
    reports = await importer.run(targets("Мама"), only_new=True)

    assert reports[0].delivered == 3
    assert cursors.cursors["c0"] == 8


async def test_a_full_reimport_is_still_available() -> None:
    """Dedup makes it harmless, and sometimes it is what the owner wants."""
    cursors = FakeBridgeRepository()
    feed = source(mama=5)
    importer = HistoryImporter(source=feed, cursors=cursors)
    await importer.run(targets("Мама"))

    reports = await importer.run(targets("Мама"), only_new=False)

    assert reports[0].delivered == 5
    assert feed.calls[-1] == (100, None)


async def test_one_failed_chat_does_not_stop_the_others() -> None:
    feed = source(mama=5, papa=5, timur=5)
    feed.failures.add(101)
    importer = HistoryImporter(source=feed, cursors=FakeBridgeRepository())

    reports = await importer.run(targets("Мама", "Папа", "Иван"))

    assert [report.delivered for report in reports] == [5, 0, 5]
    assert reports[1].error is not None
    assert reports[2].done


async def test_progress_is_drawn_by_editing_one_message() -> None:
    drawn: list[str] = []

    async def progress(reports: list[ImportReport]) -> None:
        drawn.append(progress_text(reports))

    importer = HistoryImporter(
        source=source(mama=5, papa=5), cursors=FakeBridgeRepository(), progress=progress
    )
    await importer.run(targets("Мама", "Папа"))

    assert len(drawn) >= 3, "one initial draw and one per chat"
    assert "○ Папа" in drawn[0], "not started yet"
    assert "✅ Мама · 5 сообщений" in drawn[-1]
    assert "✅ Папа · 5 сообщений" in drawn[-1]


async def test_an_import_can_be_continued_after_a_restart() -> None:
    """The cursor is on disk, so a fresh importer picks up where this one left."""
    cursors = FakeBridgeRepository()
    feed = source(mama=5, papa=5)
    feed.failures.add(101)
    await HistoryImporter(source=feed, cursors=cursors).run(targets("Мама", "Папа"))

    assert cursors.cursors == {"c0": 5}, "only the chat that worked has a cursor"

    feed.failures.clear()
    reports = await HistoryImporter(source=feed, cursors=cursors).run(
        targets("Мама", "Папа"), only_new=True
    )

    assert reports[0].delivered == 0, "already done"
    assert reports[1].delivered == 5, "resumed"


async def test_already_imported_is_recognised() -> None:
    cursors = FakeBridgeRepository()
    importer = HistoryImporter(source=source(mama=5), cursors=cursors)
    people = targets("Мама")

    assert not await importer.already_imported(people)
    await importer.run(people)
    assert await importer.already_imported(people)


def test_the_summary_names_every_chat_and_its_count() -> None:
    reports = [
        ImportReport(target=targets("Иван")[0], delivered=50, done=True),
        ImportReport(target=targets("Мама")[0], delivered=47, done=True),
    ]
    text = summary_text(reports)

    assert "Иван · 50" in text
    assert "Мама · 47" in text
    assert "Traceback" not in text


# ------------------------------------------------- asked before, not after


async def test_the_tick_is_set_before_anything_is_created() -> None:
    """The import used to be a question after the run, which made it an errand.

    Two things are asserted together on purpose: the tick reaches the import, and
    the result screen stops offering «подтянуть историю» once it has happened.
    Offering it again is how somebody imports the same conversation twice.
    """
    import tempfile
    from pathlib import Path

    from bridge.provisioning import selection as ui
    from bridge.provisioning.flow import DialogFlow
    from bridge.provisioning.journal import ProvisioningJournal
    from bridge.provisioning.picker import DialogOption
    from tests.fake_provisioning import FakeDialogPicker, FakeGateway, FakeProvisioner

    with tempfile.TemporaryDirectory() as directory:
        options = [
            DialogOption(max_chat_id=100, title="Мама", last_activity=1, max_user_id=2000)
        ]
        feed = FakeHistorySource()
        feed.messages[100] = [1, 2, 3, 4, 5]
        flow = DialogFlow(
            picker=FakeDialogPicker(options),  # type: ignore[arg-type]
            provisioner=FakeProvisioner(),
            gateway=FakeGateway(),
            journal=ProvisioningJournal.for_data_dir(Path(directory)),
            bridges=FakeBridgeRepository(),
            telegram_owner_user_id=100000001,
            history_source=feed,
        )

        await flow.open()
        epoch = flow.selection.epoch
        await flow.toggle(epoch, 100)

        screen, alert = flow.toggle_history(epoch)
        assert alert is None
        assert screen is not None
        assert ui.HISTORY_ON in str(screen[1]), "the tick is drawn as ticked"
        assert flow.selection.history

        drawn: list[tuple[str, object]] = []

        async def draw(text: str, markup: object) -> None:
            drawn.append((text, markup))

        assert await flow.commit(epoch, draw) is None

        final_text, final_markup = drawn[-1]
        assert "Готово 🎉" in final_text
        assert "Подтянуто сообщений: 5" in final_text
        assert ui.HISTORY_ALL not in str(final_markup), "already done, do not offer it again"


async def test_without_the_tick_the_offer_survives() -> None:
    """Nothing was imported, so the button that does it is still the way to."""
    import tempfile
    from pathlib import Path

    from bridge.provisioning import selection as ui
    from bridge.provisioning.flow import DialogFlow
    from bridge.provisioning.journal import ProvisioningJournal
    from bridge.provisioning.picker import DialogOption
    from tests.fake_provisioning import FakeDialogPicker, FakeGateway, FakeProvisioner

    with tempfile.TemporaryDirectory() as directory:
        options = [
            DialogOption(max_chat_id=100, title="Мама", last_activity=1, max_user_id=2000)
        ]
        feed = FakeHistorySource()
        feed.messages[100] = [1, 2, 3]
        flow = DialogFlow(
            picker=FakeDialogPicker(options),  # type: ignore[arg-type]
            provisioner=FakeProvisioner(),
            gateway=FakeGateway(),
            journal=ProvisioningJournal.for_data_dir(Path(directory)),
            bridges=FakeBridgeRepository(),
            telegram_owner_user_id=100000001,
            history_source=feed,
        )

        await flow.open()
        epoch = flow.selection.epoch
        await flow.toggle(epoch, 100)

        drawn: list[tuple[str, object]] = []

        async def draw(text: str, markup: object) -> None:
            drawn.append((text, markup))

        assert await flow.commit(epoch, draw) is None

        final_text, final_markup = drawn[-1]
        assert "Подтянуто сообщений" not in final_text
        assert ui.HISTORY_ALL in str(final_markup)
