import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Attachment(BaseModel):
    filename: str = ""
    url: str = ""
    content_type: str = ""
    data: bytes | None = Field(default=None, repr=False)


class InboundMessage(BaseModel):
    """统一入站协议：无论 QQ 还是 Telegram，Adapter 都转换为该结构。"""

    platform: str
    user_id: str
    chat_id: str
    message_id: str
    timestamp: str = ""
    message_type: str = "text"
    content: str = ""
    attachments: list[Attachment] = []
    raw: dict[str, Any] = {}


class ParsedItem(BaseModel):
    expense_date: datetime.date
    category_id: int
    category_name: str
    category_icon: str
    account_name: str = ""
    to_account_name: str = ""
    tx_type: Literal[
        "expense", "income", "refund", "fee", "adjust", "transfer_out", "transfer_in"
    ] = "expense"
    amount_cents: int
    description: str

    @field_validator("amount_cents")
    @classmethod
    def _amount_not_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("金额不能为 0")
        return v


class Reply(BaseModel):
    status: str
    reply_content: str
    parsed_count: int = 0
    requires_confirmation: bool = False


class ImportResult(BaseModel):
    total_rows: int = 0
    success_rows: int = 0
    failed_rows: int = 0
    errors: list[str] = []


class CategoryRow(BaseModel):
    id: int
    name: str
    icon: str
    keywords: list[str]
