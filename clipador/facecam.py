"""Detecção automática da facecam ("facecam": "auto" no channels.json).

Muitos streamers mudam a câmera de lugar conforme o jogo/cena. Em vez de uma posição fixa,
procuramos o rosto do streamer em alguns frames do clip (OpenCV, detector Haar) e recortamos a
região da câmera em volta dele:

- O rosto do streamer fica PARADO no mesmo lugar em todos os frames; rostos de personagens do jogo
  mudam de posição. Por isso agrupamos as detecções por posição e ficamos com o grupo mais frequente.
- O tamanho da câmera é proporcional ao tamanho do rosto (rosto ~1/4 da largura da cam).

Se não achar um rosto estável, devolve None e o clip usa o layout "blur".
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Facecam
from .recorder import ffmpeg_bin

log = logging.getLogger("clipador.facecam")

SAMPLE_W, SAMPLE_H = 960, 540
CAM_ASPECT = 1080 / 640          # proporção da área da câmera no layout facecam
FACE_TO_CAM_WIDTH = 4.0          # largura da cam ≈ 4x a largura do rosto


@dataclass
class FaceBox:
    x: float  # frações 0-1 do frame
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _sample_frames(src: Path, offset: float, duration: float, n: int, outdir: Path) -> list[Path]:
    frames = []
    for i in range(n):
        t = offset + duration * (i + 0.5) / n
        out = outdir / f"f{i}.png"
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={SAMPLE_W}:{SAMPLE_H}", str(out)], capture_output=True)
        if r.returncode == 0 and out.exists():
            frames.append(out)
    return frames


def detect_faces(image_paths: list[Path]) -> list[list[FaceBox]]:
    """Rostos encontrados em cada imagem (frações do frame)."""
    import cv2  # import tardio: só quem usa "auto" precisa do OpenCV

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    resultado = []
    for p in image_paths:
        img = cv2.imread(str(p))
        if img is None:
            resultado.append([])
            continue
        h, w = img.shape[:2]
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(28, 28))
        resultado.append([FaceBox(fx / w, fy / h, fw / w, fh / h) for (fx, fy, fw, fh) in faces])
    return resultado


def stable_face(per_frame: list[list[FaceBox]], min_frames: int = 2, tol: float = 0.06) -> FaceBox | None:
    """O rosto que aparece no MESMO lugar em mais frames (o streamer)."""
    todos = [(i, f) for i, faces in enumerate(per_frame) for f in faces]
    melhor: list[FaceBox] = []
    for _, ref in todos:
        grupo, frames_vistos = [], set()
        for i, f in todos:
            if i not in frames_vistos and abs(f.cx - ref.cx) < tol and abs(f.cy - ref.cy) < tol \
                    and 0.6 < f.w / ref.w < 1.6:
                grupo.append(f)
                frames_vistos.add(i)
        if len(grupo) > len(melhor):
            melhor = grupo
    if len(melhor) < min_frames:
        return None

    def med(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2]

    return FaceBox(med([f.x for f in melhor]), med([f.y for f in melhor]),
                   med([f.w for f in melhor]), med([f.h for f in melhor]))


def cam_from_face(face: FaceBox, frame_aspect: float = 16 / 9) -> Facecam:
    """Região da câmera (frações) em volta do rosto, na proporção da área da cam do layout."""
    cam_w = min(1.0, face.w * FACE_TO_CAM_WIDTH)
    # altura em fração do frame: (largura_px / aspect) / altura_px
    cam_h = min(1.0, cam_w * frame_aspect / CAM_ASPECT)
    x = face.cx - cam_w / 2
    y = face.cy - cam_h * 0.45          # rosto um pouco acima do centro da cam
    x = max(0.0, min(x, 1.0 - cam_w))
    y = max(0.0, min(y, 1.0 - cam_h))
    return Facecam(x=round(x, 4), y=round(y, 4), w=round(cam_w, 4), h=round(cam_h, 4))


def _first_line(scores, idxs, thr: float):
    """Primeiro índice (a partir do rosto) cuja 'linha persistente' é forte o bastante."""
    for i in idxs:
        if scores(i) >= thr:
            return i
    return None


def refine_with_edges(image_paths: list[Path], face: FaceBox) -> Facecam | None:
    """Acha a borda da câmera: linhas retas que ficam PARADAS em todos os frames em volta do rosto.

    Usa a mediana do gradiente entre os frames (sobrevivem bordas fixas, como a moldura da cam; o que
    se mexe some) e procura, a partir do rosto, a primeira linha contínua em cada direção.
    """
    import cv2
    import numpy as np

    grays = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2GRAY) for p in image_paths]
    grays = [g for g in grays if g is not None]
    if len(grays) < 2:
        return None
    H, W = grays[0].shape
    ex = np.median([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays], axis=0)  # verticais
    ey = np.median([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays], axis=0)  # horizontais
    T = 40.0    # força mínima da borda (gradiente Sobel em 0-255)
    MIN = 0.7   # fração da faixa em que a linha precisa ser contínua

    fx0, fy0 = int(face.x * W), int(face.y * H)
    fw, fh = max(1, int(face.w * W)), max(1, int(face.h * H))
    fx1, fy1 = fx0 + fw, fy0 + fh

    # Movimento por pixel ao longo do clip: o jogo (fora da cam) muda muito; o fundo da cam quase nada.
    # Uma borda de verdade tem o lado de FORA mexendo bem mais que o lado de DENTRO. Isso descarta
    # linhas paradas do fundo da câmera (estante, porta...) e do HUD.
    mov = np.std([cv2.GaussianBlur(g, (5, 5), 0).astype(np.float32) for g in grays], axis=0)
    K = max(4, fw // 5)  # largura da faixa comparada de cada lado

    def borda_real(dentro: float, fora: float) -> bool:
        return fora > dentro * 1.6 + 4.0

    # faixa de linhas/colunas onde a borda tem que ser contínua
    r0, r1 = max(0, fy0 - fh // 2), min(H, fy1 + fh)

    def col_score_dir(sentido: int):
        def score(c):
            if c <= 0 or c >= W - 1:
                return 1.0  # borda do frame conta como limite
            cont = float(np.mean(ex[r0:r1, c - 1:c + 2].max(axis=1) > T))
            if cont < MIN:
                return cont
            dentro = mov[r0:r1, max(0, c - sentido * K):c - sentido * 2][:, ::1] if sentido > 0 else \
                mov[r0:r1, c + 3:min(W, c + K)]
            fora = mov[r0:r1, c + 3:min(W, c + K)] if sentido > 0 else mov[r0:r1, max(0, c - K):c - 2]
            if dentro.size == 0 or fora.size == 0:
                return cont
            return cont if borda_real(float(dentro.mean()), float(fora.mean())) else 0.0
        return score

    right = _first_line(col_score_dir(+1), range(fx1 + fw // 3, min(W, fx0 + 6 * fw)), MIN)
    left = _first_line(col_score_dir(-1), range(fx0 - fw // 3, max(-1, fx1 - 6 * fw), -1), MIN)
    if right is None or left is None:
        return None
    left, right = max(0, left), min(W - 1, right)

    c0, c1 = left + 2, right - 1

    def row_score_dir(sentido: int):
        def score(r):
            if r <= 0 or r >= H - 1:
                return 1.0
            cont = float(np.mean(ey[r - 1:r + 2, c0:c1].max(axis=0) > T))
            if cont < MIN:
                return cont
            if sentido > 0:   # borda de baixo: dentro = acima, fora = abaixo
                dentro, fora = mov[max(0, r - K):r - 2, c0:c1], mov[r + 3:min(H, r + K), c0:c1]
            else:             # borda de cima: dentro = abaixo, fora = acima
                dentro, fora = mov[r + 3:min(H, r + K), c0:c1], mov[max(0, r - K):r - 2, c0:c1]
            if dentro.size == 0 or fora.size == 0:
                return cont
            return cont if borda_real(float(dentro.mean()), float(fora.mean())) else 0.0
        return score

    top = _first_line(row_score_dir(-1), range(fy0 - fh // 5, max(-1, fy0 - 3 * fh), -1), MIN)
    bottom = _first_line(row_score_dir(+1), range(fy1 + fh // 3, min(H, fy1 + 5 * fh)), MIN)
    if top is None or bottom is None:
        return None
    top, bottom = max(0, top), min(H - 1, bottom)

    # A área da cam no Short é 1080x640: se a caixa achada for mais "alta" que isso (ex.: pegou junto
    # uma faixa de sub/alerta embaixo da cam), corta por baixo, mantendo o rosto dentro.
    alt_ideal = int((right - left) / CAM_ASPECT * (W / H) / (16 / 9))
    if bottom - top > alt_ideal and top + alt_ideal >= fy1:
        bottom = top + alt_ideal

    w, h = (right - left) / W, (bottom - top) / H
    aspect = (right - left) / max(1, bottom - top)
    if not (0.8 <= aspect <= 2.6) or w < face.w * 1.5 or w > 0.75:
        return None
    # margem de 1% pra dentro (tira a moldura)
    mx, my = 0.004, 0.006
    return Facecam(x=round(left / W + mx, 4), y=round(top / H + my, 4),
                   w=round(max(0.01, w - 2 * mx), 4), h=round(max(0.01, h - 2 * my), 4))


def detect_facecam(src: Path, offset: float, duration: float, n_frames: int = 6) -> Facecam | None:
    """Acha a facecam no trecho do clip. None se não houver rosto estável."""
    try:
        with tempfile.TemporaryDirectory(prefix="facecam_") as d:
            frames = _sample_frames(src, offset, duration, n_frames, Path(d))
            if not frames:
                return None
            face = stable_face(detect_faces(frames), min_frames=max(2, len(frames) // 3))
            refinada = refine_with_edges(frames, face) if face else None
    except Exception as e:  # noqa: BLE001
        log.warning("detecção da facecam falhou: %s", e)
        return None
    if face is None:
        log.info("facecam auto: nenhum rosto estável encontrado; usando layout blur")
        return None
    cam = refinada or cam_from_face(face)
    cam = Facecam(x=float(cam.x), y=float(cam.y), w=float(cam.w), h=float(cam.h))
    log.info("facecam auto (%s):", "borda da cam" if refinada else "estimada pelo rosto")
    log.info("facecam auto: rosto em x=%.2f y=%.2f (w=%.2f) -> cam x=%.3f y=%.3f w=%.3f h=%.3f",
             face.cx, face.cy, face.w, cam.x, cam.y, cam.w, cam.h)
    return cam
