"""Legendas estilo TikTok com faster-whisper.

- Transcreve o áudio do clip (modelo "base", int8, CPU, language="pt") com timestamp por palavra.
- Agrupa de 2 a 4 palavras por vez, em MAIÚSCULAS, fonte grossa (Anton), contorno preto,
  parte de baixo-central, com a palavra falada no momento destacada em amarelo.
- Gera um arquivo .ass que o ffmpeg queima no vídeo com o filtro `subtitles`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("clipador.subtitles")

FONT_NAME = "Anton"
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"

_model = None
_model_key = None
_model_lock = threading.Lock()


@dataclass
class Word:
    start: float
    end: float
    text: str


def model_dir() -> str:
    """Pasta do cache do modelo (o GitHub Actions guarda essa pasta em cache)."""
    d = os.environ.get("CLIPADOR_MODEL_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "clipador", "whisper")
    os.makedirs(d, exist_ok=True)
    return d


def get_model(name: str = "base"):
    """Carrega o modelo do Whisper uma única vez (thread-safe)."""
    global _model, _model_key
    with _model_lock:
        if _model is None or _model_key != name:
            from faster_whisper import WhisperModel
            log.info("carregando modelo whisper '%s' (cache: %s)", name, model_dir())
            _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=model_dir(),
                                  cpu_threads=max(1, (os.cpu_count() or 2)))
            _model_key = name
        return _model


def transcribe(audio_path: str | Path, model_name: str = "base", language: str = "pt") -> list[Word]:
    model = get_model(model_name)
    segments, _info = model.transcribe(
        str(audio_path), language=language, word_timestamps=True, vad_filter=True,
        beam_size=5, condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append(Word(float(w.start), float(w.end), t))
    return words


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 16, max_gap: float = 0.6) -> list[list[Word]]:
    """Agrupa palavras em blocos curtos (2-4 palavras) para aparecerem juntas na tela."""
    chunks: list[list[Word]] = []
    atual: list[Word] = []
    for w in words:
        if atual:
            chars = sum(len(x.text) + 1 for x in atual) + len(w.text)
            gap = w.start - atual[-1].end
            fim_frase = atual[-1].text[-1:] in ".!?,;:"
            if len(atual) >= max_words or chars > max_chars or gap > max_gap or fim_frase:
                chunks.append(atual)
                atual = []
        atual.append(w)
    if atual:
        chunks.append(atual)
    # junta blocos de 1 palavra curtinha com o anterior quando estão colados
    resultado: list[list[Word]] = []
    for c in chunks:
        if (resultado and len(c) == 1 and len(resultado[-1]) < 4 and len(c[0].text) <= 4
                and c[0].start - resultado[-1][-1].end < 0.25):
            resultado[-1].extend(c)
        else:
            resultado.append(c)
    return resultado


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _clean(text: str) -> str:
    text = re.sub(r"[{}\\]", "", text)  # caracteres especiais do ASS
    text = text.rstrip(".,;:")              # estilo TikTok: sem ponto/vírgula no fim
    return text.upper()


def build_ass(words: list[Word], clip_duration: float, width: int = 1080, height: int = 1920,
              font_size: int = 124, margin_v: int = 500) -> str:
    """Monta o arquivo .ass. A palavra falada no momento fica amarela; o bloco entra com um 'pop'."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,{FONT_NAME},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,8,4,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    linhas = []
    chunks = chunk_words(words)
    for ci, chunk in enumerate(chunks):
        fim_chunk = chunk[-1].end + 0.25
        if ci + 1 < len(chunks):
            fim_chunk = min(fim_chunk, chunks[ci + 1][0].start)
        fim_chunk = min(fim_chunk, clip_duration)
        for wi, w in enumerate(chunk):
            ini = w.start
            fim = chunk[wi + 1].start if wi + 1 < len(chunk) else fim_chunk
            if fim - ini < 0.05:
                continue
            partes = []
            for k, x in enumerate(chunk):
                t = _clean(x.text)
                partes.append(r"{\c&H00FFFF&}" + t + r"{\c&HFFFFFF&}" if k == wi else t)
            efeito = r"{\fscx112\fscy112\t(0,90,\fscx100\fscy100)}" if wi == 0 else ""
            linhas.append(f"Dialogue: 0,{_ts(ini)},{_ts(fim)},TikTok,,0,0,0,,{efeito}{' '.join(partes)}")
    return header + "\n".join(linhas) + "\n"


def transcript_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words).strip()
