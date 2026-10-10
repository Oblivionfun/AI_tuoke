from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from workbench.store.models import User
from workbench.store.users import authenticate
from workbench.web.deps import get_wb, redirect

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    with get_wb(request).db.session() as s:
        has_users = (s.scalar(select(func.count(User.id))) or 0) > 0
    return request.app.state.templates.TemplateResponse(request, "pages/login.html", {"has_users": has_users})


@router.post("/login", response_model=None)
def login(request: Request, username: str = Form(...), password: str = Form(...)) -> Response:
    with get_wb(request).db.session() as s:
        user = authenticate(s, username.strip(), password)
    if user is None:
        context = {
            "has_users": True,
            "error": "账号或密码不对。请检查后重试；忘记密码请让管理员用 workbench user-add 重设。",
            "username": username,
        }
        return request.app.state.templates.TemplateResponse(request, "pages/login.html", context, status_code=401)
    request.session.clear()
    request.session["user"] = user.username
    return redirect("/today")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return redirect("/login")
