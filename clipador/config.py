"""Leitura e validação do channels.json.

O arquivo tem duas partes:
- "settings": configurações globais (duração do clip, sensibilidade do detector, etc.)
- "channels": lista de canais monitorados.

Qualquer campo ausente em "settings" usa o valor padrão de DEFAULT_SETTINGS.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "channels.json"

PLATFORMS = ("twitch", "kick")
LAYOUTS = ("blur", "facecam", "crop")

DEFAULT_SETTINGS: dict[str, Any] = {
    # Clip: quantos segundos antes/depois do pico entram no corte
    "clip_before_seconds": 25,
    "clip_after_seconds": 12,
    "max_clip_seconds": 60,
    # Espera depois do pico antes de montar (pra gravar o "depois")
    "wait_after_peak_seconds": 12,
    # Detector de hype
    "window_seconds": 8,          # janela somada que vira o "score"
    "baseline_seconds": 300,      # histórico usado na mediana (baseline)
    "peak_multiplier": 3.5,       # score precisa ser >= baseline * multiplicador
    "min_msgs": 12,               # ...e também >= este mínimo absoluto
    "warmup_seconds": 60,         # não dispara nos primeiros N segundos
    "cooldown_seconds": 90,       # intervalo mínimo entre dois clips
    # Gravação
    "segment_seconds": 5,
    "buffer_seconds": 180,
    "max_quality": 1080,
    "fps": 30,
    # Legenda
    "subtitles": True,            # false = clips sem legenda queimada (a fala ainda é transcrita p/ o título)
    "subtitle_size": 80,          # tamanho da fonte (1080x1920). 124 = grande, 80 = médio/pequeno
    "subtitle_position": "middle",  # "middle" (meio; no facecam fica na divisa cam/jogo) ou "bottom"
    "whisper_model": "base",
    "language": "pt",
    "timezone": "America/Sao_Paulo",
    "hashtags": ["#clips", "#cortes", "#fyp"],
    # Execução
    "max_runtime_minutes": 340,
    "max_clips_per_run": 120,
}

# Postagem automática no YouTube Shorts (o TikTok fica semiautomático: o clip chega no Telegram)
DEFAULT_YOUTUBE: dict[str, Any] = {
    "enabled": False,          # liga/desliga a postagem automática
    "privacy": "public",       # public | unlisted | private
    "min_hype_ratio": 4.0,     # só posta se o auge do pico for >= N vezes o normal do chat
    "posts_per_hour": 2,       # ritmo normal: até N posts por hora...
    "min_minutes_between_posts": 20,  # ...espaçados (não solta 2 seguidos)
    "extreme_hype_ratio": 8.0,  # chat MUITO acima do normal (>= N vezes)...
    "extreme_posts_per_hour": 3,  # ...libera um post extra na hora, sem esperar o espaçamento
    "max_posts_per_day": 50,   # teto diário por canal monitorado
    # Teto do CANAL do YouTube (todos os streamers somados, últimas 24h). O YouTube trava novos envios
    # ("uploadLimitExceeded") por volta de 10-15/24h em canal novo; ficar abaixo evita a trava.
    "max_uploads_24h": {"principal": 11},
    # Nota de viral mínima conforme o canal enche: até 40% das vagas / até 75% / o resto.
    # Assim as últimas vagas do dia ficam pros momentos mais fortes, de qualquer streamer.
    "viral_minimo": [6.5, 7.5, 8.5],
    "account_min_minutes_between_posts": 20,  # espaço mínimo entre dois envios no MESMO canal do YouTube
    "reserva_live": 4,         # o postar_lote (clips antigos) deixa N vagas das 24h livres pras lives
    # Versão horizontal (16:9, tela da live inteira) dos momentos mais fortes, como vídeo normal.
    "horizontal": {"enabled": False, "min_viral": 8.0, "max_24h": 3, "extra_before_seconds": 20},
    "category_id": "20",       # 20 = Jogos, 24 = Entretenimento
    "made_for_kids": False,
}

_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]{2,40}$")


class ConfigError(ValueError):
    pass


@dataclass
class Facecam:
    """Região da câmera em frações (0-1) da tela original."""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.25
    h: float = 0.28

    def validate(self, where: str) -> None:
        for nome in ("x", "y", "w", "h"):
            v = getattr(self, nome)
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise ConfigError(f"{where}: facecam.{nome} deve estar entre 0 e 1 (veio {v!r})")
        if self.w <= 0 or self.h <= 0:
            raise ConfigError(f"{where}: facecam.w e facecam.h precisam ser > 0")
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ConfigError(f"{where}: a facecam passa da borda da tela (x+w ou y+h > 1)")


@dataclass
class Channel:
    platform: str
    name: str
    layout: str = "blur"
    enabled: bool = True
    kick_chatroom_id: int | None = None
    facecam: Facecam | None = None
    facecam_auto: bool = False  # "facecam": "auto" -> acha a câmera sozinho em cada clip
    min_msgs: float | None = None
    youtube: bool = True       # false = nunca posta este canal no YouTube (só Telegram)
    youtube_conta: str = ""    # "" = canal principal; "dante" = usa YOUTUBE_REFRESH_TOKEN_DANTE
    hashtags: list[str] | None = None  # hashtags próprias do canal (senão usa settings.hashtags)
    watermark: str | None = None       # texto fixo no vídeo inteiro (ex.: "kick.com/allanstag")
    title_tags: list[str] | None = None  # hashtags que TÊM que estar no título (ex.: ["#allanstag"])
    so_gta: bool = False       # true = só posta no YouTube os clips de GTA (streamer que joga outras coisas)
    horizontal: bool = True    # false = nunca posta a versão horizontal deste streamer
    so_gameplay: bool = False  # true = descarta clip que não é tela de jogo (IRL, evento, só conversa)
    # edição própria: {"gancho": frase grande nos 1os 2,5s, "zoom_pico": zoom rápido no auge}
    edicao: dict | None = None

    @property
    def editar(self) -> bool:
        return bool(self.edicao and (self.edicao.get("gancho") or self.edicao.get("zoom_pico")))

    def limpar_hashtags(self, tags: list[str]) -> list[str]:
        """Tira hashtag da OUTRA plataforma (ex.: #clipsdetwitch num streamer do Kick)."""
        outra = "kick" if self.platform == "twitch" else "twitch"
        return [t for t in tags if outra not in t.lower()]

    def titulo_com_tags(self, titulo: str, limite: int = 91) -> str:
        """Garante as hashtags obrigatórias no título (cabendo no limite do YouTube com ' #Shorts')."""
        faltam = [t for t in (self.title_tags or []) if t.lower() not in titulo.lower()]
        if not faltam:
            return titulo
        sufixo = " " + " ".join(faltam)
        if len(titulo) + len(sufixo) > limite:
            titulo = titulo[: limite - len(sufixo) - 1].rstrip() + "…"
        return titulo + sufixo

    @property
    def slug(self) -> str:
        return f"{self.platform}-{self.name}"

    @property
    def url(self) -> str:
        if self.platform == "twitch":
            return f"https://www.twitch.tv/{self.name}"
        return f"https://kick.com/{self.name}"

    @property
    def platform_label(self) -> str:
        return "Twitch" if self.platform == "twitch" else "Kick"


@dataclass
class Config:
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    channels: list[Channel] = field(default_factory=list)

    def find(self, platform: str, name: str) -> Channel | None:
        name = name.lower()
        for c in self.channels:
            if c.platform == platform and c.name.lower() == name:
                return c
        return None

    def channel_or_default(self, platform: str, name: str) -> Channel:
        """Canal do config ou, se não existir (modo teste local), um canal com padrões."""
        c = self.find(platform, name)
        if c:
            return c
        c = Channel(platform=platform, name=name.lower())
        _validate_channel(c, f"canal {platform}/{name}")
        return c

    @property
    def enabled_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.enabled]


_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?(twitch\.tv|kick\.com)/(?!videos?/|clip/)([A-Za-z0-9_\-]{2,40})/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def parse_channel_ref(ref: str) -> tuple[str, str]:
    """Entende o link da live ou 'plataforma/canal'. Retorna (plataforma, canal).

    Aceita: https://www.twitch.tv/gaules · twitch.tv/gaules · https://kick.com/gaules · kick/gaules
    """
    ref = (ref or "").strip()
    m = _URL_RE.match(ref)
    if m:
        return ("twitch" if m.group(1).lower() == "twitch.tv" else "kick"), m.group(2).lower()
    plat, sep, nome = ref.lower().partition("/")
    if sep and plat in PLATFORMS and _SLUG_RE.match(nome):
        return plat, nome
    if re.search(r"(twitch\.tv|kick\.com)/(videos?|clip)/", ref, re.IGNORECASE):
        raise ConfigError(f"'{ref}' é um vídeo/VOD ou clip. O clipador só funciona com live AO VIVO "
                          "(use o link do canal, ex.: https://kick.com/gaules)")
    raise ConfigError(f"não entendi '{ref}'. Use o link da live (ex.: https://www.twitch.tv/gaules ou "
                      "https://kick.com/gaules) ou plataforma/canal (ex.: kick/gaules)")


def _parse_channel(raw: dict[str, Any], idx: int) -> Channel:
    where = f"channels[{idx}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: cada canal precisa ser um objeto JSON")
    if raw.get("url") and not raw.get("name"):
        try:
            raw = {**raw, **dict(zip(("platform", "name"), parse_channel_ref(raw["url"])))}
        except ConfigError as e:
            raise ConfigError(f"{where}: {e}") from None
    fc = raw.get("facecam")
    ch = Channel(
        platform=str(raw.get("platform", "")).lower().strip(),
        name=str(raw.get("name", "")).strip().lower(),
        layout=str(raw.get("layout", "blur")).lower().strip(),
        enabled=bool(raw.get("enabled", True)),
        kick_chatroom_id=raw.get("kick_chatroom_id"),
        facecam=Facecam(**fc) if isinstance(fc, dict) else None,
        facecam_auto=isinstance(fc, str) and fc.strip().lower() == "auto",
        min_msgs=raw.get("min_msgs"),
        youtube=bool(raw.get("youtube", True)),
        youtube_conta=str(raw.get("youtube_conta") or "").strip().lower(),
        hashtags=raw.get("hashtags"),
        watermark=(str(raw["watermark"]).strip() or None) if raw.get("watermark") else None,
        title_tags=raw.get("title_tags"),
        so_gta=bool(raw.get("so_gta", False)),
        horizontal=bool(raw.get("horizontal", True)),
        so_gameplay=bool(raw.get("so_gameplay", False)),
        edicao=raw.get("edicao") if isinstance(raw.get("edicao"), dict) else None,
    )
    _validate_channel(ch, where)
    return ch


def _validate_channel(ch: Channel, where: str) -> None:
    if ch.platform not in PLATFORMS:
        raise ConfigError(f"{where}: platform deve ser 'twitch' ou 'kick' (veio {ch.platform!r})")
    if not _SLUG_RE.match(ch.name):
        raise ConfigError(f"{where}: name inválido {ch.name!r} (use o slug do canal, ex.: 'gaules')")
    if ch.layout not in LAYOUTS:
        raise ConfigError(f"{where}: layout deve ser um de {LAYOUTS} (veio {ch.layout!r})")
    if ch.layout == "facecam" and ch.facecam is None and not ch.facecam_auto:
        ch.facecam = Facecam()  # canto superior esquerdo, padrão
    if ch.facecam:
        ch.facecam.validate(where)
    if ch.kick_chatroom_id is not None:
        try:
            ch.kick_chatroom_id = int(ch.kick_chatroom_id)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: kick_chatroom_id deve ser um número") from None
    if ch.min_msgs is not None and (not isinstance(ch.min_msgs, (int, float)) or ch.min_msgs <= 0):
        raise ConfigError(f"{where}: min_msgs deve ser um número > 0")
    if ch.youtube_conta in ("principal", "padrao", "padrão", "default"):
        ch.youtube_conta = ""
    if ch.youtube_conta and not re.fullmatch(r"[a-z0-9_]{1,30}", ch.youtube_conta):
        raise ConfigError(f"{where}: youtube_conta só pode ter letras, números e _ (veio {ch.youtube_conta!r})")
    if ch.hashtags is not None and (not isinstance(ch.hashtags, list)
                                    or not all(isinstance(h, str) for h in ch.hashtags)):
        raise ConfigError(f"{where}: hashtags deve ser uma lista de textos")
    if ch.title_tags is not None and (not isinstance(ch.title_tags, list)
                                      or not all(isinstance(h, str) for h in ch.title_tags)):
        raise ConfigError(f"{where}: title_tags deve ser uma lista de textos")


def _validate_settings(s: dict[str, Any]) -> None:
    positivos = [k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for k in positivos:
        if not isinstance(s[k], (int, float)) or s[k] <= 0:
            raise ConfigError(f"settings.{k} deve ser um número > 0 (veio {s[k]!r})")
    if s["clip_before_seconds"] + s["clip_after_seconds"] > s["max_clip_seconds"]:
        raise ConfigError("settings: clip_before_seconds + clip_after_seconds passa de max_clip_seconds")
    if s["max_clip_seconds"] > 60:
        raise ConfigError("settings.max_clip_seconds: máximo 60s")
    if s["buffer_seconds"] < s["clip_before_seconds"] + s["clip_after_seconds"] + s["wait_after_peak_seconds"] + 30:
        raise ConfigError("settings.buffer_seconds muito curto para a duração do clip")
    if s["subtitle_position"] not in ("middle", "bottom"):
        raise ConfigError("settings.subtitle_position deve ser 'middle' ou 'bottom'")
    if not isinstance(s["hashtags"], list):
        raise ConfigError("settings.hashtags deve ser uma lista")
    s["hashtags"] = [h if str(h).startswith("#") else f"#{h}" for h in s["hashtags"]]

    yt = dict(DEFAULT_YOUTUBE)
    yt.update(s.get("youtube") or {})
    if yt["privacy"] not in ("public", "unlisted", "private"):
        raise ConfigError("settings.youtube.privacy deve ser 'public', 'unlisted' ou 'private'")
    if not isinstance(yt["min_hype_ratio"], (int, float)) or yt["min_hype_ratio"] < 0:
        raise ConfigError("settings.youtube.min_hype_ratio deve ser um número >= 0")
    for k in ("max_posts_per_day", "posts_per_hour", "extreme_posts_per_hour", "min_minutes_between_posts"):
        if not isinstance(yt[k], int) or isinstance(yt[k], bool) or yt[k] < 0:
            raise ConfigError(f"settings.youtube.{k} deve ser um inteiro >= 0")
    if not isinstance(yt["extreme_hype_ratio"], (int, float)) or yt["extreme_hype_ratio"] < yt["min_hype_ratio"]:
        raise ConfigError("settings.youtube.extreme_hype_ratio deve ser um número >= min_hype_ratio")
    yt["enabled"] = bool(yt["enabled"])
    tetos = yt["max_uploads_24h"]
    if isinstance(tetos, int):
        tetos = {"principal": tetos}
    if not isinstance(tetos, dict) or not all(isinstance(v, int) and v > 0 for v in tetos.values()):
        raise ConfigError('settings.youtube.max_uploads_24h deve ser {"principal": 11, "dante": 10, ...}')
    yt["max_uploads_24h"] = {str(k).lower(): v for k, v in tetos.items()}
    vm = yt["viral_minimo"]
    if not (isinstance(vm, list) and len(vm) == 3 and all(isinstance(x, (int, float)) for x in vm)):
        raise ConfigError("settings.youtube.viral_minimo deve ser uma lista de 3 notas, ex.: [6.5, 7.5, 8.5]")
    h = dict(DEFAULT_YOUTUBE["horizontal"])
    h.update(yt.get("horizontal") or {})
    h["enabled"] = bool(h["enabled"])
    yt["horizontal"] = h
    s["youtube"] = yt


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Arquivo de configuração não encontrado: {path}") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"JSON inválido em {path}: linha {e.lineno}, coluna {e.colno}: {e.msg}") from None

    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw.get("settings") or {})
    _validate_settings(settings)

    channels = [_parse_channel(c, i) for i, c in enumerate(raw.get("channels") or [])]
    vistos = set()
    for c in channels:
        if c.slug in vistos:
            raise ConfigError(f"Canal duplicado no config: {c.platform}/{c.name}")
        vistos.add(c.slug)
    return Config(settings=settings, channels=channels)
