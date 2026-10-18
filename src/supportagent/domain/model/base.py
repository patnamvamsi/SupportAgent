"""Base for domain models (01 §4): frozen, extra-forbid, strict, aware UTC."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class DomainModel(BaseModel):
    """Shared config + the aware-UTC datetime rule (01 §4, 01 §2)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def __init__(self, /, **data: object) -> None:
        # pydantic validates first (types, strict, field constraints, and the
        # aware-UTC model validator below); domain rules then run from user-level
        # code so a DomainValidationError keeps its identity — it is also a
        # pydantic ValidationError (01 §7.1, 10-wp WP-004), and a pydantic
        # validator would re-wrap it into a plain ValidationError.
        super().__init__(**data)
        self._assert_domain_rules()

    def _assert_domain_rules(self) -> None:
        """Subclass hook: raise ``DomainValidationError`` for a domain-rule violation."""

    @model_validator(mode="after")
    def _assert_aware_utc_datetimes(self) -> Self:
        for name, value in self.__dict__.items():
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(  # noqa: TRY003 — message is the pinned validation API (01 §4)
                    f"{name} must be a timezone-aware UTC datetime"
                )
        return self
