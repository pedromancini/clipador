"""Postagem automática no YouTube Shorts (YouTube Data API v3, oficial e gratuita).

Secrets / variáveis de ambiente (gerados uma vez com `python -m clipador.youtube_auth`):
    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
Outros canais do YouTube ("youtube_conta": "dante" no channels.json): YOUTUBE_REFRESH_TOKEN_DANTE
(e, se for outro app do Google, YOUTUBE_CLIENT_ID_DANTE / YOUTUBE_CLIENT_SECRET_DANTE).
Um canal com conta própria NUNCA cai no canal principal: sem o token dele, simplesmente não posta.

Só posta os picos mais fortes (settings.youtube.min_hype_ratio), num ritmo de posts_per_hour por hora
(espaçados por min_minutes_between_posts); quando o chat explode (extreme_hype_ratio) libera um post
extra na hora (extreme_posts_per_hour). Teto de max_posts_per_day por canal. As contagens são feitas
consultando os próprios uploads do canal no YouTube (cada vídeo leva uma marca
"clipador:<plataforma>/<canal>" na descrição), então funcionam mesmo com cada job do Actions
começando do zero.
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


class YouTubeLimitError(YouTubeError):
    """O canal bateu o teto de envios do YouTube (uploadLimitExceeded). Não é a cota da API: é um limite
    da conta, menor em canal novo/sem verificação. Libera sozinho depois de algumas horas."""


def _checa_limite(resp) -> None:
    if resp.status_code in (400, 403) and "uploadLimitExceeded" in resp.text:
        raise YouTubeLimitError("o canal atingiu o limite de envios do YouTube por enquanto "
                                "(uploadLimitExceeded; verificar o canal em youtube.com/verify aumenta o limite)")


# hashtags que só funcionam no TikTok (no YouTube não ajudam e parecem spam)
SO_TIKTOK = {"fyp", "foryou", "foryoupage", "fy", "viral", "viralvideo", "tiktok", "xyzbca"}


def reserva_conta(yt_settings: dict, conta: str) -> int:
    """Vagas das 24h que o postar_lote deixa livres pras lives (por conta ou um número só)."""
    r = yt_settings.get("reserva_live", 4)
    if isinstance(r, dict):
        return int(r.get(conta or "principal", r.get("principal", 4)))
    return int(r)


def marker(platform: str, name: str) -> str:
    return f"clipador:{platform}/{name}"


def build_metadata(title: str, channel_name: str, platform_label: str, channel_url: str,
                   hashtags: list[str], platform: str, category_id: str, privacy: str,
                   made_for_kids: bool, hook: str | None = None, short: bool = True) -> dict:
    """Título (máx. 100 caracteres, com #Shorts), descrição com crédito ao streamer e tags.

    short=False: vídeo normal (versão horizontal), sem o #Shorts."""
    titulo = re.sub(r"[<>]", "", title).strip()
    hashtags = [h for h in hashtags if h.lower().lstrip("#") not in SO_TIKTOK]
    sufixo = " #Shorts" if short else ""
    if len(titulo) + len(sufixo) > 100:
        titulo = titulo[: 100 - len(sufixo) - 1].rstrip() + "…"
    titulo += sufixo
    tags_txt = " ".join(hashtags)
    descricao = (
        (f"{hook.strip()}\n\n" if hook and hook.strip() else "") +
        f"Clip da live de {channel_name} na {platform_label}.\n"
        f"Assista ao vivo: {channel_url}\n\n"
        f"{tags_txt}{' #shorts' if short else ''}\n\n"
        f"{marker(platform, channel_name)}"
    )
    tags = [h.lstrip("#") for h in hashtags][:15] + (["shorts"] if short else []) + [channel_name]
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
    extreme: bool = False


@dataclass
class PostStats:
    """Posts recentes deste canal monitorado no YouTube."""
    today: int = 0
    last_hour: int = 0
    minutes_since_last: float | None = None   # None = nenhum post nas últimas 24h


def decide(yt_settings: dict, channel_allows: bool, ratio: float, forced: bool, stats: PostStats | int,
           allow_forced: bool = False) -> PostDecision:
    """Decide se este clip vai pro YouTube (e o porquê, que aparece no resumo)."""
    if isinstance(stats, int):
        stats = PostStats(today=stats)
    if not yt_settings.get("enabled"):
        return PostDecision(False, "postagem no YouTube desligada")
    if not channel_allows:
        return PostDecision(False, "YouTube desligado para este canal")
    if forced and not allow_forced:
        return PostDecision(False, "clip de teste (--once) não é postado")
    limite = int(yt_settings.get("max_posts_per_day", 50))
    if stats.today >= limite:
        return PostDecision(False, f"limite de {limite} posts hoje atingido")
    if forced:
        return PostDecision(True, "teste")
    minimo = float(yt_settings.get("min_hype_ratio", 4.0))
    if ratio < minimo:
        return PostDecision(False, f"hype {ratio:.1f}x < {minimo:g}x (só pra revisão)")

    extremo = ratio >= float(yt_settings.get("extreme_hype_ratio", 8.0))
    por_hora = int(yt_settings.get("posts_per_hour", 2))
    if extremo:
        teto = max(por_hora, int(yt_settings.get("extreme_posts_per_hour", 3)))
        if stats.last_hour >= teto:
            return PostDecision(False, f"hype {ratio:.1f}x (explodiu), mas já foram {stats.last_hour} "
                                       "posts na última hora")
        return PostDecision(True, f"hype {ratio:.1f}x, chat explodiu", extreme=True)
    if stats.last_hour >= por_hora:
        return PostDecision(False, f"já foram {stats.last_hour} posts na última hora (ritmo: {por_hora}/h)")
    espaco = int(yt_settings.get("min_minutes_between_posts", 20))
    if stats.minutes_since_last is not None and stats.minutes_since_last < espaco:
        return PostDecision(False, f"último post há {stats.minutes_since_last:.0f} min (espera {espaco} min)")
    return PostDecision(True, f"hype {ratio:.1f}x")


@dataclass
class ContaStats:
    """Envios do CANAL do YouTube inteiro (todos os streamers + manuais) nas últimas 24h."""
    ult24h: int = 0
    ult_hora: int = 0
    min_desde_ultimo: float | None = None
    horizontais: int = 0


def conta_teto(yt_settings: dict, conta: str) -> int:
    tetos = yt_settings.get("max_uploads_24h") or {}
    if isinstance(tetos, int):
        return tetos
    return int(tetos.get(conta or "principal") or tetos.get("principal") or 11)


def viral_minimo(yt_settings: dict, usados: int, teto: int) -> float:
    """Nota mínima pra pegar uma vaga: sobe conforme o canal enche."""
    faixas = yt_settings.get("viral_minimo") or [6.5, 7.5, 8.5]
    uso = usados / max(1, teto)
    return float(faixas[0] if uso < 0.4 else faixas[1] if uso < 0.75 else faixas[2])


def decide_conta(yt_settings: dict, conta: str, cs: ContaStats, viral: float | None, fonte: str,
                 extremo: bool = False) -> PostDecision:
    """Filtro do canal do YouTube inteiro: vaga nas 24h, qualidade do título e nota de viral."""
    teto = conta_teto(yt_settings, conta)
    if cs.ult24h >= teto:
        return PostDecision(False, f"canal do YouTube cheio ({cs.ult24h}/{teto} envios nas últimas 24h)")
    if fonte != "gemini" or viral is None:
        return PostDecision(False, "título reserva (sem IA): não posto pra não repetir título genérico")
    minimo = viral_minimo(yt_settings, cs.ult24h, teto) - (1.0 if extremo else 0.0)
    if viral < minimo:
        return PostDecision(False, f"nota {viral:g} < {minimo:g} (canal com {cs.ult24h}/{teto} em 24h: "
                                   "vaga guardada pros melhores momentos)")
    espaco = float(yt_settings.get("account_min_minutes_between_posts", 20))
    if not extremo and cs.min_desde_ultimo is not None and cs.min_desde_ultimo < espaco:
        return PostDecision(False, f"o canal postou há {cs.min_desde_ultimo:.0f} min (espaço: {espaco:g} min)")
    return PostDecision(True, f"nota {viral:g}, vaga {cs.ult24h + 1}/{teto}")


class YouTube:
    def __init__(self, client_id: str | None, client_secret: str | None, refresh_token: str | None,
                 timezone: str = "America/Sao_Paulo"):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.configured = bool(self.client_id and self.client_secret and self.refresh_token)
        self.timezone = timezone
        self.conta = "principal"
        self._access_token: str | None = None
        self._token_exp = 0.0
        self._uploads_playlist: str | None = None
        self._post_times: dict[str, list[float]] = {}   # marca -> epochs dos posts das últimas ~24h
        self._conta_cache: tuple[float, list[tuple[float, str]]] | None = None   # (lido em, [(epoch, descrição)])
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, timezone: str = "America/Sao_Paulo", conta: str = "") -> "YouTube":
        """conta "" = canal principal; "dante" = YOUTUBE_REFRESH_TOKEN_DANTE (sem cair no principal)."""
        suf = f"_{conta.upper()}" if conta else ""
        env = os.environ.get
        yt = cls(env(f"YOUTUBE_CLIENT_ID{suf}") or env("YOUTUBE_CLIENT_ID"),
                 env(f"YOUTUBE_CLIENT_SECRET{suf}") or env("YOUTUBE_CLIENT_SECRET"),
                 env(f"YOUTUBE_REFRESH_TOKEN{suf}"), timezone)
        yt.conta = conta or "principal"
        return yt

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

    def _fetch_post_times(self, chave: str) -> list[float]:
        """Horários (epoch) dos uploads deste canal monitorado nas últimas 24h (até 4 páginas)."""
        return sorted(t for t, d in self._recent_uploads() if chave in d)

    def conta_stats(self, now: float | None = None, max_age: float = 300) -> ContaStats:
        """Envios do canal do YouTube inteiro nas últimas 24h (relido a cada 5 min: outros jobs postam)."""
        now = time.time() if now is None else now
        with self._lock:
            if self._conta_cache is None or now - self._conta_cache[0] > max_age:
                self._conta_cache = (now, self._recent_uploads())
            ups = [(t, d) for t, d in self._conta_cache[1] if now - t < 24 * 3600]
        tempos = [t for t, _ in ups]
        return ContaStats(ult24h=len(ups), ult_hora=sum(1 for t in tempos if now - t < 3600),
                          min_desde_ultimo=(now - max(tempos)) / 60 if tempos else None,
                          horizontais=sum(1 for _, d in ups if "clipador:horizontal/" in d))

    def _recent_uploads(self) -> list[tuple[float, str]]:
        """(epoch, descrição) de todos os uploads do canal nas últimas 24h (até 4 páginas)."""
        if not self._uploads_playlist:
            d = self._get("channels", {"part": "contentDetails", "mine": "true"})
            if not d.get("items"):
                raise YouTubeError("a conta autorizada não tem canal no YouTube")
            self._uploads_playlist = d["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        limite = time.time() - 24 * 3600
        tempos: list[tuple[float, str]] = []
        token = None
        for _ in range(4):
            params = {"part": "snippet", "playlistId": self._uploads_playlist, "maxResults": 50}
            if token:
                params["pageToken"] = token
            d = self._get("playlistItems", params)
            antigos = 0
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                try:
                    t = datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if t < limite:
                    antigos += 1
                else:
                    tempos.append((t, sn.get("description") or ""))
            token = d.get("nextPageToken")
            if not token or antigos:
                break
        return sorted(tempos)

    def titulos_postados(self, paginas: int = 4) -> set[str]:
        """Títulos dos últimos uploads do canal (sem o ' #Shorts'), pra não repostar o mesmo clip."""
        if not self._uploads_playlist:
            d = self._get("channels", {"part": "contentDetails", "mine": "true"})
            if not d.get("items"):
                raise YouTubeError("a conta autorizada não tem canal no YouTube")
            self._uploads_playlist = d["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        titulos: set[str] = set()
        token = None
        for _ in range(paginas):
            params = {"part": "snippet", "playlistId": self._uploads_playlist, "maxResults": 50}
            if token:
                params["pageToken"] = token
            d = self._get("playlistItems", params)
            for it in d.get("items", []):
                t = (it.get("snippet", {}).get("title") or "").replace("#Shorts", "").strip().lower()
                if t:
                    titulos.add(t)
            token = d.get("nextPageToken")
            if not token:
                break
        return titulos

    def stats(self, platform: str, name: str, now: float | None = None) -> PostStats:
        """Posts de hoje / da última hora / minutos desde o último (consulta o YouTube 1x por job)."""
        now = time.time() if now is None else now
        with self._lock:
            chave = marker(platform, name)
            if chave not in self._post_times:
                self._post_times[chave] = self._fetch_post_times(chave)
            tempos = self._post_times[chave]
            hoje = self._today()
            return PostStats(
                today=sum(1 for t in tempos if self._epoch_to_local_day(t) == hoje),
                last_hour=sum(1 for t in tempos if now - t < 3600),
                minutes_since_last=(now - max(tempos)) / 60 if tempos else None,
            )

    def posted_today(self, platform: str, name: str) -> int:
        return self.stats(platform, name).today

    def _epoch_to_local_day(self, t: float) -> str:
        try:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(t, ZoneInfo(self.timezone)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return datetime.fromtimestamp(t).strftime("%Y-%m-%d")

    def _increment(self, platform: str, name: str) -> None:
        with self._lock:
            self._post_times.setdefault(marker(platform, name), []).append(time.time())

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
                    _checa_limite(init)
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
                    with self._lock:
                        if self._conta_cache is not None:
                            self._conta_cache[1].append((time.time(), metadata["snippet"].get("description", "")))
                    short = "#Shorts" in metadata["snippet"].get("title", "")
                    url = f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}"
                    status = r.json().get("status", {})
                    if status.get("privacyStatus") != metadata["status"]["privacyStatus"]:
                        log.warning("YouTube deixou o vídeo como %s (pedido: %s)", status.get("privacyStatus"),
                                    metadata["status"]["privacyStatus"])
                    log.info("postado no YouTube: %s", url)
                    return url
                _checa_limite(r)
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
