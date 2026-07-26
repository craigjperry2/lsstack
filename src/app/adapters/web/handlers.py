"""Server-rendered Litestar handlers with ordinary forms and HTMX enhancement."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from litestar import Request, get, post
from litestar.params import FromPath  # noqa: TC002
from litestar.response import Redirect, Response, Template

from app.adapters.security.sessions import claims_from_session, claims_to_session
from app.adapters.web.forms import (
    password_change_form,
    registration_form,
    task_form,
    text,
)
from app.adapters.web.types import CurrentUser, TaskView
from app.application.auth import (
    AuthResult,
    authenticate,
    issue_session,
    register,
    revalidate_session,
)
from app.application.profiles import change_password
from app.application.tasks import (
    TaskResult,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    toggle_task,
    update_task,
)
from app.domain.errors import (
    CurrentPasswordMismatchError,
    DuplicateEmailError,
    InvalidCredentialsError,
    InvalidSessionError,
    TaskNotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy.orm import Session

    from app.adapters.web.dependencies import WebDependencies
    from app.application.auth import UserSession
    from app.application.ports.persistence import UnitOfWork

WebRequest = Request[Any, Any, Any]
WebResponse = Template | Redirect | Response[bytes]


def _dependencies(request: WebRequest) -> WebDependencies:
    return cast("WebDependencies", request.app.state.web_dependencies)


def _is_htmx(request: WebRequest) -> bool:
    return request.headers.get("HX-Request", "").casefold() == "true"


def _redirect(path: str) -> Redirect:
    return Redirect(path=path, status_code=303)


def _replace_session(request: WebRequest, values: Mapping[str, object]) -> None:
    request.session.clear()
    request.session.update(values)


def _task_view(result: TaskResult, dependencies: WebDependencies) -> TaskView:
    return TaskView(
        public_id=dependencies.public_ids.encode(result.id),
        title=result.title,
        description=result.description,
        is_completed=result.is_completed,
        background_processed_at=result.background_processed_at,
        created_at=result.created_at,
    )


async def _current_user(request: WebRequest) -> CurrentUser | None:
    claims = claims_from_session(dict(request.session))
    if claims is None:
        if request.session:
            request.clear_session()
        return None
    dependencies = _dependencies(request)

    def operation(session: Session) -> UserSession:
        return revalidate_session(
            dependencies.unit_of_work(session),
            dependencies.clock,
            claims=claims,
        )

    try:
        session_user = await dependencies.transactions.run(operation)
    except InvalidSessionError:
        request.clear_session()
        return None
    return CurrentUser(
        id=session_user.user_id,
        email=session_user.email_normalized,
        session_version=session_user.session_version,
    )


async def _run_authenticated[T](
    request: WebRequest,
    operation: Callable[[UnitOfWork, CurrentUser], T],
) -> tuple[CurrentUser, T] | None:
    """Revalidate and run one use case in the same synchronous transaction."""
    claims = claims_from_session(dict(request.session))
    if claims is None:
        if request.session:
            request.clear_session()
        return None
    dependencies = _dependencies(request)

    def transaction(session: Session) -> tuple[CurrentUser, T]:
        unit_of_work = dependencies.unit_of_work(session)
        session_user = revalidate_session(
            unit_of_work,
            dependencies.clock,
            claims=claims,
        )
        current_user = CurrentUser(
            id=session_user.user_id,
            email=session_user.email_normalized,
            session_version=session_user.session_version,
        )
        return current_user, operation(unit_of_work, current_user)

    try:
        return await dependencies.transactions.run(transaction)
    except InvalidSessionError:
        request.clear_session()
        return None


def _not_found(current_user: CurrentUser | None = None) -> Template:
    return Template(
        template_name="errors/not_found.html",
        context={"current_user": current_user},
        status_code=404,
    )


@get("/", name="root")
async def root(request: WebRequest) -> Redirect:
    return _redirect("/tasks" if await _current_user(request) else "/login")


@get("/register", name="register-form")
async def register_form(request: WebRequest) -> WebResponse:
    if await _current_user(request):
        return _redirect("/tasks")
    return Template(
        template_name="auth/register.html",
        context={"current_user": None, "errors": (), "values": {}},
    )


@post("/register", name="register")
async def register_action(request: WebRequest) -> WebResponse:
    if await _current_user(request):
        return _redirect("/tasks")
    submitted = registration_form(await request.form())
    context = {
        "current_user": None,
        "errors": submitted.errors,
        "values": {"email": submitted.values["email"]},
    }
    if submitted.errors:
        return Template(
            template_name="auth/register.html", context=context, status_code=422
        )
    dependencies = _dependencies(request)

    def operation(session: Session) -> AuthResult:
        return register(
            dependencies.unit_of_work(session),
            dependencies.passwords,
            dependencies.clock,
            email=submitted.values["email"],
            password=submitted.values["password"],
        )

    try:
        result = await dependencies.transactions.run(operation)
    except DuplicateEmailError:
        context["errors"] = ("An account with this email already exists.",)
        return Template(
            template_name="auth/register.html", context=context, status_code=409
        )
    except ValidationError as error:
        context["errors"] = (error.message,)
        return Template(
            template_name="auth/register.html", context=context, status_code=422
        )
    claims = issue_session(
        result,
        dependencies.clock,
        lifetime=timedelta(seconds=dependencies.session_lifetime_seconds),
    )
    _replace_session(request, claims_to_session(claims))
    return _redirect("/tasks")


@get("/login", name="login-form")
async def login_form(request: WebRequest) -> WebResponse:
    if await _current_user(request):
        return _redirect("/tasks")
    return Template(
        template_name="auth/login.html",
        context={"current_user": None, "errors": (), "values": {}},
    )


@post("/login", name="login")
async def login_action(request: WebRequest) -> WebResponse:
    if await _current_user(request):
        return _redirect("/tasks")
    form = await request.form()
    email = text(form, "email")
    password = text(form, "password")
    dependencies = _dependencies(request)

    def operation(session: Session) -> AuthResult:
        return authenticate(
            dependencies.unit_of_work(session),
            dependencies.passwords,
            dependencies.clock,
            email=email,
            password=password,
        )

    try:
        result = await dependencies.transactions.run(operation)
    except (InvalidCredentialsError, ValidationError):
        return Template(
            template_name="auth/login.html",
            context={
                "current_user": None,
                "errors": ("Invalid email or password.",),
                "values": {"email": email},
            },
            status_code=401,
        )
    claims = issue_session(
        result,
        dependencies.clock,
        lifetime=timedelta(seconds=dependencies.session_lifetime_seconds),
    )
    _replace_session(request, claims_to_session(claims))
    return _redirect("/tasks")


@post("/logout", name="logout")
async def logout_action(request: WebRequest) -> Redirect:
    def operation(_unit_of_work: UnitOfWork, _current_user: CurrentUser) -> None:
        return None

    await _run_authenticated(request, operation)
    request.clear_session()
    return _redirect("/login")


@get("/profile", name="profile")
async def profile(request: WebRequest) -> WebResponse:
    def operation(_unit_of_work: UnitOfWork, _current_user: CurrentUser) -> None:
        return None

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, _ = authenticated
    return Template(
        template_name="profile/show.html",
        context={"current_user": current_user, "errors": ()},
    )


@post("/profile/password", name="change-password")
async def change_password_action(request: WebRequest) -> WebResponse:
    submitted = password_change_form(await request.form())
    dependencies = _dependencies(request)

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> int | str | None:
        if submitted.errors:
            return None
        try:
            return change_password(
                unit_of_work,
                dependencies.passwords,
                dependencies.clock,
                user_id=current_user.id,
                current_password=submitted.values["current_password"],
                new_password=submitted.values["new_password"],
                new_password_confirmation=submitted.values["new_password_confirmation"],
            )
        except CurrentPasswordMismatchError:
            return "Current password is incorrect."
        except ValidationError as error:
            return error.message

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, outcome = authenticated
    errors = submitted.errors or ((outcome,) if isinstance(outcome, str) else ())
    if errors:
        return Template(
            template_name="profile/show.html",
            context={"current_user": current_user, "errors": errors},
            status_code=422,
        )
    assert isinstance(outcome, int)
    claims = issue_session(
        AuthResult(current_user.id, current_user.email, outcome),
        dependencies.clock,
        lifetime=timedelta(seconds=dependencies.session_lifetime_seconds),
    )
    _replace_session(request, claims_to_session(claims))
    return _redirect("/profile?password-changed=1")


@get("/tasks", name="tasks")
async def task_list(request: WebRequest) -> WebResponse:
    dependencies = _dependencies(request)

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> tuple[TaskResult, ...]:
        return list_tasks(unit_of_work, user_id=current_user.id)

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, results = authenticated
    tasks = tuple(_task_view(result, dependencies) for result in results)
    return Template(
        template_name="tasks/list.html",
        context={
            "current_user": current_user,
            "tasks": tasks,
            "errors": (),
            "values": {},
        },
    )


@post("/tasks", name="create-task")
async def create_task_action(request: WebRequest) -> WebResponse:
    submitted = task_form(await request.form())
    dependencies = _dependencies(request)

    if submitted.errors:

        def list_operation(
            unit_of_work: UnitOfWork, current_user: CurrentUser
        ) -> tuple[TaskResult, ...]:
            return list_tasks(unit_of_work, user_id=current_user.id)

        authenticated = await _run_authenticated(request, list_operation)
        if authenticated is None:
            return _redirect("/login")
        current_user, results = authenticated
        context = {
            "current_user": current_user,
            "tasks": tuple(_task_view(result, dependencies) for result in results),
            "errors": submitted.errors,
            "values": submitted.values,
        }
        if _is_htmx(request):
            return Template(
                template_name="tasks/create_validation.html",
                context=context,
                status_code=422,
            )
        return Template(
            template_name="tasks/list.html", context=context, status_code=422
        )

    def operation(unit_of_work: UnitOfWork, current_user: CurrentUser) -> TaskResult:
        result = create_task(
            unit_of_work,
            dependencies.clock,
            user_id=current_user.id,
            title=submitted.values["title"],
            description=submitted.values["description"] or None,
        )
        return result.task

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, created = authenticated
    if not _is_htmx(request):
        return _redirect("/tasks")
    return Template(
        template_name="tasks/create_response.html",
        context={
            "current_user": current_user,
            "task": _task_view(created, dependencies),
        },
    )


@get("/tasks/{public_id:str}/edit", name="edit-task-form")
async def edit_task_form(request: WebRequest, public_id: FromPath[str]) -> WebResponse:
    dependencies = _dependencies(request)
    task_id = dependencies.public_ids.decode(public_id)

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> TaskResult | None:
        if task_id is None:
            return None
        try:
            return get_task(
                unit_of_work,
                user_id=current_user.id,
                task_id=task_id,
            )
        except TaskNotFoundError:
            return None

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, result = authenticated
    if result is None:
        return _not_found(current_user)
    task = _task_view(result, dependencies)
    return Template(
        template_name="tasks/edit.html",
        context={
            "current_user": current_user,
            "task": task,
            "errors": (),
            "values": {},
        },
    )


@post("/tasks/{public_id:str}/edit", name="edit-task")
async def edit_task_action(
    request: WebRequest, public_id: FromPath[str]
) -> WebResponse:
    dependencies = _dependencies(request)
    task_id = dependencies.public_ids.decode(public_id)
    submitted = task_form(await request.form())

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> TaskResult | None:
        if task_id is None:
            return None
        try:
            if submitted.errors:
                return get_task(
                    unit_of_work,
                    user_id=current_user.id,
                    task_id=task_id,
                )
            return update_task(
                unit_of_work,
                dependencies.clock,
                user_id=current_user.id,
                task_id=task_id,
                title=submitted.values["title"],
                description=submitted.values["description"] or None,
            )
        except TaskNotFoundError:
            return None

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, result = authenticated
    if result is None:
        return _not_found(current_user)
    task = _task_view(result, dependencies)
    if submitted.errors:
        return Template(
            template_name="tasks/edit.html",
            context={
                "current_user": current_user,
                "task": task,
                "errors": submitted.errors,
                "values": submitted.values,
            },
            status_code=422,
        )
    if _is_htmx(request):
        return Template(
            template_name="tasks/row.html",
            context={
                "current_user": current_user,
                "task": task,
            },
        )
    return _redirect("/tasks")


@post("/tasks/{public_id:str}/toggle", name="toggle-task")
async def toggle_task_action(
    request: WebRequest, public_id: FromPath[str]
) -> WebResponse:
    dependencies = _dependencies(request)
    task_id = dependencies.public_ids.decode(public_id)

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> TaskResult | None:
        if task_id is None:
            return None
        try:
            return toggle_task(
                unit_of_work,
                dependencies.clock,
                user_id=current_user.id,
                task_id=task_id,
            )
        except TaskNotFoundError:
            return None

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, result = authenticated
    if result is None:
        return _not_found(current_user)
    if _is_htmx(request):
        return Template(
            template_name="tasks/row.html",
            context={
                "current_user": current_user,
                "task": _task_view(result, dependencies),
            },
        )
    return _redirect("/tasks")


@post("/tasks/{public_id:str}/delete", name="delete-task")
async def delete_task_action(
    request: WebRequest, public_id: FromPath[str]
) -> WebResponse:
    dependencies = _dependencies(request)
    task_id = dependencies.public_ids.decode(public_id)

    def operation(unit_of_work: UnitOfWork, current_user: CurrentUser) -> bool:
        if task_id is None:
            return False
        try:
            delete_task(
                unit_of_work,
                user_id=current_user.id,
                task_id=task_id,
            )
        except TaskNotFoundError:
            return False
        return True

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, deleted = authenticated
    if not deleted:
        return _not_found(current_user)
    if _is_htmx(request):
        return Response(content=b"", media_type="text/html", status_code=200)
    return _redirect("/tasks")


@get("/tasks/{public_id:str}/status", name="task-status")
async def task_status(request: WebRequest, public_id: FromPath[str]) -> WebResponse:
    dependencies = _dependencies(request)
    task_id = dependencies.public_ids.decode(public_id)

    def operation(
        unit_of_work: UnitOfWork, current_user: CurrentUser
    ) -> TaskResult | None:
        if task_id is None:
            return None
        try:
            return get_task(
                unit_of_work,
                user_id=current_user.id,
                task_id=task_id,
            )
        except TaskNotFoundError:
            return None

    authenticated = await _run_authenticated(request, operation)
    if authenticated is None:
        return _redirect("/login")
    current_user, result = authenticated
    if result is None:
        return _not_found(current_user)
    task = _task_view(result, dependencies)
    return Template(
        template_name="tasks/status.html",
        context={"current_user": current_user, "task": task},
    )


@get("/livez", name="liveness", exclude_from_csrf=True)
async def liveness() -> dict[str, str]:
    """Process liveness does not depend on external services."""
    return {"status": "ok"}


@get("/readyz", name="readiness", exclude_from_csrf=True)
async def readiness(request: WebRequest) -> Response[dict[str, str]]:
    """Readiness proves the request-scoped database transaction bridge works."""
    dependencies = _dependencies(request)
    try:
        ready = await dependencies.transactions.probe()
    except Exception:
        return Response(content={"status": "unavailable"}, status_code=503)
    status_code = 200 if ready else 503
    status = "ok" if ready else "unavailable"
    return Response(content={"status": status}, status_code=status_code)


ROUTE_HANDLERS = (
    root,
    register_form,
    register_action,
    login_form,
    login_action,
    logout_action,
    profile,
    change_password_action,
    task_list,
    create_task_action,
    edit_task_form,
    edit_task_action,
    toggle_task_action,
    delete_task_action,
    task_status,
    liveness,
    readiness,
)
