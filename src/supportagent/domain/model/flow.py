"""A business flow (ADR-0021 §2): the stages one object should pass through.

A flow is ``flows/<id>.yaml`` in the system's onboarding workspace. Each stage
names the component that performs it and one declared **lookup** that tells
whether the stage happened for one object id: a capability, its arguments,
where the evidence is in the result, and which fields the model may see.

What a flow may never do is fixed here, at load:

- an argument may refer to the traced object only as ``$object.id`` or a
  declared key ``$key.<name>``, and only inside a literal argument; the value
  is checked against :data:`SAFE_VALUE` before it is put there, so a traced
  id can't change a query's shape;
- a key is read from an earlier stage only;
- a lookup that reads business records says why logs and messages can't
  establish its stage (§4);
- the model sees only the fields a lookup lists.

``reviewed: false`` marks an onboarding draft. Only a reviewed flow is used.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from supportagent.domain.fieldpath import valid_path
from supportagent.domain.model.base import DomainModel

_ID = r"^[a-z0-9][a-z0-9-]{1,40}$"
_CAPABILITY = r"^[a-z0-9_]+(\.[a-z0-9_]+)+$"
#: What a traced id or a key must look like to enter a query, whatever the flow says.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
#: A reference inside a literal argument.
REF = re.compile(r"\$(object\.id|key\.[a-z][a-z0-9_]*)")
LookupSource = Literal["logs", "messages", "records"]


class Rejection(DomainModel):
    """The stage rejected the object on purpose when ``path`` holds one of ``values``.

    ``contains`` matches a value anywhere in the text (a log line); ``equals``
    the whole value (a status field).
    """

    path: str
    values: list[str] = Field(min_length=1)
    match: Literal["equals", "contains"] = "equals"

    def matches(self, value: object) -> bool:
        text = str(value)
        if self.match == "contains":
            return any(v in text for v in self.values)
        return text in self.values

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not valid_path(value):
            raise ValueError(f"not a field path: {value!r}")  # noqa: TRY003
        return value


class StageLookup(DomainModel):
    capability: str = Field(pattern=_CAPABILITY)
    source: LookupSource
    #: Integers, catalog refs (``$service.<field>``) or reviewed literals, which may
    #: contain ``$object.id`` / ``$key.<name>``.
    args: dict[str, int | str] = Field(default_factory=dict)
    #: The stage happened for the id when this path holds at least one value.
    found: str
    #: Where the stage's time is; the earliest value counts (RFC 3339).
    at: str | None = None
    #: The only fields the model may see (ADR-0021 §4).
    fields: list[str] = Field(min_length=1)
    #: How far back the lookup reads, in minutes.
    window_min: int = Field(default=1440, ge=1, le=60 * 24 * 31)
    #: Required for ``records``: why logs and messages can't establish this stage.
    records_reason: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> StageLookup:
        for path in [self.found, *self.fields, *([self.at] if self.at else [])]:
            if not valid_path(path):
                raise ValueError(f"not a field path: {path!r}")  # noqa: TRY003
        if self.source == "records" and not (self.records_reason or "").strip():
            raise ValueError(  # noqa: TRY003
                "a records lookup must say why logs and messages can't establish the stage"
            )
        if self.source != "records" and self.records_reason is not None:
            raise ValueError("records_reason is only for a records lookup")  # noqa: TRY003
        return self

    def refs(self) -> set[str]:
        """Every ``object.id`` / ``key.<name>`` the arguments use."""
        return {
            m.group(1) for v in self.args.values() if isinstance(v, str) for m in REF.finditer(v)
        }


class FlowStage(DomainModel):
    id: str = Field(pattern=_ID)
    title: str = Field(min_length=1)
    #: The graph node that performs it (``service:enrichment``).
    component: str = Field(min_length=1)
    lookup: StageLookup
    #: The most minutes it should take after the previous stage; none: no limit.
    within_min: int | None = Field(default=None, ge=1)
    rejection: Rejection | None = None
    terminal: bool = False
    #: The incident class RCA tests when the object stops here (its runbook);
    #: none: RCA proposes a plan for the component.
    rca_class: str | None = Field(default=None, pattern=r"^[a-z0-9_-]+/[a-z0-9_-]+$")


class FlowKey(DomainModel):
    """A secondary key the flow derives (§3): ``field`` of ``stage``'s result."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    stage: str
    field: str

    @field_validator("field")
    @classmethod
    def _path(cls, value: str) -> str:
        if not valid_path(value):
            raise ValueError(f"not a field path: {value!r}")  # noqa: TRY003
        return value


class Flow(DomainModel):
    flow: str = Field(pattern=_ID)
    title: str = Field(min_length=1)
    #: What is traced (``trade``, ``order``).
    object: str = Field(min_length=1)
    #: A traced id must match this, and :data:`SAFE_VALUE` too.
    id_pattern: str
    reviewed: bool = False
    #: What the draft came from (documents, code), for the reviewer.
    sources: list[str] = Field(default_factory=list)
    keys: list[FlowKey] = Field(default_factory=list)
    stages: list[FlowStage] = Field(min_length=2)

    @field_validator("id_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"id_pattern does not compile: {exc}") from exc  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def _order(self) -> Flow:
        ids = [s.id for s in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("stage ids must be unique")  # noqa: TRY003
        if not self.stages[-1].terminal:
            raise ValueError("the last stage must be terminal")  # noqa: TRY003
        keys = {k.name: k for k in self.keys}
        if len(keys) != len(self.keys):
            raise ValueError("key names must be unique")  # noqa: TRY003
        for key in self.keys:
            if key.stage not in ids:
                raise ValueError(f"key {key.name}: no stage {key.stage!r}")  # noqa: TRY003
        for i, stage in enumerate(self.stages):
            before = set(ids[:i])
            for ref in stage.lookup.refs():
                if ref == "object.id":
                    continue
                name = ref.removeprefix("key.")
                if name not in keys:
                    raise ValueError(f"stage {stage.id}: no key {name!r}")  # noqa: TRY003
                if keys[name].stage not in before:
                    raise ValueError(  # noqa: TRY003
                        f"stage {stage.id}: key {name!r} comes from a later stage"
                    )
        return self

    def accepts(self, object_id: str) -> bool:
        """Whether ``object_id`` may be traced: the flow's pattern and the safe charset."""
        return bool(SAFE_VALUE.match(object_id) and re.fullmatch(self.id_pattern, object_id))

    def reads_records(self) -> bool:
        return any(s.lookup.source == "records" for s in self.stages)


__all__ = [
    "REF",
    "SAFE_VALUE",
    "Flow",
    "FlowKey",
    "FlowStage",
    "LookupSource",
    "Rejection",
    "StageLookup",
]
