"""Chat da Kick via WebSocket Pusher público.

ATENÇÃO: são endpoints NÃO oficiais da Kick e podem mudar sem aviso.
- chatroom_id: GET https://kick.com/api/v2/channels/{slug} -> chatroom.id (tem Cloudflare: usamos
  curl_cffi imitando o Chrome; se falhar, usamos "kick_chatroom_id" do channels.json).
- Key do Pusher: a Kick trocou a key em out/2026 (a antiga 32cbd69e4b950bf97679 dá erro 4001).
  Por isso perguntamos a key atual em POST https://web.kick.com/api/v1/realtime/connection
  (o mesmo endpoint que o site usa) antes de cada conexão, com FALLBACK_KEY de reserva.
- Canal: "chatrooms.{chatroom_id}.v2", evento "App\\Events\\ChatMessageEvent".
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

import websockets

from .base import ChatMessage, ChatReader, log

FALLBACK_KEY = "34bf7a0ff419a2a775b9"   # conferida em 16/11/2026
FALLBACK_CLUSTER = "us2"
REALTIME_URL = "https://web.kick.com/api/v1/realtime/connection"
CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"
CHAT_EVENT = "App\\Events\\ChatMessageEvent"
READ_TIMEOUT = 150  # Pusher manda ping a cada ~120s (activity_timeout)


def _http_get_json(url: str) -> dict:
    from curl_cffi import requests as creq  # import tardio: só a Kick precisa
    r = creq.get(url, impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_channel_info(slug: str) -> dict:
    """Dados públicos do canal na Kick (chatroom, livestream, etc). Levanta exceção se falhar."""
    return _http_get_json(CHANNEL_URL.format(slug=slug))


def resolve_chatroom_id(slug: str, fallback: int | None = None) -> int:
    try:
        cid = int(fetch_channel_info(slug)["chatroom"]["id"])
        log.info("[kick/%s] chatroom_id=%s", slug, cid)
        return cid
    except Exception as e:  # noqa: BLE001
        if fallback:
            log.warning("[kick/%s] API da Kick falhou (%s); usando kick_chatroom_id=%s do config", slug, e, fallback)
            return int(fallback)
        raise RuntimeError(
            f"Não consegui descobrir o chatroom_id da Kick para '{slug}' ({e}). "
            "Coloque \"kick_chatroom_id\" no channels.json (veja o README)."
        ) from e


def resolve_pusher_credentials() -> tuple[str, str]:
    """Key e cluster atuais do Pusher da Kick (com fallback fixo)."""
    try:
        from curl_cffi import requests as creq
        body = {"client": {"id": str(uuid.uuid4()), "type": "web"},
                "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
        r = creq.post(REALTIME_URL, json=body, impersonate="chrome", timeout=15)
        r.raise_for_status()
        for conn in r.json()["data"]["connections"]:
            cred = conn.get("credentials") or {}
            if conn.get("provider") == "pusher" and cred.get("app_key") and cred.get("cluster"):
                return cred["app_key"], cred["cluster"]
        raise ValueError("resposta sem credenciais do pusher")
    except Exception as e:  # noqa: BLE001
        log.warning("[kick] não consegui a key atual do Pusher (%s); usando a reserva", e)
        return FALLBACK_KEY, FALLBACK_CLUSTER


def parse_chat_event(data: str | dict) -> tuple[str, str] | None:
    """Extrai (usuario, texto) do payload do ChatMessageEvent."""
    d = json.loads(data) if isinstance(data, str) else data
    texto = d.get("content")
    if not texto:
        return None
    usuario = (d.get("sender") or {}).get("username") or "?"
    return usuario, texto


class KickChat(ChatReader):
    platform = "kick"

    def __init__(self, channel: str, chatroom_id: int | None = None, fallback_chatroom_id: int | None = None):
        super().__init__(channel)
        self.chatroom_id = chatroom_id
        self.fallback_chatroom_id = fallback_chatroom_id

    async def _session(self) -> AsyncIterator[ChatMessage]:
        if not self.chatroom_id:
            self.chatroom_id = await asyncio.to_thread(resolve_chatroom_id, self.channel, self.fallback_chatroom_id)
        key, cluster = await asyncio.to_thread(resolve_pusher_credentials)
        url = f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
        async with websockets.connect(url, open_timeout=20, ping_interval=None, max_size=2**22) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if first.get("event") != "pusher:connection_established":
                raise ConnectionError(f"Pusher recusou a conexão: {first}")
            canal = f"chatrooms.{self.chatroom_id}.v2"
            await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": canal}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT)
                msg = json.loads(raw)
                ev = msg.get("event")
                if ev == "pusher:ping":
                    await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif ev == "pusher_internal:subscription_succeeded":
                    self.connected = True
                    log.info("[kick/%s] conectado ao chat (%s, key %s…)", self.channel, canal, key[:6])
                elif ev == "pusher:error":
                    raise ConnectionError(f"erro do Pusher: {msg.get('data')}")
                elif ev == CHAT_EVENT:
                    parsed = parse_chat_event(msg.get("data") or "{}")
                    if parsed:
                        usuario, texto = parsed
                        yield ChatMessage("kick", self.channel, usuario, texto, time.time())
