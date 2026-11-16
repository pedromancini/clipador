"""Testes da detecção automática da facecam (lógica pura, sem vídeo).

Rodar:  python -m unittest tests.test_facecam -v
"""
import unittest

from clipador.facecam import FaceBox, cam_from_face, stable_face


class TestFacecam(unittest.TestCase):
    def test_rosto_estavel_vence_personagens(self):
        streamer = FaceBox(0.10, 0.08, 0.07, 0.12)
        frames = [
            [streamer, FaceBox(0.50, 0.50, 0.05, 0.09)],             # personagem do jogo
            [FaceBox(0.101, 0.081, 0.069, 0.12)],
            [FaceBox(0.099, 0.079, 0.071, 0.12), FaceBox(0.7, 0.3, 0.06, 0.1)],
            [],
        ]
        f = stable_face(frames)
        self.assertIsNotNone(f)
        self.assertAlmostEqual(f.x, 0.10, places=2)

    def test_sem_rosto_estavel(self):
        frames = [[FaceBox(0.1, 0.1, 0.05, 0.09)], [FaceBox(0.6, 0.5, 0.05, 0.09)], []]
        self.assertIsNone(stable_face(frames))

    def test_cam_dentro_do_frame(self):
        for face in (FaceBox(0.0, 0.0, 0.08, 0.14), FaceBox(0.9, 0.85, 0.08, 0.14), FaceBox(0.45, 0.4, 0.1, 0.18)):
            c = cam_from_face(face)
            self.assertGreaterEqual(c.x, 0)
            self.assertGreaterEqual(c.y, 0)
            self.assertLessEqual(c.x + c.w, 1.0001)
            self.assertLessEqual(c.y + c.h, 1.0001)
            # o rosto fica dentro da cam
            self.assertLessEqual(c.x, face.cx)
            self.assertGreaterEqual(c.x + c.w, face.cx)


if __name__ == "__main__":
    unittest.main()
