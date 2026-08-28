import csv as csv_mod
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from paas.db import connect
from paas.modules.admin import service as admin_service
from paas.modules.account import service as account_service

router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class BotBody(BaseModel):
    platform: str
    name: str = ""
    enabled: bool = False
    fields: dict[str, Any] = {}


class BotUpdateBody(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    fields: dict[str, Any] | None = None


class TestBody(BaseModel):
    fields: dict[str, Any] = {}


class SettingsBody(BaseModel):
    updates: dict[str, Any] = {}


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "user"


class AccountsBody(BaseModel):
    templates: list[dict[str, Any]] = []


class AiBody(BaseModel):
    mode: str = "off"
    model: str = "qwen2.5:0.5b"
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: str = "45"


class BackfillBody(BaseModel):
    namespace: str = "default"
    user_id: str
    mappings: list[dict[str, str]] = []


def _require_session(request: Request) -> str:
    token = request.cookies.get("paas_session")
    if not admin_service.check_session(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return token


def _require_admin(request: Request) -> str:
    token = _require_session(request)
    if not admin_service.is_admin(token):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return token


def _viewer(request: Request) -> tuple[int, bool]:
    token = _require_session(request)
    info = admin_service.session_info(token)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, role FROM admin_users WHERE username = ?", (info["username"],)
        ).fetchone()
    finally:
        conn.close()
    return row["id"], row["role"] == "admin"


def _state(request: Request):
    return request.app.state


@router.post("/login")
def login(body: LoginBody) -> Response:
    token = admin_service.login(body.username.strip(), body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    info = admin_service.session_info(token)
    resp = JSONResponse({"ok": True, "username": body.username.strip(), "role": info["role"]})
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
    token = _require_session(request)
    info = admin_service.session_info(token)
    return {"username": info["username"], "role": info["role"]}


# ---------- 机器人 ----------

@router.get("/bots")
def list_bots(request: Request) -> dict:
    viewer_id, admin = _viewer(request)
    conn = connect()
    try:
        return {"bots": admin_service.list_bots(conn, viewer_id, admin), "max_per_platform": admin_service.MAX_BOTS_PER_PLATFORM}
    finally:
        conn.close()


@router.post("/bots")
async def create_bot(request: Request, body: BotBody) -> dict:
    viewer_id, admin = _viewer(request)
    conn = connect()
    try:
        ok, msg = admin_service.create_bot(conn, viewer_id, body.platform, body.name, body.fields, body.enabled)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        await _state(request).manager.apply_configs(conn)
    finally:
        conn.close()
    return {"ok": True, "bot_id": msg}


@router.get("/bots/{bot_id}")
def get_bot(request: Request, bot_id: str) -> dict:
    viewer_id, admin = _viewer(request)
    conn = connect()
    try:
        bot = admin_service.get_bot(conn, bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        if not admin and bot["owner_id"] != viewer_id:
            raise HTTPException(status_code=403, detail="无权访问该机器人")
        return bot
    finally:
        conn.close()


@router.put("/bots/{bot_id}")
async def update_bot(request: Request, bot_id: str, body: BotUpdateBody) -> dict:
    viewer_id, admin = _viewer(request)
    conn = connect()
    try:
        bot = admin_service.get_bot(conn, bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        if not admin and bot["owner_id"] != viewer_id:
            raise HTTPException(status_code=403, detail="无权修改该机器人")
        if not admin_service.update_bot(conn, bot_id, fields=body.fields, enabled=body.enabled, name=body.name):
            raise HTTPException(status_code=404, detail="机器人不存在")
        await _state(request).manager.apply_configs(conn)
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/bots/{bot_id}")
async def delete_bot(request: Request, bot_id: str) -> dict:
    viewer_id, admin = _viewer(request)
    conn = connect()
    try:
        bot = admin_service.get_bot(conn, bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        if not admin and bot["owner_id"] != viewer_id:
            raise HTTPException(status_code=403, detail="无权删除该机器人")
        if not admin_service.delete_bot(conn, bot_id):
            raise HTTPException(status_code=404, detail="机器人不存在")
        await _state(request).manager.apply_configs(conn)
    finally:
        conn.close()
    return {"ok": True}


@router.post("/bots/{bot_id}/test")
async def test_bot(request: Request, bot_id: str, body: TestBody) -> dict:
    _require_session(request)
    conn = connect()
    try:
        bot = admin_service.get_bot(conn, bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
    finally:
        conn.close()
    ok, msg = await admin_service.test_config(bot["platform"], body.fields, bot_id=bot_id)
    return {"ok": ok, "message": msg}


# ---------- 用户管理（仅管理员） ----------

@router.get("/users")
def list_users(request: Request) -> dict:
    _require_admin(request)
    conn = connect()
    try:
        return {"users": admin_service.list_users(conn)}
    finally:
        conn.close()


@router.post("/users")
def create_user(request: Request, body: UserBody) -> dict:
    _require_admin(request)
    conn = connect()
    try:
        ok, msg = admin_service.create_user(conn, body.username, body.password, body.role)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: int) -> dict:
    _require_admin(request)
    conn = connect()
    try:
        ok, msg = admin_service.delete_user(conn, user_id)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()
    return {"ok": True}


@router.get("/bots/{bot_id}/accounts")
def bot_accounts(request: Request, bot_id: str) -> dict:
    _require_session(request)
    conn = connect()
    try:
        return {"templates": admin_service.list_account_templates(conn, bot_id),
                "users": admin_service.bot_users(conn, bot_id)}
    finally:
        conn.close()


@router.put("/bots/{bot_id}/accounts")
def put_bot_accounts(request: Request, bot_id: str, body: AccountsBody) -> dict:
    _require_session(request)
    conn = connect()
    try:
        ok, msg = admin_service.replace_account_templates(conn, bot_id, body.templates)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()
    return {"ok": True, "message": msg}


# ---------- AI ----------

@router.get("/ai")
def get_ai(request: Request) -> dict:
    _require_session(request)
    conn = connect()
    try:
        return admin_service.get_ai_settings(conn)
    finally:
        conn.close()


@router.put("/ai")
def put_ai(request: Request, body: AiBody) -> dict:
    _require_session(request)
    if body.mode not in ("off", "ollama", "cloud"):
        raise HTTPException(status_code=400, detail="mode 必须是 off/ollama/cloud")
    conn = connect()
    try:
        applied = admin_service.put_ai_settings(
            conn,
            {
                "ai_mode": body.mode,
                "ai_model": body.model,
                "ai_base_url": body.base_url,
                "ai_api_key": body.api_key,
                "ai_timeout_seconds": body.timeout_seconds,
            },
        )
    finally:
        conn.close()
    return {"ok": True, "applied": applied}


@router.post("/ai/status")
async def ai_status(request: Request) -> dict:
    _require_session(request)
    from paas.interpreter.core import ollama_status

    conn = connect()
    try:
        base = admin_service.get_ai_settings(conn).get("base_url") or "http://localhost:11434"
    finally:
        conn.close()
    return await ollama_status(base)


@router.post("/ai/pull")
async def ai_pull(request: Request, body: dict | None = None) -> dict:
    _require_session(request)
    from paas.interpreter.core import ollama_pull

    model = (body or {}).get("model") or "qwen2.5:0.5b"
    conn = connect()
    try:
        base = admin_service.get_ai_settings(conn).get("base_url") or "http://localhost:11434"
    finally:
        conn.close()
    ok, msg = await ollama_pull(model, base)
    return {"ok": ok, "message": msg}


# ---------- 最近对话 / 回填 ----------

@router.get("/conversations")
def conversations(request: Request, limit: int = 50) -> dict:
    _require_session(request)
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, namespace, platform, message_id, user_id, content, reply, created_at
            FROM raw_messages ORDER BY id DESC LIMIT ?
            """,
            (min(max(limit, 1), 200),),
        ).fetchall()
        return {"conversations": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/backfill/preview")
def backfill_preview(request: Request, namespace: str = "default", user_id: str = "", keyword: str = "") -> dict:
    _require_session(request)
    if not user_id or not keyword:
        raise HTTPException(status_code=400, detail="缺少 user_id 或 keyword")
    conn = connect()
    try:
        return account_service.backfill_preview(conn, namespace, user_id, keyword)
    finally:
        conn.close()


@router.post("/backfill/apply")
def backfill_apply(request: Request, body: BackfillBody) -> dict:
    _require_session(request)
    conn = connect()
    try:
        return account_service.backfill_apply(conn, body.namespace, body.user_id, body.mappings)
    finally:
        conn.close()


# ---------- 状态 / 设置 / 备份 / 导入导出 ----------

@router.get("/status")
def status(request: Request) -> dict:
    _require_session(request)
    state = _state(request)
    conn = connect()
    try:
        expense_count = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()
        import_count = conn.execute("SELECT COUNT(*) AS n FROM imports").fetchone()
        bot_count = conn.execute("SELECT COUNT(*) AS n FROM bot_configs").fetchone()
    finally:
        conn.close()
    return {
        "adapters": state.manager.status(),
        "expenses": expense_count["n"],
        "imports": import_count["n"],
        "bots": bot_count["n"],
        "scheduler_running": getattr(state.scheduler, "running", False),
    }


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
    username = admin_service.session_info(token)["username"]
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
    namespace: str = "default",
    format: str = "csv",
    start: str | None = None,
    end: str | None = None,
) -> Response:
    _require_session(request)
    conn = connect()
    try:
        where = "WHERE e.namespace = ? AND e.user_id = ?"
        params: list[Any] = [namespace, user_id]
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
