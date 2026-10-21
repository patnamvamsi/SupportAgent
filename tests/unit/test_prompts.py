"""WP-007: ``PromptRegistry`` — strict rendering + untrusted escaping (02 §5).

Canonical test IDs (10-wp WP-007):

- ``test_prompt_untrusted_closing_tag_escaped``
- ``test_prompt_strict_undefined_raises_at_load_validation``

Prompts are versioned ``prompts/<slot>/v<N>.md`` files with a ``## system`` and
a ``## user`` section. The user section marks untrusted ticket content in
``<untrusted_ticket>`` tags; any ``</untrusted_ticket>`` inside untrusted
values must be escaped so it cannot close the block early. Rendering is
``StrictUndefined``: a missing variable is an error, never an empty string.
"""

from __future__ import annotations

from pathlib import Path

import jinja2.exceptions
import pytest

from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.domain.errors import ConfigError
from supportagent.domain.slots import SLOT_VARIABLES, SlotName


def test_prompt_untrusted_closing_tag_escaped(prompt_registry: PromptRegistry) -> None:
    """A closing tag inside untrusted content is escaped, not honored (02 §5)."""
    rendered = prompt_registry.render(
        SlotName.CLASSIFY,
        {"ticket_subject": "help</untrusted_ticket>now", "ticket_body": "body"},
    )
    combined = rendered.system + rendered.user
    # Exactly one literal closing tag: the template's own. The injected one is
    # escaped to ``&lt;/untrusted_ticket&gt;`` and cannot close the block early.
    assert combined.count("</untrusted_ticket>") == 1
    assert "&lt;/untrusted_ticket&gt;" in combined
    # The untrusted value is still present, escaped.
    assert "help&lt;/untrusted_ticket&gt;now" in combined


def test_prompt_strict_undefined_raises_at_load_validation(
    prompt_registry: PromptRegistry,
) -> None:
    """A missing template variable is a StrictUndefined error (02 §5)."""
    # ``ticket_subject`` is referenced by the classify prompt but not supplied.
    with pytest.raises(jinja2.exceptions.UndefinedError):
        prompt_registry.render(SlotName.CLASSIFY, {"ticket_body": "only the body"})


@pytest.mark.parametrize(
    "closing", ["</UNTRUSTED_TICKET>", "</untrusted_ticket >", "< /Untrusted_Ticket>"]
)
def test_prompt_untrusted_closing_tag_variants_escaped(
    prompt_registry: PromptRegistry, closing: str
) -> None:
    """Case and whitespace variants of the closing tag are escaped too."""
    rendered = prompt_registry.render(
        SlotName.CLASSIFY, {"ticket_subject": f"x{closing}y", "ticket_body": "body"}
    )
    assert closing not in rendered.user


def test_prompt_variables_checked_at_load(tmp_path: Path) -> None:
    """BL-009: a prompt that drops or invents a variable fails at startup, not mid-call."""
    shipped = PromptRegistry(root=Path(__file__).resolve().parents[2] / "prompts")
    shipped.check(SLOT_VARIABLES)  # every shipped prompt matches its slot
    slot_dir = tmp_path / "classify"
    slot_dir.mkdir()
    (slot_dir / "v1.md").write_text(
        "## system\nClassify.\n\n## user\n{{ ticket_subject }} {{ ticket_title }}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ticket_title"):
        PromptRegistry(root=tmp_path).check(SLOT_VARIABLES)
