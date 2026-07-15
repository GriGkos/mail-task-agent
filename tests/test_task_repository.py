from sqlalchemy import select

from app.db.models import Task, TaskEvent
from app.db.repositories import TaskRepository


async def test_set_status_marks_task_done_and_records_telegram_event(session):
    task = Task(title="Call supplier", description="Discuss delivery", status="inbox")
    session.add(task)
    await session.commit()

    repository = TaskRepository(session)
    updated = await repository.set_status(task.id, "done", "Задача отмечена выполненной.")
    await session.commit()

    assert updated.status == "done"
    assert updated.completed_at is not None
    event = (await session.scalars(select(TaskEvent))).all()
    assert len(event) == 1
    assert event[0].source == "telegram"
    assert event[0].old_values["status"] == "inbox"
    assert event[0].new_values["status"] == "done"


async def test_list_open_and_completed_are_separate(session):
    session.add_all(
        [
            Task(title="Open", status="inbox"),
            Task(title="Done", status="done"),
            Task(title="Cancelled", status="cancelled"),
        ]
    )
    await session.commit()

    repository = TaskRepository(session)

    assert [task.title for task in await repository.list_open()] == ["Open"]
    assert [task.title for task in await repository.list_completed()] == ["Done"]
