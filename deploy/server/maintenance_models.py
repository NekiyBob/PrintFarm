from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import CheckConstraint, Column, DateTime, Enum as SAEnum, Index, Integer, Numeric, String, Text

from maintenance_db import Base


MAINTENANCE_TIMEZONE = timezone(timedelta(hours=4))


def maintenance_now() -> datetime:
    return datetime.now(MAINTENANCE_TIMEZONE)


class MaintenanceEventType(str, Enum):
    FAN_REPLACEMENT = "FAN_REPLACEMENT"
    NOZZLE_REPLACEMENT = "NOZZLE_REPLACEMENT"
    CLEANING_LUBRICATION = "CLEANING_LUBRICATION"
    EXTRUDER_REPLACEMENT = "EXTRUDER_REPLACEMENT"
    EXTRUDER_CLEANING = "EXTRUDER_CLEANING"
    OTHER = "OTHER"


class MaintenanceEvent(Base):
    __tablename__ = "printer_maintenance_events"

    id = Column(Integer, primary_key=True)
    printer_id = Column(String(64), nullable=False)
    event_type = Column(
        SAEnum(MaintenanceEventType, native_enum=False, validate_strings=True),
        nullable=False,
    )
    event_at = Column(DateTime(timezone=True), nullable=False)
    performed_by = Column(String(128), nullable=False)
    note = Column(Text, nullable=False, default="")
    nozzle_diameter = Column(Numeric(4, 2), nullable=True)
    custom_type_name = Column(String(128), nullable=True)
    print_hours_snapshot = Column(Numeric(10, 2), nullable=True)
    print_count_snapshot = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=maintenance_now)

    __table_args__ = (
        Index("ix_maintenance_printer_event_at", "printer_id", "event_at"),
        Index("ix_maintenance_event_type", "event_type"),
        CheckConstraint(
            "nozzle_diameter IS NULL OR nozzle_diameter > 0",
            name="ck_maintenance_nozzle_positive",
        ),
        CheckConstraint(
            "print_hours_snapshot IS NULL OR print_hours_snapshot >= 0",
            name="ck_maintenance_hours_nonnegative",
        ),
        CheckConstraint(
            "print_count_snapshot IS NULL OR print_count_snapshot >= 0",
            name="ck_maintenance_count_nonnegative",
        ),
        CheckConstraint(
            "(event_type != 'OTHER' AND custom_type_name IS NULL) OR "
            "(event_type = 'OTHER' AND custom_type_name IS NOT NULL AND trim(custom_type_name) != '')",
            name="ck_maintenance_other_custom_name",
        ),
    )


# Backward-compatible alias while the rest of the codebase migrates to the
# shorter and clearer model name.
PrinterMaintenanceEvent = MaintenanceEvent
