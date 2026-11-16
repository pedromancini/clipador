"""Envio dos clips e avisos pelo Telegram (Bot API).

Secrets/variáveis de ambiente:
    TELEGRAM_BOT_TOKEN  — token do bot criado no @BotFather
    TELEGRAM_CHAT_ID    — seu chat_id (veja o README)

Sem essas variáveis, o envio é desativado e só aparece no log (útil para testes locais).
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from .detector import Peak

log = logging.getLogger("clipador.telegram")
API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.token and self.chat_id)
        self._last_error_msg = 0.0
        if not self.enabled:
            log.info("Telegram não configurado (opcional): clips só no resumo do job e no artifact")

    @classmethod
    def from_env(cls) -> "Telegram":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))

    def _call(self, method: str, data: dict, files: dict | None = None, timeout: int = 60) -> dict:
        url = API.format(token=self.token, method=method)
        for tentativa in range(4):
            try:
                if files:
                    # reabre o arquivo a cada tentativa
                    abertos = {k: open(v, "rb") for k, v in files.items()}
                    try:
                        r = requests.post(url, data=data, files=abertos, timeout=timeout)
                    finally:
                        for f in abertos.values():
                            f.close()
                else:
                    r = requests.post(url, data=data, timeout=timeout)
            except requests.RequestException as e:
                log.warning("telegram %s: erro de rede (%s), tentativa %d", method, e, tentativa + 1)
                time.sleep(3 * (tentativa + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"ok": False, "description": r.text[:200]}
            if body.get("ok"):
                return body["result"]
            if r.status_code == 429:
                espera = int((body.get("parameters") or {}).get("retry_after", 5))
                log.warning("telegram: limite de envio, esperando %ss", espera)
                time.sleep(espera + 1)
                continue
            if r.status_code >= 500:
                time.sleep(3 * (tentativa + 1))
                continue
            # erro de requisição (token errado, chat_id errado, legenda inválida...): não adianta repetir
            raise TelegramError(f"{method}: {r.status_code} {body.get('description')}")
        raise TelegramError(f"{method}: falhou depois de várias tentativas")

    def send_message(self, text: str, html_mode: bool = True) -> None:
        if not self.enabled:
            log.info("[telegram desativado] %s", re.sub("<[^>]+>", "", text))
            return
        data = {"chat_id": self.chat_id, "text": text[:TEXT_LIMIT], "disable_web_page_preview": "true"}
        if html_mode:
            data["parse_mode"] = "HTML"
        self._call("sendMessage", data)

    def send_video(self, path: Path | str, caption: str, duration: float | None = None,
                   width: int = 1080, height: int = 1920) -> None:
        path = Path(path)
        if not self.enabled:
            log.info("[telegram desativado] vídeo %s\n%s", path, re.sub("<[^>]+>", "", caption))
            return
        if path.stat().st_size >= 50 * 1024 * 1024:
            raise TelegramError(f"{path.name} tem {path.stat().st_size / 1e6:.1f} MB (limite do bot: 50 MB)")
        data = {"chat_id": self.chat_id, "caption": caption[:CAPTION_LIMIT], "parse_mode": "HTML",
                "supports_streaming": "true", "width": str(width), "height": str(height)}
        if duration:
            data["duration"] = str(int(round(duration)))
        self._call("sendVideo", data, files={"video": path}, timeout=300)

    def send_error(self, text: str, min_interval: float = 300) -> None:
        """Avisa erro sem floodar: no máximo 1 mensagem de erro a cada `min_interval` segundos."""
        agora = time.time()
        if agora - self._last_error_msg < min_interval:
            log.error("(erro não enviado ao Telegram por limite) %s", text)
            return
        self._last_error_msg = agora
        try:
            self.send_message(f"⚠️ <b>Clipador — erro</b>\n{html.escape(text[:3500])}")
        except Exception as e:  # noqa: BLE001
            log.error("não consegui avisar o erro no Telegram: %s", e)


# ---------------------------------------------------------------- legenda da mensagem

def _tag(texto: str) -> str:
    t = re.sub(r"[^0-9a-zA-ZÀ-ÿ]", "", texto or "").lower()
    return f"#{t}" if t else ""


def build_hashtags(defaults: list[str], channel: str, category: str | None, limit: int = 8) -> list[str]:
    tags: list[str] = []
    for t in [*defaults, _tag(channel), _tag(category or "")]:
        if t and t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    return tags[:limit]


_GTA6_RE = re.compile(r"grand theft auto (vi|6)\b|\bgta ?(vi|6)\b", re.IGNORECASE)


def game_prefix(category: str | None) -> str:
    """Prefixo do jogo no título (ex.: 'GTA 6: ') quando a categoria da live é conhecida."""
    if category and _GTA6_RE.search(category):
        return "GTA 6: "
    return ""


def suggest_title(channel: str, transcript: str, top_messages: list[tuple[str, int]],
                  stream_title: str | None, category: str | None = None) -> str:
    """Sugestão de título para o TikTok/YouTube (dá pra editar antes de postar)."""
    return game_prefix(category) + _suggest_title(channel, transcript, top_messages, stream_title)


def _suggest_title(channel: str, transcript: str, top_messages: list[tuple[str, int]],
                   stream_title: str | None) -> str:
    fala = re.split(r"(?<=[.!?])\s+", (transcript or "").strip())[0] if transcript else ""
    fala = fala.strip(" .")
    if 8 <= len(fala) <= 80:
        return f"“{fala}” \U0001F602 {channel}"
    top = top_messages[0][0].lower() if top_messages else ""
    if "kkk" in top or "kekw" in top or "lul" in top:
        return f"{channel} não aguentou \U0001F602\U0001F602"
    if "clip" in top:
        return f"o chat pediu clip \U0001F525 {channel}"
    if stream_title:
        return f"{channel}: {stream_title[:60]}"
    return f"momento insano do {channel} \U0001F525"


def build_caption(channel: str, platform_label: str, peak: Peak, transcript: str,
                  stream_title: str | None, category: str | None, hashtags: list[str],
                  timezone: str = "America/Sao_Paulo", max_chat_lines: int = 5,
                  youtube_line: str | None = None) -> str:
    """Legenda HTML da mensagem do Telegram (máx. 1024 caracteres)."""
    try:
        from zoneinfo import ZoneInfo
        quando = datetime.fromtimestamp(peak.t, ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        quando = datetime.fromtimestamp(peak.t)
    titulo = suggest_title(channel, transcript, peak.top_messages, stream_title, category)
    tags = " ".join(build_hashtags(hashtags, channel, category))
    e = html.escape

    def montar(n_chat: int, max_titulo_live: int) -> str:
        linhas = [f"🎬 <b>{e(channel)}</b> · {platform_label} · {quando:%d/%m %H:%M}"]
        if peak.forced:
            linhas.append("🧪 clip forçado (modo --once)")
        else:
            ratio = f"{peak.ratio:.1f}x o normal" if peak.baseline > 0 else "chat explodiu"
            linhas.append(f"🔥 Hype: <b>{peak.score:g}</b> ({ratio})")
        if category or stream_title:
            live = (stream_title or "")[:max_titulo_live]
            linhas.append(f"🎮 {e(category or '')}{' — ' if category and live else ''}{e(live)}")
        if peak.top_messages and n_chat:
            linhas.append("💬 Chat:")
            for msg, qtd in peak.top_messages[:n_chat]:
                linhas.append(f"  • {e(msg[:40])} ×{qtd}")
        if youtube_line:
            linhas.append(e(youtube_line))
        linhas.append("")
        linhas.append("📋 <b>Pra colar no TikTok:</b>")
        linhas.append(f"<code>{e(titulo)} {e(tags)}</code>")
        return "\n".join(linhas)

    for n_chat, max_live in ((max_chat_lines, 90), (3, 60), (1, 40), (0, 0)):
        cap = montar(n_chat, max_live)
        if len(cap) <= CAPTION_LIMIT:
            return cap
    return cap[:CAPTION_LIMIT]
