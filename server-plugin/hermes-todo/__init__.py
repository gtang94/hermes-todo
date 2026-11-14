"""Hermes plugin registration for Hermes Todo."""

from __future__ import annotations

from . import todo_tool, personal_todo
from .cli import register_cli, todo_command


def register(ctx) -> None:
    ctx.register_cli_command(
        name="todo",
        help="Read and update the shared Hermes Todo board",
        setup_fn=register_cli,
        handler_fn=todo_command,
        description="Agent-writable CLI for the shared Hermes Todo task board.",
    )
    ctx.register_tool(
        name="todo_board",
        toolset="hermes-todo",
        schema=todo_tool.SCHEMA,
        handler=todo_tool.todo_board,
        check_fn=lambda: not personal_todo.enabled(),
        description=(
            "Read and write the shared Hermes Todo board: list, get, create, "
            "update any task settings, reorder within/between categories, "
            "start (Now), complete tasks, and manage subtask checklists. "
            "Delete of parent tasks is intentionally absent."
        ),
        emoji="\U0001f4dd",
        is_async=False,
    )

    ctx.register_tool(
        name="personal_todo", toolset="personal-todo", schema=personal_todo.SCHEMA,
        handler=personal_todo.tool, check_fn=personal_todo.enabled,
        description="个人待办与钉钉提醒", emoji="📝", is_async=False,
    )
