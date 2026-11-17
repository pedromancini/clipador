"""Testes do detector de hype com chat sintético.

Rodar:  python -m unittest tests.test_detector -v
"""
import random
import unittest

from clipador.config import DEFAULT_SETTINGS
from clipador.detector import HypeDetector, clean_text, message_weight, normalize_for_top

T0 = 1_700_000_000  # epoch arbitrário

NORMAIS = ["boa", "que jogada", "salve salve", "alguém sabe a música?", "primeira vez aqui",
           "vai dar bom", "o cara é bom", "eae chat", "tá lagado?", "quando volta o gta?"]
HYPE = ["KKKKKKKKKK", "kkkkkkk", "CLIP", "!clip", "KEKW KEKW KEKW", "[emote:37226:KEKW]",
        "caralho", "NOSSA MANO", "mds kkkk", "OMEGALUL", "CLIPA ISSO"]


def simular(det: HypeDetector, duracao: int, msgs_por_seg: float, picos: dict[int, tuple[int, int]], seed=1):
    """Gera chat sintético. picos = {segundo_inicio: (duracao, msgs_por_seg)}. Retorna os Peaks."""
    rnd = random.Random(seed)
    det.start(T0)
    achados = []
    for s in range(duracao):
        taxa, lista = msgs_por_seg, NORMAIS
        for ini, (dur, tx) in picos.items():
            if ini <= s < ini + dur:
                taxa, lista = tx, HYPE
        # número de mensagens neste segundo (Poisson aproximado)
        n = sum(1 for _ in range(int(taxa * 3)) if rnd.random() < 1 / 3)
        for _ in range(n):
            det.add(rnd.choice(lista), T0 + s + rnd.random())
        p = det.tick(T0 + s + 1)
        if p:
            achados.append(p)
    return achados


class TestPesos(unittest.TestCase):
    def test_peso_normal(self):
        self.assertEqual(message_weight("boa jogada"), 1.0)

    def test_peso_hype(self):
        self.assertGreater(message_weight("KKKKKKK"), 1.5)
        self.assertGreater(message_weight("!clip"), 2.0)
        self.assertGreater(message_weight("KEKW KEKW KEKW"), 2.0)
        self.assertGreater(message_weight("[emote:37226:KEKW]"), 1.5)
        self.assertGreater(message_weight("caralho"), 1.5)
        self.assertLessEqual(message_weight("!CLIP KKKKKK CARALHO KEKW KEKW KEKW"), 4.0)

    def test_vazio(self):
        self.assertEqual(message_weight(""), 0.0)
        self.assertEqual(message_weight("   "), 0.0)

    def test_emote_kick(self):
        self.assertEqual(clean_text("oi [emote:1:KEKW] [emote:2:LUL]"), "oi KEKW LUL")

    def test_normaliza(self):
        self.assertEqual(normalize_for_top("kkkkkkkkkkk"), "KKKK")
        self.assertEqual(normalize_for_top("KKKKK"), "KKKK")
        self.assertEqual(normalize_for_top("clip"), "CLIP")


class TestDeteccao(unittest.TestCase):
    def novo(self, **kw):
        s = dict(DEFAULT_SETTINGS)
        s.update(kw)
        return HypeDetector.from_settings(s)

    def test_detecta_pico(self):
        det = self.novo()
        picos = simular(det, 400, 2, {200: (10, 15)})
        self.assertEqual(len(picos), 1, f"esperava 1 pico, veio {[p.t - T0 for p in picos]}")
        p = picos[0]
        self.assertTrue(200 <= p.t - T0 <= 210, f"pico no segundo {p.t - T0}")
        self.assertGreaterEqual(p.score, p.threshold)
        textos = [m for m, _ in p.top_messages]
        self.assertTrue(any(m in textos for m in ("KKKK", "CLIP", "!CLIP", "KEKW KEKW KEKW", "KEKW")), textos)

    def test_sem_falso_positivo(self):
        det = self.novo()
        picos = simular(det, 900, 3, {})
        self.assertEqual(picos, [], f"falso positivo em {[p.t - T0 for p in picos]}")

    def test_aquecimento(self):
        det = self.novo()
        picos = simular(det, 120, 1, {20: (10, 20)})  # pico antes dos 60s de aquecimento
        self.assertEqual(picos, [])

    def test_cooldown(self):
        det = self.novo(cooldown_seconds=45)
        # dois picos com 20s de diferença: só o primeiro conta
        picos = simular(det, 400, 2, {200: (8, 15), 220: (8, 15)})
        self.assertEqual(len(picos), 1)
        # dois picos com 120s de diferença: os dois contam
        det = self.novo(cooldown_seconds=45)
        picos = simular(det, 500, 2, {200: (8, 15), 320: (8, 15)})
        self.assertEqual(len(picos), 2, [p.t - T0 for p in picos])

    def test_chat_pequeno_min_msgs(self):
        # chat quase parado: baseline ~0, então quem segura é o min_msgs
        s = dict(DEFAULT_SETTINGS)
        det = HypeDetector.from_settings(s, min_msgs=6)
        picos = simular(det, 400, 0.1, {200: (6, 2)})
        self.assertEqual(len(picos), 1)
        # com min_msgs alto, o mesmo chat não dispara
        det = HypeDetector.from_settings(s, min_msgs=40)
        self.assertEqual(simular(det, 400, 0.1, {200: (6, 2)}), [])

    def test_chat_grande_usa_baseline(self):
        # chat enorme (30 msg/s): min_msgs é irrelevante, só dispara com aumento real
        det = self.novo()
        self.assertEqual(simular(det, 600, 30, {}), [])
        det = self.novo()
        self.assertEqual(len(simular(det, 600, 30, {400: (10, 120)})), 1)

    def test_mensagem_atrasada(self):
        det = self.novo()
        det.start(T0)
        det.tick(T0 + 100)
        det.add("kkkk", T0 + 10)  # timestamp no passado não pode quebrar nada
        self.assertIsNone(det.tick(T0 + 102))

    def test_force_peak(self):
        det = self.novo()
        det.start(T0)
        det.add("CLIP", T0 + 5)
        p = det.force_peak(T0 + 6)
        self.assertTrue(p.forced)
        self.assertEqual(p.top_messages[0][0], "CLIP")


class TestAuge(unittest.TestCase):
    def test_ratio_usa_o_auge_do_pico(self):
        from clipador.detector import HypeDetector
        d = HypeDetector(window_seconds=4, baseline_seconds=60, peak_multiplier=3, min_msgs=5,
                         warmup_seconds=10, cooldown_seconds=30)
        t0 = 1_000_000
        for s in range(30):              # chat normal: 2 msgs/s
            for _ in range(2):
                d.add("oi", t0 + s)
            d.tick(t0 + s + 1)
        pico = None
        for s in range(30, 45):          # explode: 10 -> 40 msgs/s
            for _ in range(10 if s < 33 else 40):
                d.add("oi", t0 + s)
            p = d.tick(t0 + s + 1)
            pico = pico or p
        self.assertIsNotNone(pico)
        self.assertGreater(pico.max_score, pico.score)
        self.assertGreater(pico.ratio, pico.score / pico.baseline)


if __name__ == "__main__":
    unittest.main()
