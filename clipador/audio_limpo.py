"""Tira a música do áudio do clip e deixa a voz do streamer (Demucs, modelo htdemucs).

Por quê: a rádio/trilha do GTA 6 e a música/narração de transmissões de futebol são o que mais gera
reivindicação de direitos autorais (o vídeo fica limitado ou mudo). Separando as fontes, o clip fica com:
- a voz (stem "vocals") em volume cheio;
- um fundo bem baixo do resto (jogo, bateria, baixo...) — `resto` (0 = só a voz), pra não ficar seco.

Precisa de `torch` + `demucs` (no GitHub Actions são instalados só nos workflows que usam):
    pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
    pip install demucs==4.0.1
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .recorder import ffmpeg_bin

log = logging.getLogger("clipador.audio_limpo")

TAXA = 44100
_modelo = None


def disponivel() -> bool:
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _carregar_modelo():
    global _modelo
    if _modelo is None:
        import torch
        from demucs.pretrained import get_model
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        _modelo = get_model("htdemucs")
        _modelo.eval()
    return _modelo


def separar_voz(audio, resto: float = 0.1):
    """audio: numpy float32 (2, n) a 44,1 kHz -> mesmo formato, só voz + `resto` do restante."""
    import numpy as np
    import torch
    from demucs.apply import apply_model
    modelo = _carregar_modelo()
    wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
    ref = wav.mean(0)
    media, desvio = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        fontes = apply_model(modelo, ((wav - media) / desvio)[None], device="cpu", split=True,
                             overlap=0.25, progress=False)[0]
    fontes = fontes * desvio + media
    nomes = list(modelo.sources)          # ['drums', 'bass', 'other', 'vocals']
    voz = fontes[nomes.index("vocals")]
    outros = sum(fontes[i] for i, n in enumerate(nomes) if n != "vocals")
    return (voz + resto * outros).numpy()


def tirar_musica(video: Path, workdir: Path, resto: float = 0.1) -> Path:
    """Troca o áudio do .mp4 pela versão sem música (o vídeo não é recodificado)."""
    import numpy as np
    inicio = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    bruto = subprocess.run([ffmpeg_bin(), "-v", "error", "-nostdin", "-i", str(video), "-vn", "-f", "f32le",
                            "-ac", "2", "-ar", str(TAXA), "-"], capture_output=True, check=True, timeout=300).stdout
    audio = np.frombuffer(bruto, dtype=np.float32).reshape(-1, 2).T
    if audio.shape[1] < TAXA:   # menos de 1s de áudio: nada a fazer
        return video
    limpo = separar_voz(audio, resto)
    pcm = workdir / "voz.f32"
    pcm.write_bytes(np.ascontiguousarray(limpo.T, dtype=np.float32).tobytes())
    tmp = workdir / "sem_musica.mp4"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-nostdin", "-y", "-i", str(video),
                    "-f", "f32le", "-ar", str(TAXA), "-ac", "2", "-i", str(pcm),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-shortest", "-movflags", "+faststart", str(tmp)],
                   check=True, timeout=300, capture_output=True)
    shutil.move(str(tmp), video)
    pcm.unlink(missing_ok=True)
    log.info("música removida (voz + %.0f%% do resto) em %.0fs", resto * 100, time.monotonic() - inicio)
    return video


_avisou = False


def aplicar(ch, video: Path, workdir: Path) -> None:
    """Se o canal pede ("tirar_musica"), tira a música; se falhar ou não tiver o Demucs, segue com o original."""
    global _avisou
    if not getattr(ch, "tirar_musica", False):
        return
    if not disponivel():
        if not _avisou:
            log.warning("tirar_musica ligado, mas torch/demucs não estão instalados: áudio original")
            _avisou = True
        return
    try:
        tirar_musica(Path(video), Path(workdir), resto=float(getattr(ch, "musica_resto", 0.1)))
    except Exception as e:  # noqa: BLE001
        log.warning("não consegui tirar a música (%s); áudio original", str(e)[:200])
