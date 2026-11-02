"""Binding an access request's credential, once it is proven read-only (ADR-0019, WP-O5b).

The gap plan asks for least-privilege access (a plugin's ``requires``
contract); a person approves the request and provides the credential. Here:

1. the plugin's contract is looked up as installed now (not as the plan quoted it);
2. its verifier runs every probe with the candidate credential:
   ``verify_can`` must work, every ``verify_cannot`` must be refused;
3. proven: the credential goes to the vault (``ports.credentials``) and the
   request is ``bound``, serving the plugin's backend. Refused: nothing is
   stored, and the request reopens with why.

Each probe's result, redacted of the credential, is kept on the request and
in the audit trail. :meth:`AccessBinding.reverify` runs them again for every
bound credential: one that can now write is taken out of the vault.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from supportagent.app.request_review import RequestReview, RequestView
from supportagent.domain.access_verify import ProbeResult, Verdict, judge, redact
from supportagent.domain.errors import DomainValidationError, NotFound, ToolError
from supportagent.domain.model.plugin import AccessContract, PluginManifest
from supportagent.ports.access import AccessVerifier
from supportagent.ports.clock import Clock
from supportagent.ports.credentials import CredentialStore, StoredCredential


@dataclass(frozen=True, slots=True)
class BindOutcome:
    request: RequestView
    bound: bool
    why: str | None
    probes: tuple[ProbeResult, ...]


class AccessBinding:
    def __init__(  # noqa: PLR0913 — the review, the vault, the verifiers and the contracts
        self,
        *,
        review: RequestReview,
        store: CredentialStore,
        verifiers: Mapping[str, AccessVerifier],
        plugins: Sequence[PluginManifest],
        clock: Clock,
        timeout_s: float = 15.0,
    ) -> None:
        self._review = review
        self._store = store
        self._verifiers = verifiers
        self._plugins = {p.id: p for p in plugins}
        self._clock = clock
        self._timeout_s = timeout_s

    def bind(self, rid: str, secret: str, *, actor: str) -> BindOutcome:
        """Verify ``secret`` for request ``rid`` and bind it, or refuse and reopen."""
        req = self._review.get(rid)
        if req.status == "declined":
            raise DomainValidationError(f"request {rid} was declined: reopen it first")  # noqa: TRY003
        if req.status == "bound":
            raise DomainValidationError(  # noqa: TRY003
                f"request {rid} is bound already: unbind it, then bind the new credential"
            )
        if not secret.strip():
            raise DomainValidationError("an empty credential")  # noqa: TRY003
        manifest, contract = self._contract(req)
        verdict, probes = self._verify(req, contract, secret)
        verification = self._document(actor, contract, verdict, probes)
        if not verdict.bound:
            view = self._review.record_verification(
                rid, bound=False, actor=actor, verification=verification
            )
            return BindOutcome(view, False, verdict.why, probes)
        self._store.put(
            StoredCredential(
                request=rid,
                plugin=req.plugin,
                access=req.access,
                target=req.target,
                backend=_backend(manifest, req.access),
                bound_ts=int(self._clock.now().timestamp()),
                secret=secret,
            )
        )
        try:
            view = self._review.record_verification(
                rid, bound=True, actor=actor, verification=verification
            )
        except Exception:
            self._store.remove(rid)  # the request isn't bound: neither is its credential
            raise
        return BindOutcome(view, True, None, probes)

    def unbind(self, rid: str, *, actor: str, why: str = "unbound by a person") -> RequestView:
        req = self._review.get(rid)
        if req.status != "bound":
            raise DomainValidationError(f"request {rid} has no bound credential")  # noqa: TRY003
        self._store.remove(rid)
        return self._review.mark_unbound(rid, actor=actor, why=why)

    def reverify(self, *, actor: str = "system") -> list[BindOutcome]:
        """Every bound credential's probes again. One that fails leaves the vault, and
        its request reopens with why; one whose request is gone or no longer bound
        leaves the vault too."""
        outcomes: list[BindOutcome] = []
        bound = {r.id: r for r in self._review.requests(include_gone=True) if r.status == "bound"}
        for cred in self._store.all():
            req = bound.get(cred.request)
            if req is None or not req.present:
                self._store.remove(cred.request)
                if req is not None:
                    self._review.mark_unbound(
                        req.id, actor=actor, why="the plan no longer makes this request"
                    )
                continue
            contract: AccessContract | None = None
            probes: tuple[ProbeResult, ...] = ()
            try:
                _, contract = self._contract(req)
            except NotFound as exc:
                verdict = Verdict(False, str(exc))
            else:
                verdict, probes = self._verify(req, contract, cred.secret)
            verification = self._document(actor, contract, verdict, probes)
            if not verdict.bound:
                self._store.remove(cred.request)
            view = self._review.record_verification(
                req.id,
                bound=verdict.bound,
                actor=(req.decided_by or actor) if verdict.bound else actor,
                verification=verification,
            )
            outcomes.append(BindOutcome(view, verdict.bound, verdict.why, probes))
        return outcomes

    def _contract(self, req: RequestView) -> tuple[PluginManifest, AccessContract]:
        manifest = self._plugins.get(req.plugin)
        contract = manifest.requires.get(req.access) if manifest is not None else None
        if manifest is None or contract is None:
            raise NotFound(  # noqa: TRY003
                f"plugin {req.plugin!r} with contract {req.access!r} isn't installed"
            )
        return manifest, contract

    def _verify(
        self, req: RequestView, contract: AccessContract, secret: str
    ) -> tuple[Verdict, tuple[ProbeResult, ...]]:
        verifier = self._verifiers.get(contract.verifier or "")
        if verifier is None:
            named = (
                f"names verifier {contract.verifier!r}, which this core lacks"
                if (contract.verifier)
                else "names no verifier"
            )
            return Verdict(False, f"it can't be proven read-only: the contract {named}"), ()
        try:
            raw = verifier.verify(
                secret,
                req.target,
                can=contract.verify_can,
                cannot=contract.verify_cannot,
                timeout_s=self._timeout_s,
            )
        except (ToolError, ValueError) as exc:
            return Verdict(False, redact(f"the probes didn't run: {exc}", secret)), ()
        probes = tuple(
            ProbeResult(r.kind, r.probe, r.outcome, redact(r.detail, secret)) for r in raw
        )
        verdict = judge(probes, can=contract.verify_can, cannot=contract.verify_cannot)
        why = None if verdict.why is None else redact(verdict.why, secret)
        return Verdict(verdict.bound, why), probes

    def _document(
        self,
        actor: str,
        contract: AccessContract | None,
        verdict: Verdict,
        probes: Sequence[ProbeResult],
    ) -> dict[str, object]:
        return {
            "at": self._clock.now().isoformat(),
            "by": actor,
            "bound": verdict.bound,
            "why": verdict.why,
            "verifier": contract.verifier if contract is not None else None,
            "probes": [
                {"kind": p.kind, "probe": p.probe, "outcome": p.outcome, "detail": p.detail}
                for p in probes
            ],
        }


def _backend(manifest: PluginManifest, access: str) -> str:
    """What serves the credential: the plugin's backend whose contract it satisfies."""
    for backend in manifest.provides.backends:
        if backend.access != access:
            continue
        if backend.first_party is not None:
            return f"first_party:{backend.first_party}"
        if backend.mcp is not None:
            return f"mcp:{backend.mcp.image}"
    return "none"


__all__ = ["AccessBinding", "BindOutcome"]
