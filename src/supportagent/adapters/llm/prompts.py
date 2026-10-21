"""Prompt registry (02 §5): versioned slot prompts, rendered strictly.

A slot's prompt lives at ``prompts/<slot>/v<N>.md`` with two markdown
sections: ``## system`` (static) and ``## user`` (the untrusted ticket is
wrapped in ``<untrusted_ticket>`` tags). The registry:

- picks the highest ``v<N>`` for the slot;
- escapes untrusted values so a ``</untrusted_ticket>`` in the ticket body
  cannot close the block early (prompt-injection hardening);
- renders with Jinja2 ``StrictUndefined``: a missing variable is a load
  error, never a silently-empty prompt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import jinja2
import jinja2.meta

from supportagent.domain.errors import ConfigError
from supportagent.domain.slots import SlotName

# Any spelling a model might read as the closing tag: case and inner
# whitespace vary (``</UNTRUSTED_TICKET>``, ``< /untrusted_ticket >``).
_UNTRUSTED_CLOSING_RE = re.compile(r"<\s*/\s*untrusted_ticket\s*>", re.IGNORECASE)
_UNTRUSTED_CLOSING_ESCAPED = "&lt;/untrusted_ticket&gt;"
_VERSION_RE = re.compile(r"v(\d+)\.md")


def _escape_untrusted(value: str) -> str:
    """Escape a closing ``</untrusted_ticket>`` so it cannot close the block."""
    return _UNTRUSTED_CLOSING_RE.sub(_UNTRUSTED_CLOSING_ESCAPED, value)


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """The rendered system + user sections, plus the prompt version used."""

    system: str
    user: str
    version: str


class PromptRegistry:
    """Loads and renders versioned slot prompts (02 §5)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._env = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            autoescape=False,  # noqa: S701  # nosec B701 — prompts, not HTML
        )

    def render(self, slot: SlotName, variables: Mapping[str, str]) -> RenderedPrompt:
        """Render the latest prompt for ``slot`` with ``variables``."""
        version = self._latest_version(slot)
        path = self._root / slot.value / f"{version}.md"
        if not path.is_file():
            raise ConfigError(f"missing prompt file {path}")  # noqa: TRY003
        text = path.read_text(encoding="utf-8")
        system_template, user_template = self._split_sections(text, slot)
        escaped = {
            key: _escape_untrusted(value)
            for key, value in variables.items()
            if isinstance(value, str)
        }
        system = self._env.from_string(system_template).render(**escaped)
        user = self._env.from_string(user_template).render(**escaped)
        return RenderedPrompt(system=system, user=user, version=version)

    def variables(self, slot: SlotName) -> frozenset[str]:
        """The variables the latest prompt for ``slot`` references (BL-009)."""
        version = self._latest_version(slot)
        text = (self._root / slot.value / f"{version}.md").read_text(encoding="utf-8")
        found: set[str] = set()
        for section in self._split_sections(text, slot):
            found |= jinja2.meta.find_undeclared_variables(self._env.parse(section))
        return frozenset(found)

    def check(self, expected: Mapping[SlotName, frozenset[str]]) -> None:
        """BL-009: every prompt present uses exactly its slot's variables, or ``ConfigError``.

        A slot without a prompt directory is skipped: an unconfigured slot fails
        closed at call time, and ``classify`` is required by config validation.
        """
        for slot, supplied in expected.items():
            if not (self._root / slot.value).is_dir():
                continue
            used = self.variables(slot)
            if used != supplied:
                raise ConfigError(  # noqa: TRY003
                    f"prompt {slot.value}: uses {sorted(used - supplied)} not supplied, "
                    f"ignores {sorted(supplied - used)} (BL-009)"
                )

    def _latest_version(self, slot: SlotName) -> str:
        slot_dir = self._root / slot.value
        if not slot_dir.is_dir():
            raise ConfigError(f"no prompt directory for slot {slot.value}")  # noqa: TRY003
        versions = [
            int(m.group(1)) for p in slot_dir.iterdir() if (m := _VERSION_RE.fullmatch(p.name))
        ]
        if not versions:
            raise ConfigError(f"no versioned prompt (v<N>.md) for slot {slot.value}")  # noqa: TRY003
        return f"v{max(versions)}"

    @staticmethod
    def _split_sections(text: str, slot: SlotName) -> tuple[str, str]:
        system = re.search(r"^## system[ \t]*\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.MULTILINE)
        user = re.search(r"^## user[ \t]*\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.MULTILINE)
        if not system or not user:
            raise ConfigError(  # noqa: TRY003
                f"slot {slot.value} prompt must define '## system' and '## user' sections"
            )
        return system.group(1).strip(), user.group(1).strip()
