"""Transport-agnostic core: sessions, orchestrator, fleet, supervision.

Nothing in this package may import a chat platform. Transports adapt this core
to Telegram/Slack/whatever via the contracts in ports.py.
"""
