"""be-a-boss — run your own agent org over chat.

An orchestrator session hires and supervises coder sessions; each conversation is
a visible, joinable thread. The core is transport-agnostic (Telegram is one
adapter). The bot's display persona is configurable via the BOT_NAME env var.
"""

DEFAULT_BOT_NAME = "Orchestrator"
__version__ = "0.1.0"
