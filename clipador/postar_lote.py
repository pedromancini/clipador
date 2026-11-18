"""Posta no YouTube uma lista de clips JÁ PRONTOS (ex.: os de uma live anterior), um de cada vez.

Usado pelo workflow "postar_clips.yml". Os posts levam a marca "clipador:extra/<canal>", então NÃO
entram na conta dos 2 por hora da live: se o canal estiver ao vivo, os posts automáticos continuam.

Lista (JSON):  [{"arquivo": "twitch-alanzoka_20261116-211240.mp4", "titulo": "...", "legenda": "...",
                 "hashtags": ["#alanzoka", "#gta6"]}, ...]

Rodar:  python -m clipador.postar_lote --dir clips --lista lista.json --intervalo 20
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

from .config import load_config
from .youtube import YouTube, YouTubeLimitError, build_metadata, marker

log = logging.getLogger("clipador.postar_lote")

_NOME_RE = re.compile(r"^(twitch|kick)-([A-Za-z0-9_\-]+?)_\d{8}-\d{6}\.mp4$")


def metadados(item: dict, settings: dict) -> tuple[dict, str, str]:
    """Metadados do YouTube para um item da lista. Retorna (metadata, plataforma, canal)."""
    m = _NOME_RE.match(item["arquivo"])
    if not m:
        raise ValueError(f"nome de arquivo inesperado: {item['arquivo']}")
    platform, name = m.group(1), m.group(2).lower()
    ch = load_config().channel_or_default(platform, name)
    ys = settings["youtube"]
    tags = item.get("hashtags") or [f"#{name}"] + [t for t in (ch.hashtags or settings.get("hashtags", []))
                                                  if "gta" not in t]
    legenda = (item.get("legenda") or "").strip()
    meta = build_metadata(ch.titulo_com_tags(item["titulo"]), ch.name, ch.platform_label, ch.url, tags, platform,
                          ys["category_id"], item.get("privacy") or ys["privacy"], ys["made_for_kids"],
                          hook=legenda if legenda and legenda != item["titulo"] else None)
    desc = meta["snippet"]["description"]
    meta["snippet"]["description"] = desc.replace(marker(platform, ch.name), marker("extra", ch.name))
    return meta, platform, name


def lista_auto(pasta: Path, maximo: int = 0) -> list[dict]:
    """Clips da pasta que ainda não foram pro YouTube (pelo .json de cada um), do mais viral pro menos."""
    itens = []
    for j in sorted(pasta.glob("*.json")):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("youtube") or not d.get("titulo") or not (pasta / j.with_suffix(".mp4").name).exists():
            continue
        itens.append({"arquivo": j.with_suffix(".mp4").name, "titulo": d["titulo"], "legenda": d.get("legenda", ""),
                      "hashtags": d.get("hashtags") or [], "_viral": d.get("viral") or 0, "_hype": d.get("hype") or 0})
    itens.sort(key=lambda x: (-x["_viral"], -x["_hype"]))
    for it in itens:
        it.pop("_viral"), it.pop("_hype")
    return itens[:maximo] if maximo else itens


def conta_do(item: dict) -> str:
    """Conta do YouTube do canal do clip ("" = canal principal)."""
    m = _NOME_RE.match(item["arquivo"])
    return load_config().channel_or_default(m.group(1), m.group(2).lower()).youtube_conta if m else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="clips", help="pasta com os .mp4")
    ap.add_argument("--lista", required=True, help="arquivo JSON com a lista (ou o próprio JSON)")
    ap.add_argument("--intervalo", type=float, default=20, help="minutos entre um post e outro")
    ap.add_argument("--esperar-limite", action="store_true",
                    help="se o canal estiver no limite de envios, espera e tenta de novo a cada 30 min")
    ap.add_argument("--prazo", type=float, default=330, help="minutos máximos desta execução")
    ap.add_argument("--restantes", default="restantes.json", help="onde salvar o que não deu tempo de postar")
    ap.add_argument("--max", type=int, default=0, help="lista \"auto\": quantos postar (0 = todos)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    bruto = args.lista
    if bruto.strip().lower() != "auto" and not bruto.lstrip().startswith("["):
        bruto = Path(bruto).read_text(encoding="utf-8")   # caminho de arquivo
    if bruto.strip().lower() == "auto":
        lista = lista_auto(Path(args.dir), args.max)
        print(f"auto: {len(lista)} clip(s) ainda não postados no YouTube", flush=True)
    else:
        lista = json.loads(bruto)
    settings = load_config().settings
    contas: dict[str, YouTube] = {}

    pasta = Path(args.dir)
    falhas = 0
    fim = time.monotonic() + args.prazo * 60
    for i, item in enumerate(lista):
        video = pasta / item["arquivo"]
        if not video.exists():
            log.error("não achei %s, pulando", video)
            falhas += 1
            continue
        conta = conta_do(item)
        if conta not in contas:
            contas[conta] = YouTube.from_env(settings.get("timezone", "America/Sao_Paulo"), conta)
        yt = contas[conta]
        if not yt.configured:
            log.error("%s: faltam os secrets do YouTube da conta %s", item["arquivo"], yt.conta)
            falhas += 1
            continue
        meta, platform, name = metadados(item, settings)
        while True:
            try:
                url = yt.upload(video, meta, platform, name)
                print(f"▶️ {i + 1}/{len(lista)} {meta['snippet']['title']}  {url}", flush=True)
                break
            except YouTubeLimitError as e:
                if args.esperar_limite and time.monotonic() + 1800 < fim:
                    log.warning("%s; tento de novo em 30 min", e)
                    time.sleep(1800)
                    continue
                log.error("falhou %s: %s", item["arquivo"], e)
                if args.esperar_limite:  # acabou o tempo desta execução: guarda o resto pra próxima
                    Path(args.restantes).write_text(json.dumps(lista[i:], ensure_ascii=False), encoding="utf-8")
                    print(f"⏸️ canal no limite; {len(lista) - i} clip(s) ficam pra próxima execução", flush=True)
                    return 0
                return 1
            except Exception as e:  # noqa: BLE001
                log.error("falhou %s: %s", item["arquivo"], e)
                falhas += 1
                url = None
                if "cota" in str(e):
                    return 1
                break
        if url is None:
            continue
        if i < len(lista) - 1 and args.intervalo > 0:
            log.info("próximo em %.0f min", args.intervalo)
            time.sleep(args.intervalo * 60)
    return 1 if falhas == len(lista) else 0


if __name__ == "__main__":
    sys.exit(main())
