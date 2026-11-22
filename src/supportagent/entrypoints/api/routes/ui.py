"""Server-rendered admin UI (02 §3): login, setup, approvals, proposal, case, grants.

Plain HTML forms, no JavaScript: every form carries a hidden ``csrf_token``
(checked by ``csrf.verify_form``) and the approve/reject forms carry the
``payload_hash`` of the exact payload rendered on the page, so the approver
approves exactly what they saw (02 §3). Jinja autoescaping is on, so payload
text always renders as text. Signed-out GETs redirect to ``/ui/login``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from supportagent.app import system_view
from supportagent.app.detector_review import DetectorReview
from supportagent.app.graph_review import LAYER_LABELS, GraphReview
from supportagent.app.onboarding_control import OnboardingControl
from supportagent.app.queries import auto_timeline
from supportagent.app.request_review import RequestReview
from supportagent.app.setup import SessionUser
from supportagent.app.stats import Mode as StatsMode
from supportagent.app.stats import parse_window
from supportagent.app.trace_answer import TraceAnswer
from supportagent.app.tracing import TraceRun
from supportagent.domain.autopilot import NEED_LABELS
from supportagent.domain.errors import (
    AuthorizationDenied,
    ConcurrencyConflict,
    ConfigError,
    DomainValidationError,
    NotFound,
    SupportAgentError,
)
from supportagent.domain.rbac import Permission, service_role_scope
from supportagent.domain.roles import Role
from supportagent.entrypoints.api import csrf, graph_svg, session
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context

router = APIRouter()

_HERE = Path(__file__).resolve().parent.parent
_CSS = (_HERE / "static" / "app.css").read_text(encoding="utf-8")
_REVIEW_JS = (_HERE / "static" / "review.js").read_text(encoding="utf-8")


def _age(ts: int | None, now_ts: int) -> str:
    if ts is None:
        return "—"
    minutes = max(0, now_ts - int(ts)) // 60
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"  # noqa: PLR2004


def _ttl(ts: int | None, now_ts: int) -> str:
    if ts is None:
        return "—"
    left = int(ts) - now_ts
    return "expired" if left <= 0 else _age(now_ts - left, now_ts)


def _date(ts: int | None) -> str:
    return "—" if ts is None else datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")


_ENV = Environment(
    loader=FileSystemLoader(_HERE / "templates"),
    autoescape=select_autoescape(["html"]),
    auto_reload=False,
)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _secs(value: int | None) -> str:
    return "—" if value is None else f"{value}s"


_ENV.filters.update(age=_age, ttl=_ttl, date=_date, pct=_pct, secs=_secs)

_STATUS_TITLES = {403: "Forbidden", 404: "Not found", 409: "Conflict", 422: "Invalid"}


def _render(  # noqa: PLR0913 — the shared template context
    request: Request,
    ctx: ApiContext,
    name: str,
    user: SessionUser | None,
    *,
    status: int = 200,
    error: str | None = None,
    **values: object,
) -> HTMLResponse:
    body = _ENV.get_template(name).render(
        user=user,
        is_admin=user is not None and user.can(Permission.ADMINISTER, anywhere=True),
        can_review=user is not None and user.can(Permission.REVIEW_KNOWLEDGE, anywhere=True),
        csrf_token=request.cookies.get(csrf.COOKIE, ""),
        now_ts=int(ctx.clock.now().timestamp()),
        idp_label=ctx.idp_label,
        error=error,
        **values,
    )
    return HTMLResponse(body, status_code=status)


def _error(
    request: Request, ctx: ApiContext, user: SessionUser | None, status: int, detail: str
) -> HTMLResponse:
    return _render(
        request,
        ctx,
        "error.html",
        user,
        status=status,
        title=_STATUS_TITLES.get(status, "Error"),
        detail=detail,
        trace_id=getattr(request.state, "trace_id", ""),
    )


def _status_for(exc: SupportAgentError) -> int:
    if isinstance(exc, AuthorizationDenied):
        return 409 if exc.code == "stale_proposal" else 403
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, ConcurrencyConflict):
        return 409
    return 422


def _ui_user(request: Request, ctx: ApiContext) -> SessionUser | None:
    user_id = session.read(request, ctx).get("uid")
    return ctx.setup().user(user_id) if isinstance(user_id, str) else None


def _page(
    request: Request,
    ctx: ApiContext,
    permission: Permission,
    build: Callable[[SessionUser], Response],
) -> Response:
    """Auth + permission for a UI page; domain errors render the error page."""
    user = _ui_user(request, ctx)
    if user is None:
        return RedirectResponse("/ui/login", status_code=303)
    if not user.can(permission, anywhere=True):
        return _error(
            request, ctx, user, 403, f"This page needs the {permission.value} permission."
        )
    try:
        return build(user)
    except (NotFound, AuthorizationDenied, ConcurrencyConflict, DomainValidationError) as exc:
        return _error(request, ctx, user, _status_for(exc), str(exc))


async def _post(
    request: Request,
    ctx: ApiContext,
    permission: Permission,
    act: Callable[[SessionUser, Mapping[str, str]], Response],
) -> Response:
    """Every UI POST: CSRF form check first, then auth, permission and the action."""
    form = await csrf.verify_form(request)
    return await run_in_threadpool(_page, request, ctx, permission, lambda user: act(user, form))


# --- pages ------------------------------------------------------------------


@router.get("/static/app.css")
def stylesheet() -> Response:
    return Response(_CSS, media_type="text/css")


@router.get("/static/review.js")
def review_script() -> Response:
    return Response(_REVIEW_JS, media_type="text/javascript")


@router.get("/")
@router.get("/ui")
def ui_home() -> Response:
    """The approvals queue is the UI's landing page; it sends a signed-out user to login."""
    return RedirectResponse("/ui/approvals", status_code=303)


@router.get("/ui/login")
def login_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    return _render(request, ctx, "login.html", None)


@router.get("/ui/approvals")
def approvals_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        proposals = ctx.queries().proposals(status="pending_approval")
        return _render(request, ctx, "approvals.html", user, proposals=proposals)

    return _page(request, ctx, Permission.VIEW, build)


def _proposal_page(  # noqa: PLR0913 — the page plus its optional error state
    request: Request,
    ctx: ApiContext,
    user: SessionUser,
    proposal_id: str,
    *,
    status: int = 200,
    error: str | None = None,
) -> Response:
    proposal = ctx.queries().proposal(proposal_id)
    can_decide = proposal.status == "pending_approval" and user.can(
        Permission.DECIDE_PROPOSAL, service_role_scope(proposal.service)
    )
    return _render(
        request,
        ctx,
        "proposal.html",
        user,
        status=status,
        error=error,
        p=proposal,
        can_decide=can_decide,
    )


@router.get("/ui/proposals/{proposal_id}")
def proposal_page(
    proposal_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    return _page(
        request, ctx, Permission.VIEW, lambda user: _proposal_page(request, ctx, user, proposal_id)
    )


@router.get("/ui/cases/{case_id}")
def case_page(
    case_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def build(user: SessionUser) -> Response:
        view = ctx.queries().case(case_id)
        service = view.case.get("service")
        scope = service_role_scope(str(service) if service else None)
        can_hand_back = view.case.get("status") == "human_owned" and user.can(
            Permission.HAND_BACK, scope
        )
        # ADR-0014 part 2: the case's SOP auto decisions, from its decrypted trace.
        try:
            events = ctx.replay(str(view.case["trace_id"])).events
        except NotFound:  # a case whose trace has no events yet
            events = ()
        auto = auto_timeline(view, events)
        return _render(
            request,
            ctx,
            "case.html",
            user,
            c=view,
            can_hand_back=can_hand_back,
            auto=auto,
            can_retract=user.can(Permission.DECIDE_PROPOSAL, scope),
        )

    return _page(request, ctx, Permission.VIEW, build)


@router.get("/ui/cases")
def cases_page(
    request: Request,
    service: str = "",
    status: str = "",
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    """WP-225: every case, newest first: what each alert or ticket became."""

    def build(user: SessionUser) -> Response:
        rows = ctx.queries().cases(service=service or None, status=status or None)
        return _render(
            request, ctx, "cases.html", user, cases=rows, service=service, status_filter=status
        )

    return _page(request, ctx, Permission.VIEW, build)


# --- the system (ADR-0023 §3, WP-225) ------------------------------------------------


@router.get("/ui/system")
def system_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        facts = _review(ctx).facts()
        return _render(
            request,
            ctx,
            "system.html",
            user,
            o=system_view.overview(facts, _observed(ctx)),
            groups=list(system_view.GROUPS),
            counts=ctx.queries().case_counts(),
            summaries=_summaries(ctx),
            svg=graph_svg.render(facts, link=graph_svg.system_link) if facts else "",
        )

    return _page(request, ctx, Permission.VIEW, build)


@router.get("/ui/system/component")
def component_page(
    request: Request,
    id: str = "",
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def build(user: SessionUser) -> Response:
        facts = _review(ctx).facts()
        found = system_view.components(facts, _observed(ctx)).get(id)
        if found is None:
            raise NotFound(f"no component {id!r} in the system graph")  # noqa: TRY003
        around = system_view.neighbourhood(facts, id)
        return _render(
            request,
            ctx,
            "component.html",
            user,
            c=found,
            groups=list(system_view.GROUPS),
            summary=_summaries(ctx).get(id),
            svg=graph_svg.render(around, focus=id, link=graph_svg.system_link),
            cases=ctx.queries().cases(service=found.service, limit=50),
            detectors=_component_detectors(ctx, found.service),
        )

    return _page(request, ctx, Permission.VIEW, build)


@router.post("/ui/cases/{case_id}/hand-back")
async def hand_back_form(
    case_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, _form: Mapping[str, str]) -> Response:
        ctx.lifecycle().hand_back(case_id, user.id, grants=user.grants)
        return RedirectResponse(f"/ui/cases/{case_id}", status_code=303)

    return await _post(request, ctx, Permission.HAND_BACK, act)


@router.get("/ui/setup")
def setup_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        setup = ctx.setup()
        return _render(
            request,
            ctx,
            "setup.html",
            user,
            state=setup.state(),
            automation=setup.automation_actors(),
            doctor=ctx.queries().doctor_report(),
        )

    return _page(request, ctx, Permission.ADMINISTER, build)


@router.get("/ui/grants")
def grants_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        return _render(request, ctx, "grants.html", user, grants=ctx.queries().grants())

    return _page(request, ctx, Permission.ADMINISTER, build)


@router.get("/ui/stats")
def stats_page(
    request: Request,
    mode: str = "shadow",
    window: str = "30d",
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def build(user: SessionUser) -> Response:
        chosen: StatsMode = "assisted" if mode == "assisted" else "shadow"
        stats = ctx.stats()
        span = parse_window(window)
        report = stats.report(window=span, mode=chosen)
        return _render(
            request,
            ctx,
            "stats.html",
            user,
            r=report,
            window=window,
            gate=stats.promotion(ctx.promotion_gate, window=span) if chosen == "shadow" else None,
            fatigue=stats.fatigue(window=span),
            holds=stats.readiness_holds(window=span),
            calibration=ctx.calibration_in_use() if ctx.calibration_in_use else None,
        )

    return _page(request, ctx, Permission.VIEW, build)


# --- form posts -------------------------------------------------------------


@router.post("/ui/proposals/{proposal_id}/approve")
async def approve_form(
    proposal_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        ctx.approvals().approve(
            proposal_id,
            user.id,
            seen_hash=form.get("payload_hash", ""),
            reason=form.get("reason") or None,
            grants=user.grants,
        )
        return RedirectResponse(f"/ui/proposals/{proposal_id}", status_code=303)

    return await _post(request, ctx, Permission.DECIDE_PROPOSAL, act)


@router.post("/ui/proposals/{proposal_id}/retract")
async def retract_form(
    proposal_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    """ADR-0014 part 2: propose the retraction of an auto action; it waits for approval."""

    def act(user: SessionUser, _form: Mapping[str, str]) -> Response:
        retraction = ctx.approvals().retract(proposal_id, user.id, grants=user.grants)
        return RedirectResponse(f"/ui/proposals/{retraction}", status_code=303)

    return await _post(request, ctx, Permission.DECIDE_PROPOSAL, act)


@router.post("/ui/proposals/{proposal_id}/reject")
async def reject_form(
    proposal_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        reason = form.get("reason", "")
        if not reason.strip():
            return _proposal_page(
                request, ctx, user, proposal_id, status=422, error="A reason is required to reject."
            )
        ctx.approvals().reject(
            proposal_id,
            user.id,
            seen_hash=form.get("payload_hash", ""),
            reason=reason,
            grants=user.grants,
        )
        return RedirectResponse(f"/ui/proposals/{proposal_id}", status_code=303)

    return await _post(request, ctx, Permission.DECIDE_PROPOSAL, act)


@router.post("/ui/setup/approve")
async def setup_form(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def act(user: SessionUser, _form: Mapping[str, str]) -> Response:
        ctx.setup().run_setup(approver_id=user.id, requested_by=ctx.config_marker)
        return RedirectResponse("/ui/setup", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.post("/ui/grants/{grant_id}/approve")
async def grant_approve_form(
    grant_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        raw = form.get("valid_days", "365")
        if not raw.isdigit() or not 1 <= int(raw) <= 365:  # noqa: PLR2004 — 02 §2 bound
            raise DomainValidationError("valid_days must be between 1 and 365")  # noqa: TRY003
        days = int(raw)
        ctx.setup().approve_grant(grant_id, approver_id=user.id, valid_days=days)
        return RedirectResponse("/ui/grants", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.post("/ui/grants/{grant_id}/revoke")
async def grant_revoke_form(
    grant_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, _form: Mapping[str, str]) -> Response:
        ctx.setup().revoke_grant(grant_id, actor_id=user.id)
        return RedirectResponse("/ui/grants", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.get("/ui/users")
def users_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        return _render(
            request,
            ctx,
            "users.html",
            user,
            users=ctx.queries().users(),
            roles=[r.value for r in Role],
            services=ctx.setup().service_names(),
        )

    return _page(request, ctx, Permission.ADMINISTER, build)


@router.post("/ui/users/{user_id}/roles")
async def role_assign_form(
    user_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        try:
            role = Role(form.get("role", ""))
        except ValueError as exc:
            raise DomainValidationError(f"unknown role {form.get('role')!r}") from exc  # noqa: TRY003
        ctx.setup().assign_role(
            actor_id=user.id, user_id=user_id, role=role, scope=form.get("scope", "*")
        )
        return RedirectResponse("/ui/users", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.post("/ui/users/{user_id}/roles/{role}/revoke")
async def role_revoke_form(
    user_id: str,
    role: Role,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, _form: Mapping[str, str]) -> Response:
        ctx.setup().revoke_role(actor_id=user.id, user_id=user_id, role=role)
        return RedirectResponse("/ui/users", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.post("/ui/setup/automation/{sys_user}")
async def automation_form(
    sys_user: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        decision = form.get("decision", "")
        if decision not in ("automation", "human"):
            raise DomainValidationError("decision must be 'automation' or 'human'")  # noqa: TRY003
        ctx.setup().decide_automation_actor(
            actor_id=user.id, sys_user=sys_user, automation=decision == "automation"
        )
        return RedirectResponse("/ui/setup", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


# --- onboarding review (ADR-0015 §3, WP-O3b) -----------------------------------------


def _review(ctx: ApiContext) -> GraphReview:
    if ctx.graph_review is None:
        raise NotFound("the graph review is not configured")  # noqa: TRY003
    return ctx.graph_review()


def _summaries(ctx: ApiContext) -> dict[str, Any]:
    """Each component's summary (WP-222); none before onboarding writes them."""
    if ctx.component_summaries is None:
        return {}
    stored = ctx.component_summaries().load() or {}
    found = stored.get("components", {})
    return found if isinstance(found, dict) else {}


def _requests(ctx: ApiContext) -> RequestReview:
    if ctx.request_review is None:
        raise NotFound("the request review is not configured")  # noqa: TRY003
    return ctx.request_review()


def _trace_page(  # noqa: PLR0913 — the page plus its optional result and error
    request: Request,
    ctx: ApiContext,
    user: SessionUser,
    *,
    flow: str = "",
    object_id: str = "",
    result: tuple[TraceRun, TraceAnswer | None] | None = None,
    error: str | None = None,
    status: int = 200,
) -> Response:
    flows = ctx.flows() if ctx.flows is not None else {}
    run, answer = result if result is not None else (None, None)
    return _render(
        request,
        ctx,
        "trace.html",
        user,
        status=status,
        error=error,
        flows=flows,
        flow=flow,
        object_id=object_id,
        run=run,
        answer=answer,
        stages={s.id: s for f in flows.values() for s in f.stages if f.flow == flow},
    )


@router.get("/ui/trace")
def trace_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """ADR-0021: where is this business object, and is that where it should be?"""
    return _page(request, ctx, Permission.TRACE, lambda user: _trace_page(request, ctx, user))


@router.post("/ui/trace")
async def trace_form(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """One trace, audited as the signed-in user; a refusal shows on the form."""

    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        flow, object_id = form.get("flow", "").strip(), form.get("object_id", "").strip()
        if ctx.trace is None:
            raise NotFound("transaction tracing is not configured")  # noqa: TRY003
        try:
            result = ctx.trace(flow, object_id, f"user:{user.id}")
        except (ConfigError, DomainValidationError) as exc:
            return _trace_page(
                request, ctx, user, flow=flow, object_id=object_id, error=str(exc), status=422
            )
        return _trace_page(request, ctx, user, flow=flow, object_id=object_id, result=result)

    return await _post(request, ctx, Permission.TRACE, act)


@router.get("/ui/onboarding")
def onboarding_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        asks = _requests(ctx).progress() if ctx.request_review is not None else None
        ready = ctx.readiness().load() if ctx.readiness is not None else None
        run = ctx.onboarding_status().load() if ctx.onboarding_status is not None else None
        activity = ctx.onboarding_history() if ctx.onboarding_history is not None else []
        control = ctx.onboarding_control().state() if ctx.onboarding_control else None
        return _render(
            request,
            ctx,
            "onboarding.html",
            user,
            p=_review(ctx).progress(),
            asks=asks,
            ready=ready,
            run=run,
            need_labels=NEED_LABELS,
            activity=activity,
            control=control,
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


#: ADR-0023 §2: what /ui/onboarding/system offers before a manifest exists.
_STARTER_MANIFEST = """\
system:
  id: my-system            # lower case, digits and hyphens
  name: My system
sources:
  git:
    # A checkout mounted read-only into the instance (file:<dir>), or GitHub:
    # - repo: github:<owner>/<repository glob>
    #   token_env: GITHUB_TOKEN   # a read-only token, from the environment
    - repo: file:/mnt/system
      paths: ["**/*.py", "**/*compose*.y*ml", "**/Dockerfile*", "**/requirements*.txt",
              "**/pyproject.toml", "**/*.sql", "**/*.yaml", "**/*.yml", "**/*.md"]
      # Never read: archived copies, vendored trees (dependencies and secret files
      # are never read anyway).
      exclude: ["backups/**", "**/archive*/**"]
"""


def _control(ctx: ApiContext) -> OnboardingControl:
    if ctx.onboarding_control is None:
        raise NotFound("onboarding from the instance is not configured")  # noqa: TRY003
    return ctx.onboarding_control()


@router.get("/ui/onboarding/system")
def onboarding_system(
    request: Request,
    saved: str = "",
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    """ADR-0023 §2: the manifest, edited here and stored by the instance."""

    def build(user: SessionUser) -> Response:
        control = _control(ctx)
        return _render(
            request,
            ctx,
            "onboarding_system.html",
            user,
            manifest=control.manifest() or _STARTER_MANIFEST,
            state=control.state(),
            saved=saved,
            can_run=True,
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


@router.post("/ui/onboarding/system")
async def onboarding_system_save(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        control = _control(ctx)
        text = form.get("manifest", "").replace("\r\n", "\n")
        try:
            system = control.save_manifest(text, actor=user.id)
        except DomainValidationError as exc:
            return _render(
                request,
                ctx,
                "onboarding_system.html",
                user,
                status=422,
                error=str(exc),
                manifest=text,
                state=control.state(),
                saved="",
                can_run=True,
            )
        return RedirectResponse(f"/ui/onboarding/system?saved={system}", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.post("/ui/onboarding/run")
async def onboarding_run_now(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """ADR-0023 §2: ask the worker to run onboarding now (it continues where it stopped)."""

    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        del form
        _control(ctx).request_run(actor=user.id)
        return RedirectResponse("/ui/onboarding", status_code=303)

    return await _post(request, ctx, Permission.REVIEW_KNOWLEDGE, act)


def _detectors(ctx: ApiContext) -> DetectorReview:
    if ctx.detector_review is None:
        raise NotFound("detector review is not configured")  # noqa: TRY003
    return ctx.detector_review()


def _observed(ctx: ApiContext) -> list[str] | None:
    """The components the deployment sees running, from the last measurement (WP-221)."""
    if ctx.detector_review is None:
        return None
    found = _detectors(ctx).state().get("observed")
    return [str(o) for o in found] if isinstance(found, list) else None


def _component_detectors(ctx: ApiContext, service: str) -> list[Mapping[str, Any]]:
    """The detectors measured for one component, each marked enabled or not."""
    if ctx.detector_review is None:
        return []
    state = _detectors(ctx).state()
    return [d for d in state.get("detectors", []) if d.get("subject") == service]


_CLASS_LABELS = {
    "container_down": "Container down",
    "crash_loop": "Crash loop",
    "log_silence": "Log silence",
    "progress_stopped": "Progress stopped",
    "host_restart": "Host restart",
}


@router.get("/ui/onboarding/detectors")
def onboarding_detectors(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """ADR-0023 §2: what onboarding measured, to enable or leave off."""

    def build(user: SessionUser) -> Response:
        state = _detectors(ctx).state()
        return _render(
            request,
            ctx,
            "onboarding_detectors.html",
            user,
            state=state,
            labels=_CLASS_LABELS,
            enabled=state.get("enabled", {}),
            watches=ctx.detector_watches() if ctx.detector_watches is not None else {},
            saved=request.query_params.get("saved"),
        )

    return _page(request, ctx, Permission.VIEW, build)


@router.post("/ui/onboarding/detectors")
async def onboarding_detectors_save(
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        ids = [k.removeprefix("on:") for k in form if k.startswith("on:")]
        changed = _detectors(ctx).decide(ids, actor=user.id)
        return RedirectResponse(f"/ui/onboarding/detectors?saved={changed}", status_code=303)

    return await _post(request, ctx, Permission.ADMINISTER, act)


@router.get("/ui/onboarding/readiness")
def onboarding_readiness(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """WP-O6b: which incident classes the history proved, and why the others aren't ready."""

    def build(user: SessionUser) -> Response:
        if ctx.readiness is None:
            raise NotFound("readiness is not configured")  # noqa: TRY003
        return _render(
            request, ctx, "onboarding_readiness.html", user, report=ctx.readiness().load()
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


@router.get("/ui/onboarding/graph")
def onboarding_graph(
    request: Request,
    status: str = "",
    layer: str = "",
    focus: str = "",
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def build(user: SessionUser) -> Response:
        review = _review(ctx)
        chosen = int(layer) if layer.isdigit() else None
        cards = review.facts(status=status or None, layer=chosen, focus=focus or None)
        drawing = graph_svg.render(review.facts(), focus=focus or None)
        back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return _render(
            request,
            ctx,
            "onboarding_graph.html",
            user,
            facts=cards,
            svg=drawing,
            status_filter=status,
            layer=chosen,
            focus=focus,
            layers=LAYER_LABELS,
            back=back,
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


@router.get("/ui/onboarding/questions")
def onboarding_questions(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        return _render(
            request, ctx, "onboarding_questions.html", user, questions=_review(ctx).questions()
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


@router.post("/ui/onboarding/facts")
async def onboarding_decide(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    """One card's button (``one=<id>|<decision>``) or the checked cards and a bulk decision."""

    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        if form.get("one"):
            fid, _, decision = form["one"].partition("|")
            ids = [fid]
        else:
            decision = form.get("decision", "")
            ids = [k.removeprefix("id_") for k in form if k.startswith("id_")]
        _review(ctx).decide(ids, decision, actor=user.id)  # type: ignore[arg-type]
        back = form.get("back", "")
        target = back if back.startswith("/ui/onboarding") else "/ui/onboarding/graph"
        return RedirectResponse(target, status_code=303)

    return await _post(request, ctx, Permission.REVIEW_KNOWLEDGE, act)


@router.post("/ui/onboarding/layer")
async def onboarding_decide_layer(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        raw = form.get("layer", "")
        if not raw.isdigit():
            raise DomainValidationError("layer must be a number")  # noqa: TRY003
        _review(ctx).decide_layer(int(raw), form.get("decision", ""), actor=user.id)  # type: ignore[arg-type]
        return RedirectResponse("/ui/onboarding", status_code=303)

    return await _post(request, ctx, Permission.REVIEW_KNOWLEDGE, act)


@router.post("/ui/onboarding/questions/{question_id}")
async def onboarding_resolve(
    question_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        raw = form.get("choice", "")
        if not raw.isdigit():
            raise DomainValidationError("choose one of the options")  # noqa: TRY003
        _review(ctx).resolve(question_id, int(raw), actor=user.id)
        return RedirectResponse("/ui/onboarding/questions", status_code=303)

    return await _post(request, ctx, Permission.REVIEW_KNOWLEDGE, act)


# --- onboarding requests (ADR-0015 §4, WP-O5) -----------------------------------------


@router.get("/ui/onboarding/requests")
def onboarding_requests(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        return _render(
            request, ctx, "onboarding_requests.html", user, requests=_requests(ctx).requests()
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


@router.post("/ui/onboarding/requests/{request_id}")
async def onboarding_request_decide(
    request_id: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    def act(user: SessionUser, form: Mapping[str, str]) -> Response:
        _requests(ctx).decide(request_id, form.get("decision", ""), actor=user.id)  # type: ignore[arg-type]
        return RedirectResponse("/ui/onboarding/requests", status_code=303)

    return await _post(request, ctx, Permission.REVIEW_KNOWLEDGE, act)
