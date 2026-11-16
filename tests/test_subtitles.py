"""Testes da geração de legenda .ass (sem rodar o Whisper).

Rodar:  python -m unittest tests.test_subtitles -v
"""
import unittest

from clipador.subtitles import Word, _ts, build_ass, chunk_words


def palavras(texto: str, inicio=0.0, dur=0.3, gap=0.05):
    out, t = [], inicio
    for p in texto.split():
        out.append(Word(t, t + dur, p))
        t += dur + gap
    return out


class TestLegenda(unittest.TestCase):
    def test_chunks_de_2_a_4(self):
        ws = palavras("meu deus do céu que jogada inacreditável ele passou por todo mundo")
        chunks = chunk_words(ws)
        self.assertTrue(all(1 <= len(c) <= 4 for c in chunks), [[w.text for w in c] for c in chunks])
        self.assertEqual(sum(len(c) for c in chunks), len(ws))

    def test_quebra_em_pausa(self):
        ws = palavras("olha isso") + palavras("caramba", inicio=5.0)
        chunks = chunk_words(ws)
        self.assertEqual([w.text for w in chunks[-1]], ["caramba"])

    def test_quebra_em_pontuacao(self):
        ws = palavras("foi gol! que isso")
        chunks = chunk_words(ws)
        self.assertEqual([w.text for w in chunks[0]], ["foi", "gol!"])

    def test_ass_maiusculo_e_destaque(self):
        ass = build_ass(palavras("que jogada inacreditável."), 10)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Style: TikTok,Anton", ass)
        dialogos = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(dialogos), 3)  # um evento por palavra (destaque andando)
        self.assertIn(r"{\c&H00FFFF&}QUE{\c&HFFFFFF&}", dialogos[0])
        self.assertNotIn("INACREDITÁVEL.", ass)  # ponto final removido
        self.assertIn("INACREDITÁVEL", ass)

    def test_ass_escapa_chaves(self):
        ass = build_ass([Word(0, 1, "{oi}\\n")], 5)
        self.assertNotIn("{OI}", ass)
        self.assertIn("OIN", ass)

    def test_tempo(self):
        self.assertEqual(_ts(0), "0:00:00.00")
        self.assertEqual(_ts(61.5), "0:01:01.50")
        self.assertEqual(_ts(3725.999), "1:02:06.00")

    def test_sem_palavras(self):
        ass = build_ass([], 10)
        self.assertNotIn("Dialogue:", ass)



class TestPosicao(unittest.TestCase):
    def test_meio(self):
        ass = build_ass(palavras("olha isso"), 5, font_size=80, center_y=590)
        self.assertIn("Style: TikTok,Anton,80,", ass)
        self.assertIn(r"\an5\pos(540,590)", ass)
        self.assertIn(",5,60,60,", ass)  # alinhamento 5 (centro)

    def test_embaixo(self):
        ass = build_ass(palavras("olha isso"), 5)
        self.assertNotIn(r"\pos(", ass)
        self.assertIn(",2,60,60,", ass)


if __name__ == "__main__":
    unittest.main()
