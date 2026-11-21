"""Orquestra o monitoramento de UM canal.

    chat (asyncio) ──> detector ──pico──> fila de clips (thread separada) ──> Telegram
    stream (thread) ──> ffmpeg ──> segmentos .ts em buffer ──────┘

A montagem do clip roda numa thread própria (o pesado é ffmpeg/Whisper, que liberam o GIL),
então a leitura do chat nunca trava.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import signal
import threading
import json
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from . import audio_limpo, summary
from .chat import make_chat_reader
from .clipper import build_clip, build_horizontal, editar_final
from .copywriter import _GTA6_RE, ClipCopy, Copywriter
from .config import DEFAULT_YOUTUBE, ROOT, Channel, Config, Facecam
from .detector import HypeDetector, Peak
from .recorder import Recorder, select_segments
from .stream import StreamInfo, get_stream_info, make_session
from .telegram import Telegram, build_caption
from .youtube import YouTube, YouTubeLimitError, build_metadata, conta_teto, decide, decide_conta, marker

log = logging.getLogger("clipador.runner")


def quadros_do_clip(video: Path, duracao: float, workdir: Path) -> list[Path]:
    """2 quadros do clip (1/3 e 2/3) pra IA ver se é gameplay ou câmera da vida real."""
    from .recorder import ffmpeg_bin
    import subprocess
    out = []
    for i, frac in enumerate((0.33, 0.66)):
        p = workdir / f"quadro{i}.jpg"
        try:
            subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-ss", f"{max(0.0, duracao * frac):.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=540:-2", "-q:v", "4", str(p)], check=True, timeout=60,
                           capture_output=True)
            if p.exists():
                out.append(p)
        except Exception as e:  # noqa: BLE001
            log.warning("não consegui tirar quadro do clip: %s", e)
    return out


def _norm_titulo(t: str) -> str:
    t = re.sub(r"#\w+", " ", (t or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


@dataclass
class RunResult:
    live: bool
    clips: int = 0
    reason: str = ""
    still_live: bool = False   # parou por tempo máximo com a live ainda no ar


class ChannelRunner:
    def __init__(self, cfg: Config, channel: Channel, telegram: Telegram, out_dir: Path, work_dir: Path,
                 once: bool = False, max_minutes: float | None = None, youtube: YouTube | None = None,
                 post_forced: bool = False):
        self.cfg = cfg
        self.s = cfg.settings
        self.ch = channel
        self.tg = telegram
        self.out_dir = Path(out_dir)
        self.work_dir = Path(work_dir) / channel.slug
        self.once = once
        self.max_minutes = max_minutes or self.s["max_runtime_minutes"]
        self.yt_settings = self.s.get("youtube") or dict(DEFAULT_YOUTUBE)
        self.yt = youtube or YouTube.from_env(self.s["timezone"], channel.youtube_conta)
        self.hashtags = channel.hashtags or self.s["hashtags"]
        self._yt_pausa_ate = 0.0   # limite de envios do canal: não tenta de novo antes disso
        self._titulos: list[str] = []       # títulos já usados no canal (a IA não repete)
        self._titulos_lidos = False
        self.copy = Copywriter()        # título/legenda com IA (GEMINI_API_KEY) ou plano B local
        self.post_forced = post_forced   # --post-youtube: posta até o clip de teste (--once)

        self.info: StreamInfo | None = None
        self.session = None
        self.recorder: Recorder | None = None
        self.detector = HypeDetector.from_settings(self.s, channel.min_msgs)
        self.clips_ok = 0
        self.clips_started = 0
        self._stop = threading.Event()
        self._clip_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip")
        self._futures: list[Future] = []
        self._clip_lock = threading.Lock()
        # última moldura da cam achada pelas bordas (fica salva entre execuções; no Actions via cache)
        self._facecam_file = Path(os.environ.get("CLIPADOR_STATE_DIR") or ROOT / "state") / f"facecam-{channel.slug}.json"
        self._facecam_last_good = self._load_facecam() if channel.facecam_auto else None

    # ------------------------------------------------------------------ facecam salva
    def _load_facecam(self) -> Facecam | None:
        try:
            d = json.loads(self._facecam_file.read_text(encoding="utf-8"))
            fc = Facecam(**{k: float(d[k]) for k in ("x", "y", "w", "h")})
            fc.validate("state")
            log.info("[%s] moldura da cam salva carregada: %s", self.ch.slug, fc)
            return fc
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] moldura salva inválida (%s); ignorando", self.ch.slug, e)
            return None

    def _save_facecam(self, fc: Facecam) -> None:
        try:
            self._facecam_file.parent.mkdir(parents=True, exist_ok=True)
            self._facecam_file.write_text(json.dumps({"x": fc.x, "y": fc.y, "w": fc.w, "h": fc.h}), encoding="utf-8")
        except OSError as e:
            log.warning("[%s] não consegui salvar a moldura da cam: %s", self.ch.slug, e)

    # ------------------------------------------------------------------ stream
    def _open_stream(self):
        """Usado pelo gravador a cada (re)conexão. Atualiza título/categoria de quebra."""
        if self.info is not None and self.info.stream is not None:
            stream, self.info.stream = self.info.stream, None  # usa o stream já resolvido na 1ª vez
            return stream
        info = get_stream_info(self.ch, self.s["max_quality"], self.session)
        if info.live:
            self.info = StreamInfo(live=True, title=info.title, category=info.category, author=info.author,
                                   quality=info.quality)
            return info.stream
        return None

    # ------------------------------------------------------------------ clips
    def _clip_window(self, peak: Peak) -> tuple[float, float]:
        before, after = self.s["clip_before_seconds"], self.s["clip_after_seconds"]
        dur = min(before + after, self.s["max_clip_seconds"])
        t0 = peak.t - before
        return t0, t0 + dur

    def _extra_antes_h(self) -> float:
        """Segundos a mais de contexto antes do clip, pra versão horizontal (0 se desligada)."""
        h = (self.yt_settings or {}).get("horizontal") or {}
        return float(h.get("extra_before_seconds", 20)) if h.get("enabled") and self.ch.horizontal else 0.0

    def _titulos_evitar(self) -> list[str]:
        """Últimos títulos do canal do YouTube (1x por job) + os desta execução: a IA não repete."""
        if not self._titulos_lidos:
            self._titulos_lidos = True
            if self.yt.configured:
                try:
                    self._titulos = sorted(self.yt.titulos_postados(paginas=1))[:30] + self._titulos
                except Exception as e:  # noqa: BLE001
                    log.warning("não consegui ler os títulos do canal: %s", e)
        return self._titulos[-25:]

    def submit_clip(self, peak: Peak) -> None:
        if self.clips_started >= self.s["max_clips_per_run"]:
            log.warning("limite de %d clips nesta execução atingido; ignorando pico", self.s["max_clips_per_run"])
            return
        self.clips_started += 1
        t0, t1 = self._clip_window(peak)
        token = self.recorder.protect(t0 - self._extra_antes_h(), t1)  # protege já, antes de entrar na fila
        log.info("🔥 PICO #%d: score %.1f (baseline %.1f, limiar %.1f) top=%s", self.clips_started, peak.score,
                 peak.baseline, peak.threshold, peak.top_messages[:3])
        self._futures.append(self._clip_pool.submit(self._make_clip, peak, token, self.clips_started))

    def _make_clip(self, peak: Peak, token: int, n: int) -> None:
        rec = self.recorder
        t0, t1 = self._clip_window(peak)
        workdir = self.work_dir / f"clip_{n}"
        try:
            # espera gravar o "depois" do pico
            espera = peak.t + self.s["wait_after_peak_seconds"] - time.time()
            if espera > 0:
                self._stop.wait(espera)
            if not rec.wait_for_coverage(t1, timeout=45):
                log.warning("clip #%d: segmento final não fechou a tempo; usando o que tem", n)
            segs = select_segments(rec.segments(), t0, t1)
            if not segs:
                raise RuntimeError("nenhum segmento gravado cobre o trecho do pico (gravação parada?)")
            quando = datetime.fromtimestamp(peak.t).strftime("%Y%m%d-%H%M%S")
            out = self.out_dir / f"{self.ch.slug}_{quando}.mp4"
            res = build_clip(segs, t0, t1 - t0, out, workdir, layout=self.ch.layout, facecam=self.ch.facecam,
                             fps=int(self.s["fps"]), with_subtitles=True, burn_subtitles=bool(self.s["subtitles"]),
                             whisper_model=self.s["whisper_model"], language=self.ch.idioma or self.s["language"],
                             facecam_auto=self.ch.facecam_auto, facecam_last_good=self._facecam_last_good,
                             watermark=None if self.ch.editar else self.ch.watermark,
                             sub_size=int(self.s["subtitle_size"]),
                             sub_position=self.s["subtitle_position"])
            if res.facecam_refined is not None:
                self._facecam_last_good = res.facecam_refined
                self._save_facecam(res.facecam_refined)
            info = self.info or StreamInfo(live=True)
            # título + legenda + hashtags (IA ouvindo o áudio do clip, ou plano B local)
            evitar = self._titulos_evitar()
            quadros = quadros_do_clip(res.path, res.duration, workdir)
            texto = self.copy.make(self.ch.name, res.words, res.transcript, workdir / "audio.wav",
                                   max(0.0, peak.t - t0), peak.top_messages, info.category, info.title,
                                   self.hashtags, evitar=evitar, images=quadros, idioma=self.ch.idioma)
            if self.ch.exige_participacao and texto.participa is False and not peak.forced:
                log.info("clip #%d: %s não participa do trecho (regra do campeonato): descartado", n, self.ch.name)
                res.path.unlink(missing_ok=True)
                return
            if self.ch.so_gameplay and texto.gameplay is False and not peak.forced:
                log.info("clip #%d não é gameplay (IRL/conversa) e o canal é só gameplay: descartado", n)
                res.path.unlink(missing_ok=True)
                return
            if self.ch.editar:
                try:
                    editar_final(res.path, workdir / "edit", texto.gancho if (self.ch.edicao or {}).get("gancho") else "",
                                 max(0.0, peak.t - t0), self.ch.watermark, zoom=bool((self.ch.edicao or {}).get("zoom_pico")),
                                 y_gancho=700 if self.ch.layout == "facecam" else 300, lower=self.ch.lower)
                except Exception as e:  # noqa: BLE001
                    log.warning("edição falhou (%s); clip vai sem ela", e)
                    if self.ch.watermark:   # a marca d'água é obrigatória: sem ela, não posta
                        raise
            audio_limpo.aplicar(self.ch, res.path, workdir / "audio")
            repetido = _norm_titulo(texto.titulo) in {_norm_titulo(t) for t in evitar}
            self._titulos.append(texto.titulo)
            texto.titulo = self.ch.titulo_com_tags(texto.titulo)
            texto.hashtags = self.ch.limpar_hashtags(texto.hashtags)
            th0 = t0 - self._extra_antes_h()

            def render_h(th0=th0, t1=t1):
                segs_h = select_segments(rec.segments(), th0, t1)
                return build_horizontal(segs_h, th0, t1 - th0, out.with_name(out.stem + "_horizontal.mp4"),
                                        workdir / "h", fps=int(self.s["fps"]), watermark=self.ch.watermark)

            yt_line = self._maybe_post_youtube(res, peak, info, texto, repetido=repetido, render_h=render_h)
            # resumo na página do job (não depende do Telegram)
            try:
                summary.write_clip(self.out_dir, out.name, peak, self.s["timezone"], yt_line,
                                   f"{texto.titulo} {' '.join(texto.hashtags)}", res.transcript)
            except OSError as e:
                log.warning("não consegui escrever o resumo: %s", e)
            self._write_sidecar(out, peak, res, info, texto, yt_line)
            cap = build_caption(self.ch.name, self.ch.platform_label, peak, res.transcript, info.title,
                                info.category, self.hashtags, self.s["timezone"], youtube_line=yt_line)
            self.tg.send_video(res.path, cap, duration=res.duration)
            with self._clip_lock:
                self.clips_ok += 1
            log.info("clip #%d enviado: %s", n, out.name)
        except Exception as e:  # noqa: BLE001
            log.exception("clip #%d falhou", n)
            self.tg.send_error(f"{self.ch.slug}: clip #{n} falhou: {type(e).__name__}: {e}")
        finally:
            rec.release(token)
            shutil.rmtree(workdir, ignore_errors=True)

    def _write_sidecar(self, out: Path, peak: Peak, res, info: StreamInfo, texto: ClipCopy,
                       yt_line: str | None) -> None:
        """<clip>.json ao lado do vídeo: dados que o baixador do TikTok usa (legenda, horário, hype)."""
        yt_url = None
        if yt_line and "youtube.com/" in yt_line:
            yt_url = "https://" + yt_line.split("https://", 1)[1].strip()
        dados = {
            "arquivo": out.name,
            "canal": self.ch.name,
            "plataforma": self.ch.platform_label,
            "url_canal": self.ch.url,
            "pico_epoch": peak.t,
            "hype": None if peak.ratio == float("inf") else round(peak.ratio, 1),
            "titulo": texto.titulo,
            "legenda": texto.legenda,
            "hashtags": texto.hashtags,
            "viral": texto.viral,
            "sobre_gta6": texto.sobre_gta6,
            "texto_por": texto.fonte,
            "categoria": info.category,
            "titulo_live": info.title,
            "fala": res.transcript[:500],
            "duracao": round(res.duration, 1),
            "youtube": yt_url,
        }
        try:
            out.with_suffix(".json").write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("não consegui salvar os dados do clip: %s", e)

    def _maybe_post_youtube(self, res, peak: Peak, info: StreamInfo, texto: ClipCopy, repetido: bool = False,
                            render_h=None) -> str | None:
        """Posta no YouTube Shorts se passar no filtro. Devolve a linha que vai na mensagem do Telegram."""
        ys = self.yt_settings
        if not ys.get("enabled"):
            return None
        if not self.yt.configured:
            return "⚠️ YouTube ligado no channels.json, mas faltam os secrets YOUTUBE_*"
        if time.time() < self._yt_pausa_ate:
            volta = datetime.fromtimestamp(self._yt_pausa_ate, ZoneInfo(self.s["timezone"])).strftime("%H:%M")
            return f"⏸️ YouTube: canal no limite de envios, tento de novo às {volta}"
        try:
            st = self.yt.stats(self.ch.platform, self.ch.name)
            d = decide(ys, self.ch.youtube, peak.ratio, peak.forced, st, allow_forced=self.post_forced)
            if not d.post:
                log.info("YouTube: não postado (%s)", d.reason)
                return f"⏭️ YouTube: {d.reason}"
            if not peak.forced:
                if self.ch.so_gameplay and texto.gameplay is not True:
                    return "⏭️ YouTube: não deu pra confirmar que é gameplay (canal só gameplay)"
                if self.ch.so_gta and not (texto.sobre_gta6 or _GTA6_RE.search(info.category or "")):
                    return "⏭️ YouTube: esse streamer só vai pro canal quando o clip é de GTA"
                if repetido:
                    return "⏭️ YouTube: a IA repetiu um título que já está no canal"
                cs = self.yt.conta_stats()
                dc = decide_conta(ys, self.ch.youtube_conta, cs, texto.viral, texto.fonte, extremo=d.extreme,
                                  bonus=self.ch.prioridade)
                if not dc.post:
                    log.info("YouTube: não postado (%s)", dc.reason)
                    return f"⏭️ YouTube: {dc.reason}"
            meta = build_metadata(texto.titulo, self.ch.name, self.ch.platform_label, self.ch.url, texto.hashtags,
                                  self.ch.platform, ys["category_id"], ys["privacy"], ys["made_for_kids"],
                                  hook=texto.legenda if texto.legenda != texto.titulo else None)
            url = self.yt.upload(res.path, meta, self.ch.platform, self.ch.name)
            if self.ch.comentario:
                self.yt.comentar(url, self.ch.comentario)
            extra = " 🔥 post extra" if d.extreme else ""
            linha = f"▶️ Postado no YouTube ({st.today + 1}º hoje, {d.reason}){extra}: {url}"
            if not peak.forced:
                h = self._maybe_post_horizontal(texto, render_h)
                if h:
                    linha += f"\n🖥️ Versão horizontal: {h}"
            return linha
        except YouTubeLimitError as e:
            self._yt_pausa_ate = time.time() + 2 * 3600
            log.warning("YouTube: %s; pausando os posts por 2h", e)
            self.tg.send_error(f"{self.ch.slug}: {e}", min_interval=7200)
            return f"⏸️ YouTube: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("YouTube falhou")
            self.tg.send_error(f"{self.ch.slug}: postagem no YouTube falhou: {e}", min_interval=1800)
            return f"⚠️ YouTube falhou: {str(e)[:150]}"

    def _maybe_post_horizontal(self, texto: ClipCopy, render_h) -> str | None:
        """Momento muito forte: posta também a versão 16:9 (vídeo normal), poucas por dia."""
        ys = self.yt_settings
        hs = ys.get("horizontal") or {}
        if not (hs.get("enabled") and self.ch.horizontal and render_h):
            return None
        if (texto.viral or 0) < float(hs.get("min_viral", 8.0)):
            return None
        cs = self.yt.conta_stats()
        if cs.horizontais >= int(hs.get("max_24h", 3)) or cs.ult24h >= conta_teto(ys, self.ch.youtube_conta) - 1:
            return None
        try:
            video = render_h()
            titulo = re.sub(r"#\S+", "", texto.titulo_longo or texto.titulo).strip(" -|")
            serie = f" | {self.ch.name} GTA 6" if texto.sobre_gta6 else f" | {self.ch.name}"
            titulo = self.ch.titulo_com_tags(titulo[: 100 - len(serie)].rstrip() + serie, limite=100)
            meta = build_metadata(titulo, self.ch.name, self.ch.platform_label, self.ch.url, texto.hashtags,
                                  self.ch.platform, ys["category_id"], ys["privacy"], ys["made_for_kids"],
                                  hook=texto.legenda if texto.legenda != texto.titulo else None, short=False)
            meta["snippet"]["description"] = meta["snippet"]["description"].replace(
                marker(self.ch.platform, self.ch.name), f"clipador:horizontal/{self.ch.name}")
            return self.yt.upload(video, meta, "horizontal", self.ch.name)
        except YouTubeLimitError as e:
            self._yt_pausa_ate = time.time() + 2 * 3600
            log.warning("YouTube (horizontal): %s; pausando por 2h", e)
        except Exception as e:  # noqa: BLE001
            log.warning("versão horizontal falhou: %s", e)
        return None

    # ------------------------------------------------------------------ principal
    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> RunResult:
        self.session = make_session()
        info = await asyncio.to_thread(get_stream_info, self.ch, self.s["max_quality"], self.session)
        if not info.live:
            log.info("[%s] offline — nada a fazer", self.ch.slug)
            return RunResult(live=False, reason="offline")
        self.info = info
        log.info("[%s] AO VIVO: %s (%s)", self.ch.slug, info.summary, info.quality)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)
        self.recorder = Recorder(self.work_dir / "segs", self._open_stream, self.s["segment_seconds"],
                                 self.s["buffer_seconds"], name=self.ch.slug)
        self.recorder.start()
        inicio = time.time()

        try:
            summary.write_header(self.out_dir, self.ch.name, self.ch.platform_label, self.ch.url, info.title,
                                 info.category)
        except OSError:
            pass
        modo = "🧪 teste (--once)" if self.once else f"até {self.max_minutes:.0f} min"
        if self.yt_settings.get("enabled") and self.ch.youtube:
            modo += (f" · YouTube auto (≥{self.yt_settings['min_hype_ratio']:g}x, "
                     f"{self.yt_settings['posts_per_hour']}/h, máx {self.yt_settings['max_posts_per_day']}/dia)")
        await asyncio.to_thread(self._safe_send, (
            f"🔴 <b>Monitorando {html.escape(self.ch.name)}</b> ({self.ch.platform_label})\n"
            f"{html.escape(info.title or '')}\n🎮 {html.escape(info.category or '-')} · {info.quality} · {modo}\n"
            f"{self.ch.url}"))

        stop_chat = asyncio.Event()
        chat = make_chat_reader(self.ch)
        chat_task = asyncio.create_task(chat.run(lambda m: self.detector.add(m.text, m.ts), stop_chat))
        self.detector.start(time.time())

        reason, still_live = "", False
        deadline = inicio + self.max_minutes * 60
        forced: Future | None = None
        ultimo_status = time.time()
        sem_dados_desde: float | None = None
        try:
            while True:
                await asyncio.sleep(1)
                agora = time.time()
                if self.once:
                    # espera ter gravado o "antes" e força um clip
                    if forced is None and self.recorder.last_data and \
                            agora - inicio >= self.s["clip_before_seconds"] + 8:
                        self.submit_clip(self.detector.force_peak(agora))
                        forced = self._futures[-1]
                    if forced is not None and forced.done():
                        reason = "teste concluído"
                        break
                    if agora - inicio > 600:
                        reason = "teste demorou demais (10 min)"
                        break
                else:
                    pico = self.detector.tick(agora)
                    if pico:
                        self.submit_clip(pico)

                if self._stop.is_set():
                    reason = "interrompido"
                    break
                if self.recorder.ended.is_set():
                    reason = "live acabou"
                    break
                if agora >= deadline:
                    reason, still_live = "tempo máximo do job", True
                    break

                # gravação travada por muito tempo (sem a live ter acabado): avisa
                if self.recorder.healthy:
                    sem_dados_desde = None
                elif agora - inicio > 120:
                    sem_dados_desde = sem_dados_desde or agora
                    if agora - sem_dados_desde > 300:
                        self.tg.send_error(f"{self.ch.slug}: gravação sem dados há 5 min (tentando reconectar)",
                                           min_interval=1800)

                if agora - ultimo_status >= 60:
                    ultimo_status = agora
                    log.info("[%s] status: chat=%s msgs=%d score=%.1f baseline=%.1f segs=%d gravação=%s clips=%d",
                             self.ch.slug, "ok" if chat.connected else "desconectado",
                             self.detector.total_messages, self.detector.last_score, self.detector.last_baseline,
                             len(self.recorder.segments()), "ok" if self.recorder.healthy else "parada",
                             self.clips_ok)
        finally:
            log.info("[%s] encerrando: %s", self.ch.slug, reason or "erro")
            # termina os clips pendentes (no máx. ~4 min) antes de parar a gravação
            pendentes = [asyncio.wrap_future(f) for f in self._futures if not f.done()]
            if pendentes:
                log.info("[%s] esperando %d clip(s) pendente(s)...", self.ch.slug, len(pendentes))
                await asyncio.wait(pendentes, timeout=240)
            stop_chat.set()
            chat_task.cancel()
            try:
                await chat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stop.set()
            await asyncio.to_thread(self.recorder.stop)
            self._clip_pool.shutdown(wait=False, cancel_futures=True)

        dur = (time.time() - inicio) / 60
        await asyncio.to_thread(self._safe_send, (
            f"⏹️ <b>{html.escape(self.ch.name)}</b>: monitoramento encerrado ({reason}). "
            f"{self.clips_ok} clip(s) em {int(dur // 60)}h{int(dur % 60):02d}min."
            + ("\n↪️ A live continua: o próximo job assume." if still_live else "")))
        return RunResult(live=True, clips=self.clips_ok, reason=reason, still_live=still_live)

    def _safe_send(self, text: str) -> None:
        try:
            self.tg.send_message(text)
        except Exception as e:  # noqa: BLE001
            log.error("falha ao enviar mensagem no Telegram: %s", e)


async def run_channel(cfg: Config, channel: Channel, telegram: Telegram, out_dir: Path, work_dir: Path,
                      once: bool = False, max_minutes: float | None = None,
                      post_forced: bool = False) -> RunResult:
    runner = ChannelRunner(cfg, channel, telegram, out_dir, work_dir, once=once, max_minutes=max_minutes,
                           post_forced=post_forced)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        # SIGTERM: GitHub Actions cancelando/encerrando o job -> parada limpa
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, runner.request_stop)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        return await runner.run()
    except asyncio.CancelledError:
        runner.request_stop()
        raise
