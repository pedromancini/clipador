"""Montagem do clip vertical 1080x1920.

Passos:
1. Junta (concatena em binário) os segmentos .ts que cobrem [t0, t0+duração].
2. Extrai o áudio do trecho e transcreve com o Whisper (se falhar, segue sem legenda).
3. Aplica o layout (blur / facecam / crop) + legenda .ass e codifica em H.264/AAC.
4. Garante que o arquivo fique abaixo de 50 MB (limite de bot do Telegram); se passar, recodifica
   com bitrate calculado.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import subtitles as subs
from .config import Facecam
from .recorder import ffmpeg_bin

log = logging.getLogger("clipador.clipper")

W, H = 1080, 1920
TELEGRAM_LIMIT = 50 * 1024 * 1024
SIZE_TARGET = 48 * 1024 * 1024   # margem de segurança


@dataclass
class ClipResult:
    path: Path
    duration: float
    size: int
    subtitled: bool
    transcript: str = ""
    words: list = field(default_factory=list)
    encode_seconds: float = 0.0


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"comando falhou ({p.returncode}): {' '.join(cmd[:6])}...\n{p.stderr[-1500:]}")
    return p


def probe(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    p = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(p.stdout)


def media_duration(path: Path) -> float:
    try:
        return float(probe(path)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def layout_filter(layout: str, fps: int, facecam: Facecam | None = None) -> str:
    """Filtergraph do ffmpeg que transforma qualquer vídeo em 1080x1920. Saída: [v]."""
    if layout == "facecam":
        fc = facecam or Facecam()
        cam_h, game_h = 640, H - 640
        return (
            f"[0:v]fps={fps},split=2[c][g];"
            f"[c]crop=iw*{fc.w:.4f}:ih*{fc.h:.4f}:iw*{fc.x:.4f}:ih*{fc.y:.4f},"
            f"scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h},setsar=1[cam];"
            f"[g]scale={W}:{game_h}:force_original_aspect_ratio=increase,crop={W}:{game_h},setsar=1[game];"
            f"[cam][game]vstack=inputs=2,format=yuv420p[v]"
        )
    if layout == "crop":
        return (f"[0:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,format=yuv420p[v]")
    # padrão: blur (fundo desfocado + jogo inteiro no centro)
    return (
        f"[0:v]fps={fps},split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
        f"scale={W}:{H},eq=brightness=-0.06:saturation=1.2[bg];"
        f"[fgsrc]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )


def concat_segments(paths: list[Path], out: Path) -> None:
    """MPEG-TS pode ser concatenado byte a byte (os segmentos vêm do mesmo ffmpeg)."""
    with open(out, "wb") as dst:
        for p in paths:
            with open(p, "rb") as src:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def _encode(src: Path, out: Path, workdir: Path, offset: float, duration: float, vf: str,
            fps: int, video_args: list[str]) -> None:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{offset:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-filter_complex", vf, "-map", "[vout]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", *video_args,
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-af", "aresample=async=1:first_pts=0",
        "-movflags", "+faststart", str(out),
    ]
    _run(cmd, cwd=workdir)


def build_clip(
    segments: list[tuple[float, Path]],
    t0: float,
    duration: float,
    out_path: Path,
    workdir: Path,
    layout: str = "blur",
    facecam: Facecam | None = None,
    fps: int = 30,
    with_subtitles: bool = True,
    whisper_model: str = "base",
    language: str = "pt",
    facecam_auto: bool = False,
    sub_size: int = 80,
    sub_position: str = "middle",
) -> ClipResult:
    """Gera o clip final. `segments` = lista (epoch_inicio, caminho) ordenada."""
    if not segments:
        raise ValueError("nenhum segmento disponível para o clip")
    inicio = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    src = workdir / "source.ts"
    concat_segments([p for _, p in segments], src)
    total = media_duration(src)
    offset = max(0.0, t0 - segments[0][0])
    if total > 0:
        offset = min(offset, max(0.0, total - 3))
        duration = min(duration, total - offset)
    if duration < 3:
        raise ValueError(f"trecho gravado curto demais ({duration:.1f}s)")

    # ---- facecam automática ----
    if layout == "facecam" and facecam_auto:
        from .facecam import detect_facecam
        achada = detect_facecam(src, offset, duration)
        if achada:
            facecam = achada
        else:
            layout = "blur"  # sem rosto estável (ex.: tela de loading, streamer saiu): blur não erra

    # ---- legenda ----
    words: list[subs.Word] = []
    ass_ok = False
    if with_subtitles:
        try:
            wav = workdir / "audio.wav"
            _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-ss", f"{offset:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
                  "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], cwd=workdir)
            words = subs.transcribe(wav, whisper_model, language)
            if words:
                if sub_position == "middle":
                    centro = 590 if layout == "facecam" else H // 2  # facecam: base da cam (não cobre o HUD do jogo)
                else:
                    centro = None
                ass = subs.build_ass(words, duration, font_size=sub_size, center_y=centro)
                (workdir / "subs.ass").write_text(ass, encoding="utf-8")
                fonts = workdir / "fonts"
                fonts.mkdir(exist_ok=True)
                for f in subs.FONTS_DIR.glob("*.ttf"):
                    shutil.copy2(f, fonts / f.name)
                ass_ok = True
            else:
                log.info("whisper não encontrou fala no trecho; clip sem legenda")
        except Exception as e:  # noqa: BLE001
            log.warning("legenda falhou (%s); gerando o clip sem legenda", e)

    base = layout_filter(layout, fps, facecam)
    if ass_ok:
        # caminhos relativos (cwd=workdir) evitam o problema de escapar "C:\" no filtro
        vf = base + ";[v]subtitles=subs.ass:fontsdir=fonts[vout]"
    else:
        vf = base + ";[v]null[vout]"

    tmp = workdir / "clip.mp4"
    args = ["-crf", "23", "-maxrate", "6M", "-bufsize", "12M"]
    try:
        _encode(src, tmp, workdir, offset, duration, vf, fps, args)
    except RuntimeError as e:
        if not ass_ok:
            raise
        log.warning("encode com legenda falhou (%s); tentando sem legenda", str(e)[-300:])
        ass_ok = False
        vf = base + ";[v]null[vout]"
        _encode(src, tmp, workdir, offset, duration, vf, fps, args)

    size = tmp.stat().st_size
    if size > SIZE_TARGET:
        # bitrate que cabe: (48MB * 8 / duração) - áudio
        kbps = int(SIZE_TARGET * 8 / duration / 1000) - 160
        log.info("clip com %.1f MB (> 48MB); recodificando a %d kbps", size / 1e6, kbps)
        _encode(src, tmp, workdir, offset, duration, vf, fps,
                ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"])
        size = tmp.stat().st_size

    shutil.move(str(tmp), out_path)
    real = media_duration(out_path) or duration
    res = ClipResult(path=out_path, duration=real, size=size, subtitled=ass_ok,
                     transcript=subs.transcript_text(words), words=words,
                     encode_seconds=time.monotonic() - inicio)
    log.info("clip pronto: %s (%.1fs, %.1f MB, legenda=%s, %.0fs de processamento)",
             out_path.name, res.duration, size / 1e6, ass_ok, res.encode_seconds)
    return res
