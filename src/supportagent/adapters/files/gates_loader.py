"""``evals/gates.yaml`` (ARCH §11, CAP §4.7): the promotion gate thresholds."""

from __future__ import annotations

from pathlib import Path

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.promotion import PromotionGate, Threshold
from supportagent.domain.quality import DEFAULT_GATES, QualityGate


def _threshold(raw: object, where: str) -> Threshold:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected {{min_lower_bound, min_cases}}")  # noqa: TRY003
    try:
        lb = float(raw["min_lower_bound"])
        n = int(raw["min_cases"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc  # noqa: TRY003
    if not 0 < lb <= 1 or n < 1:
        raise ConfigError(f"{where}: min_lower_bound in (0, 1], min_cases >= 1")  # noqa: TRY003
    return Threshold(min_lower_bound=lb, min_cases=n)


def load_promotion_gate(path: Path) -> PromotionGate:
    data = load_yaml_file(path).get("promotion")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: 'promotion' section is required")  # noqa: TRY003
    bands = data.get("bands") or {}
    if not isinstance(bands, dict):
        raise ConfigError(f"{path}: promotion.bands must be a mapping")  # noqa: TRY003
    return PromotionGate(
        overall=_threshold(data.get("overall"), f"{path}: promotion.overall"),
        bands={str(k): _threshold(v, f"{path}: promotion.bands.{k}") for k, v in bands.items()},
        provisional=bool(data.get("provisional", True)),
    )


def load_quality_gates(path: Path) -> tuple[QualityGate, ...]:
    """``quality:`` rows of ``evals/gates.yaml``; the ARCH §11 defaults when absent."""
    rows = load_yaml_file(path).get("quality")
    if rows is None:
        return DEFAULT_GATES
    if not isinstance(rows, list):
        raise ConfigError(f"{path}: 'quality' must be a list")  # noqa: TRY003
    gates: list[QualityGate] = []
    for row in rows:
        try:
            gate = QualityGate(
                name=str(row["name"]),
                band=str(row["band"]),
                metric=str(row["metric"]),
                min_lb=float(row["min_lb"]),
                min_cases=int(row["min_cases"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: quality gate {row!r}: {exc}") from exc  # noqa: TRY003
        if gate.metric not in ("classification", "severity") or gate.band not in ("P1-P2", "P3-P4"):
            raise ConfigError(f"{path}: quality gate {gate.name}: bad band or metric")  # noqa: TRY003
        gates.append(gate)
    return tuple(gates)


__all__ = ["load_promotion_gate", "load_quality_gates"]
