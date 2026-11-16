"""Detecção de picos de hype no chat.

Como funciona:
1. Cada mensagem recebe um peso (1 = normal; mais se tiver KKKK, CLIP, KEKW, caps lock, emote repetido...).
2. Os pesos são somados por segundo.
3. A cada segundo fechado, o "score" é a soma da janela dos últimos `window_seconds` segundos.
4. A baseline é a MEDIANA dos scores dos últimos `baseline_seconds` (o "normal" do chat).
5. Dispara um pico quando score >= max(min_msgs, baseline * peak_multiplier),
   respeitando o aquecimento inicial e o cooldown entre clips.

Tudo é baseado nos timestamps das mensagens (não no relógio), então dá pra testar com chat sintético.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field

# Palavras/emotes que indicam hype (comparadas em minúsculas, palavra inteira)
HYPE_WORDS = {
    "clip", "clipa", "clipar", "clipou", "clipped", "lul", "lol", "omegalul", "kekw", "kek", "lmao",
    "caralho", "crlh", "krl", "nossa", "mds", "meudeus", "pog", "poggers", "pogchamp",
    "pogu", "monkas", "pepelaugh", "icant", "wtf", "aaaa", "jesus", "socorro", "gg", "ez",
    "omg", "insano", "absurdo", "pqp", "vish", "eita", "uau", "xd",
}
# Expressões que valem mais (pedido explícito de clip)
CLIP_RE = re.compile(r"(^|\s)!?clip+(\s|$|!)", re.IGNORECASE)
LAUGH_RE = re.compile(r"\b(k{3,}|(?:ha){2,}h?|(?:rs){2,}|(?:ja){3,}|(?:he){3,})\b", re.IGNORECASE)
KICK_EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")
WORD_RE = re.compile(r"[\w!]+", re.UNICODE)

MAX_WEIGHT = 4.0


def clean_text(text: str) -> str:
    """Troca emotes da Kick ([emote:123:KEKW]) pelo nome e tira espaços extras."""
    text = KICK_EMOTE_RE.sub(lambda m: f" {m.group(1)} ", text or "")
    return " ".join(text.split())


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def message_weight(text: str) -> float:
    """Peso de uma mensagem para o score de hype (1.0 a MAX_WEIGHT)."""
    text = clean_text(text)
    if not text:
        return 0.0
    w = 1.0
    low = _strip_accents(text.lower())
    words = WORD_RE.findall(low)

    if CLIP_RE.search(low):
        w += 1.5
    if LAUGH_RE.search(low):
        w += 1.0
    if any(word.strip("!") in HYPE_WORDS for word in words):
        w += 0.75

    # CAPS LOCK (pelo menos 5 letras e 70% maiúsculas)
    letras = [c for c in text if c.isalpha()]
    if len(letras) >= 5 and sum(c.isupper() for c in letras) / len(letras) >= 0.7:
        w += 0.5

    # Emote/palavra repetida 3+ vezes na mesma mensagem (ex.: "KEKW KEKW KEKW")
    if words:
        _, n = Counter(words).most_common(1)[0]
        if n >= 3:
            w += 0.5

    return min(w, MAX_WEIGHT)


def normalize_for_top(text: str) -> str:
    """Normaliza a mensagem para agrupar repetições: 'kkkkkkk' e 'KKKK' viram 'KKKK'."""
    text = clean_text(text)
    text = re.sub(r"(.)\1{3,}", r"\1\1\1", text)  # aaaaaaa -> aaa
    text = re.sub(r"(?i)k{3,}", "KKKK", text)
    text = text.strip()
    if len(text) > 60:
        text = text[:57] + "..."
    # mesma mensagem com caixa diferente conta junto; mostramos em maiúsculas se for curta
    return text.upper() if len(text) <= 20 else text


@dataclass
class Peak:
    t: float                 # segundo (epoch) em que o pico disparou
    score: float             # score da janela no disparo
    baseline: float          # mediana dos scores anteriores
    threshold: float         # limiar usado
    top_messages: list[tuple[str, int]] = field(default_factory=list)
    forced: bool = False     # True quando foi forçado (modo --once)

    @property
    def ratio(self) -> float:
        return self.score / self.baseline if self.baseline > 0 else float("inf")


class HypeDetector:
    def __init__(
        self,
        window_seconds: int = 8,
        baseline_seconds: int = 300,
        peak_multiplier: float = 2.5,
        min_msgs: float = 12,
        warmup_seconds: int = 60,
        cooldown_seconds: int = 45,
    ):
        self.window = int(window_seconds)
        self.baseline_len = int(baseline_seconds)
        self.multiplier = float(peak_multiplier)
        self.min_msgs = float(min_msgs)
        self.warmup = int(warmup_seconds)
        self.cooldown = int(cooldown_seconds)

        self._buckets: dict[int, float] = {}               # segundo -> soma dos pesos
        self._history: deque[float] = deque(maxlen=self.baseline_len)  # scores anteriores
        self._recent: deque[tuple[int, str]] = deque()     # (segundo, msg normalizada) p/ top mensagens
        self._start: int | None = None
        self._next_second: int | None = None               # próximo segundo a ser processado
        self._cooldown_until: int = 0
        self.total_messages = 0
        self.last_score = 0.0
        self.last_baseline = 0.0

    @classmethod
    def from_settings(cls, settings: dict, min_msgs: float | None = None) -> "HypeDetector":
        return cls(
            window_seconds=settings["window_seconds"],
            baseline_seconds=settings["baseline_seconds"],
            peak_multiplier=settings["peak_multiplier"],
            min_msgs=min_msgs if min_msgs is not None else settings["min_msgs"],
            warmup_seconds=settings["warmup_seconds"],
            cooldown_seconds=settings["cooldown_seconds"],
        )

    def start(self, now: float) -> None:
        """Marca o início do monitoramento (o aquecimento conta a partir daqui)."""
        if self._start is None:
            self._start = int(now)
            self._next_second = int(now)

    def add(self, text: str, ts: float) -> None:
        """Registra uma mensagem do chat com seu timestamp (epoch, segundos)."""
        self.start(ts)
        sec = int(ts)
        if self._next_second is not None and sec < self._next_second:
            sec = self._next_second  # mensagem atrasada: conta no segundo ainda aberto
        w = message_weight(text)
        if w <= 0:
            return
        self.total_messages += 1
        self._buckets[sec] = self._buckets.get(sec, 0.0) + w
        norm = normalize_for_top(text)
        if norm:
            self._recent.append((sec, norm))

    def _window_score(self, sec: int) -> float:
        return sum(self._buckets.get(s, 0.0) for s in range(sec - self.window + 1, sec + 1))

    def _top_messages(self, sec: int, n: int = 5) -> list[tuple[str, int]]:
        ini = sec - self.window - 4  # um pouquinho antes da janela também
        c = Counter(m for s, m in self._recent if ini <= s <= sec)
        return c.most_common(n)

    def _cleanup(self, sec: int) -> None:
        limite = sec - self.window - 10
        for s in [s for s in self._buckets if s < limite]:
            del self._buckets[s]
        while self._recent and self._recent[0][0] < limite:
            self._recent.popleft()

    def baseline(self) -> float:
        return statistics.median(self._history) if self._history else 0.0

    def tick(self, now: float) -> Peak | None:
        """Processa todos os segundos já fechados até `now`. Retorna um Peak se disparou."""
        self.start(now)
        assert self._next_second is not None and self._start is not None
        fechado = int(now) - 1  # o segundo atual ainda pode receber mensagens
        pico: Peak | None = None
        while self._next_second <= fechado:
            sec = self._next_second
            self._next_second += 1
            score = self._window_score(sec)
            base = self.baseline()
            limiar = max(self.min_msgs, base * self.multiplier)
            self.last_score, self.last_baseline = score, base

            aquecido = sec - self._start >= self.warmup
            if pico is None and aquecido and sec >= self._cooldown_until and score >= limiar:
                pico = Peak(t=float(sec), score=round(score, 1), baseline=round(base, 1),
                            threshold=round(limiar, 1), top_messages=self._top_messages(sec))
                self._cooldown_until = sec + self.cooldown
            self._history.append(score)
            self._cleanup(sec)
        return pico

    def force_peak(self, now: float) -> Peak:
        """Cria um pico manual (modo --once), com as mensagens recentes."""
        sec = int(now)
        self._cooldown_until = sec + self.cooldown
        return Peak(t=float(sec), score=round(self._window_score(sec), 1), baseline=round(self.baseline(), 1),
                    threshold=0.0, top_messages=self._top_messages(sec), forced=True)
