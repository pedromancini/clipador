"""Análise de VOD: depois da live, acha os melhores momentos e gera os clips.

Em vez de clipar ao vivo, olha a live INTEIRA e escolhe o que bombou:

1. Pega a VOD (Twitch: a última transmissão gravada; Kick: o último vídeo do canal).
2. Sinais de hype ao longo da VOD:
   - replay do chat: densidade de mensagens e risadas (KKKK, LUL...) a cada 15s;
   - clips que o próprio público criou durante a live (posição na VOD + views).
3. Junta tudo num ranking de candidatos, baixa SÓ esses trechos (não a VOD inteira).
4. Cada candidato vira clip vertical; a IA ouve o áudio, vê 2 quadros (descarta o que não é gameplay
   nos canais "so_gameplay") e dá a nota. Ela também recebe os assuntos de GTA 6 em alta no YouTube.
5. Ranking final = nota da IA + força dos sinais. Os melhores ficam em clips/ (com o .json de cada um,
   igual aos clips ao vivo: o baixador do TikTok e o postar_lote usam os mesmos arquivos).

    python -m clipador.vod_analise --canal twitch/alanzoka [--vod 2932200023] [--max 15] [--candidatos 30]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clipper import build_clip
from .config import ROOT, load_config, parse_channel_ref
from .copywriter import Copywriter
from .cortar_vod import baixar

log = logging.getLogger("clipador.vod_analise")

GQL = "https://gql.twitch.tv/gql"
TWITCH_ID = {"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko"}
HASH_CHAT = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
RISADA = re.compile(r"k{4,}|kekw|omegalul|\blul\b|lmao|kkk|haha|rsrs|icant|💀|😂|🤣", re.IGNORECASE)
PASSO = 15          # segundos entre amostras do chat


@dataclass
class Vod:
    plataforma: str
    canal: str
    url: str            # o que o cortar_vod.baixar entende (twitch.tv/videos/ID ou .m3u8)
    inicio: float       # epoch do começo da live
    duracao: float      # segundos
    titulo: str = ""
    categoria: str = ""
    live_id: str = ""   # Kick: id da livestream (pra achar os clips dela)


@dataclass
class Candidato:
    centro: float                 # segundo da VOD onde está o momento
    sinal: float = 0.0            # 0-1, força dos sinais (chat + clips)
    origem: list[str] = field(default_factory=list)
    views: int = 0
    chat: float = 0.0             # chat N vezes acima do normal
    titulo_clip: str = ""         # título do clip da comunidade (dica do que acontece)
    mensagens: list[tuple[str, int]] = field(default_factory=list)


# ------------------------------------------------------------------ HTTP
def _twitch(query: dict | list) -> dict | list:
    import requests
    for tentativa in range(4):
        try:
            r = requests.post(GQL, headers=TWITCH_ID, json=query, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1 + tentativa * 2)
    return {}


def _kick(url: str, params: dict | None = None):
    from curl_cffi import requests as creq
    for tentativa in range(4):
        try:
            r = creq.get(url, params=params, impersonate="chrome", timeout=25,
                         headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1 + tentativa * 2)
    return None


# ------------------------------------------------------------------ VOD
def achar_vod(plataforma: str, canal: str, vod: str = "") -> Vod:
    if plataforma == "twitch":
        vid = vod.rstrip("/").split("/")[-1] if vod else ""
        if vid:
            q = f'query{{video(id:"{vid}"){{id createdAt lengthSeconds title game{{name}}}}}}'
            v = (_twitch({"query": q}).get("data") or {}).get("video")
        else:
            q = (f'query{{user(login:"{canal}"){{videos(first:1, type:ARCHIVE, sort:TIME){{edges{{node{{'
                 f'id createdAt lengthSeconds title game{{name}}}}}}}}}}}}')
            ed = ((_twitch({"query": q}).get("data") or {}).get("user") or {}).get("videos", {}).get("edges") or []
            v = ed[0]["node"] if ed else None
        if not v:
            raise RuntimeError(f"não achei VOD da Twitch de {canal}")
        return Vod("twitch", canal, f"https://www.twitch.tv/videos/{v['id']}",
                   datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00")).timestamp(),
                   float(v["lengthSeconds"]), v.get("title") or "", (v.get("game") or {}).get("name") or "")
    vs = _kick(f"https://kick.com/api/v2/channels/{canal}/videos") or []
    if vod:
        uuid = vod.rstrip("/").split("/")[-1]
        vs = [v for v in vs if (v.get("video") or {}).get("uuid") == uuid]
    else:
        vs = [v for v in vs if v.get("duration")]   # só live já encerrada
    if not vs:
        raise RuntimeError(f"não achei VOD da Kick de {canal}")
    v = vs[0]
    ini = datetime.strptime(v["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    cat = ((v.get("categories") or [{}])[0] or {}).get("name") or ""
    return Vod("kick", canal, v["source"], ini, float(v.get("duration") or 0) / 1000, v.get("session_title") or "",
               cat, str(v.get("id") or ""))


# ------------------------------------------------------------------ sinais: chat
def _amostra_twitch(vod: Vod, off: int) -> list[tuple[float, str]]:
    vid = vod.url.rstrip("/").split("/")[-1]
    body = [{"operationName": "VideoCommentsByOffsetOrCursor",
             "variables": {"videoID": vid, "contentOffsetSeconds": off},
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH_CHAT}}}]
    d = _twitch(body)
    try:
        eds = d[0]["data"]["video"]["comments"]["edges"]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for e in eds:
        n = e.get("node") or {}
        txt = "".join(f.get("text", "") for f in (n.get("message") or {}).get("fragments") or [])
        out.append((float(n.get("contentOffsetSeconds", off)), txt))
    return out


def _amostra_kick(vod: Vod, off: int, canal_id: int) -> list[tuple[float, str]]:
    t = datetime.fromtimestamp(vod.inicio + off, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    d = _kick(f"https://kick.com/api/v2/channels/{canal_id}/messages", {"start_time": t}) or {}
    out = []
    for m in ((d.get("data") or {}).get("messages") or []):
        try:
            tm = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        out.append((tm - vod.inicio, m.get("content") or ""))
    return out


def sinais_chat(vod: Vod, passo: int = PASSO) -> dict[int, dict]:
    """{segundo: {"dens": msgs/s, "risada": fração, "msgs": [...]}} a cada `passo` segundos."""
    canal_id = 0
    if vod.plataforma == "kick":
        canal_id = int((_kick(f"https://kick.com/api/v2/channels/{vod.canal}") or {}).get("id") or 0)
    offs = list(range(0, int(vod.duracao), passo))

    def um(off):
        msgs = _amostra_twitch(vod, off) if vod.plataforma == "twitch" else _amostra_kick(vod, off, canal_id)
        msgs = [(t, m) for t, m in msgs if off - 1 <= t <= off + passo]
        if len(msgs) < 2:
            return off, {"dens": len(msgs) / passo, "risada": 0.0, "msgs": [m for _, m in msgs]}
        span = max(1.0, max(t for t, _ in msgs) - min(t for t, _ in msgs))
        ris = sum(1 for _, m in msgs if RISADA.search(m)) / len(msgs)
        return off, {"dens": len(msgs) / span, "risada": ris, "msgs": [m for _, m in msgs]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = dict(ex.map(um, offs))
    ok = sum(1 for v in res.values() if v["msgs"])
    log.info("chat: %d amostras (%d com mensagens)", len(res), ok)
    return res


def serie_chat(chat: dict[int, dict]) -> dict[int, float]:
    """{segundo: quantas vezes o chat estava acima do normal (mediana dos 10 min em volta)}."""
    ts = sorted(chat)
    dens = [chat[t]["dens"] for t in ts]
    janela = max(1, 600 // PASSO)
    out = {}
    for i, t in enumerate(ts):
        base = statistics.median(dens[max(0, i - janela): i + janela + 1]) or 0.01
        out[t] = dens[i] / base
    return out


def _top_msgs(msgs: list[str]) -> list[tuple[str, int]]:
    cont: dict[str, int] = {}
    for m in msgs:
        k = m.strip()[:40].upper()
        if k:
            cont[k] = cont.get(k, 0) + 1
    return sorted(cont.items(), key=lambda kv: -kv[1])[:6]


def chat_no_momento(cands: list[Candidato], chat: dict[int, dict], serie: dict[int, float]) -> None:
    """Mede o chat de cada candidato no segundo exato (o chat reage logo depois do momento)."""
    for c in cands:
        ts = [t for t in serie if c.centro - 10 <= t <= c.centro + 25]
        if not ts:
            continue
        t = max(ts, key=lambda t: serie[t] * (1 + chat[t]["risada"]))
        c.chat = max(c.chat, round(serie[t], 1))
        if not c.mensagens:
            c.mensagens = _top_msgs(chat[t]["msgs"])


def picos_chat(chat: dict[int, dict], n: int = 40, sep: int = 90) -> list[Candidato]:
    """Trechos em que o chat ficou muito acima do normal (comparado aos 10 min em volta)."""
    if not chat:
        return []
    serie = serie_chat(chat)
    score = [(r * (1 + chat[t]["risada"]), r, t) for t, r in serie.items()]
    score.sort(reverse=True)
    out: list[Candidato] = []
    for s, ratio, t in score:
        if ratio < 1.8 or len(out) >= n:
            break
        if any(abs(c.centro - (t - 8)) < sep for c in out):
            continue
        # o chat reage uns 8s depois do momento
        out.append(Candidato(centro=t - 8, chat=round(ratio, 1), origem=["chat"], mensagens=_top_msgs(chat[t]["msgs"])))
    return out


# ------------------------------------------------------------------ sinais: clips da comunidade
def clips_comunidade(vod: Vod) -> list[Candidato]:
    out: list[Candidato] = []
    if vod.plataforma == "twitch":
        vid = vod.url.rstrip("/").split("/")[-1]
        cursor = None
        for _ in range(5):
            after = f', after:"{cursor}"' if cursor else ""
            q = (f'query{{user(login:"{vod.canal}"){{clips(first:100{after}, criteria:{{period:LAST_WEEK, '
                 f'sort:VIEWS_DESC}}){{pageInfo{{hasNextPage}} edges{{cursor node{{title viewCount durationSeconds '
                 f'videoOffsetSeconds video{{id}}}}}}}}}}}}')
            d = {}
            for tentativa in range(4):   # a Twitch às vezes devolve a página vazia: tenta de novo
                d = ((_twitch({"query": q}).get("data") or {}).get("user") or {}).get("clips") or {}
                if d.get("edges"):
                    break
                time.sleep(2 + tentativa * 3)
            for e in d.get("edges") or []:
                n = e["node"]
                if (n.get("video") or {}).get("id") == vid and n.get("videoOffsetSeconds") is not None:
                    out.append(Candidato(centro=n["videoOffsetSeconds"] + n["durationSeconds"] * 0.6,
                                         views=int(n["viewCount"]), origem=["clip"], titulo_clip=n["title"]))
                cursor = e.get("cursor")
            if not (d.get("pageInfo") or {}).get("hasNextPage"):
                break
        return out
    vistos = 0
    cursor = None
    for _ in range(6):
        params = {"sort": "view", "time": "week"}
        if cursor:
            params["cursor"] = cursor
        d = _kick(f"https://kick.com/api/v2/channels/{vod.canal}/clips", params) or {}
        cls = d.get("clips") or []
        for c in cls:
            if vod.live_id and str(c.get("livestream_id")) != vod.live_id:
                continue
            det = (_kick(f"https://kick.com/api/v2/clips/{c['id']}") or {}).get("clip") or {}
            off = det.get("vod_starts_at")
            if off is None:
                continue
            out.append(Candidato(centro=float(off) + float(c.get("duration") or 30) * 0.6,
                                 views=int(c.get("views") or 0), origem=["clip"], titulo_clip=c.get("title") or ""))
            vistos += 1
            if vistos >= 60:
                return out
        novo = d.get("nextCursor")
        if not novo or novo == cursor or not cls:
            break
        cursor = novo
    return out


# ------------------------------------------------------------------ ranking
def juntar(clips: list[Candidato], picos: list[Candidato], n: int, chat: dict | None = None) -> list[Candidato]:
    """Clips perto de um pico de chat viram um candidato só, com os dois sinais."""
    tudo: list[Candidato] = []
    for c in sorted(clips, key=lambda c: -c.views):
        par = next((x for x in tudo if abs(x.centro - c.centro) < 45), None)
        if par:
            par.views += c.views
            continue
        tudo.append(c)
    for p in picos:
        par = next((x for x in tudo if abs(x.centro - p.centro) < 45), None)
        if par:
            par.chat, par.mensagens = max(par.chat, p.chat), par.mensagens or p.mensagens
            if "chat" not in par.origem:
                par.origem.append("chat")
        else:
            tudo.append(p)
    if chat:
        chat_no_momento(tudo, chat, serie_chat(chat))
    vmax = max([math.log1p(c.views) for c in tudo] + [1])
    for c in tudo:
        # views dos clips do público pesam mais (é gente escolhendo o momento); chat 8x+ = nota cheia
        c.sinal = round(0.65 * math.log1p(c.views) / vmax + 0.35 * min(1.0, max(0.0, c.chat - 1) / 7), 3)
    tudo.sort(key=lambda c: -c.sinal)
    return tudo[:n]


def assuntos_em_alta(limite: int = 25) -> list[str]:
    """Títulos dos vídeos de GTA 6 mais vistos dos últimos dias no YouTube (o que está em alta)."""
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return []
    titulos: list[tuple[int, str]] = []
    try:
        with YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": 60, "no_warnings": True}) as y:
            for url in ("https://www.youtube.com/results?search_query=gta+6&sp=EgIIAw%253D%253D",
                        "https://www.youtube.com/results?search_query=gta+6+shorts&sp=EgIIAw%253D%253D",
                        "https://www.youtube.com/results?search_query=gta+6+streamer&sp=EgIIAw%253D%253D"):
                for e in (y.extract_info(url, download=False) or {}).get("entries") or []:
                    if e and e.get("title"):
                        titulos.append((int(e.get("view_count") or 0), e["title"]))
    except Exception as e:  # noqa: BLE001
        log.warning("não consegui ver o que está em alta: %s", e)
    vistos, out = set(), []
    for _, t in sorted(titulos, reverse=True):
        if t.lower() not in vistos:
            vistos.add(t.lower())
            out.append(t[:90])
    return out[:limite]


# ------------------------------------------------------------------ principal
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--canal", required=True, help="plataforma/canal, ex.: twitch/alanzoka")
    ap.add_argument("--vod", default="", help="id/link da VOD (vazio = a última live encerrada)")
    ap.add_argument("--max", type=int, default=15, help="quantos clips finais guardar")
    ap.add_argument("--candidatos", type=int, default=30, help="quantos momentos analisar com a IA")
    ap.add_argument("--out", default=str(ROOT / "clips"))
    ap.add_argument("--so-sinais", action="store_true", help="só lista os candidatos (não baixa nada)")
    ap.add_argument("--so-gameplay", action="store_true", help="descarta o que não é gameplay (mesmo se o canal não tiver so_gameplay)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config()
    s = cfg.settings
    plat, nome = parse_channel_ref(args.canal)
    ch = cfg.channel_or_default(plat, nome)
    vod = achar_vod(plat, nome, args.vod)
    log.info("VOD: %s (%s, %.0f min) %s", vod.url[:80], datetime.fromtimestamp(vod.inicio).strftime("%d/%m %H:%M"),
             vod.duracao / 60, vod.titulo[:60])

    clips = clips_comunidade(vod)
    log.info("clips da comunidade nesta live: %d", len(clips))
    chat = sinais_chat(vod)
    picos = picos_chat(chat)
    log.info("picos de chat: %d", len(picos))
    cands = juntar(clips, picos, args.candidatos, chat)
    for i, c in enumerate(cands, 1):
        log.info("%2d. %s sinal %.2f  views %d  chat %.1fx  %s  %s", i, _hms(c.centro), c.sinal, c.views, c.chat,
                 "+".join(c.origem), c.titulo_clip[:50])
    if args.so_sinais:
        return 0

    em_alta = assuntos_em_alta()
    log.info("em alta no YouTube: %s", em_alta[:8])
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = ROOT / "work" / f"vodan_{int(time.time())}"
    copy = Copywriter()
    tags = ch.hashtags or s["hashtags"]
    from .runner import quadros_do_clip
    feitos: list[tuple[float, dict, Path]] = []
    usados: list[str] = []
    for i, c in enumerate(cands, 1):
        t0 = max(0.0, c.centro - 30)
        dur = min(52.0, vod.duracao - t0) if vod.duracao else 52.0
        if dur < 10:
            continue
        w = work / f"c{i}"
        w.mkdir(parents=True, exist_ok=True)
        try:
            src = w / "vod.ts"
            baixar(vod.url, int(t0), int(dur) + 2, src)
            quando = datetime.fromtimestamp(vod.inicio + c.centro, timezone.utc)
            out = out_dir / f"{ch.slug}_{quando:%Y%m%d-%H%M%S}.mp4"
            res = build_clip([(0.0, src)], 0.0, dur, out, w / "clip", layout=ch.layout, facecam=ch.facecam,
                             fps=int(s["fps"]), with_subtitles=True, burn_subtitles=bool(s["subtitles"]),
                             whisper_model=s["whisper_model"], language=s["language"],
                             facecam_auto=ch.facecam_auto, watermark=ch.watermark,
                             sub_size=int(s["subtitle_size"]), sub_position=s["subtitle_position"])
            quadros = quadros_do_clip(res.path, res.duration, w)
            dica = f"{vod.titulo} | clip do público: \"{c.titulo_clip}\"" if c.titulo_clip else vod.titulo
            if em_alta:
                dica += " | em alta no YouTube agora: " + "; ".join(em_alta[:15])
            wav = w / "clip" / "audio.wav"
            texto = copy.make(ch.name, res.words, res.transcript, wav if wav.exists() else None, 30.0,
                              c.mensagens, vod.categoria, dica, tags, evitar=usados, images=quadros)
            if texto.fonte != "gemini" and copy.configured:   # IA lenta/ocupada: espera e tenta de novo
                time.sleep(30)
                copy._falhas_seguidas = min(copy._falhas_seguidas, 4)
                texto = copy.make(ch.name, res.words, res.transcript, wav if wav.exists() else None, 30.0,
                                  c.mensagens, vod.categoria, dica, tags, evitar=usados, images=quadros)
            if (ch.so_gameplay or args.so_gameplay) and texto.gameplay is False:
                log.info("candidato %d (%s) não é gameplay: descartado", i, _hms(c.centro))
                res.path.unlink(missing_ok=True)
                continue
            texto.titulo = ch.titulo_com_tags(texto.titulo)
            texto.hashtags = ch.limpar_hashtags(texto.hashtags)
            usados.append(texto.titulo)
            # sem nota da IA (falhou), o ranking usa só os sinais: o momento não é punido por isso
            nota = texto.viral / 10 if texto.viral is not None else c.sinal
            final = round(0.5 * nota + 0.5 * c.sinal, 3)
            dados = {"arquivo": out.name, "canal": ch.name, "plataforma": ch.platform_label, "url_canal": ch.url,
                     "pico_epoch": vod.inicio + c.centro, "hype": c.chat or None, "titulo": texto.titulo,
                     "legenda": texto.legenda, "hashtags": texto.hashtags, "viral": texto.viral,
                     "sobre_gta6": texto.sobre_gta6, "texto_por": texto.fonte, "categoria": vod.categoria,
                     "titulo_live": vod.titulo, "fala": res.transcript[:500], "duracao": round(res.duration, 1),
                     "youtube": None, "vod": f"{vod.url} @ {_hms(t0)}", "origem": "vod",
                     "sinais": {"views_clips": c.views, "chat": c.chat, "sinal": c.sinal, "final": final,
                                "fontes": c.origem, "clip_publico": c.titulo_clip},
                     "gameplay": texto.gameplay, "titulo_longo": texto.titulo_longo}
            out.with_suffix(".json").write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
            feitos.append((final, dados, out))
            log.info("✅ %d/%d %s  nota %s  final %.2f  %s", i, len(cands), _hms(c.centro), texto.viral, final,
                     texto.titulo)
        except Exception as e:  # noqa: BLE001
            log.warning("candidato %d (%s) falhou: %s", i, _hms(c.centro), str(e)[:300])
        finally:
            shutil.rmtree(w, ignore_errors=True)

    feitos.sort(key=lambda x: -x[0])
    for _, d, p in feitos[args.max:]:   # fica só com os melhores
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)
    feitos = feitos[: args.max]
    linhas = [f"## {ch.name}: {len(feitos)} melhores momentos da VOD ({vod.duracao / 3600:.1f}h)", "",
              f"VOD: {vod.url.split('?')[0][:90]} | clips do público: {len(clips)} | picos de chat: {len(picos)}", "",
              "| # | momento | nota IA | sinal | views clips | chat | título |", "|---|---|---|---|---|---|---|"]
    for n, (final, d, _) in enumerate(feitos, 1):
        sg = d["sinais"]
        linhas.append(f"| {n} | {d['vod'].split('@')[-1].strip()} | {d['viral']} | {sg['sinal']} | "
                      f"{sg['views_clips']} | {sg['chat']}x | {d['titulo']} |")
    rel = "\n".join(linhas)
    (out_dir / f"ranking_{ch.slug}.md").write_text(rel, encoding="utf-8")
    print(rel, flush=True)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(rel + "\n\n")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if feitos else 1


def _hms(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


if __name__ == "__main__":
    sys.exit(main())
