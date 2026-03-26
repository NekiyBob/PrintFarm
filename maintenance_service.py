from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import select

from maintenance_db import get_maintenance_session
from maintenance_models import MAINTENANCE_TIMEZONE, MaintenanceEvent, MaintenanceEventType


def create_maintenance_event(
    *,
    printer_id: str,
    event_type: MaintenanceEventType | str,
    event_at: datetime,
    performed_by: Optional[str] = None,
    note: Optional[str] = None,
    nozzle_diameter: Any = None,
    custom_type_name: Optional[str] = None,
    print_hours_snapshot: Any = None,
    print_count_snapshot: Optional[int] = None,
) -> MaintenanceEvent:
    normalized_event_type = _normalize_event_type(event_type)
    normalized_nozzle_diameter = _normalize_positive_decimal(
        nozzle_diameter,
        field_name="nozzle_diameter",
    )
    normalized_custom_type_name = _normalize_optional_text(custom_type_name)

    _validate_required_fields_for_event_type(
        event_type=normalized_event_type,
        nozzle_diameter=normalized_nozzle_diameter,
        custom_type_name=normalized_custom_type_name,
    )

    normalized_event = MaintenanceEvent(
        printer_id=_require_non_empty_text(printer_id, field_name="printer_id"),
        event_type=normalized_event_type,
        event_at=_normalize_datetime(event_at, field_name="event_at"),
        performed_by=_normalize_stored_text(performed_by),
        note=_normalize_stored_text(note),
        nozzle_diameter=normalized_nozzle_diameter,
        custom_type_name=normalized_custom_type_name,
        print_hours_snapshot=_normalize_non_negative_decimal(
            print_hours_snapshot,
            field_name="print_hours_snapshot",
        ),
        print_count_snapshot=_normalize_non_negative_int(
            print_count_snapshot,
            field_name="print_count_snapshot",
        ),
    )

    _validate_custom_type_name(
        event_type=normalized_event.event_type,
        custom_type_name=normalized_event.custom_type_name,
    )

    with get_maintenance_session() as session:
        with session.begin():
            session.add(normalized_event)
        session.refresh(normalized_event)
        return normalized_event


def get_printer_maintenance_history(
    printer_id: str,
    *,
    event_type: MaintenanceEventType | str | None = None,
    performed_by: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[MaintenanceEvent]:
    statement = select(MaintenanceEvent)
    statement = _apply_history_filters(
        statement,
        printer_id=printer_id,
        event_type=event_type,
        performed_by=performed_by,
        date_from=date_from,
        date_to=date_to,
    )
    statement = _apply_default_sort(statement)
    statement = _apply_limit(statement, limit)

    with get_maintenance_session() as session:
        return list(session.scalars(statement).all())


def get_farm_maintenance_history(
    *,
    printer_id: Optional[str] = None,
    event_type: MaintenanceEventType | str | None = None,
    performed_by: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[MaintenanceEvent]:
    statement = select(MaintenanceEvent)
    statement = _apply_history_filters(
        statement,
        printer_id=printer_id,
        event_type=event_type,
        performed_by=performed_by,
        date_from=date_from,
        date_to=date_to,
    )
    statement = _apply_default_sort(statement)
    statement = _apply_limit(statement, limit)

    with get_maintenance_session() as session:
        return list(session.scalars(statement).all())


def get_last_maintenance_by_type(
    printer_id: str,
    event_type: MaintenanceEventType | str,
) -> MaintenanceEvent | None:
    normalized_printer_id = _require_non_empty_text(printer_id, field_name="printer_id")
    normalized_event_type = _normalize_event_type(event_type)
    statement = (
        select(MaintenanceEvent)
        .where(MaintenanceEvent.printer_id == normalized_printer_id)
        .where(MaintenanceEvent.event_type == normalized_event_type)
    )
    statement = _apply_default_sort(statement).limit(1)

    with get_maintenance_session() as session:
        return session.scalars(statement).first()


def _apply_default_sort(statement):
    return statement.order_by(
        MaintenanceEvent.event_at.desc(),
        MaintenanceEvent.created_at.desc(),
        MaintenanceEvent.id.desc(),
    )


def _apply_history_filters(
    statement,
    *,
    printer_id: Optional[str] = None,
    event_type: MaintenanceEventType | str | None = None,
    performed_by: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
):
    if printer_id is not None:
        statement = statement.where(
            MaintenanceEvent.printer_id == _require_non_empty_text(printer_id, field_name="printer_id")
        )

    if event_type is not None:
        statement = statement.where(MaintenanceEvent.event_type == _normalize_event_type(event_type))

    if performed_by is not None:
        statement = statement.where(
            MaintenanceEvent.performed_by == _require_non_empty_text(performed_by, field_name="performed_by")
        )

    if date_from is not None:
        statement = statement.where(
            MaintenanceEvent.event_at >= _normalize_datetime(date_from, field_name="date_from")
        )

    if date_to is not None:
        statement = statement.where(
            MaintenanceEvent.event_at <= _normalize_datetime(date_to, field_name="date_to")
        )

    return statement


def _apply_limit(statement, limit: Optional[int]):
    normalized_limit = _normalize_non_negative_int(limit, field_name="limit", allow_zero=False)
    if normalized_limit is None:
        return statement
    return statement.limit(normalized_limit)


def _normalize_event_type(event_type: MaintenanceEventType | str) -> MaintenanceEventType:
    if event_type is None:
        raise ValueError("event_type must be a non-empty string")

    if isinstance(event_type, MaintenanceEventType):
        return event_type

    candidate = _require_non_empty_text(str(event_type), field_name="event_type").upper()
    try:
        return MaintenanceEventType(candidate)
    except ValueError as exc:
        allowed_values = ", ".join(item.value for item in MaintenanceEventType)
        raise ValueError(f"Unsupported event_type '{candidate}'. Allowed values: {allowed_values}") from exc


def _normalize_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime instance")
    if value.tzinfo is None:
        return value.replace(tzinfo=MAINTENANCE_TIMEZONE)
    return value.astimezone(MAINTENANCE_TIMEZONE)


def _normalize_stored_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_decimal(value: Any, *, field_name: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal value") from exc


def _normalize_positive_decimal(value: Any, *, field_name: str) -> Optional[Decimal]:
    normalized = _normalize_decimal(value, field_name=field_name)
    if normalized is None:
        return None
    if normalized <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return normalized


def _normalize_non_negative_decimal(value: Any, *, field_name: str) -> Optional[Decimal]:
    normalized = _normalize_decimal(value, field_name=field_name)
    if normalized is None:
        return None
    if normalized < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return normalized


def _normalize_non_negative_int(
    value: Optional[int],
    *,
    field_name: str,
    allow_zero: bool = True,
) -> Optional[int]:
    if value is None:
        return None

    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc

    if normalized < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    if not allow_zero and normalized == 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return normalized


def _validate_custom_type_name(
    *,
    event_type: MaintenanceEventType,
    custom_type_name: Optional[str],
) -> None:
    if event_type == MaintenanceEventType.OTHER:
        if not custom_type_name:
            raise ValueError("custom_type_name is required when event_type is OTHER")
        return

    if custom_type_name is not None:
        raise ValueError("custom_type_name can only be used when event_type is OTHER")


def _validate_required_fields_for_event_type(
    *,
    event_type: MaintenanceEventType,
    nozzle_diameter: Optional[Decimal],
    custom_type_name: Optional[str],
) -> None:
    if event_type == MaintenanceEventType.NOZZLE_REPLACEMENT and nozzle_diameter is None:
        raise ValueError("nozzle_diameter is required when event_type is NOZZLE_REPLACEMENT")

    if event_type == MaintenanceEventType.OTHER and not custom_type_name:
        raise ValueError("custom_type_name is required when event_type is OTHER")
