"""Postagem automática no YouTube Shorts (YouTube Data API v3, oficial e gratuita).

Secrets / variáveis de ambiente (gerados uma vez com `python -m clipador.youtube_auth`):
    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

Só posta os picos mais fortes (settings.youtube.min_hype_ratio) e no máximo
settings.youtube.max_posts_per_day por canal. A contagem do dia é feita consultando os próprios
uploads do canal no YouTube (cada vídeo leva uma marca "clipador:<plataforma>/<canal>" na descrição),
então funciona mesmo com cada job do Actions começando do zero.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("clipador.youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]

from .config import DEFAULT_YOUTUBE as DEFAULT_YT_SETTINGS  # noqa: E402,F401


class YouTubeError(RuntimeError):
    pass


def marker(platform: str, name: str) -> str:
    return f"clipador:{platform}/{name}"


def build_metadata(title: str, channel_name: str, platform_label: str, channel_url: str,
                   hashtags: list[str], platform: str, category_id: str, privacy: str,
                   made_for_kids: bool) -> dict:
    """Título (máx. 100 caracteres, com #Shorts), descrição com crédito ao streamer e tags."""
    titulo = re.sub(r"[<>]", "", title).strip()
    sufixo = " #Shorts"
    if len(titulo) + len(sufixo) > 100:
        titulo = titulo[: 100 - len(sufixo) - 1].rstrip() + "…"
    titulo += sufixo
    tags_txt = " ".join(hashtags)
    descricao = (
        f"Clip da live de {channel_name} na {platform_label}.\n"
        f"Assista ao vivo: {channel_url}\n\n"
        f"{tags_txt} #shorts\n\n"
        f"{marker(platform, channel_name)}"
    )
    tags = [h.lstrip("#") for h in hashtags][:15] + ["shorts", channel_name]
    return {
        "snippet": {"title": titulo, "description": descricao[:4900], "tags": tags,
                    "categoryId": str(category_id), "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids),
                   "embeddable": True},
    }


@dataclass
class PostDecision:
    post: bool
    reason: str


def decide(yt_settings: dict, channel_allows: bool, ratio: float, forced: bool, posted_today: int,
           allow_forced: bool = False) -> PostDecision:
    """Decide se este clip vai pro YouTube (e o porquê, que aparece no Telegram)."""
    if not yt_settings.get("enabled"):
        return PostDecision(False, "postagem no YouTube desligada")
    if not channel_allows:
        return PostDecision(False, "YouTube desligado para este canal")
    if forced and not allow_forced:
        return PostDecision(False, "clip de teste (--once) não é postado")
    minimo = float(yt_settings.get("min_hype_ratio", 4.0))
    if not forced and ratio < minimo:
        return PostDecision(False, f"hype {ratio:.1f}x < {minimo:g}x (só pra revisão)")
    limite = int(yt_settings.get("max_posts_per_day", 6))
    if posted_today >= limite:
        return PostDecision(False, f"limite de {limite} posts hoje atingido")
    return PostDecision(True, "ok")


class YouTube:
    def __init__(self, client_id: str | None, client_secret: str | None, refresh_token: str | None,
                 timezone: str = "America/Sao_Paulo"):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.configured = bool(self.client_id and self.client_secret and self.refresh_token)
        self.timezone = timezone
        self._access_token: str | None = None
        self._token_exp = 0.0
        self._uploads_playlist: str | None = None
        self._today_count: dict[str, int] = {}
        self._count_day: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, timezone: str = "America/Sao_Paulo") -> "YouTube":
        return cls(os.environ.get("YOUTUBE_CLIENT_ID"), os.environ.get("YOUTUBE_CLIENT_SECRET"),
                   os.environ.get("YOUTUBE_REFRESH_TOKEN"), timezone)

    # ---------------------------------------------------------------- auth
    def _token(self) -> str:
        if self._access_token and time.time() < self._token_exp - 60:
            return self._access_token
        r = requests.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}, timeout=30)
        if r.status_code != 200:
            detalhe = r.text[:300]
            if "invalid_grant" in detalhe:
                raise YouTubeError("refresh token inválido ou expirado. Rode de novo `python -m clipador.youtube_auth` "
                                   "(e confira se o app do Google está 'Em produção', não 'Teste').")
            raise YouTubeError(f"falha ao renovar token do Google: {r.status_code} {detalhe}")
        d = r.json()
        self._access_token = d["access_token"]
        self._token_exp = time.time() + int(d.get("expires_in", 3600))
        return self._access_token

    def _get(self, path: str, params: dict) -> dict:
        r = requests.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {self._token()}"},
                         timeout=30)
        if r.status_code != 200:
            raise YouTubeError(f"{path}: {r.status_code} {r.text[:300]}")
        return r.json()

    # ---------------------------------------------------------------- contagem diária
    def _today(self) -> str:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return datetime.now().strftime("%Y-%m-%d")

    def _to_local_day(self, iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(self.timezone))
        except Exception:  # noqa: BLE001
            pass
        return dt.strftime("%Y-%m-%d")

    def posted_today(self, platform: str, name: str) -> int:
        """Quantos Shorts deste canal monitorado já foram postados hoje (consulta o YouTube 1x por job)."""
        with self._lock:
            hoje = self._today()
            if self._count_day != hoje:
                self._today_count, self._count_day = {}, hoje
            chave = marker(platform, name)
            if chave in self._today_count:
                return self._today_count[chave]
            if not self._uploads_playlist:
                d = self._get("channels", {"part": "contentDetails", "mine": "true"})
                if not d.get("items"):
                    raise YouTubeError("a conta autorizada não tem canal no YouTube")
                self._uploads_playlist = d["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            d = self._get("playlistItems", {"part": "snippet", "playlistId": self._uploads_playlist,
                                            "maxResults": 50})
            n = 0
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                if chave in (sn.get("description") or "") and self._to_local_day(sn.get("publishedAt", "")) == hoje:
                    n += 1
            self._today_count[chave] = n
            return n

    def _increment(self, platform: str, name: str) -> None:
        with self._lock:
            chave = marker(platform, name)
            self._today_count[chave] = self._today_count.get(chave, 0) + 1

    # ---------------------------------------------------------------- upload
    def upload(self, path: Path | str, metadata: dict, platform: str, name: str) -> str:
        """Envia o vídeo (upload resumável). Retorna a URL do Short."""
        path = Path(path)
        size = path.stat().st_size
        ultimo_erro = ""
        for tentativa in range(4):
            try:
                init = requests.post(
                    UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={"Authorization": f"Bearer {self._token()}",
                             "Content-Type": "application/json; charset=UTF-8",
                             "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                    data=json.dumps(metadata), timeout=60)
                if init.status_code != 200:
                    if init.status_code == 403 and "quota" in init.text.lower():
                        raise YouTubeError("cota diária da API do YouTube esgotada (volta amanhã)")
                    if init.status_code in (400, 401, 403):
                        raise YouTubeError(f"upload recusado: {init.status_code} {init.text[:400]}")
                    raise requests.HTTPError(f"init {init.status_code}: {init.text[:200]}")
                with open(path, "rb") as f:
                    r = requests.put(init.headers["Location"], data=f,
                                     headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                                     timeout=600)
                if r.status_code in (200, 201):
                    vid = r.json()["id"]
                    self._increment(platform, name)
                    url = f"https://youtube.com/shorts/{vid}"
                    status = r.json().get("status", {})
                    if status.get("privacyStatus") != metadata["status"]["privacyStatus"]:
                        log.warning("YouTube deixou o vídeo como %s (pedido: %s)", status.get("privacyStatus"),
                                    metadata["status"]["privacyStatus"])
                    log.info("postado no YouTube: %s", url)
                    return url
                if r.status_code in (400, 401, 403):
                    raise YouTubeError(f"upload recusado: {r.status_code} {r.text[:400]}")
                ultimo_erro = f"{r.status_code} {r.text[:200]}"
            except YouTubeError:
                raise
            except (requests.RequestException, KeyError) as e:
                ultimo_erro = str(e)
            espera = 5 * (2 ** tentativa)
            log.warning("upload no YouTube falhou (%s); tentando de novo em %ss", ultimo_erro, espera)
            time.sleep(espera)
        raise YouTubeError(f"upload no YouTube falhou depois de várias tentativas: {ultimo_erro}")
