"""Corta um trecho de um VOD (Twitch) no formato do clipador e, se pedir, posta no YouTube.

Serve pra momentos que o monitoramento ao vivo não pegou (ex.: a live começou antes do job).
Usado pelo workflow "cortar_vod.yml".

    python -m clipador.cortar_vod https://www.twitch.tv/videos/123 --inicio 43:20 --duracao 55 \
        --canal twitch/alanzoka [--titulo "..."] [--legenda "..."] [--postar]
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .clipper import build_clip
from .config import ROOT, load_config, parse_channel_ref
from .copywriter import ClipCopy, Copywriter
from .youtube import YouTube, build_metadata

log = logging.getLogger("clipador.cortar_vod")


def segundos(txt: str) -> int:
    """'43:20' / '1:02:03' / '2600' -> segundos."""
    partes = [int(p) for p in str(txt).strip().split(":")]
    s = 0
    for p in partes:
        s = s * 60 + p
    return s


def baixar(vod: str, inicio: int, duracao: int, destino: Path) -> None:
    ini = f"{inicio // 3600:02d}:{inicio % 3600 // 60:02d}:{inicio % 60:02d}"
    cmd = [sys.executable, "-m", "streamlink", "--hls-start-offset", ini, "--hls-duration", str(duracao),
           "-o", str(destino), "--force", vod, "1080p60,1080p,936p60,720p60,720p,best"]
    log.info("baixando %s a partir de %s (%ss)", vod, ini, duracao)
    subprocess.run(cmd, check=True, timeout=1800)
    if not destino.exists() or destino.stat().st_size < 100_000:
        raise RuntimeError("download do VOD falhou")


def vod_inicio_epoch(vod: str) -> tuple | None:
    """(início do VOD em epoch, título da live, jogo): dá nome ao arquivo com a hora real do momento."""
    import requests
    vid = vod.rstrip("/").split("/")[-1].split("?")[0]
    try:
        r = requests.post("https://gql.twitch.tv/gql", headers={"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko"},
                          json={"query": f'query{{video(id:"{vid}"){{createdAt title game{{name}}}}}}'}, timeout=15)
        v = r.json()["data"]["video"]
        return datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00")).timestamp(), v.get("title"), \
            (v.get("game") or {}).get("name")
    except Exception as e:  # noqa: BLE001
        log.warning("não consegui ler os dados do VOD: %s", e)
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vod", help="link do VOD (https://www.twitch.tv/videos/...)")
    ap.add_argument("--inicio", required=True, help="onde começa no VOD (mm:ss ou hh:mm:ss)")
    ap.add_argument("--duracao", type=int, default=55, help="segundos (máx. 60)")
    ap.add_argument("--canal", default="twitch/alanzoka", help="plataforma/canal (define o canal do YouTube)")
    ap.add_argument("--titulo", default="", help="vazio = a IA escreve")
    ap.add_argument("--legenda", default="")
    ap.add_argument("--postar", action="store_true", help="posta no YouTube")
    ap.add_argument("--privacy", default=None, choices=["public", "unlisted", "private"])
    ap.add_argument("--out", default=str(ROOT / "clips"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config()
    s = cfg.settings
    platform, name = parse_channel_ref(args.canal)
    ch = cfg.channel_or_default(platform, name)
    inicio, dur = segundos(args.inicio), max(5, min(60, args.duracao))

    work = ROOT / "work" / f"vod_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "vod.ts"
    baixar(args.vod, inicio, dur + 2, src)

    info = vod_inicio_epoch(args.vod)
    base_epoch, titulo_live, categoria = info if info else (time.time(), None, None)
    quando = datetime.fromtimestamp(base_epoch + inicio, timezone.utc)
    out = Path(args.out) / f"{ch.slug}_{quando:%Y%m%d-%H%M%S}.mp4"

    res = build_clip([(0.0, src)], 0.0, dur, out, work / "clip", layout=ch.layout, facecam=ch.facecam,
                     fps=int(s["fps"]), with_subtitles=True, whisper_model=s["whisper_model"],
                     language=s["language"], facecam_auto=ch.facecam_auto,
                     sub_size=int(s["subtitle_size"]), sub_position=s["subtitle_position"],
                     watermark=ch.watermark)
    log.info("clip pronto: %s (%.0fs)", out, res.duration)

    tags = ch.hashtags or s["hashtags"]
    if args.titulo:
        texto = ClipCopy(titulo=args.titulo, legenda=args.legenda or args.titulo,
                         hashtags=[f"#{ch.name}"] + [t for t in tags if t != f"#{ch.name}"][:6],
                         sobre_gta6="gta" in (args.titulo + (categoria or "")).lower(), fonte="manual")
    else:
        wav = work / "clip" / "audio.wav"
        texto = Copywriter().make(ch.name, res.words, res.transcript, wav if wav.exists() else None,
                                  dur / 2, [], categoria, titulo_live, tags)
    texto.titulo = ch.titulo_com_tags(texto.titulo)
    print(f"TÍTULO: {texto.titulo}\nLEGENDA: {texto.legenda}\nHASHTAGS: {' '.join(texto.hashtags)}", flush=True)

    url = None
    if args.postar:
        ys = s["youtube"]
        yt = YouTube.from_env(s["timezone"], ch.youtube_conta)
        if not yt.configured:
            log.error("faltam os secrets do YouTube da conta %s", yt.conta)
        else:
            meta = build_metadata(texto.titulo, ch.name, ch.platform_label, ch.url, texto.hashtags, ch.platform,
                                  ys["category_id"], args.privacy or ys["privacy"], ys["made_for_kids"],
                                  hook=texto.legenda if texto.legenda != texto.titulo else None)
            # marca "extra": não conta no limite de 2/h do monitoramento ao vivo
            meta["snippet"]["description"] = meta["snippet"]["description"].replace(
                f"clipador:{ch.platform}/{ch.name}", f"clipador:extra/{ch.name}")
            url = yt.upload(out, meta, ch.platform, ch.name)
            print(f"▶️ POSTADO: {url}", flush=True)

    dados = {"arquivo": out.name, "canal": ch.name, "plataforma": ch.platform_label, "url_canal": ch.url,
             "pico_epoch": base_epoch + inicio + dur / 2, "hype": None, "titulo": texto.titulo,
             "legenda": texto.legenda, "hashtags": texto.hashtags, "viral": texto.viral or 9,
             "sobre_gta6": texto.sobre_gta6, "texto_por": texto.fonte, "categoria": categoria,
             "titulo_live": titulo_live, "fala": res.transcript[:500], "duracao": round(res.duration, 1),
             "youtube": url, "vod": f"{args.vod} @ {args.inicio}"}
    out.with_suffix(".json").write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
