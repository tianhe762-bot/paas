from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from paas.models import InboundMessage, Reply

MessageHandler = Callable[[InboundMessage], Awaitable[Reply]]


class BaseAdapter(ABC):
    platform: str = ""

    def __init__(self) -> None:
        self.namespace = "default"
        self.bot_id = "default"
        self.bot_name = ""
        self._handler: MessageHandler | None = None
        self._running = False
        self.last_error = ""
        self.last_message_at = ""

    @property
    def running(self) -> bool:
        return self._running

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(
        self, chat_id: str, text: str, reply_to_msg_id: str | None = None
    ) -> bool: ...

    @abstractmethod
    async def test(self) -> tuple[bool, str]: ...
