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
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import summary
from .chat import make_chat_reader
from .clipper import build_clip
from .config import DEFAULT_YOUTUBE, Channel, Config
from .detector import HypeDetector, Peak
from .recorder import Recorder, select_segments
from .stream import StreamInfo, get_stream_info, make_session
from .telegram import Telegram, build_caption, build_hashtags, suggest_title
from .youtube import YouTube, build_metadata, decide

log = logging.getLogger("clipador.runner")


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
        self.yt = youtube or YouTube.from_env(self.s["timezone"])
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

    def submit_clip(self, peak: Peak) -> None:
        if self.clips_started >= self.s["max_clips_per_run"]:
            log.warning("limite de %d clips nesta execução atingido; ignorando pico", self.s["max_clips_per_run"])
            return
        self.clips_started += 1
        t0, t1 = self._clip_window(peak)
        token = self.recorder.protect(t0, t1)  # protege já, antes de entrar na fila
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
                             fps=int(self.s["fps"]), with_subtitles=True,
                             whisper_model=self.s["whisper_model"], language=self.s["language"])
            info = self.info or StreamInfo(live=True)
            yt_line = self._maybe_post_youtube(res, peak, info)
            # resumo na página do job (não depende do Telegram)
            titulo = suggest_title(self.ch.name, res.transcript, peak.top_messages, info.title, info.category)
            tags = " ".join(build_hashtags(self.s["hashtags"], self.ch.name, info.category))
            try:
                summary.write_clip(self.out_dir, out.name, peak, self.s["timezone"], yt_line,
                                   f"{titulo} {tags}", res.transcript)
            except OSError as e:
                log.warning("não consegui escrever o resumo: %s", e)
            cap = build_caption(self.ch.name, self.ch.platform_label, peak, res.transcript, info.title,
                                info.category, self.s["hashtags"], self.s["timezone"], youtube_line=yt_line)
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

    def _maybe_post_youtube(self, res, peak: Peak, info: StreamInfo) -> str | None:
        """Posta no YouTube Shorts se passar no filtro. Devolve a linha que vai na mensagem do Telegram."""
        ys = self.yt_settings
        if not ys.get("enabled"):
            return None
        if not self.yt.configured:
            return "⚠️ YouTube ligado no channels.json, mas faltam os secrets YOUTUBE_*"
        try:
            hoje = self.yt.posted_today(self.ch.platform, self.ch.name)
            d = decide(ys, self.ch.youtube, peak.ratio, peak.forced, hoje, allow_forced=self.post_forced)
            if not d.post:
                log.info("YouTube: não postado (%s)", d.reason)
                return f"⏭️ YouTube: {d.reason}"
            titulo = suggest_title(self.ch.name, res.transcript, peak.top_messages, info.title, info.category)
            tags = build_hashtags(self.s["hashtags"], self.ch.name, info.category)
            meta = build_metadata(titulo, self.ch.name, self.ch.platform_label, self.ch.url, tags,
                                  self.ch.platform, ys["category_id"], ys["privacy"], ys["made_for_kids"])
            url = self.yt.upload(res.path, meta, self.ch.platform, self.ch.name)
            return f"▶️ Postado no YouTube ({hoje + 1}/{ys['max_posts_per_day']} hoje): {url}"
        except Exception as e:  # noqa: BLE001
            log.exception("YouTube falhou")
            self.tg.send_error(f"{self.ch.slug}: postagem no YouTube falhou: {e}", min_interval=1800)
            return f"⚠️ YouTube falhou: {str(e)[:150]}"

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
                     f"máx {self.yt_settings['max_posts_per_day']}/dia)")
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
