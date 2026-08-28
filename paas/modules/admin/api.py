import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from paas.db import connect
from paas.modules.admin import service as admin_service

router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class ConfigBody(BaseModel):
    enabled: bool = False
    fields: dict[str, Any] = {}


class TestBody(BaseModel):
    fields: dict[str, Any] = {}


class SettingsBody(BaseModel):
    updates: dict[str, Any] = {}


def _require_session(request: Request) -> None:
    token = request.cookies.get("paas_session")
    if not admin_service.check_session(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")


def _state(request: Request):
    return request.app.state


@router.post("/login")
def login(body: LoginBody) -> Response:
    token = admin_service.login(body.username.strip(), body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    resp = JSONResponse({"ok": True, "username": body.username.strip()})
    resp.set_cookie(
        "paas_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=admin_service.settings.session_ttl_hours * 3600,
    )
    return resp


@router.post("/logout")
def logout(request: Request) -> dict:
    admin_service.logout(request.cookies.get("paas_session"))
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    _require_session(request)
    token = request.cookies.get("paas_session")
    return {"username": admin_service.SESSIONS.get(token or "", {}).get("username", "")}


@router.get("/status")
def status(request: Request) -> dict:
    _require_session(request)
    state = _state(request)
    conn = connect()
    try:
        expense_count = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()
        import_count = conn.execute("SELECT COUNT(*) AS n FROM imports").fetchone()
    finally:
        conn.close()
    return {
        "adapters": state.manager.status(),
        "expenses": expense_count["n"],
        "imports": import_count["n"],
        "scheduler_running": getattr(state.scheduler, "running", False),
    }


@router.get("/config/{platform}")
def get_config(request: Request, platform: str) -> dict:
    _require_session(request)
    conn = connect()
    try:
        return admin_service.get_config(conn, platform)
    finally:
        conn.close()


@router.put("/config/{platform}")
async def put_config(request: Request, platform: str, body: ConfigBody) -> dict:
    _require_session(request)
    if platform not in admin_service.CONFIG_DEFAULTS:
        raise HTTPException(status_code=400, detail="未知平台")
    conn = connect()
    try:
        admin_service.set_config(conn, platform, body.fields, body.enabled)
        await _state(request).manager.apply_configs(conn)
    finally:
        conn.close()
    return {"ok": True}


@router.post("/config/{platform}/test")
async def test_config(request: Request, platform: str, body: TestBody) -> dict:
    _require_session(request)
    if platform not in admin_service.CONFIG_DEFAULTS:
        raise HTTPException(status_code=400, detail="未知平台")
    ok, msg = await admin_service.test_config(platform, body.fields)
    return {"ok": ok, "message": msg}


@router.get("/settings")
def get_settings(request: Request) -> dict:
    _require_session(request)
    conn = connect()
    try:
        return admin_service.get_settings(conn)
    finally:
        conn.close()


@router.put("/settings")
def put_settings(request: Request, body: SettingsBody) -> dict:
    _require_session(request)
    conn = connect()
    try:
        applied = admin_service.put_settings(conn, body.updates)
    finally:
        conn.close()
    scheduler = _state(request).scheduler
    if scheduler is not None:
        scheduler.reschedule()
    return {"ok": True, "applied": applied}


@router.post("/password")
def change_password(request: Request, body: PasswordBody) -> dict:
    _require_session(request)
    token = request.cookies.get("paas_session")
    username = admin_service.SESSIONS.get(token or "", {}).get("username", "")
    ok, msg = admin_service.change_password(username, body.old_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/backup")
def backup(request: Request) -> dict:
    _require_session(request)
    conn = connect()
    try:
        path = admin_service.backup_now(conn)
    finally:
        conn.close()
    return {"ok": True, "path": path}


@router.get("/imports")
def imports(request: Request) -> dict:
    _require_session(request)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, filename, file_type, total_rows, success_rows, failed_rows, "
            "created_at FROM imports ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return {"imports": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/import-template")
def import_template(request: Request) -> Response:
    _require_session(request)
    data = admin_service.import_template_csv()
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="paas_import_template.csv"'},
    )


@router.get("/export")
def export_ledger(
    request: Request,
    user_id: str,
    format: str = "csv",
    start: str | None = None,
    end: str | None = None,
) -> Response:
    _require_session(request)
    conn = connect()
    try:
        where = "WHERE e.user_id = ?"
        params: list[Any] = [user_id]
        if start:
            where += " AND e.expense_date >= ?"
            params.append(start)
        if end:
            where += " AND e.expense_date <= ?"
            params.append(end)
        rows = conn.execute(
            f"""
            SELECT e.id, e.expense_date, e.tx_type, c.name AS category_name,
                   a.name AS account_name, e.amount_cents, e.description, e.status, e.raw_text
            FROM expenses e JOIN categories c ON c.id = e.category_id
            LEFT JOIN accounts a ON a.id = e.account_id
            {where}
            ORDER BY e.expense_date DESC, e.id DESC
            """,
            params,
        ).fetchall()
        headers = ["ID", "日期", "类型", "分类", "账户", "金额(元)", "备注", "状态", "原始消息"]
        type_map = {
            "expense": "支出", "income": "收入", "refund": "退款", "fee": "手续费",
            "adjust": "平账调整", "transfer_out": "转出", "transfer_in": "转入",
        }
        if format == "xlsx":
            from openpyxl import Workbook
            from openpyxl.styles import Font

            wb = Workbook()
            ws = wb.active
            ws.title = "账本"
            ws.append(headers)
            for c in ws[1]:
                c.font = Font(bold=True)
            for r in rows:
                ws.append(
                    [
                        r["id"], r["expense_date"], type_map.get(r["tx_type"], r["tx_type"]),
                        r["category_name"], r["account_name"] or "",
                        r["amount_cents"] / 100, r["description"], r["status"], r["raw_text"] or "",
                    ]
                )
            buf = io.BytesIO()
            wb.save(buf)
            return Response(
                content=buf.getvalue(),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": 'attachment; filename="paas_ledger.xlsx"'},
            )
        import csv as csv_mod

        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(headers)
        for r in rows:
            writer.writerow(
                [
                    r["id"], r["expense_date"], type_map.get(r["tx_type"], r["tx_type"]),
                    r["category_name"], r["account_name"] or "",
                    f"{r['amount_cents'] / 100:.2f}", r["description"], r["status"], r["raw_text"] or "",
                ]
            )
        return Response(
            content=buf.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="paas_ledger.csv"'},
        )
    finally:
        conn.close()
