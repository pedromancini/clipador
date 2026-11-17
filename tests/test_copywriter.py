"""Testes do título/legenda dos clips (Gemini simulado, sem rede).

Rodar:  python -m unittest tests.test_copywriter -v
"""
import json
import unittest
from unittest import mock

from clipador.copywriter import Copywriter, local_copy
from clipador.tiktok_baixar import Clip, legenda

TAGS = ["#gta6", "#gta", "#clips", "#cortes", "#fyp", "#viral"]


def resposta(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    return r


def gemini(texto: str):
    return resposta(body={"candidates": [{"content": {"parts": [{"text": texto}]}}]})


class TestLocal(unittest.TestCase):
    def test_risada_nao_cita_fala_errada(self):
        c = local_copy("alanzoka", [], 25, [("KKKK", 20), ("OMEGALUL", 4)], "Just Chatting", "vôlei", TAGS)
        self.assertIn("alanzoka", c.titulo)
        self.assertIn("😂", c.titulo)
        self.assertNotIn("\"", c.titulo)
        self.assertNotIn("#gta6", c.hashtags)  # live não é de GTA: sem hashtag enganosa

    def test_gta6_quando_a_live_e_de_gta(self):
        c = local_copy("alanzoka", [], 25, [("D:", 6)], "Grand Theft Auto VI", "zerando", TAGS)
        self.assertTrue(c.titulo.startswith("GTA 6: "))
        self.assertIn("#gta6", c.hashtags)
        self.assertTrue(c.sobre_gta6)


class TestGemini(unittest.TestCase):
    def cw(self):
        return Copywriter("chave", model="gemini-teste")

    @mock.patch("requests.post")
    def test_usa_resposta_da_ia(self, post):
        post.return_value = gemini(json.dumps({
            "titulo": "alanzoka sobre o GTA 6: \"parece apresentação de PowerPoint\" 💀",
            "legenda": "A Rockstar só solta imagem... Concorda? #gta6",
            "sobre_gta6": True, "hashtags": ["#rockstar", "#gta6"], "viral": 8.5}))
        c = self.cw().make("alanzoka", [], "texto", None, 25, [("KKKK", 9)], "Just Chatting", "x", TAGS)
        self.assertEqual(c.fonte, "gemini")
        self.assertIn("PowerPoint", c.titulo)
        self.assertNotIn("#", c.legenda)          # hashtags ficam só na lista
        self.assertEqual(c.viral, 8.5)
        self.assertEqual(c.hashtags[:3], ["#alanzoka", "#gta6", "#gtavi"])
        self.assertIn("#rockstar", c.hashtags)

    @mock.patch("requests.post")
    def test_ia_diz_que_nao_e_gta_tira_hashtag(self, post):
        post.return_value = gemini('{"titulo": "alanzoka perdeu a paciência no vôlei 😂", "legenda": "x?", '
                                   '"sobre_gta6": false, "hashtags": ["#volei", "#gta6"], "viral": 5}')
        c = self.cw().make("alanzoka", [], "t", None, 25, [], "Just Chatting", "semana de gta 6", TAGS)
        self.assertNotIn("#gta6", c.hashtags)
        self.assertNotIn("#gta", c.hashtags)
        self.assertIn("#volei", c.hashtags)

    @mock.patch("requests.post")
    def test_ia_fora_do_ar_usa_plano_b(self, post):
        post.return_value = resposta(403, {"error": "PERMISSION_DENIED"})
        c = self.cw().make("alanzoka", [], "t", None, 25, [("KKKK", 5)], "x", "y", TAGS)
        self.assertEqual(c.fonte, "local")
        self.assertIn("alanzoka", c.titulo)

    @mock.patch("time.sleep")
    @mock.patch("requests.post")
    def test_429_tenta_de_novo(self, post, _sleep):
        post.side_effect = [resposta(429, {}), gemini('{"titulo": "título bom do clip", "legenda": "oi?", '
                                                      '"sobre_gta6": false, "hashtags": [], "viral": 6}')]
        c = self.cw().make("alanzoka", [], "t", None, 25, [], "x", "y", TAGS)
        self.assertEqual(c.titulo, "título bom do clip")

    def test_sem_chave_nao_chama_api(self):
        with mock.patch("requests.post") as post:
            c = Copywriter("").make("alanzoka", [], "t", None, 25, [], "x", "y", TAGS)
        post.assert_not_called()
        self.assertEqual(c.fonte, "local")


class TestTxtTikTok(unittest.TestCase):
    def test_legenda_da_ia_no_txt(self):
        c = Clip("twitch-alanzoka_20261116-211240.mp4", b"",
                 {"titulo": "alanzoka sobre o GTA 6", "legenda": "A Rockstar só solta imagem. Concorda?",
                  "hashtags": ["#alanzoka", "#gta6"], "url_canal": "https://www.twitch.tv/alanzoka"})
        txt = legenda(c)
        self.assertTrue(txt.startswith("A Rockstar só solta imagem. Concorda?"))
        self.assertIn("#gta6", txt)


if __name__ == "__main__":
    unittest.main()
