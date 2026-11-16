"""Acesso às lives via API Python do Streamlink (Twitch e Kick).

O mesmo objeto do plugin serve para:
- saber se o canal está ao vivo (tem streams?)
- pegar título e categoria (get_title / get_category)
- abrir o stream de vídeo para gravação.

O plugin da Kick usa a API "webbrowser" do Streamlink (Chromium/Chrome headless) para passar no
desafio JS do Cloudflare. No runner Ubuntu do GitHub o Chrome já vem instalado (/usr/bin/google-chrome).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Channel

log = logging.getLogger("clipador.stream")

_QUALITY_RE = re.compile(r"^(\d{3,4})p(\d+)?")


def find_chrome() -> str | None:
    """Procura um Chrome/Chromium instalado (variável CHROME_PATH tem prioridade)."""
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    for nome in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(nome)
        if p:
            return p
    for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Chromium\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(p).exists():
            return p
    return None


def pick_quality(names: list[str], max_height: int = 1080) -> str | None:
    """Escolhe a melhor qualidade com altura <= max_height (ex.: '1080p60'); senão 'best'."""
    candidatos = []
    for n in names:
        m = _QUALITY_RE.match(n)
        if m:
            h, fps = int(m.group(1)), int(m.group(2) or 30)
            if h <= max_height:
                candidatos.append((h, fps, n))
    if candidatos:
        return max(candidatos)[2]
    return "best" if "best" in names else (names[0] if names else None)


@dataclass
class StreamInfo:
    live: bool
    title: str | None = None
    category: str | None = None
    author: str | None = None
    quality: str | None = None
    stream: Any = None   # objeto Stream do streamlink (tem .open())

    @property
    def summary(self) -> str:
        partes = [self.title or "(sem título)"]
        if self.category:
            partes.append(f"[{self.category}]")
        return " ".join(partes)


def make_session():
    """Cria a sessão do Streamlink configurada para gravação contínua."""
    from streamlink import Streamlink

    session = Streamlink()
    session.set_option("stream-timeout", 60)
    session.set_option("hls-live-edge", 3)
    session.set_option("stream-segment-threads", 2)
    chrome = find_chrome()
    if chrome:
        session.set_option("webbrowser-executable", chrome)
    session.set_option("webbrowser-headless", True)
    session.set_option("webbrowser-timeout", 60)
    return session


def get_stream_info(channel: Channel, max_height: int = 1080, session=None) -> StreamInfo:
    """Consulta o canal: ao vivo? título? categoria? E já devolve o stream escolhido."""
    from streamlink.exceptions import NoStreamsError, PluginError

    session = session or make_session()
    _, plugin_cls, url = session.resolve_url(channel.url)
    opts: dict[str, Any] = {}
    if channel.platform == "twitch":
        # pula os anúncios (a Twitch insere tela de "comercial" para quem não é sub)
        opts = {"disable-ads": True, "low-latency": True, "disable-reruns": True}
    elif channel.platform == "kick":
        opts = {"low-latency": True}
    plugin = plugin_cls(session, url, opts)
    try:
        streams = plugin.streams() or {}
    except NoStreamsError:
        streams = {}
    except PluginError as e:
        # canal inexistente/offline costuma vir como PluginError em alguns plugins
        log.info("[%s] plugin respondeu: %s", channel.slug, e)
        streams = {}

    info = StreamInfo(live=bool(streams))
    for attr, fn in (("title", plugin.get_title), ("category", plugin.get_category), ("author", plugin.get_author)):
        try:
            setattr(info, attr, fn())
        except Exception:  # noqa: BLE001
            pass
    if streams:
        nomes = [n for n in streams if n not in ("audio_only", "worst", "best_unfiltered", "worst_unfiltered")]
        q = pick_quality(nomes, max_height)
        info.quality = q
        info.stream = streams.get(q) if q else None
        info.live = info.stream is not None
    return info
