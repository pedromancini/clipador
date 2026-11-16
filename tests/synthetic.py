"""Teste ponta a ponta SEM live: vídeo sintético + chat falso -> detecção -> corte -> legenda.

Rodar:
    python -m tests.synthetic                  # os 3 layouts
    python -m tests.synthetic --layouts blur   # só um
    python -m tests.synthetic --send           # também envia o clip no Telegram (precisa dos secrets)

O que ele faz:
1. Gera 100s de vídeo 1920x1080 (testsrc2 + um "facecam" colorido no canto) com uma fala em
   português aos 55s (voz do Windows ou espeak-ng no Linux).
2. Corta em segmentos de 5s com o horário no nome, igual ao gravador faz com a live.
3. Gera um chat falso: ~2 msg/s normais e uma explosão de KKKK/CLIP logo depois da fala.
4. Roda o detector, monta o clip em cada layout e confere: 1080x1920, duração, < 50MB, legenda.
5. Salva um frame PNG de cada clip para conferir visualmente.
"""
from __future__ import annotations

import argparse
import logging
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from clipador.clipper import build_clip, probe
from clipador.config import DEFAULT_SETTINGS, Facecam
from clipador.detector import HypeDetector
from clipador.recorder import ffmpeg_bin, list_segments, select_segments

FALA = ("Meu Deus do céu, que jogada inacreditável! Ele passou por todo mundo e fez o gol no último "
        "segundo. Isso aqui vai virar clip com certeza, pode clipar!")
FALA_EM = 55.0       # segundo do vídeo em que a fala começa
DURACAO = 100
FACECAM = Facecam(x=0.0, y=0.0, w=0.25, h=0.30)


def gerar_fala(wav: Path) -> bool:
    """Gera a fala sintética em pt-BR. Retorna False se nenhum TTS estiver disponível."""
    if sys.platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$v=$s.GetInstalledVoices()|?{$_.VoiceInfo.Culture.Name -like 'pt*'}|select -First 1;"
            "if($v){$s.SelectVoice($v.VoiceInfo.Name)};"
            f"$s.SetOutputToWaveFile('{wav}');$s.Speak('{FALA}');$s.Dispose()"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        return r.returncode == 0 and wav.exists() and wav.stat().st_size > 10000
    for tts in ("espeak-ng", "espeak"):
        if shutil.which(tts):
            r = subprocess.run([tts, "-v", "pt-br", "-s", "150", "-w", str(wav), FALA], capture_output=True)
            return r.returncode == 0 and wav.exists()
    return False


def gerar_video(pasta: Path, fala: Path | None) -> Path:
    src = pasta / "fonte.ts"
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={DURACAO}",
           "-f", "lavfi", "-i", f"sine=frequency=220:duration={DURACAO}:sample_rate=44100"]
    # "facecam" = caixa magenta no canto superior esquerdo (25% x 30% da tela)
    vf = "drawbox=x=0:y=0:w=iw*0.25:h=ih*0.30:color=magenta@1:t=fill,drawbox=x=40:y=40:w=200:h=200:color=yellow@1:t=fill"
    if fala:
        cmd += ["-i", str(fala)]
        af = (f"[1:a]volume=0.05[bg];[2:a]aresample=44100,adelay={int(FALA_EM * 1000)}:all=1,volume=1.6[f];"
              f"[bg][f]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        af = "[1:a]volume=0.1[a]"
    cmd += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac", "-f", "mpegts", str(src)]
    subprocess.run(cmd, check=True)
    return src


def segmentar(src: Path, seg_dir: Path, base_epoch: int) -> None:
    """Corta em segmentos de 5s e renomeia para seg_<epoch>.ts (como o gravador)."""
    seg_dir.mkdir(parents=True, exist_ok=True)
    lista = seg_dir / "lista.csv"
    subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0",
                    "-c", "copy", "-f", "segment", "-segment_time", "5", "-segment_list", str(lista),
                    "-segment_list_type", "csv", str(seg_dir / "tmp_%03d.ts")], check=True)
    for linha in lista.read_text().splitlines():
        nome, ini, _fim = linha.split(",")
        (seg_dir / nome).rename(seg_dir / f"seg_{base_epoch + int(round(float(ini)))}.ts")
    lista.unlink()


def chat_falso(base_epoch: int, seed: int = 7) -> list[tuple[float, str]]:
    rnd = random.Random(seed)
    normais = ["boa", "salve", "que isso", "eae chat", "tá lagado?", "primeira vez aqui", "bora"]
    hype = ["KKKKKKKKK", "kkkkkkk", "CLIP", "!clip", "CARALHO", "KEKW KEKW KEKW", "mds", "NOSSA"]
    msgs = []
    for s in range(DURACAO):
        reacao = FALA_EM + 3 <= s < FALA_EM + 11
        n = rnd.randint(10, 18) if reacao else rnd.randint(0, 4)
        for _ in range(n):
            msgs.append((base_epoch + s + rnd.random(), rnd.choice(hype if reacao else normais)))
    return sorted(msgs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layouts", default="blur,facecam,crop")
    ap.add_argument("--out", default=None, help="pasta de saída (padrão: temporária)")
    ap.add_argument("--send", action="store_true", help="envia o primeiro clip no Telegram")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    pasta = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="clipador_teste_"))
    pasta.mkdir(parents=True, exist_ok=True)
    print(f"pasta de teste: {pasta}")
    base = int(time.time()) - 1000

    fala = pasta / "fala.wav"
    tem_fala = gerar_fala(fala)
    print(f"fala sintética: {'ok' if tem_fala else 'INDISPONÍVEL (sem TTS) — a legenda não será validada'}")
    src = gerar_video(pasta, fala if tem_fala else None)
    segmentar(src, pasta / "segs", base)
    segs = list_segments(pasta / "segs")
    print(f"{len(segs)} segmentos gerados")

    # ---- detecção ----
    s = dict(DEFAULT_SETTINGS)
    s["warmup_seconds"] = 30
    det = HypeDetector.from_settings(s)
    det.start(base)
    msgs = chat_falso(base)
    picos, i = [], 0
    for seg in range(DURACAO + 1):
        agora = base + seg
        while i < len(msgs) and msgs[i][0] < agora:
            det.add(msgs[i][1], msgs[i][0])
            i += 1
        p = det.tick(agora)
        if p:
            picos.append(p)
    falhas = []
    if len(picos) != 1:
        falhas.append(f"esperava 1 pico, achei {len(picos)}")
    if not picos:
        print("FALHOU: nenhum pico detectado")
        return 1
    pico = picos[0]
    print(f"pico em t={pico.t - base:.0f}s (score {pico.score}, baseline {pico.baseline}, limiar {pico.threshold}), "
          f"top: {pico.top_messages[:3]}")
    if not FALA_EM + 2 <= pico.t - base <= FALA_EM + 12:
        falhas.append(f"pico fora do lugar esperado ({pico.t - base:.0f}s)")

    t0 = pico.t - s["clip_before_seconds"]
    dur = s["clip_before_seconds"] + s["clip_after_seconds"]
    escolhidos = select_segments(segs, t0, t0 + dur)

    resultados = []
    for layout in args.layouts.split(","):
        out = pasta / f"clip_{layout}.mp4"
        r = build_clip(escolhidos, t0, dur, out, pasta / f"work_{layout}", layout=layout,
                       facecam=FACECAM, fps=30, with_subtitles=tem_fala)
        info = probe(out)
        v = next(st for st in info["streams"] if st["codec_type"] == "video")
        tem_audio = any(st["codec_type"] == "audio" for st in info["streams"])
        frame = pasta / f"frame_{layout}.png"
        # frame no meio da fala (a fala começa em FALA_EM; o clip começa em t0)
        subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-ss",
                        f"{(base + FALA_EM + 2.5) - t0:.2f}", "-i", str(out), "-frames:v", "1", str(frame)], check=True)
        print(f"[{layout}] {v['width']}x{v['height']} {r.duration:.1f}s {r.size / 1e6:.1f}MB "
              f"áudio={tem_audio} legenda={r.subtitled} ({r.encode_seconds:.0f}s) frame={frame}")
        print(f"   transcrição: {r.transcript!r}")
        if (v["width"], v["height"]) != (1080, 1920):
            falhas.append(f"{layout}: resolução {v['width']}x{v['height']}")
        if abs(r.duration - dur) > 1.5:
            falhas.append(f"{layout}: duração {r.duration:.1f}s (esperado ~{dur}s)")
        if r.size >= 50 * 1024 * 1024:
            falhas.append(f"{layout}: arquivo grande demais")
        if not tem_audio:
            falhas.append(f"{layout}: sem áudio")
        if tem_fala and not r.subtitled:
            falhas.append(f"{layout}: legenda não foi gerada")
        if tem_fala and not any(p in r.transcript.lower() for p in ("jogada", "gol", "clip", "deus")):
            falhas.append(f"{layout}: transcrição não bate com a fala")
        resultados.append(r)

    if args.send and resultados:
        from clipador.telegram import Telegram, build_caption
        tg = Telegram.from_env()
        cap = build_caption("canal_teste", "Twitch", pico, resultados[0].transcript, "Teste sintético",
                            "Just Chatting", s["hashtags"], s["timezone"])
        tg.send_video(resultados[0].path, cap, duration=resultados[0].duration)
        print("enviado no Telegram")

    if not args.keep and not args.out:
        print(f"(arquivos mantidos em {pasta} para conferência; apague quando quiser)")
    if falhas:
        print("FALHOU:\n - " + "\n - ".join(falhas))
        return 1
    print("OK: detecção, corte e legenda funcionando")
    return 0


if __name__ == "__main__":
    sys.exit(main())
