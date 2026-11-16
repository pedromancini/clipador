"""Chat da Twitch via IRC anônimo (não precisa de conta).

Conecta em irc.chat.twitch.tv:6667 com NICK justinfanNNNNN (usuário anônimo, só leitura).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import AsyncIterator

from .base import ChatMessage, ChatReader, log

HOST, PORT = "irc.chat.twitch.tv", 6667
# A Twitch manda PING a cada ~5 min; sem nada por 6 min, consideramos a conexão morta
READ_TIMEOUT = 360


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Extrai (usuario, texto) de uma linha IRC PRIVMSG. Ignora tags (@...) se houver."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if " PRIVMSG #" not in line or not line.startswith(":"):
        return None
    prefixo, _, resto = line[1:].partition(" PRIVMSG #")
    usuario = prefixo.split("!", 1)[0]
    _, _, texto = resto.partition(" :")
    # mensagens /me chegam como \x01ACTION texto\x01
    if texto.startswith("\x01ACTION ") and texto.endswith("\x01"):
        texto = texto[8:-1]
    return usuario, texto


class TwitchChat(ChatReader):
    platform = "twitch"

    async def _session(self) -> AsyncIterator[ChatMessage]:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, PORT), timeout=15)
        try:
            nick = f"justinfan{random.randint(10000, 99999)}"
            # twitch.tv/commands: recebe RECONNECT quando a Twitch vai derrubar o servidor
            writer.write(f"CAP REQ :twitch.tv/commands\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\n"
                         f"JOIN #{self.channel}\r\n".encode())
            await writer.drain()
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
                if not raw:
                    raise ConnectionError("servidor IRC fechou a conexão")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("PING"):
                    writer.write(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    await writer.drain()
                    continue
                if " RECONNECT" in line:
                    raise ConnectionError("Twitch pediu RECONNECT")
                if " JOIN #" in line and not self.connected:
                    self.connected = True
                    log.info("[twitch/%s] conectado ao chat (%s)", self.channel, nick)
                    continue
                if " NOTICE " in line and ("not allowed" in line.lower() or "suspended" in line.lower()):
                    log.warning("[twitch/%s] %s", self.channel, line)
                parsed = parse_privmsg(line)
                if parsed:
                    usuario, texto = parsed
                    yield ChatMessage("twitch", self.channel, usuario, texto, time.time())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
