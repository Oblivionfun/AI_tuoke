"""TikTok 询盘：入口和页面先留好，后端未接入。设计见 docs/05-规划功能.md。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from workbench.web.deps import current_user, render

router = APIRouter()

PREREQUISITES = [
    ("TikTok 企业号", "冠维注册企业号，绑定 TikTok for Business 或 Business Center"),
    ("开发者应用和权限", "在 TikTok API for Business 建应用，申请评论管理和私信权限，企业号授权给应用"),
    ("海外服务器", "TikTok 在国内打不开，接收推送和调用接口要放在海外服务器上"),
    ("私信设置", "企业号私信设为所有人可发，否则私信要在手机上手动接受"),
]


@router.get("/tiktok", response_class=HTMLResponse)
def tiktok(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return render(request, "pages/tiktok.html", nav="tiktok", prerequisites=PREREQUISITES)
