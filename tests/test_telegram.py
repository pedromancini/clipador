"""Testes da legenda da mensagem do Telegram (sem rede).

Rodar:  python -m unittest tests.test_telegram -v
"""
import unittest

from clipador.detector import Peak
from clipador.telegram import CAPTION_LIMIT, Telegram, build_caption, build_hashtags, suggest_title


class TestTelegram(unittest.TestCase):
    def peak(self, **kw):
        base = dict(t=1794848201.0, score=81.8, baseline=16.0, threshold=40.0,
                    top_messages=[("KKKK", 8), ("!CLIP", 6), ("CLIP", 4)])
        base.update(kw)
        return Peak(**base)

    def test_caption_basica(self):
        cap = build_caption("gaules", "Kick", self.peak(), "Meu Deus do céu, que jogada! Ele fez o gol.",
                            "Tribo <Miami>", "IRL", ["#gta6", "#clips"])
        self.assertIn("<b>gaules</b>", cap)
        self.assertIn("5.1x o normal", cap)
        self.assertIn("KKKK ×8", cap)
        self.assertIn("&lt;Miami&gt;", cap)  # HTML escapado
        self.assertIn("“Meu Deus do céu, que jogada!”", cap)
        self.assertIn("#gta6 #clips #gaules #irl", cap)
        self.assertLessEqual(len(cap), CAPTION_LIMIT)

    def test_caption_gigante_cabe(self):
        top = [("x" * 60 + str(i), 99) for i in range(5)]
        cap = build_caption("canal", "Twitch", self.peak(top_messages=top), "", "T" * 500, "C" * 200,
                            ["#a" * 50])
        self.assertLessEqual(len(cap), CAPTION_LIMIT)
        self.assertIn("Pra colar no TikTok", cap)

    def test_hashtags_sem_repetir(self):
        tags = build_hashtags(["#gta6", "#GTA6", "#clips"], "gaules", "Grand Theft Auto V")
        self.assertEqual(tags, ["#gta6", "#clips", "#gaules", "#grandtheftautov"])

    def test_titulo_sem_fala(self):
        self.assertIn("não aguentou", suggest_title("x", "", [("KKKK", 5)], None))
        self.assertIn("clip", suggest_title("x", "", [("!CLIP", 5)], None))
        self.assertIn("Live top", suggest_title("x", "", [], "Live top"))

    def test_prefixo_gta6(self):
        t = suggest_title("alanzoka", "", [("KKKK", 5)], None, "Grand Theft Auto VI")
        self.assertTrue(t.startswith("GTA 6: "), t)
        self.assertTrue(suggest_title("x", "", [], "live", "GTA 6").startswith("GTA 6: "))
        self.assertFalse(suggest_title("x", "", [], "live", "Grand Theft Auto V").startswith("GTA 6"))
        self.assertFalse(suggest_title("x", "", [], "live", "Just Chatting").startswith("GTA 6"))

    def test_desativado_sem_token(self):
        tg = Telegram(None, None)
        self.assertFalse(tg.enabled)
        tg.send_message("teste")  # não levanta exceção


if __name__ == "__main__":
    unittest.main()
