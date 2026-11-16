"""Gravação contínua da live em buffer de segmentos.

Uma thread lê o stream do Streamlink e joga os bytes no stdin de um ffmpeg que só reempacota
(-c copy) em segmentos de ~5s com o horário no nome:
    Linux/macOS: seg_<epoch>.ts      (ex.: seg_1794848201.ts)  — o %s do strftime
    Windows:     seg_<AAAAMMDD-HHMMSS>.ts                      — o strftime do Windows não tem %s
O horário no nome permite casar o vídeo com o horário das mensagens do chat.

Outra thread apaga segmentos mais velhos que `buffer_seconds`, exceto os protegidos por um clip
em andamento.
"""
from __future__ import annotations

import itertools
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("clipador.recorder")

IS_WINDOWS = sys.platform.startswith("win")
SEG_PATTERN = "seg_%Y%m%d-%H%M%S.ts" if IS_WINDOWS else "seg_%s.ts"
_SEG_RE = re.compile(r"^seg_(\d{10,})\.ts$|^seg_(\d{8}-\d{6})\.ts$")


def ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg não encontrado no PATH (no Ubuntu: sudo apt install ffmpeg)")
    return p


def segment_epoch(path: Path | str) -> float | None:
    """Horário (epoch) de início do segmento, a partir do nome do arquivo."""
    m = _SEG_RE.match(Path(path).name)
    if not m:
        return None
    if m.group(1):
        return float(m.group(1))
    # formato Windows: horário LOCAL (o ffmpeg usa localtime no strftime)
    return time.mktime(time.strptime(m.group(2), "%Y%m%d-%H%M%S"))


def list_segments(seg_dir: Path) -> list[tuple[float, Path]]:
    segs = []
    for p in seg_dir.glob("seg_*.ts"):
        e = segment_epoch(p)
        if e is not None:
            segs.append((e, p))
    segs.sort()
    return segs


def select_segments(segs: list[tuple[float, Path]], t0: float, t1: float) -> list[tuple[float, Path]]:
    """Segmentos que cobrem o intervalo [t0, t1]. Cada segmento i cobre [e_i, e_{i+1})."""
    escolhidos = []
    for i, (e, p) in enumerate(segs):
        fim = segs[i + 1][0] if i + 1 < len(segs) else float("inf")
        if fim > t0 and e <= t1:
            escolhidos.append((e, p))
    return escolhidos


class Recorder:
    def __init__(
        self,
        seg_dir: Path,
        open_stream: Callable[[], object | None],
        segment_seconds: int = 5,
        buffer_seconds: int = 180,
        offline_grace_seconds: int = 240,
        name: str = "",
    ):
        """
        open_stream: função que devolve um objeto Stream do streamlink (com .open()) ou None se
                     o canal estiver offline. É chamada de novo a cada queda.
        offline_grace_seconds: quanto tempo offline até considerarmos que a live acabou.
        """
        self.seg_dir = Path(seg_dir)
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        self.open_stream = open_stream
        self.segment_seconds = segment_seconds
        self.buffer_seconds = buffer_seconds
        self.offline_grace = offline_grace_seconds
        self.name = name or "stream"

        self.stop_event = threading.Event()
        self.ended = threading.Event()        # a live acabou (offline por muito tempo)
        self.last_data = 0.0                  # time.time() do último byte recebido
        self.bytes_total = 0
        self.restarts = 0
        self.error: str | None = None

        self._protect: dict[int, tuple[float, float]] = {}
        self._protect_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._proc: subprocess.Popen | None = None
        self._fd = None
        self._threads: list[threading.Thread] = []

    # ---------- proteção de segmentos usados por clips ----------
    def protect(self, t0: float, t1: float) -> int:
        with self._protect_lock:
            token = next(self._ids)
            self._protect[token] = (t0 - self.segment_seconds * 2, t1 + self.segment_seconds * 2)
            return token

    def release(self, token: int) -> None:
        with self._protect_lock:
            self._protect.pop(token, None)

    def _is_protected(self, epoch: float) -> bool:
        with self._protect_lock:
            return any(a <= epoch <= b for a, b in self._protect.values())

    # ---------- consulta ----------
    def segments(self) -> list[tuple[float, Path]]:
        return list_segments(self.seg_dir)

    def newest_epoch(self) -> float | None:
        segs = self.segments()
        return segs[-1][0] if segs else None

    def wait_for_coverage(self, t1: float, timeout: float = 40) -> bool:
        """Espera existir um segmento que começa DEPOIS de t1 (ou seja, o que contém t1 já fechou)."""
        limite = time.time() + timeout
        while time.time() < limite and not self.stop_event.is_set():
            e = self.newest_epoch()
            if e is not None and e > t1:
                return True
            time.sleep(1)
        return False

    @property
    def healthy(self) -> bool:
        return time.time() - self.last_data < 30

    # ---------- ciclo de vida ----------
    def start(self) -> None:
        for alvo, nome in ((self._record_loop, "gravador"), (self._cleanup_loop, "limpeza")):
            t = threading.Thread(target=alvo, name=f"{nome}-{self.name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self.stop_event.set()
        self._close_current()
        for t in self._threads:
            t.join(timeout=15)

    def _close_current(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fd.close()
            except Exception:  # noqa: BLE001
                pass
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _start_ffmpeg(self) -> subprocess.Popen:
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
            "-strftime", "1", SEG_PATTERN,
        ]
        return subprocess.Popen(cmd, cwd=self.seg_dir, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)

    def _record_loop(self) -> None:
        offline_desde: float | None = None
        while not self.stop_event.is_set():
            try:
                stream = self.open_stream()
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] erro ao abrir stream: %s", self.name, e)
                stream = None
            if stream is None:
                offline_desde = offline_desde or time.time()
                if time.time() - offline_desde > self.offline_grace:
                    log.info("[%s] offline há mais de %ss: live encerrada", self.name, self.offline_grace)
                    self.ended.set()
                    return
                self.stop_event.wait(15)
                continue
            offline_desde = None
            try:
                self._fd = stream.open()
                self._proc = self._start_ffmpeg()
                log.info("[%s] gravando (ffmpeg pid %s)", self.name, self._proc.pid)
                while not self.stop_event.is_set():
                    data = self._fd.read(64 * 1024)
                    if not data:
                        raise EOFError("stream terminou")
                    self._proc.stdin.write(data)
                    self.bytes_total += len(data)
                    self.last_data = time.time()
            except Exception as e:  # noqa: BLE001
                if not self.stop_event.is_set():
                    err = b""
                    if self._proc and self._proc.poll() is not None and self._proc.stderr:
                        err = self._proc.stderr.read()[-500:]
                    log.warning("[%s] gravação interrompida: %s %s", self.name, e, err.decode(errors="replace"))
                    self.restarts += 1
            finally:
                self._close_current()
            if not self.stop_event.is_set():
                self.stop_event.wait(3)

    def _cleanup_loop(self) -> None:
        while not self.stop_event.wait(10):
            self.cleanup()

    def cleanup(self, now: float | None = None) -> int:
        """Apaga segmentos mais velhos que buffer_seconds (menos os protegidos)."""
        limite = (now or time.time()) - self.buffer_seconds
        apagados = 0
        segs = self.segments()
        for e, p in segs[:-1]:  # nunca apaga o último (pode estar sendo escrito)
            if e < limite and not self._is_protected(e):
                try:
                    p.unlink()
                    apagados += 1
                except OSError:
                    pass  # no Windows o arquivo pode estar aberto; tenta na próxima
        return apagados
