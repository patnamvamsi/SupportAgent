"""Base for domain models (01 §4): frozen, extra-forbid, strict, aware UTC."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator


class DomainModel(BaseModel):
    """Shared config + the aware-UTC datetime rule (01 §4, 01 §2).

    Domain rules (``_assert_domain_rules``) run from user-level code, never from
    inside a pydantic validator, so a ``DomainValidationError`` keeps its
    identity (it is also a pydantic ``ValidationError``; 01 §7.1, 10-wp WP-004).
    A pydantic validator — including ``model_post_init`` — would re-wrap it.

    They run on every construction path: ``Model(...)`` via ``__init__``, and
    ``model_validate`` / ``model_validate_json`` via the overrides below, which
    also check nested domain models.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def __init__(self, /, **data: object) -> None:
        super().__init__(**data)
        self._assert_domain_rules()

    # pydantic treats any overridden ``__init__`` as a "custom init" and then
    # validates JSON by calling ``__init__`` with the parsed dict — in *python*
    # mode, where ``strict`` rejects enum values ("P2") and ISO datetime
    # strings. Marking this ``__init__`` as the base init keeps pydantic's
    # native JSON validation; the rules are applied by the overrides instead.
    __init__.__pydantic_base_init__ = True  # type: ignore[attr-defined]

    @classmethod
    def model_validate(  # noqa: PLR0913 — pydantic's signature
        cls,
        obj: Any,  # noqa: ANN401 — pydantic's signature
        *,
        strict: bool | None = None,
        extra: Any = None,  # noqa: ANN401
        from_attributes: bool | None = None,
        context: Any | None = None,  # noqa: ANN401
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        model = super().model_validate(
            obj,
            strict=strict,
            extra=extra,
            from_attributes=from_attributes,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        _assert_rules_deep(model)
        return model

    @classmethod
    def model_validate_json(  # noqa: PLR0913 — pydantic's signature
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any = None,  # noqa: ANN401
        context: Any | None = None,  # noqa: ANN401
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        model = super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        _assert_rules_deep(model)
        return model

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


def _assert_rules_deep(value: object) -> None:
    """Run domain rules on ``value`` and every nested domain model (children first)."""
    if isinstance(value, BaseModel):
        for child in value.__dict__.values():
            _assert_rules_deep(child)
        if isinstance(value, DomainModel):
            value._assert_domain_rules()
    elif isinstance(value, Mapping):
        for child in value.values():
            _assert_rules_deep(child)
    elif isinstance(value, list | tuple | set | frozenset):
        for child in value:
            _assert_rules_deep(child)
