"""Ranking da análise de VOD (sem rede).

Rodar:  python -m unittest tests.test_vod_analise -v
"""
import unittest

from clipador.vod_analise import Candidato, juntar, picos_chat


def chat_falso():
    """15 min de chat normal (2 msg/s) com uma explosão de risada aos 600s."""
    chat = {}
    for t in range(0, 900, 15):
        if t == 600:
            chat[t] = {"dens": 14.0, "risada": 0.8, "msgs": ["KKKKKKKK"] * 30 + ["que isso"] * 5}
        else:
            chat[t] = {"dens": 2.0, "risada": 0.05, "msgs": ["oi", "boa"]}
    return chat


class TestVodAnalise(unittest.TestCase):
    def test_pico_de_chat(self):
        picos = picos_chat(chat_falso())
        self.assertEqual(len(picos), 1)
        self.assertEqual(picos[0].centro, 592)        # chat reage ~8s depois do momento
        self.assertGreaterEqual(picos[0].chat, 6)
        self.assertEqual(picos[0].mensagens[0][0], "KKKKKKKK")

    def test_clip_e_chat_juntos(self):
        chat = chat_falso()
        clips = [Candidato(centro=595, views=500, origem=["clip"], titulo_clip="o jacaré"),
                 Candidato(centro=200, views=20000, origem=["clip"], titulo_clip="que isso mandy?"),
                 Candidato(centro=610, views=100, origem=["clip"])]   # mesmo momento do primeiro
        cands = juntar(clips, picos_chat(chat), 10, chat)
        self.assertEqual(len(cands), 2)                # 3 clips + 1 pico -> 2 momentos
        # chat explodindo 7x + clip do público passa na frente de um clip só com views
        self.assertEqual([c.titulo_clip for c in cands], ["o jacaré", "que isso mandy?"])
        jac = cands[0]
        self.assertEqual(jac.views, 600)               # views do clip repetido somam
        self.assertIn("chat", jac.origem)
        self.assertGreaterEqual(jac.chat, 6)


if __name__ == "__main__":
    unittest.main()
