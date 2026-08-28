import json
import logging
import re

import httpx

from paas import settings_store, timeutil
from paas.db import connect
from paas.models import Attachment, InboundMessage, ParsedItem, Reply
from paas.modules.account import service as s
from paas.modules.account.parser import (
    detect_accounts,
    has_time_expression,
    parse_amount_cents,
    parse_amount_with_unit,
    parse_balance_command,
    parse_expense_date,
    parse_expenses,
    parse_modify_request,
    parse_time_range,
    yuan_to_cents,
)
from paas.modules.account.importer import parse_import
from paas.interpreter.core import interpret
from paas.modules.account.queries import day_summary, period_detail, recent_expenses, touch_chat

log = logging.getLogger("paas.router")

HELP_TEXT = (
    "📖 使用说明：\n"
    "· 记账：今天微信吃饭花了25（时间/账户/金额，缺啥问啥）\n"
    "· 类型：收入（工资5000）/ 转账（微信转银行卡500）/ 退款 / 手续费\n"
    "· 补记：昨天支付宝买显卡花了3999\n"
    "· 撤销：删除/撤销刚才那笔（软删除，保留历史）\n"
    "· 修改：刚才那笔不是25，是35\n"
    "· 平账：微信平账到90元（需确认）\n"
    "· 余额：微信还有多少钱 / 总资产\n"
    "· 统计：今天/昨天/本周/上周/本月/上月花了多少、这个月吃饭花了多少、详细一点\n"
    "· 设置初始余额：设置微信余额1000\n"
    "· 导入：发送 .csv/.xlsx 账本文件；导出：发送「导出」\n"
    "· 帮助：查看本说明"
)

DELETE_RE = re.compile(r"^(?:删除|撤销|删掉|作废|取消这笔)")
MODIFY_RE = re.compile(r"(?:修改|改一下|改下|记错了|记错|不对|不是|改成|改为|改到)")
BALANCE_QUERY_RE = re.compile(r"余额|总资产|还有多少钱|多少钱|资产")
STATS_EXACT = {
    "今天", "昨日", "昨天", "本周", "上周", "本月", "上月", "最近",
    "查询", "账本", "流水", "详细", "详情", "今日", "统计",
}
SET_BALANCE_RE = re.compile(
    r"(?:设置|设定|初始化)\s*(微信|支付宝|银行卡|信用卡|现金|[\u4e00-\u9fa5]{1,6})"
    r"(?:的)?(?:初始)?余额\s*([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万零]+)"
)


class Router:
    """机长（Command Router）：统一意图识别，分发到各业务模块。"""

    def __init__(self) -> None:
        self._download_client = httpx.AsyncClient(timeout=30.0)

    async def handle(self, msg: InboundMessage) -> Reply:
        conn = connect()
        try:
            reply = await self._process(conn, msg)
            if reply and reply.reply_content:
                conn.execute(
                    "UPDATE raw_messages SET reply = ? "
                    "WHERE namespace = ? AND platform = ? AND message_id = ?",
                    (reply.reply_content[:2000], msg.namespace, msg.platform, msg.message_id),
                )
                conn.commit()
            return reply
        finally:
            conn.close()

    async def _process(self, conn, msg: InboundMessage) -> Reply:
        try:
            touch_chat(conn, msg.namespace, msg.user_id, msg.platform, msg.chat_id)
            s.save_raw_message(conn, msg.namespace, msg.platform, msg.message_id, msg.user_id, msg.content)

            file_attach = self._pick_import_attachment(msg)
            if file_attach is not None:
                return await self._handle_import(conn, msg, file_attach)

            content = msg.content.strip()
            if not content:
                return Reply(status="unrecognized", reply_content="收到空消息，请发送消费明细。")

            pending = s.get_pending(conn, msg.namespace, msg.user_id)
            if pending is not None:
                return await self._handle_pending(conn, msg, pending, content)

            # 防重：同一消息 ID 重发直接拒绝，与 30 秒防抖是两套独立机制
            if s.message_already_processed(conn, msg.namespace, msg.platform, msg.message_id):
                return Reply(status="duplicate", reply_content="⚠️ 该消息已记录过，请勿重复发送。")

            ai_match = re.match(r"^(?:用AI|AI识别|AI解析)[:：]\s*(.+)", content)
            if ai_match:
                return await self._handle_ai(conn, msg, ai_match.group(1).strip())

            if DELETE_RE.search(content):
                return await self._handle_delete(conn, msg, content)
            if MODIFY_RE.search(content):
                return await self._handle_modify(conn, msg, content)
            if "平账" in content or content.startswith("对账"):
                return await self._handle_balance(conn, msg, content)

            m = SET_BALANCE_RE.search(content)
            if m:
                return self._handle_set_balance(conn, msg.namespace, msg.user_id, m.group(1), m.group(2))

            if self._is_query(content):
                return await self._handle_query(conn, msg, content)

            if content.startswith("导出"):
                return await self._handle_export(conn, msg.namespace, msg.user_id)

            if content in {"帮助", "help", "命令", "菜单", "?", "？"}:
                return Reply(status="success", reply_content=HELP_TEXT)

            if content in s.ZERO_PHRASES:
                s.mark_zero(conn, msg.namespace, msg.user_id)
                return Reply(status="success", reply_content="✅ 已确认今日无消费，无需记账。")
            if content in s.SKIP_PHRASES:
                s.mark_skipped(conn, msg.namespace, msg.user_id)
                return Reply(status="success", reply_content="👌 已跳过今日记账。")

            return await self._record(conn, msg, content)
        finally:
            pass

    async def shutdown(self) -> None:
        await self._download_client.aclose()

    # ---------- 意图判断 ----------

    @staticmethod
    def _is_query(content: str) -> bool:
        if any(
            w in content
            for w in (
                "详细", "统计", "余额", "总资产", "还有多少钱", "多少钱",
                "所有账户", "资产", "账单", "报表", "报告", "汇总", "生成",
            )
        ):
            return True
        if content in STATS_EXACT:
            return True
        if "多少" in content and any(w in content for w in ("花", "收入", "支出", "钱")):
            return True
        if re.search(r"\d{4}\s*年", content) and any(
            w in content for w in ("多少", "账单", "报表", "统计", "汇总", "查询", "花了多少钱")
        ):
            return True
        if re.search(r"\d{2}\s*年", content) and any(
            w in content for w in ("多少", "账单", "报表", "统计", "汇总", "查询", "花了多少钱")
        ):
            return True
        if ("今年" in content or "去年" in content) and any(
            w in content for w in ("多少", "账单", "报表", "统计", "汇总", "查询", "花了多少钱")
        ):
            return True
        if re.search(r"最近\s*\d+\s*[天日]", content):
            return True
        if re.fullmatch(r"\s*\d{4}\s*年(?:\s*\d{1,2}\s*月(?:份)?)?\s*", content):
            return True
        if re.fullmatch(r"\s*\d{2}\s*年(?:\s*\d{1,2}\s*月(?:份)?)?\s*", content):
            return True
        if re.fullmatch(r"\s*\d{1,2}\s*月(?:份)?\s*", content):
            return True
        if re.fullmatch(r"\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?\s*", content):
            return True
        if re.search(r"(?:^|[^0-9])\d{1,2}\s*月\s*\d{1,2}\s*[日号]?(?:$|[^0-9])", content) and (
            "多少" in content or "统计" in content or "详细" in content
        ):
            return True
        if re.search(
            r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?\s*(?:到|至|-|~|—)", content
        ):
            return True
        return False

    # ---------- 待确认动作 ----------

    async def _handle_pending(
        self, conn, msg: InboundMessage, pending: dict, content: str
    ) -> Reply:
        action = pending["action_type"]
        if content in s.CANCEL_PHRASES:
            s.clear_pending(conn, msg.namespace, msg.user_id)
            return Reply(status="cancelled", reply_content="👌 已取消。")

        if action == "IMPORT_CONFIRM":
            if content not in s.CONFIRM_PHRASES:
                return Reply(
                    status="pending_confirmation",
                    reply_content="请回复【是】确认合并导入，或【否】取消。",
                    requires_confirmation=True,
                )
            s.clear_pending(conn, msg.namespace, msg.user_id)
            payload = json.loads(pending["payload"])
            result = s.merge_staged(
                conn, payload["staging_id"], msg.namespace, msg.user_id, msg.platform
            )
            if result["errors"]:
                detail = "；".join(result["errors"][:5])
                return Reply(
                    status="success",
                    reply_content=(
                        f"📥 合并完成：新增 {result['new']} 行，跳过 {result['skip']} 行。"
                        f"（部分行重复：{detail}）"
                    ),
                )
            return Reply(
                status="success",
                reply_content=f"📥 合并完成：新增 {result['new']} 行，跳过 {result['skip']} 行（与现有账本或文件内重复）。",
            )

        if action == "DUPLICATE_CONFIRM":
            if content not in s.CONFIRM_PHRASES:
                return Reply(
                    status="pending_confirmation",
                    reply_content="请先回复【是】确认，或【否】取消。",
                    requires_confirmation=True,
                )
            s.clear_pending(conn, msg.namespace, msg.user_id)
            items = s.confirm_payload_items(json.loads(pending["payload"]))
            return self._record_final(conn, msg, items, pending.get("raw_text", ""), skip_debounce=True)

        if action == "DELETE_CONFIRM" and content in s.CONFIRM_PHRASES:
            s.clear_pending(conn, msg.namespace, msg.user_id)
            payload = json.loads(pending["payload"])
            row = s.void_record(conn, msg.namespace, msg.user_id, payload["record_id"])
            if row is None:
                return Reply(status="error", reply_content="⚠️ 该记录不存在或已撤销。")
            return Reply(
                status="success",
                reply_content=(
                    f"🗑️ 已撤销 {row['expense_date']} 的 "
                    f"{s.format_money(row['amount_cents'])} 元（{row['description']}）"
                    "，保留历史记录，余额已同步。"
                ),
            )

        if action == "MODIFY_CONFIRM" and content in s.CONFIRM_PHRASES:
            s.clear_pending(conn, msg.namespace, msg.user_id)
            payload = json.loads(pending["payload"])
            row = s.modify_record(
                conn, msg.namespace, msg.user_id, payload["record_id"], payload["new_amount_cents"]
            )
            if row is None:
                return Reply(status="error", reply_content="⚠️ 该记录不存在或已撤销。")
            return Reply(
                status="success",
                reply_content=(
                    f"✏️ 已修改：{row['description']} 由 "
                    f"{s.format_money(row['amount_cents'])} 元改为 "
                    f"{s.format_money(payload['new_amount_cents'])} 元，余额已同步。"
                ),
            )

        if action == "MODIFY_AMOUNT":
            new_cents = parse_amount_cents(content)
            if new_cents is None:
                return Reply(
                    status="pending_confirmation",
                    reply_content="请直接回复新金额，例如：35",
                    requires_confirmation=True,
                )
            s.clear_pending(conn, msg.namespace, msg.user_id)
            payload = json.loads(pending["payload"])
            row = s.modify_record(conn, msg.namespace, msg.user_id, payload["record_id"], new_cents)
            if row is None:
                return Reply(status="error", reply_content="⚠️ 该记录不存在或已撤销。")
            return Reply(
                status="success",
                reply_content=(
                    f"✏️ 已修改：{row['description']} 由 {s.format_money(row['amount_cents'])} 元"
                    f"改为 {s.format_money(new_cents)} 元，余额已同步。"
                ),
            )

        if action == "BALANCE_CONFIRM" and content in s.CONFIRM_PHRASES:
            s.clear_pending(conn, msg.namespace, msg.user_id)
            payload = json.loads(pending["payload"])
            item = s.create_adjustment(
                conn,
                msg.namespace,
                msg.user_id,
                payload["account"],
                payload["amount_cents"],
                payload.get("note", ""),
                msg.platform,
                msg.message_id,
                pending.get("raw_text", ""),
            )
            if item is None:
                return Reply(status="error", reply_content="⚠️ 平账写入失败。")
            sign = "+" if payload["amount_cents"] >= 0 else "-"
            return Reply(
                status="success",
                reply_content=(
                    f"✅ 已确认平账：{payload['account'] or '默认'} 余额"
                    f"{sign}{s.format_money(payload['amount_cents'])} 元"
                    f"（{item.description}），不计入消费统计。"
                ),
            )

        if action == "ASK_ACCOUNT":
            s.clear_pending(conn, msg.namespace, msg.user_id)
            items = s.confirm_payload_items(json.loads(pending["payload"]))
            account = content
            for it in items:
                if it.tx_type != "transfer_out" and not it.account_name:
                    it.account_name = account
            return self._draft_next(conn, msg, items, pending)

        if action == "ASK_TRANSFER_TO":
            s.clear_pending(conn, msg.namespace, msg.user_id)
            items = s.confirm_payload_items(json.loads(pending["payload"]))
            for it in items:
                if it.tx_type == "transfer_out":
                    it.to_account_name = content
            return self._draft_next(conn, msg, items, pending)

        if action == "ASK_TIME":
            s.clear_pending(conn, msg.namespace, msg.user_id)
            items = s.confirm_payload_items(json.loads(pending["payload"]))
            day = self._parse_answer_date(content)
            if day is None:
                ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
                s.set_pending(
                    conn, msg.namespace, msg.user_id, "ASK_TIME",
                    {"items": [it.model_dump(mode="json") for it in items], "raw_text": pending.get("raw_text", "")},
                    ttl,
                )
                return Reply(
                    status="pending_confirmation",
                    reply_content="这笔消费是什么时候发生的？（例如：今天/昨天/8月20日/就现在）",
                    requires_confirmation=True,
                )
            for it in items:
                it.expense_date = day
            return self._record_final(conn, msg, items, pending.get("raw_text", ""))

        if content in s.CONFIRM_PHRASES:
            return Reply(status="pending_confirmation", reply_content="请稍等，无法识别的确认请求。")
        return Reply(
            status="pending_confirmation",
            reply_content="请回复【是】确认，或【否】取消。",
            requires_confirmation=True,
        )

    @staticmethod
    def _parse_answer_date(content: str):
        if content in {"现在", "刚才", "就是现在", "就现在", "刚刚", "就是刚才"}:
            return timeutil.today()
        if has_time_expression(content):
            return parse_expense_date(content, timeutil.today())
        return None

    def _draft_next(self, conn, msg: InboundMessage, items: list[ParsedItem], pending: dict) -> Reply:
        raw_text = pending.get("raw_text", "")
        if any(it.tx_type == "transfer_out" and not it.to_account_name for it in items):
            ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
            s.set_pending(
                conn, msg.namespace, msg.user_id, "ASK_TRANSFER_TO",
                {"items": [it.model_dump(mode="json") for it in items], "raw_text": raw_text}, ttl,
            )
            return Reply(
                status="pending_confirmation",
                reply_content="转给哪个账户？例如：银行卡",
                requires_confirmation=True,
            )
        if any(not it.account_name for it in items if it.tx_type != "transfer_out"):
            ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
            s.set_pending(
                conn, msg.namespace, msg.user_id, "ASK_ACCOUNT",
                {"items": [it.model_dump(mode="json") for it in items], "raw_text": raw_text}, ttl,
            )
            return Reply(
                status="pending_confirmation",
                reply_content="使用哪个账户？（微信/支付宝/银行卡/现金/信用卡）",
                requires_confirmation=True,
            )
        if not has_time_expression(raw_text):
            ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
            s.set_pending(
                conn, msg.namespace, msg.user_id, "ASK_TIME",
                {"items": [it.model_dump(mode="json") for it in items], "raw_text": raw_text}, ttl,
            )
            return Reply(
                status="pending_confirmation",
                reply_content="这笔消费是什么时候发生的？（例如：今天/昨天/8月20日/就现在）",
                requires_confirmation=True,
            )
        return self._record_final(conn, msg, items, raw_text)

    # ---------- 记账 ----------

    async def _record(self, conn, msg: InboundMessage, content: str) -> Reply:
        categories = s.load_categories(conn)
        accts = s.account_keywords(conn, msg.namespace, msg.user_id)
        _, items, _ = await interpret(
            conn, content, categories=categories, account_list=accts
        )
        if not items:
            return Reply(
                status="unrecognized",
                reply_content="未能识别消费内容。例如：「今天微信吃饭花了25」，或发送 .csv/.xlsx 账本导入。",
            )
        return self._draft_next(conn, msg, items, {"raw_text": content})

    async def _handle_ai(self, conn, msg: InboundMessage, content: str) -> Reply:
        categories = s.load_categories(conn)
        accts = s.account_keywords(conn, msg.namespace, msg.user_id)
        engine, items, error = await interpret(
            conn, content, forced_ai=True, categories=categories, account_list=accts
        )
        if items:
            return self._draft_next(conn, msg, items, {"raw_text": content})
        if engine is None and error is None:
            return Reply(
                status="unrecognized",
                reply_content="AI 识别未启用（本地/云端均未开启）。可在管理界面 → 系统设置 → AI 识别中开启。",
            )
        # AI 全部失败：降级为规则
        items = parse_expenses(content, categories, timeutil.today(), accts)
        if items:
            return self._draft_next(conn, msg, items, {"raw_text": content})
        return Reply(status="error", reply_content=f"AI 解析失败：{error or '未知错误'}")

    def _record_final(
        self, conn, msg: InboundMessage, items: list[ParsedItem], raw_text: str, skip_debounce: bool = False
    ) -> Reply:
        if not skip_debounce:
            window = settings_store.get_int(conn, "dedup_window_seconds", 30)
            duplicate = next(
                (
                    it
                    for it in items
                    if s.find_recent_duplicate(conn, msg.namespace, msg.user_id, it, window)
                ),
                None,
            )
            if duplicate is not None:
                ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
                s.set_pending(
                    conn,
                    msg.namespace,
                    msg.user_id,
                    "DUPLICATE_CONFIRM",
                    {
                        "items": [it.model_dump(mode="json") for it in items],
                        "raw_text": raw_text,
                    },
                    ttl,
                )
                return Reply(
                    status="pending_confirmation",
                    reply_content=(
                        f"⚠️ 检测到 {window} 秒内已有相同消费："
                        f"{s.format_money(duplicate.amount_cents)} 元（{duplicate.description}）。\n"
                        "这是新消费需要再次记录吗？回复【是】确认，回复【否】取消。"
                    ),
                    requires_confirmation=True,
                )
        return Reply(
            status="success",
            reply_content=s.summary_text(
                conn, msg.namespace, msg.user_id, items, msg.platform, msg.message_id, raw_text
            ),
            parsed_count=len(items),
        )

    # ---------- 删除 / 修改 / 平账 / 余额 ----------

    async def _handle_delete(self, conn, msg: InboundMessage, content: str) -> Reply:
        amount_cents = parse_amount_with_unit(content)
        row = s.find_target_record(conn, msg.namespace, msg.user_id, amount_cents)
        if row is None:
            return Reply(status="error", reply_content="没有找到可撤销的记录。")
        ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
        s.set_pending(
            conn, msg.namespace, msg.user_id, "DELETE_CONFIRM",
            {"record_id": row["id"], "raw_text": content}, ttl,
        )
        acc = f"（{row['account_name']}）" if row.get("account_name") else ""
        return Reply(
            status="pending_confirmation",
            reply_content=(
                f"确认撤销这笔吗？\n{row['expense_date']} {s.format_money(row['amount_cents'])} 元{acc}"
                f"（{row['description']}）\n撤销采用作废方式，保留历史记录。回复【是】确认，【否】取消。"
            ),
            requires_confirmation=True,
        )

    async def _handle_modify(self, conn, msg: InboundMessage, content: str) -> Reply:
        parsed = parse_modify_request(content)
        target = s.find_target_record(conn, msg.namespace, msg.user_id, parsed["old_cents"])
        if target is None:
            return Reply(status="error", reply_content="没有找到要修改的记录。")
        if parsed["new_cents"] is None:
            ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
            s.set_pending(
                conn, msg.namespace, msg.user_id, "MODIFY_AMOUNT",
                {"record_id": target["id"], "raw_text": content}, ttl,
            )
            return Reply(
                status="pending_confirmation",
                reply_content=(
                    f"这笔是 {s.format_money(target['amount_cents'])} 元（{target['description']}），"
                    "要改成多少？直接回复金额即可。"
                ),
                requires_confirmation=True,
            )
        ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
        s.set_pending(
            conn, msg.namespace, msg.user_id, "MODIFY_CONFIRM",
            {
                "record_id": target["id"],
                "new_amount_cents": parsed["new_cents"],
                "raw_text": content,
            },
            ttl,
        )
        return Reply(
            status="pending_confirmation",
            reply_content=(
                f"确认把 {s.format_money(target['amount_cents'])} 元（{target['description']}）"
                f"改为 {s.format_money(parsed['new_cents'])} 元吗？回复【是】确认，【否】取消。"
            ),
            requires_confirmation=True,
        )

    async def _handle_balance(self, conn, msg: InboundMessage, content: str) -> Reply:
        parsed = parse_balance_command(content)
        if not parsed["mode"]:
            return Reply(
                status="unrecognized",
                reply_content="平账格式：微信平账到90元（按目标余额），或 平账10元 备注（直接调整）。",
            )
        if parsed["mode"] == "target":
            if not parsed["account"]:
                return Reply(
                    status="unrecognized",
                    reply_content="请说明账户，例如：微信平账到90元",
                )
            acc = s.account_balance_by_name(conn, msg.namespace, msg.user_id, parsed["account"])
            current = acc["balance_cents"] if acc else 0
            adjustment = parsed["amount_cents"] - current
            if adjustment == 0:
                return Reply(
                    status="success",
                    reply_content=f"✅ {parsed['account']} 当前余额已是 {s.format_money(current)} 元，无需平账。",
                )
        else:
            adjustment = parsed["amount_cents"]
            acc = (
                s.account_balance_by_name(conn, msg.namespace, msg.user_id, parsed["account"])
                if parsed["account"]
                else None
            )
            current = acc["balance_cents"] if acc else None
        ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
        s.set_pending(
            conn, msg.namespace, msg.user_id, "BALANCE_CONFIRM",
            {
                "account": parsed["account"],
                "amount_cents": adjustment,
                "note": parsed["note"],
                "raw_text": content,
            },
            ttl,
        )
        sign = "+" if adjustment >= 0 else "-"
        hint = f"（当前 {s.format_signed_money(current)} 元）" if current is not None else ""
        return Reply(
            status="pending_confirmation",
            reply_content=(
                f"平账属于特殊资金调整，请确认：\n{parsed['account'] or '默认'} 余额"
                f"{sign}{s.format_money(adjustment)} 元{hint}\n回复【是】确认，【否】取消。"
            ),
            requires_confirmation=True,
        )

    def _handle_set_balance(self, conn, namespace: str, user_id: str, account: str, raw_amount: str) -> Reply:
        amount = float(raw_amount) if re.fullmatch(r"\d+(\.\d+)?", raw_amount) else None
        if amount is None:
            from paas.modules.account.parser import parse_chinese_number

            amount = parse_chinese_number(raw_amount)
        if not amount or amount < 0:
            return Reply(status="unrecognized", reply_content="余额格式：设置微信余额1000")
        s.set_account_initial_balance(conn, namespace, user_id, account, yuan_to_cents(amount))
        acc = s.account_balance_by_name(conn, namespace, user_id, account)
        return Reply(
            status="success",
            reply_content=(
                f"✅ 已设置 {account} 初始余额 {s.format_money(acc['balance_cents'])} 元"
                "（后续按流水自动变化）"
            ),
        )

    # ---------- 查询 / 统计 / 导出 ----------

    async def _handle_query(self, conn, msg: InboundMessage, content: str) -> Reply:
        # 账户余额 / 总资产
        accounts_in_text = detect_accounts(content)
        money_query = (
            "余额" in content
            or "总资产" in content
            or "所有账户" in content
            or "全部账户" in content
            or (
                any(w in content for w in ("还有多少钱", "有多少钱", "总共多少钱", "总共有多少钱", "一共有多少钱", "多少钱"))
                and not any(w in content for w in ("花", "支出", "消费", "收入", "花费"))
            )
        )
        if money_query:
            return self._balance_reply(
                conn, msg.namespace, msg.user_id,
                accounts_in_text[0] if accounts_in_text else None,
            )

        # 时间段统计
        today = timeutil.today()
        period = parse_time_range(content, today)
        if period is None:
            period = (today - __import__("datetime").timedelta(days=6), today)
        start, end = period
        account_name = detect_accounts(content)[0] if detect_accounts(content) else None
        category_name = None
        for cat in s.load_categories(conn):
            if cat.name in content:
                category_name = cat.name
                break
        stats = s.period_stats(
            conn, msg.namespace, msg.user_id, start, end, account_name=account_name, category_name=category_name
        )
        lines = [
            f"📊 {start.isoformat()} ~ {end.isoformat()}："
            f"支出 {s.format_money(stats['expense_cents'])} 元，"
            f"收入 {s.format_money(stats['income_cents'])} 元，共 {stats['count']} 笔",
        ]
        if stats["adjust_cents"]:
            lines.append(f"平账调整：{s.format_signed_money(stats['adjust_cents'])} 元（不计入消费）")
        if stats["top_categories"]:
            lines.append("主要支出分类：")
            for c in stats["top_categories"]:
                lines.append(
                    f"  {c['icon']} {c['name']}：{s.format_money(c['total_cents'])} 元（{c['cnt']} 笔）"
                )
        if stats["top_day"]:
            lines.append(f"最高消费日：{stats['top_day']['expense_date']} {s.format_money(stats['top_day']['total_cents'])} 元")
        if account_name:
            lines.append(f"（仅统计 {account_name} 账户）")
        if category_name:
            lines.append(f"（仅统计 {category_name} 分类）")
        if "详细" in content or "详情" in content:
            rows = period_detail(
                conn, msg.namespace, msg.user_id, start, end,
                account_name=account_name, category_name=category_name,
            )
            if rows:
                lines.append("明细：")
                tag = {"expense": "支", "income": "收", "refund": "退", "fee": "费",
                       "adjust": "平", "transfer_out": "转出", "transfer_in": "转入"}
                for r in rows:
                    acc = f"({r['account_name'] or '-'})"
                    lines.append(
                        f"  {r['expense_date']} {tag.get(r['tx_type'], '?')} {r['category_name']} "
                        f"{s.format_money(r['amount_cents'])}元 {acc} {r['description']}"
                    )
        return Reply(status="success", reply_content="\n".join(lines))

    def _balance_reply(self, conn, namespace: str, user_id: str, account: str | None) -> Reply:
        balances = s.account_balances(conn, namespace, user_id)
        if not balances:
            return Reply(status="error", reply_content="还没有账户数据，先记账一笔试试。")
        if account:
            acc = next((a for a in balances if a["name"] == account), None)
            if acc is None:
                return Reply(status="error", reply_content=f"没有找到账户「{account}」。")
            return Reply(
                status="success",
                reply_content=f"💰 {acc['name']} 当前余额：{s.format_signed_money(acc['balance_cents'])} 元",
            )
        lines = ["💰 各账户余额："]
        for a in balances:
            lines.append(f"  {a['name']}：{s.format_signed_money(a['balance_cents'])} 元")
        lines.append(f"总资产：{s.format_signed_money(sum(a['balance_cents'] for a in balances))} 元")
        return Reply(status="success", reply_content="\n".join(lines))

    async def _handle_export(self, conn, namespace: str, user_id: str) -> Reply:
        rows = conn.execute(
            """
            SELECT e.expense_date, e.tx_type, c.name AS category_name, e.amount_cents,
                   a.name AS account_name, e.description, e.status
            FROM expenses e JOIN categories c ON c.id = e.category_id
            LEFT JOIN accounts a ON a.id = e.account_id
            WHERE e.namespace = ? AND e.user_id = ?
            ORDER BY e.expense_date DESC, e.id DESC
            """,
            (namespace, user_id),
        ).fetchall()
        total = len(rows)
        normal = sum(1 for r in rows if r["status"] == "normal")
        return Reply(
            status="success",
            reply_content=(
                f"📤 共 {total} 笔流水（含撤销 {total - normal} 笔）。\n"
                "请在管理界面下载：/admin/api/export?user_id=<你的ID>&format=csv\n"
                "（聊天内直接发送文件导出功能将在下一版本提供）"
            ),
        )

    # ---------- 导入 ----------

    @staticmethod
    def _pick_import_attachment(msg: InboundMessage) -> Attachment | None:
        for att in msg.attachments:
            name = (att.filename or "").lower()
            if name.endswith((".csv", ".xlsx", ".xls")):
                return att
        return None

    async def _handle_import(self, conn, msg: InboundMessage, att: Attachment) -> Reply:
        data = att.data
        if data is None and att.url:
            try:
                resp = await self._download_client.get(att.url)
                resp.raise_for_status()
                data = resp.content
            except Exception as exc:  # noqa: BLE001
                log.warning("附件下载失败: %s", exc)
                return Reply(
                    status="error",
                    reply_content="⚠️ 附件下载失败，请重试或直接发送 .csv/.xlsx 文件。",
                )
        if not data:
            return Reply(status="error", reply_content="⚠️ 未获取到文件内容。")
        categories = s.load_categories(conn)
        result, items = parse_import(data, att.filename or "import", categories)
        if not items:
            return Reply(
                status="error",
                reply_content="⚠️ 文件中没有可识别的有效记录。" +
                (f"\n原因：{'；'.join(result.errors[:5])}" if result.errors else ""),
            )
        preview = s.preview_merge(conn, msg.namespace, msg.user_id, items)
        staging_id = s.create_import_staging(
            conn, msg.namespace, msg.user_id, msg.platform, msg.message_id,
            att.filename or "import", items,
        )
        ttl = settings_store.get_int(conn, "pending_ttl_seconds", 600)
        s.set_pending(
            conn, msg.namespace, msg.user_id, "IMPORT_CONFIRM",
            {"staging_id": staging_id}, ttl,
        )
        lines = [
            f"📥 账本预览（{att.filename}）",
            f"· 文件行数：{result.total_rows}，可识别：{len(items)} 行，无效：{result.failed_rows} 行",
        ]
        if preview["date_min"]:
            lines.append(f"· 日期范围：{preview['date_min']} ~ {preview['date_max']}")
        lines.append(f"· 支出合计：{s.format_money(preview['expense_cents'])} 元，收入合计：{s.format_money(preview['income_cents'])} 元")
        if preview["categories"]:
            cats = "、".join(f"{c[0]}({c[1]})" for c in preview["categories"])
            lines.append(f"· 主要分类：{cats}")
        lines.append(f"· 合并后：将新增 {preview['new']} 行，跳过 {preview['skip']} 行（与现有账本或文件内重复）")
        if result.errors:
            lines.append(f"· 无效行原因示例：{'；'.join(result.errors[:3])}")
        lines.append("\n回复【是】确认合并，回复【否】取消。")
        return Reply(
            status="pending_confirmation",
            reply_content="\n".join(lines),
            requires_confirmation=True,
        )
