"""Leitores de chat (Twitch IRC anônimo e Kick Pusher)."""
from __future__ import annotations

from ..config import Channel
from .base import ChatMessage, ChatReader
from .kick import KickChat
from .twitch import TwitchChat


def make_chat_reader(channel: Channel) -> ChatReader:
    if channel.platform == "twitch":
        return TwitchChat(channel.name)
    return KickChat(channel.name, fallback_chatroom_id=channel.kick_chatroom_id)


__all__ = ["ChatMessage", "ChatReader", "KickChat", "TwitchChat", "make_chat_reader"]
