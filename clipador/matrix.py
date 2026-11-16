"""Gera a matrix do GitHub Actions com os canais ativos do channels.json.

    python -m clipador.matrix                 # todos os canais com "enabled": true
    python -m clipador.matrix --only https://kick.com/xyz   # só uma live (link ou kick/xyz)

Escreve em $GITHUB_OUTPUT:
    matrix={"include":[{"platform":"twitch","name":"x"}, ...]}
    has_channels=true|false
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import ConfigError, load_config, parse_channel_ref


def build_matrix(config_path: str | None = None, only: str | None = None) -> dict:
    cfg = load_config(config_path)
    canais = cfg.enabled_channels
    if only:
        # link da live ou plataforma/canal; não precisa estar no channels.json (usa as configurações padrão)
        plat, nome = parse_channel_ref(only)
        canais = [cfg.channel_or_default(plat, nome)]
    return {"include": [{"platform": c.platform, "name": c.name} for c in canais]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    try:
        m = build_matrix(args.config, args.only or None)
    except ConfigError as e:
        print(f"::error::{e}")
        return 2
    has = "true" if m["include"] else "false"
    print(json.dumps(m, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"matrix={json.dumps(m)}\n")
            f.write(f"has_channels={has}\n")
    if has == "false":
        print("Nenhum canal ativo no channels.json (\"enabled\": true). Nada a fazer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
