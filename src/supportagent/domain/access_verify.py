"""Judging a credential's probes (ADR-0019, WP-O5b). Pure.

A credential is bound only when every ``can`` probe was allowed and every
``cannot`` probe refused. Anything else refuses it, with the first reason:

- a read it must make didn't work (wrong account, a missing grant);
- a write was allowed: a read-only account can't do it;
- a write couldn't be proven refused: fail closed, never assume.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit

ProbeKind = Literal["can", "cannot"]
ProbeOutcome = Literal["allowed", "refused", "error"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe's answer (``ports.access``): allowed, refused, or couldn't be told."""

    kind: ProbeKind
    probe: str
    outcome: ProbeOutcome
    #: What the system said; never the credential (the caller redacts it again).
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    bound: bool
    #: Why it was refused (``None`` when bound).
    why: str | None


def judge(results: Sequence[ProbeResult], *, can: Sequence[str], cannot: Sequence[str]) -> Verdict:
    """The verdict on a verifier's results for the contract's probes (one each, any order)."""
    by_probe = {(r.kind, r.probe): r for r in results}
    for probe in can:
        r = by_probe.get(("can", probe))
        if r is None:
            return Verdict(False, f"{probe!r} wasn't probed")
        if r.outcome != "allowed":
            return Verdict(False, f"it can't {probe}: {r.detail or r.outcome}")
    for probe in cannot:
        r = by_probe.get(("cannot", probe))
        if r is None:
            return Verdict(False, f"{probe!r} wasn't probed")
        if r.outcome == "allowed":
            return Verdict(False, f"it can {probe}, and a read-only account can't: {r.detail}")
        if r.outcome != "refused":
            return Verdict(False, f"couldn't prove it can't {probe}: {r.detail or r.outcome}")
    return Verdict(True, None)


_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")
_ORACLE_PASSWORD = re.compile(r"^([^/@\s]+)/[^@\s]+@")


def redact(text: str, secret: str) -> str:
    """``text`` without the credential or any password inside it (a DSN's, a URL's)."""
    parts = {secret}
    if "://" in secret:
        password = urlsplit(secret).password
        if password:
            parts |= {password, unquote(password)}
    elif match := _ORACLE_PASSWORD.match(secret):
        parts.add(secret[len(match.group(1)) + 1 : secret.index("@")])
    for part in sorted((p for p in parts if len(p) >= 3), key=len, reverse=True):  # noqa: PLR2004
        text = text.replace(part, "***")
    return _USERINFO.sub("***@", text)


__all__ = ["ProbeKind", "ProbeOutcome", "ProbeResult", "Verdict", "judge", "redact"]
