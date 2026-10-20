"""The provider model-deprecation feed (11-wp WP-106, ARCH §9).

```yaml
models:
  claude-sonnet-4-5-20250929: {retires_on: 2027-09-29}
```
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError


def load_deprecation_feed(path: Path) -> dict[str, date]:
    data = load_yaml_file(path)
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError(f"{path}: 'models' must be a mapping")  # noqa: TRY003
    feed: dict[str, date] = {}
    for model_id, entry in models.items():
        value = entry.get("retires_on") if isinstance(entry, dict) else None
        if isinstance(value, date):
            feed[str(model_id)] = value
        elif isinstance(value, str):
            try:
                feed[str(model_id)] = date.fromisoformat(value)
            except ValueError as exc:
                raise ConfigError(f"{path}: {model_id}: bad retires_on {value!r}") from exc  # noqa: TRY003
        else:
            raise ConfigError(f"{path}: {model_id}: retires_on is required")  # noqa: TRY003
    return feed


__all__ = ["load_deprecation_feed"]
