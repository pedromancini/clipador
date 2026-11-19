"""Vagas do canal do YouTube divididas entre os streamers, títulos/hashtags e versão horizontal.

Rodar:  python -m unittest tests.test_vagas -v
"""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipador.config import DEFAULT_YOUTUBE, load_config
from clipador.copywriter import hashtag_relacionada
from clipador.recorder import ffmpeg_bin
from clipador.youtube import ContaStats, YouTube, build_metadata, conta_teto, decide_conta, viral_minimo

YS = {**DEFAULT_YOUTUBE, "max_uploads_24h": {"principal": 10, "dante": 6}, "viral_minimo": [6.5, 7.5, 8.5],
      "account_min_minutes_between_posts": 20}


class TestVagas(unittest.TestCase):
    def test_teto_por_conta(self):
        self.assertEqual(conta_teto(YS, ""), 10)
        self.assertEqual(conta_teto(YS, "dante"), 6)
        self.assertEqual(conta_teto(YS, "outra"), 10)   # conta sem teto próprio usa o do principal

    def test_nota_sobe_conforme_enche(self):
        self.assertEqual(viral_minimo(YS, 0, 10), 6.5)
        self.assertEqual(viral_minimo(YS, 5, 10), 7.5)
        self.assertEqual(viral_minimo(YS, 9, 10), 8.5)

    def test_decide_conta(self):
        livre = ContaStats(ult24h=2, min_desde_ultimo=60)
        self.assertTrue(decide_conta(YS, "", livre, 7.0, "gemini").post)
        self.assertFalse(decide_conta(YS, "", livre, 6.0, "gemini").post)         # nota baixa
        self.assertFalse(decide_conta(YS, "", livre, 9.0, "local").post)          # título reserva
        self.assertFalse(decide_conta(YS, "", ContaStats(ult24h=10), 10, "gemini").post)   # cheio
        quase = ContaStats(ult24h=9, min_desde_ultimo=60)
        self.assertFalse(decide_conta(YS, "", quase, 8.0, "gemini").post)         # última vaga: só 8.5+
        self.assertTrue(decide_conta(YS, "", quase, 9.0, "gemini").post)
        recente = ContaStats(ult24h=2, min_desde_ultimo=5)
        self.assertFalse(decide_conta(YS, "", recente, 9.0, "gemini").post)       # canal postou há 5 min
        self.assertTrue(decide_conta(YS, "", recente, 9.0, "gemini", extremo=True).post)

    def test_conta_stats(self):
        yt = YouTube("id", "sec", "tok")
        agora = time.time()
        ups = [(agora - 100, "clipador:twitch/alanzoka"), (agora - 7200, "clipador:horizontal/alanzoka"),
               (agora - 30 * 3600, "velho"), (agora - 50, "postado à mão")]
        with mock.patch.object(yt, "_recent_uploads", return_value=ups) as m:
            cs = yt.conta_stats(now=agora)
            yt.conta_stats(now=agora + 10)   # cache: não relê
            self.assertEqual(m.call_count, 1)
        self.assertEqual(cs.ult24h, 3)
        self.assertEqual(cs.ult_hora, 2)
        self.assertEqual(cs.horizontais, 1)
        self.assertAlmostEqual(cs.min_desde_ultimo, 50 / 60, places=2)

    def test_metadata_horizontal(self):
        m = build_metadata("\"CAIU DO AVIÃO\" - alanzoka | alanzoka GTA 6", "alanzoka", "Twitch",
                           "https://www.twitch.tv/alanzoka", ["#gta6"], "twitch", "20", "public", False, short=False)
        self.assertNotIn("#Shorts", m["snippet"]["title"])
        self.assertNotIn("#shorts", m["snippet"]["description"])
        self.assertNotIn("shorts", m["snippet"]["tags"])
        s = build_metadata("x", "alanzoka", "Twitch", "u", ["#gta6"], "twitch", "20", "public", False)
        self.assertTrue(s["snippet"]["title"].endswith("#Shorts"))

    def test_hashtag_relacionada(self):
        ctx = "ele fugiu da polícia numa perseguição de helicóptero no GTA 6"
        self.assertTrue(hashtag_relacionada("#perseguicao", ctx, "alanzoka"))
        self.assertTrue(hashtag_relacionada("#gtavi", ctx, "alanzoka"))
        self.assertTrue(hashtag_relacionada("#alanzokaclips", ctx, "alanzoka"))
        self.assertFalse(hashtag_relacionada("#justvolleyball", ctx, "alanzoka"))
        self.assertFalse(hashtag_relacionada("#fy", ctx, "alanzoka"))

    def test_config_canais(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "channels.json"
        p.write_text(json.dumps({"settings": {"youtube": {"max_uploads_24h": 9}},
                                 "channels": [{"url": "https://kick.com/bistecone", "so_gta": True},
                                              {"url": "https://kick.com/allanstag", "horizontal": False}]}),
                     encoding="utf-8")
        c = load_config(p)
        self.assertTrue(c.find("kick", "bistecone").so_gta)
        self.assertFalse(c.find("kick", "allanstag").horizontal)
        self.assertEqual(c.settings["youtube"]["max_uploads_24h"], {"principal": 9})
        self.assertFalse(c.settings["youtube"]["horizontal"]["enabled"])   # padrão: desligada


class TestHorizontal(unittest.TestCase):
    def test_build_horizontal(self):
        from clipador.clipper import build_horizontal, media_duration
        d = Path(tempfile.mkdtemp())
        src = d / "seg.ts"
        subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=8",
                        "-f", "lavfi", "-i", "sine=d=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-bf", "0", "-c:a", "aac",
                        "-f", "mpegts",
                        str(src)], check=True)
        out = build_horizontal([(1000.0, src)], 1002.0, 5.0, d / "h.mp4", d / "w", watermark="kick.com/teste")
        self.assertTrue(out.exists())
        self.assertAlmostEqual(media_duration(out), 5.0, delta=0.5)
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(out)],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(info, "1920,1080")


if __name__ == "__main__":
    unittest.main()


class TestEdicao(unittest.TestCase):
    def test_hashtag_de_tiktok_nao_vai_pro_youtube(self):
        m = build_metadata("x", "allanstag", "Kick", "u", ["#allanstag", "#fyp", "#viral", "#gta6"], "kick", "20",
                           "public", False)
        self.assertNotIn("#fyp", m["snippet"]["description"])
        self.assertNotIn("viral", m["snippet"]["tags"])
        self.assertIn("allanstag", m["snippet"]["tags"])

    def test_reserva_por_conta(self):
        from clipador.youtube import reserva_conta
        self.assertEqual(reserva_conta({"reserva_live": {"principal": 4, "dante": 0}}, "dante"), 0)
        self.assertEqual(reserva_conta({"reserva_live": {"principal": 4, "dante": 0}}, ""), 4)
        self.assertEqual(reserva_conta({"reserva_live": 3}, "dante"), 3)

    def test_editar_final(self):
        from clipador.clipper import editar_final, media_duration
        d = Path(tempfile.mkdtemp())
        src = d / "v.mp4"
        subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30:d=6",
                        "-f", "lavfi", "-i", "sine=d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(src)], check=True)
        editar_final(src, d / "w", "ELE NÃO ESPERAVA ISSO", 3.0, "kick.com/teste", zoom=True)
        self.assertAlmostEqual(media_duration(src), 6.0, delta=0.5)
        self.assertEqual((d / "w" / "gancho.txt").read_bytes().count(b"\r"), 0)
