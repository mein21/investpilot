"""Small SQLAlchemy helpers shared by model modules."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def enum_type(enum_cls: type[StrEnum], *, length: int = 32) -> SAEnum:
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
