"""Resumo dos clips na página do job do GitHub Actions ($GITHUB_STEP_SUMMARY).

Funciona sem Telegram: em Actions → (execução) → resumo aparecem os clips com o link do Short
(se postado) e o título + hashtags prontos pra colar no TikTok. O vídeo fica no artifact do job.
Fora do Actions (sem GITHUB_STEP_SUMMARY), grava em clips/RESUMO.md.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from .detector import Peak

_lock = threading.Lock()


def _destino(out_dir: Path) -> Path:
    env = os.environ.get("GITHUB_STEP_SUMMARY")
    return Path(env) if env else Path(out_dir) / "RESUMO.md"


def _append(out_dir: Path, texto: str) -> None:
    with _lock:
        p = _destino(out_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(texto)


def write_header(out_dir: Path, channel: str, platform_label: str, url: str, title: str | None,
                 category: str | None) -> None:
    _append(out_dir, f"## ✂️ {channel} ({platform_label})\n\n"
                     f"{title or ''}{' — ' + category if category else ''} · {url}\n\n"
                     f"Os vídeos ficam no **artifact** desta execução (role até o fim da página → Artifacts).\n\n")


def write_clip(out_dir: Path, filename: str, peak: Peak, timezone: str, youtube_line: str | None,
               tiktok_text: str, transcript: str) -> None:
    try:
        from zoneinfo import ZoneInfo
        quando = datetime.fromtimestamp(peak.t, ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        quando = datetime.fromtimestamp(peak.t)
    if peak.forced:
        hype = "clip de teste (--once)"
    elif peak.baseline > 0:
        hype = f"hype {peak.score:g} ({peak.ratio:.1f}x o normal)"
    else:
        hype = f"hype {peak.score:g}"
    chat = ", ".join(f"{m} ×{q}" for m, q in peak.top_messages[:4]) or "-"
    linhas = [
        f"### 🎬 `{filename}`",
        f"- {quando:%d/%m %H:%M} · {hype}",
        f"- {youtube_line or 'YouTube: desligado'}",
        f"- 💬 Chat: {chat}",
    ]
    if transcript:
        linhas.append(f"- 🗣️ Fala: _{transcript[:200]}_")
    linhas += ["", "Pra colar no TikTok:", "```", tiktok_text, "```", "", ""]
    _append(out_dir, "\n".join(linhas))
