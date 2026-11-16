"""Base comum dos leitores de chat: modelo da mensagem e laço de reconexão automática."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

log = logging.getLogger("clipador.chat")


@dataclass
class ChatMessage:
    platform: str
    channel: str
    user: str
    text: str
    ts: float  # epoch (segundos) em que a mensagem chegou


class ChatReader:
    """Classe base. Subclasses implementam `_session()`, um gerador assíncrono que conecta
    e produz mensagens até a conexão cair (ou levantar exceção)."""

    platform = "?"

    def __init__(self, channel: str):
        self.channel = channel.lower()
        self.connected = False
        self.reconnects = 0

    async def _session(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover - abstrato
        raise NotImplementedError
        yield

    async def run(self, on_message: Callable[[ChatMessage], None], stop: asyncio.Event) -> None:
        """Lê o chat para sempre (até `stop`), reconectando com backoff exponencial."""
        espera = 1.0
        while not stop.is_set():
            inicio = time.monotonic()
            try:
                async for msg in self._session():
                    if stop.is_set():
                        break
                    on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - qualquer falha de rede vira reconexão
                log.warning("[%s/%s] chat caiu: %s: %s", self.platform, self.channel, type(e).__name__, e)
            finally:
                self.connected = False
            if stop.is_set():
                break
            # se a conexão durou bastante, volta o backoff pro início
            if time.monotonic() - inicio > 60:
                espera = 1.0
            self.reconnects += 1
            atraso = espera + random.uniform(0, espera / 2)
            log.info("[%s/%s] reconectando em %.1fs", self.platform, self.channel, atraso)
            try:
                await asyncio.wait_for(stop.wait(), timeout=atraso)
            except asyncio.TimeoutError:
                pass
            espera = min(espera * 2, 60.0)
