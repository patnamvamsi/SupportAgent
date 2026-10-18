"""WP-001: system adapters and test doubles (01 §3, 04 §3)."""

from __future__ import annotations

from datetime import timedelta

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.adapters.system.clock import SystemClock
from supportagent.adapters.system.ids import UlidIdGenerator
from supportagent.domain.ids import IdPrefix
from tests.builders import T0


def test_system_clock_now_is_aware_utc() -> None:
    now = SystemClock().now()
    # 01 §3: "timezone-aware UTC, always".
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_ulid_id_generator_prefix_and_ulid() -> None:
    generator = UlidIdGenerator()
    case_id = generator.new(IdPrefix.CASE)
    assert case_id.startswith("case_")
    ulid_part = case_id.removeprefix("case_")
    # A ULID is 26 Crockford-base32 characters.
    assert len(ulid_part) == 26
    assert ulid_part.isalnum()
    # Ids are unique.
    assert generator.new(IdPrefix.CASE) != case_id
    # Every prefix maps to its own id space (03 §1).
    assert generator.new(IdPrefix.PROPERTY).startswith("prop_")
    assert generator.new(IdPrefix.GRANT).startswith("grnt_")


def test_frozen_clock_frozen_and_advances() -> None:
    clock = FrozenClock(start=T0)
    assert clock.now() == T0
    assert clock.now() == T0  # frozen: time does not pass
    clock.advance(90)
    assert clock.now() == T0 + timedelta(seconds=90)


def test_sequential_id_generator() -> None:
    generator = SequentialIdGenerator()
    assert generator.new(IdPrefix.CASE) == "case_0001"
    assert generator.new(IdPrefix.CASE) == "case_0002"
    # Counters are per-prefix (04 §3: case_0001, ...).
    assert generator.new(IdPrefix.PROPERTY) == "prop_0001"
