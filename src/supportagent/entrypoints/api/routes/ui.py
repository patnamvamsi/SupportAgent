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

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from supportagent.app.graph_review import LAYER_LABELS, GraphReview
from supportagent.app.queries import auto_timeline
from supportagent.app.request_review import RequestReview
from supportagent.app.setup import SessionUser
from supportagent.app.stats import Mode as StatsMode
from supportagent.app.stats import parse_window
from supportagent.domain.autopilot import NEED_LABELS
from supportagent.domain.errors import (
    AuthorizationDenied,
    ConcurrencyConflict,
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


def _requests(ctx: ApiContext) -> RequestReview:
    if ctx.request_review is None:
        raise NotFound("the request review is not configured")  # noqa: TRY003
    return ctx.request_review()


@router.get("/ui/onboarding")
def onboarding_page(request: Request, ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    def build(user: SessionUser) -> Response:
        asks = _requests(ctx).progress() if ctx.request_review is not None else None
        ready = ctx.readiness().load() if ctx.readiness is not None else None
        run = ctx.onboarding_status().load() if ctx.onboarding_status is not None else None
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
        )

    return _page(request, ctx, Permission.REVIEW_KNOWLEDGE, build)


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
