"""Título, legenda e hashtags de cada clip.

Com GEMINI_API_KEY (grátis em https://aistudio.google.com/apikey): o Gemini OUVE o áudio do clip e lê
as reações do chat, e escreve um título chamativo, a legenda do TikTok, as hashtags e uma nota de
viralidade (0-10, usada pra escolher os melhores horários do TikTok).

Sem a chave (ou se a IA falhar): plano B local, que usa a frase dita no AUGE do momento (não a primeira
frase do clip) e as reações do chat.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("clipador.copywriter")

API = "https://generativelanguage.googleapis.com/v1beta"
_GTA6_RE = re.compile(r"grand theft auto (vi|6)\b|\bgta ?(vi|6)\b", re.IGNORECASE)


@dataclass
class ClipCopy:
    titulo: str                      # título do Short / nome do arquivo
    legenda: str                     # texto do post no TikTok (sem hashtags)
    hashtags: list[str] = field(default_factory=list)
    viral: float | None = None       # 0-10 (só com IA)
    sobre_gta6: bool = False
    fonte: str = "local"             # "gemini" ou "local"


# ------------------------------------------------------------------ plano B (sem IA)
# A transcrição automática erra muito em live (gíria, jogo alto, várias vozes), então o plano B NÃO cita
# fala: escreve uma chamada pela REAÇÃO do chat, que nunca sai sem sentido.
_REACOES: list[tuple[re.Pattern, list[tuple[str, str]]]] = [
    (re.compile(r"k{3,}|kekw|omegalul|\blul\b|lmao|haha|rsrs|icant", re.IGNORECASE), [
        ("{c} fez o chat inteiro chorar de rir 😂", "Eu não aguentei no final 😂 E você?"),
        ("ninguém aguentou esse momento do {c} 😂", "Assiste até o fim 😂"),
        ("o chat do {c} não parou de rir depois disso 💀", "Quem nunca? 💀"),
        ("{c} e o momento mais engraçado da live 😂", "Marca aquele amigo que faria isso 😂"),
    ]),
    (re.compile(r"\bgg\b|f+\b|perdeu|sad|rip|pain|nooo", re.IGNORECASE), [
        ("{c} não acreditou no que aconteceu 😭", "Doeu até em mim 😭"),
        ("a derrota mais sofrida da live do {c} 😭", "Você teria aguentado? 😭"),
    ]),
    (re.compile(r"\bd:|nossa|mds|meu deus|ue\b|ué|wtf|huhh|\?\?|caraca|eita|monka", re.IGNORECASE), [
        ("o chat do {c} ficou em choque com isso 😳", "Ninguém esperava por essa 😳"),
        ("{c} não esperava por isso 😳", "Você esperava? 😳"),
    ]),
    (re.compile(r"clip|corta|corte", re.IGNORECASE), [
        ("o chat inteiro pediu clip desse momento do {c} 🎬", "Pediram, tá aqui 🎬"),
    ]),
]
_PADRAO = [
    ("o momento em que o chat do {c} explodiu 🔥", "Assiste até o fim 🔥"),
    ("{c} e o momento mais insano da live 🔥", "Você viu ao vivo? 🔥"),
]


def local_copy(channel: str, words, peak_offset: float, top_messages: list[tuple[str, int]],
               category: str | None, stream_title: str | None, base_tags: list[str],
               seed: float = 0.0) -> ClipCopy:
    """Chamada escolhida pela reação do chat (risada, choque, derrota...)."""
    top = " ".join(m for m, _ in (top_messages or [])[:3])
    opcoes = _PADRAO
    for padrao, frases in _REACOES:
        if padrao.search(top):
            opcoes = frases
            break
    titulo, gancho = opcoes[int(seed) % len(opcoes)]  # varia entre clips, mas é estável pro mesmo pico
    titulo, gancho = titulo.format(c=channel), gancho.format(c=channel)
    fala = " ".join(w.text for w in (words or []))
    gta = bool(_GTA6_RE.search(f"{category or ''} {stream_title or ''}")) and bool(
        _GTA6_RE.search(f"{category or ''} {fala} {top}"))
    if gta:
        titulo = "GTA 6: " + titulo
    return ClipCopy(titulo=titulo[:95], legenda=f"{titulo}\n{gancho}", hashtags=_hashtags(base_tags, channel, gta, []),
                    sobre_gta6=gta, fonte="local")


def _hashtags(base: list[str], channel: str, gta: bool, extras: list[str], limite: int = 7) -> list[str]:
    tags: list[str] = []
    for t in [f"#{channel}", *(["#gta6", "#gtavi"] if gta else []), *extras, *base]:
        t = "#" + re.sub(r"[^\wÀ-ÿ]", "", str(t).lstrip("#")).lower()
        if len(t) > 1 and (t not in ("#gta6", "#gtavi", "#gta") or gta) and t not in tags:
            tags.append(t)
    return tags[:limite]


# ------------------------------------------------------------------ Gemini
PROMPT = """Você é social media de um canal de CORTES de lives brasileiras (TikTok e YouTube Shorts).
Ouça o áudio deste clip de ~40s da live de "{canal}" e escreva o texto do post.

Contexto:
- Título da live: {titulo_live}
- Jogo/categoria: {categoria}
- O pico de agitação do chat acontece por volta de {pico:.0f}s do clip.
- Mensagens mais repetidas no chat nesse momento: {chat}
- Transcrição automática (pode ter erros, o ÁUDIO vale mais): {fala}

Regras:
- "titulo": até 70 caracteres, em português do Brasil informal, que dê vontade de assistir. Use o nome
  "{canal}". Pode ser uma frase marcante dita no clip (entre aspas) OU uma descrição curiosa do que acontece.
  NÃO invente nada que não acontece no clip. Sem clickbait mentiroso. No máximo 2 emojis, no final.
- "legenda": 1 ou 2 frases curtas pro TikTok (gancho + uma pergunta que puxe comentários). Sem hashtags.
- "sobre_gta6": true SÓ se o clip fala ou mostra GTA 6 / GTA VI de verdade.
- "hashtags": 3 a 5 hashtags específicas do conteúdo (jogo, situação). Sem #fyp/#viral (já entram sozinhas).
- "viral": nota de 0 a 10 de potencial de viralizar (graça, surpresa, frase marcante, contexto que
  quem não conhece o streamer entende). Seja exigente: 8+ só para momentos realmente fortes.
Responda SÓ o JSON: {{"titulo": "...", "legenda": "...", "sobre_gta6": false, "hashtags": ["#..."], "viral": 0}}"""


class Copywriter:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        self.model_env = (model if model is not None else os.environ.get("GEMINI_MODEL", "")).strip()
        self._models: list[str] | None = None
        self._lock = threading.Lock()
        self._falhas_seguidas = 0
        self._pausa_ate = 0.0   # Gemini sobrecarregado: descansa uns minutos e tenta de novo

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _resolve_models(self, refresh: bool = False) -> list[str]:
        """Modelos 'flash' disponíveis pra chave (os nomes mudam quando o Google aposenta modelos)."""
        if self.model_env and not refresh:
            return [self.model_env]
        if self._models and not refresh:
            return self._models
        import requests

        escolhidos = ["gemini-flash-latest"]
        try:
            r = requests.get(f"{API}/models", params={"pageSize": 200, "key": self.api_key}, timeout=15)
            r.raise_for_status()

            def versao(n: str) -> float:
                m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
                return float(m.group(1)) if m else 0.0

            nomes = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            cands = [n for n in nomes if re.match(r"^gemini-[\d.]+-flash", n)
                     and not re.search(r"lite|image|tts|live|audio|thinking|exp", n)]
            cands.sort(key=lambda n: (-versao(n), "preview" in n, len(n)))
            if cands:
                escolhidos = [*cands[:4], "gemini-flash-latest"]
            if "gemini-flash-lite-latest" in nomes:
                escolhidos.append("gemini-flash-lite-latest")
        except Exception as e:  # noqa: BLE001
            log.warning("não consegui listar os modelos do Gemini: %s", e)
        self._models = escolhidos
        return escolhidos

    def _call(self, parts: list[dict]) -> str:
        import requests

        def chamar(model: str, thinking: bool):
            cfg = {"temperature": 0.8, "maxOutputTokens": 600, "responseMimeType": "application/json"}
            if thinking:
                cfg["thinkingConfig"] = {"thinkingBudget": 0}
            return requests.post(f"{API}/models/{model}:generateContent", params={"key": self.api_key},
                                 json={"contents": [{"parts": parts}], "generationConfig": cfg}, timeout=35)

        with self._lock:
            modelos = self._resolve_models()
        ultimo = ""
        prazo = time.monotonic() + 120   # não segura o clip mais que ~2 min
        for i, m in enumerate(list(modelos)):
            r = None
            for tentativa in range(2):
                if time.monotonic() > prazo:
                    raise RuntimeError(f"Gemini lento demais agora ({ultimo})")
                try:
                    r = chamar(m, thinking=True)
                    if r.status_code == 400:
                        r = chamar(m, thinking=False)  # alguns modelos não aceitam thinkingConfig
                except requests.RequestException as e:
                    ultimo = f"{m}: {type(e).__name__}"
                    r = None
                    break  # travou: tenta o próximo modelo
                if r.status_code == 200:
                    with self._lock:
                        self._models = [m, *[x for x in modelos if x != m]]
                    cand = (r.json().get("candidates") or [{}])[0]
                    return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
                ultimo = f"{m}: {r.status_code} {r.text[:200]}"
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(3 * (tentativa + 1))
                    continue
                break
            if r is not None and r.status_code == 404 and i == 0 and not self.model_env:
                with self._lock:
                    modelos = self._resolve_models(refresh=True)
        raise RuntimeError(ultimo or "Gemini não respondeu")

    def gemini_copy(self, channel: str, audio_path: Path | None, transcript: str, peak_offset: float,
                    top_messages: list[tuple[str, int]], category: str | None, stream_title: str | None,
                    base_tags: list[str]) -> ClipCopy:
        chat = ", ".join(f"{m} ({n}x)" for m, n in (top_messages or [])[:6]) or "(sem dados)"
        prompt = PROMPT.format(canal=channel, titulo_live=stream_title or "(desconhecido)",
                               categoria=category or "(desconhecida)", pico=peak_offset, chat=chat,
                               fala=(transcript or "(sem fala detectada)")[:1500])
        parts: list[dict] = []
        if audio_path and Path(audio_path).exists() and Path(audio_path).stat().st_size < 15_000_000:
            parts.append({"inline_data": {"mime_type": "audio/wav",
                                          "data": base64.b64encode(Path(audio_path).read_bytes()).decode()}})
        parts.append({"text": prompt})
        txt = self._call(parts)
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            raise ValueError(f"resposta sem JSON: {txt[:200]}")
        d = json.loads(m.group(0))
        titulo = re.sub(r"\s+", " ", str(d.get("titulo", ""))).strip()
        if not 5 <= len(titulo) <= 110:
            raise ValueError(f"título inválido: {titulo!r}")
        gta = bool(d.get("sobre_gta6"))
        if gta and not _GTA6_RE.search(titulo):
            titulo = "GTA 6: " + titulo
        legenda = re.sub(r"#\S+", "", str(d.get("legenda", ""))).strip() or titulo
        extras = [str(h) for h in (d.get("hashtags") or []) if isinstance(h, str)][:5]
        viral = d.get("viral")
        viral = max(0.0, min(10.0, float(viral))) if isinstance(viral, (int, float)) else None
        return ClipCopy(titulo=titulo[:95], legenda=legenda[:300], viral=viral, sobre_gta6=gta,
                        hashtags=_hashtags(base_tags, channel, gta, extras, limite=8), fonte="gemini")

    def make(self, channel: str, words, transcript: str, audio_path: Path | None, peak_offset: float,
             top_messages: list[tuple[str, int]], category: str | None, stream_title: str | None,
             base_tags: list[str]) -> ClipCopy:
        """Texto do post: Gemini se configurado, senão (ou se falhar) o plano B local."""
        base = [t for t in base_tags if t.lower() not in ("#gta6", "#gta", "#gtavi")]
        if self.configured and self._falhas_seguidas >= 5 and time.time() >= self._pausa_ate:
            self._falhas_seguidas = 4   # passou a pausa: mais uma chance
        if self.configured and self._falhas_seguidas < 5:
            try:
                c = self.gemini_copy(channel, audio_path, transcript, peak_offset, top_messages, category,
                                     stream_title, base)
                self._falhas_seguidas = 0
                log.info("título (IA, viral %s/10): %s", c.viral, c.titulo)
                return c
            except Exception as e:  # noqa: BLE001
                self._falhas_seguidas += 1
                if self._falhas_seguidas >= 5:
                    self._pausa_ate = time.time() + 600
                    log.warning("Gemini falhou 5x seguidas; tento de novo em 10 min")
                log.warning("Gemini falhou (%s); usando título local", str(e)[:200])
        return local_copy(channel, words, peak_offset, top_messages, category, stream_title, base,
                          seed=time.time() // 60)
