"""可选 AI 解释器：默认关闭，手动触发"用AI：…"。"""

from paas.interpreter.core import InterpreterDisabled, ai_interpret, ollama_pull, ollama_status

__all__ = ["InterpreterDisabled", "ai_interpret", "ollama_pull", "ollama_status"]

