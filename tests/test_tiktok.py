"""Testes do baixador de clips pro TikTok (sem rede).

Rodar:  python -m unittest tests.test_tiktok -v
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from clipador.tiktok_baixar import Clip, agendar, legenda, nome_seguro, proximos_horarios, salvar


class TestAgenda(unittest.TestCase):
    H = ["12:00", "18:00", "21:00"]

    def test_horarios_livres_hoje_e_amanha(self):
        agora = datetime(2026, 11, 17, 17, 50)
        livres = proximos_horarios(agora, self.H, set())
        # 18:00 está a menos de 20 min: pula
        self.assertEqual(livres[0], datetime(2026, 11, 17, 21, 0))
        self.assertEqual(len(livres), 4)  # 21h hoje + 3 amanhã

    def test_pula_ocupados(self):
        agora = datetime(2026, 11, 17, 8, 0)
        livres = proximos_horarios(agora, self.H, {"2026-11-17 12:00"})
        self.assertEqual(livres[0], datetime(2026, 11, 17, 18, 0))

    def test_mais_quente_no_horario_mais_cedo(self):
        clips = [Clip("a.mp4", b"", {"hype": 4.1}), Clip("b.mp4", b"", {"hype": 9.0}),
                 Clip("c.mp4", b"", {"hype": 5.0})]
        livres = [datetime(2026, 11, 17, 18), datetime(2026, 11, 17, 21)]
        ag = agendar(clips, livres)
        self.assertEqual([(c.nome, q) for c, q in ag],
                         [("b.mp4", livres[0]), ("c.mp4", livres[1]), ("a.mp4", None)])


class TestArquivos(unittest.TestCase):
    def test_nome_seguro(self):
        self.assertEqual(nome_seguro('GTA 6: ele "caiu" <de novo>? #fyp'), "GTA 6 ele caiu de novo fyp")
        self.assertEqual(nome_seguro("???"), "clip")

    def test_legenda_sem_json_usa_nome_do_arquivo(self):
        c = Clip("twitch-alanzoka_20261116-194147.mp4", b"", {})
        txt = legenda(c)
        self.assertIn("twitch.tv/alanzoka", txt)
        self.assertIn("#alanzoka", txt)
        self.assertEqual(c.quando, datetime(2026, 11, 16, 19, 41, 47))

    def test_salvar_pasta_do_dia_e_txt(self):
        c = Clip("twitch-alanzoka_20261116-194147.mp4", b"video",
                 {"titulo": "GTA 6: alanzoka foi preso", "hashtags": ["#gta6", "#alanzoka"],
                  "url_canal": "https://www.twitch.tv/alanzoka", "hype": 7.2})
        with tempfile.TemporaryDirectory() as d:
            p = salvar(Path(d), c, datetime(2026, 11, 19, 19, 30))
            self.assertEqual(p.parent.name, "2026-11-19 (quinta)")
            self.assertEqual(p.name, "19h30 - GTA 6 alanzoka foi preso.mp4")
            txt = p.with_suffix(".txt").read_text(encoding="utf-8-sig")
            self.assertIn("POSTAR: quinta 19/11 às 19h30", txt)
            self.assertIn("GTA 6: alanzoka foi preso", txt)
            self.assertIn("Créditos: twitch.tv/alanzoka", txt)
            self.assertIn("7.2x", txt)
            extra = salvar(Path(d), c, None)
            self.assertEqual(extra.parent.name, "extras")


if __name__ == "__main__":
    unittest.main()
