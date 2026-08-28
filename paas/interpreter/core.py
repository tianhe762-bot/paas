import asyncio
import json
import re

import httpx

from paas import settings_store, timeutil
from paas.modules.account.parser import parse_expense_date, yuan_to_cents

SYSTEM_PROMPT = (
    "你是个人记账助手。把用户的中文记账消息解析为 JSON，只输出 JSON，不要任何其他文字。"
    "JSON 字段：{\"date\":\"YYYY-MM-DD 或 今天/昨天/前天/8月20日\","
    "\"type\":\"expense|income|transfer|refund|fee|adjust\","
    "\"category\":\"餐饮|交通|购物|娱乐|生活|医疗|其他\","
    "\"amount\":25.5,\"account\":\"微信\",\"to_account\":\"银行卡\","
    "\"note\":\"备注\"}。"
    "type 为 transfer 时 account 是转出账户、to_account 是转入账户。"
)


class InterpreterDisabled(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI 输出中未找到 JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI 输出不是 JSON 对象")
    return data


async def _chat_completion(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"AI 响应格式异常: {data}") from exc
    return _extract_json(content)


async def ai_interpret(conn, content: str) -> dict:
    mode = settings_store.get_setting(conn, "ai_mode", "off") or "off"
    if mode == "off":
        raise InterpreterDisabled("AI 识别未启用")
    model = settings_store.get_setting(conn, "ai_model", "qwen2.5:0.5b") or "qwen2.5:0.5b"
    base_url = (settings_store.get_setting(conn, "ai_base_url", "") or "").strip()
    timeout = float(settings_store.get_setting(conn, "ai_timeout_seconds", "45") or 45)
    user_prompt = (
        f"记账消息：{content}\n"
        "请只输出 JSON。若信息不足（缺账户、缺时间、缺金额），对应字段给空字符串，不要编造。"
    )
    if mode == "ollama":
        base = base_url or "http://localhost:11434"
        url = base.rstrip("/") + "/v1/chat/completions"
        return await _chat_completion(
            url,
            {"Content-Type": "application/json"},
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "temperature": 0,
            },
            timeout,
        )
    if mode == "cloud":
        key_enc = settings_store.get_setting(conn, "ai_api_key", "") or ""
        if not key_enc:
            raise ValueError("云端 AI 未配置 API Key")
        from paas.security import decrypt_json

        api_key = decrypt_json(key_enc).get("key", "")
        if not api_key:
            raise ValueError("云端 AI 未配置 API Key")
        url = base_url.rstrip("/") + "/chat/completions"
        return await _chat_completion(
            url,
            {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
            },
            timeout,
        )
    raise InterpreterDisabled("AI 识别未启用")


TYPE_MAP = {
    "expense": "expense", "支出": "expense", "消费": "expense", "花": "expense",
    "income": "income", "收入": "income",
    "transfer": "transfer_out", "转账": "transfer_out", "转出": "transfer_out",
    "refund": "refund", "退款": "refund",
    "fee": "fee", "手续费": "fee",
    "adjust": "adjust", "平账": "adjust",
}


def ai_result_to_item(result: dict, categories, base_date):
    """AI JSON → ParsedItem；字段缺失/非法时抛 ValueError。"""
    from paas.models import ParsedItem

    amount = result.get("amount")
    try:
        amount_cents = int(round(float(amount) * 100))
    except (TypeError, ValueError) as exc:
        raise ValueError("AI 未给出有效金额") from exc
    if amount_cents <= 0:
        raise ValueError("AI 金额无效")

    date_str = str(result.get("date") or "").strip()
    if date_str:
        expense_date = parse_expense_date(date_str, timeutil.today())
    else:
        expense_date = None

    tx_type = TYPE_MAP.get(str(result.get("type") or "").strip(), "expense")
    cat_name = str(result.get("category") or "").strip() or "其他"
    cat = next((c for c in categories if c.name == cat_name), None)
    if cat is None:
        cat = next((c for c in categories if c.name == "其他"), categories[-1])

    item = ParsedItem(
        expense_date=expense_date or timeutil.today(),
        category_id=cat.id,
        category_name=cat.name,
        category_icon=cat.icon,
        account_name=str(result.get("account") or "").strip(),
        to_account_name=str(result.get("to_account") or "").strip(),
        tx_type=tx_type,
        amount_cents=amount_cents,
        description=str(result.get("note") or "").strip() or cat.name,
    )
    return item


async def ollama_status(base_url: str | None = None) -> dict:
    base = (base_url or "http://localhost:11434").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(base + "/api/tags")
            resp.raise_for_status()
            return {"ok": True, "models": [m.get("name") for m in resp.json().get("models", [])]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def ollama_pull(model: str, base_url: str | None = None) -> tuple[bool, str]:
    base = (base_url or "http://localhost:11434").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", base + "/api/pull", json={"model": model, "stream": False}) as resp:
                resp.raise_for_status()
                await resp.aread()
        return True, f"模型 {model} 已就绪"
    except Exception as exc:  # noqa: BLE001
        return False, f"拉取失败（请确认已启动 ollama 容器：docker compose --profile ai up -d）：{exc}"

