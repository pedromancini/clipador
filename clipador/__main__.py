"""Linha de comando do clipador.

Exemplos:
    # cola o link da live: monitora até a live acabar / tempo máximo, clipando os picos do chat
    python -m clipador https://kick.com/gaules
    python -m clipador https://www.twitch.tv/gaules

    # o mesmo, informando plataforma e canal separados
    python -m clipador --platform twitch --channel gaules

    # teste local: grava, gera 1 clip forçado, envia no Telegram e sai
    python -m clipador --platform kick --channel gaules --once

    # só checa se está ao vivo (usado pelo GitHub Actions; escreve live=true/false no GITHUB_OUTPUT)
    python -m clipador --platform twitch --channel gaules --check-live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .config import ROOT, ConfigError, load_config, parse_channel_ref


def _gh_output(**kv) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipador", description="Clips automáticos de lives da Twitch/Kick")
    ap.add_argument("url", nargs="?", help="link da live (ex.: https://kick.com/gaules) ou plataforma/canal")
    ap.add_argument("--platform", choices=["twitch", "kick"])
    ap.add_argument("--channel", help="slug do canal (ex.: gaules)")
    ap.add_argument("--config", default=None, help="caminho do channels.json")
    ap.add_argument("--once", action="store_true", help="grava, gera 1 clip forçado, envia e sai")
    ap.add_argument("--check-live", action="store_true", help="só verifica se está ao vivo")
    ap.add_argument("--max-minutes", type=float, default=None, help="tempo máximo de monitoramento")
    ap.add_argument("--post-youtube", action="store_true",
                    help="com --once: posta o clip de teste no YouTube também (se o YouTube estiver ligado)")
    ap.add_argument("--layout", choices=["blur", "facecam", "crop"], default=None, help="sobrescreve o layout")
    ap.add_argument("--out", default=str(ROOT / "clips"), help="pasta dos clips gerados")
    ap.add_argument("--work", default=str(ROOT / "work"), help="pasta temporária (segmentos)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for ruidoso in ("httpx", "httpx2", "urllib3", "websockets", "streamlink", "faster_whisper", "huggingface_hub"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    try:
        if args.url:
            platform, name = parse_channel_ref(args.url)
        elif args.platform and args.channel:
            platform, name = args.platform, args.channel
        else:
            ap.error("informe o link da live (ex.: python -m clipador https://kick.com/gaules) "
                     "ou --platform e --channel")
        cfg = load_config(args.config)
        channel = cfg.channel_or_default(platform, name)
    except ConfigError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 2
    if args.layout:
        channel.layout = args.layout
        if args.layout == "facecam" and channel.facecam is None:
            from .config import Facecam
            channel.facecam = Facecam()

    if args.check_live:
        from .stream import get_stream_info
        try:
            info = get_stream_info(channel, cfg.settings["max_quality"])
        except Exception as e:  # noqa: BLE001
            logging.error("falha ao checar %s: %s", channel.slug, e)
            _gh_output(live="false")
            return 0
        print(json.dumps({"live": info.live, "title": info.title, "category": info.category,
                          "quality": info.quality}, ensure_ascii=False))
        _gh_output(live="true" if info.live else "false")
        return 0

    from .runner import run_channel
    from .telegram import Telegram

    tg = Telegram.from_env()
    try:
        res = asyncio.run(run_channel(cfg, channel, tg, Path(args.out), Path(args.work),
                                      once=args.once, max_minutes=args.max_minutes,
                                      post_forced=args.post_youtube))
    except KeyboardInterrupt:
        logging.info("interrompido pelo usuário")
        return 130
    except Exception as e:  # noqa: BLE001
        logging.exception("erro fatal")
        tg.send_error(f"{channel.slug}: erro fatal: {type(e).__name__}: {e}", min_interval=0)
        return 1
    _gh_output(still_live="true" if res.still_live else "false", clips=str(res.clips))
    logging.info("fim: live=%s clips=%d motivo=%s", res.live, res.clips, res.reason)
    if args.once and res.live and res.clips == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
