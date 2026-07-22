from __future__ import annotations

from typing import Literal

from alembic import op

TriggerEvent = Literal["DELETE", "UPDATE"]


def create_reject_trigger(table_name: str, event: TriggerEvent, message: str) -> None:
    suffix = event.casefold()
    trigger_name = f"{table_name}_no_{suffix}"
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE {event} ON {table_name} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
        )
        return
    if dialect_name == "postgresql":
        function_name = f"{trigger_name}_fn"
        op.execute(
            f"CREATE FUNCTION {function_name}() RETURNS trigger AS $corvus_trigger$ "
            f"BEGIN RAISE EXCEPTION '{message}'; END; "
            "$corvus_trigger$ LANGUAGE plpgsql"
        )
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE {event} ON {table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )
        return
    raise RuntimeError(f"unsupported_migration_dialect:{dialect_name}")


def create_immutable_triggers(table_name: str, label: str) -> None:
    create_reject_trigger(table_name, "DELETE", f"{label} cannot be deleted")
    create_reject_trigger(table_name, "UPDATE", f"{label} are immutable")


def drop_reject_trigger(table_name: str, event: TriggerEvent) -> None:
    suffix = event.casefold()
    trigger_name = f"{table_name}_no_{suffix}"
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        op.execute(f"DROP TRIGGER {trigger_name}")
        return
    if dialect_name == "postgresql":
        op.execute(f"DROP TRIGGER {trigger_name} ON {table_name}")
        op.execute(f"DROP FUNCTION {trigger_name}_fn()")
        return
    raise RuntimeError(f"unsupported_migration_dialect:{dialect_name}")


def drop_immutable_triggers(table_name: str) -> None:
    drop_reject_trigger(table_name, "UPDATE")
    drop_reject_trigger(table_name, "DELETE")
