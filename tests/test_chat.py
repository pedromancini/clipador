"""Testes dos parsers de chat, da escolha de qualidade e dos segmentos (sem rede).

Rodar:  python -m unittest tests.test_chat -v
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from clipador.chat.kick import parse_chat_event
from clipador.chat.twitch import parse_privmsg
from clipador.recorder import Recorder, list_segments, segment_epoch, select_segments
from clipador.stream import pick_quality


class TestTwitch(unittest.TestCase):
    def test_privmsg(self):
        l = ":fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #gaules :KKKKKKK que isso"
        self.assertEqual(parse_privmsg(l), ("fulano", "KKKKKKK que isso"))

    def test_privmsg_com_tags(self):
        l = "@badge-info=;color=#FF0000 :ciclano!ciclano@ciclano.tmi.twitch.tv PRIVMSG #canal :oi : tudo"
        self.assertEqual(parse_privmsg(l), ("ciclano", "oi : tudo"))

    def test_action(self):
        l = ":a!a@a.tmi.twitch.tv PRIVMSG #c :\x01ACTION dança\x01"
        self.assertEqual(parse_privmsg(l), ("a", "dança"))

    def test_outras_linhas(self):
        self.assertIsNone(parse_privmsg("PING :tmi.twitch.tv"))
        self.assertIsNone(parse_privmsg(":tmi.twitch.tv ROOMSTATE #gaules"))


class TestKick(unittest.TestCase):
    def test_evento(self):
        data = json.dumps({"id": "x", "content": "[emote:37226:KEKW] boa", "sender": {"username": "Yagher"}})
        self.assertEqual(parse_chat_event(data), ("Yagher", "[emote:37226:KEKW] boa"))

    def test_evento_vazio(self):
        self.assertIsNone(parse_chat_event({"content": ""}))


class TestQualidade(unittest.TestCase):
    def test_escolhe_1080(self):
        nomes = ["160p", "360p", "480p", "720p60", "1080p60", "1440p60", "best", "worst"]
        self.assertEqual(pick_quality(nomes, 1080), "1080p60")
        self.assertEqual(pick_quality(nomes, 720), "720p60")

    def test_prefere_fps_maior(self):
        self.assertEqual(pick_quality(["720p", "720p60"], 1080), "720p60")

    def test_fallback_best(self):
        self.assertEqual(pick_quality(["source", "best"], 1080), "best")


class TestSegmentos(unittest.TestCase):
    def test_nomes(self):
        self.assertEqual(segment_epoch("seg_1794848201.ts"), 1794848201.0)
        local = time.mktime(time.strptime("20261116-135822", "%Y%m%d-%H%M%S"))
        self.assertEqual(segment_epoch("seg_20261116-135822.ts"), local)
        self.assertIsNone(segment_epoch("outro.ts"))

    def test_selecao(self):
        segs = [(100.0, Path("a")), (105.0, Path("b")), (110.0, Path("c")), (115.0, Path("d"))]
        self.assertEqual([p.name for _, p in select_segments(segs, 103, 111)], ["a", "b", "c"])
        self.assertEqual([p.name for _, p in select_segments(segs, 116, 130)], ["d"])

    def test_limpeza_respeita_protecao(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            B = 1_794_848_000  # epoch realista (10 dígitos)
            for e in (0, 5, 10, 15, 20, 300):
                (d / f"seg_{B + e}.ts").write_bytes(b"x")
            rec = Recorder(d, lambda: None, buffer_seconds=180)
            tok = rec.protect(B + 10, B + 12)  # protege ~[B, B+22] (margem de 2 segmentos)
            self.assertEqual(rec.cleanup(now=B + 400), 0)
            rec.release(tok)
            self.assertEqual(rec.cleanup(now=B + 400), 5)  # o último (B+300) nunca é apagado
            self.assertEqual([e for e, _ in list_segments(d)], [float(B + 300)])


class TestLinkDaLive(unittest.TestCase):
    def test_links(self):
        from clipador.config import parse_channel_ref
        casos = {
            "https://www.twitch.tv/Gaules": ("twitch", "gaules"),
            "twitch.tv/gaules": ("twitch", "gaules"),
            "https://m.twitch.tv/gaules?sr=a": ("twitch", "gaules"),
            "https://kick.com/gaules": ("kick", "gaules"),
            "kick.com/loud_coringa/": ("kick", "loud_coringa"),
            "kick/gaules": ("kick", "gaules"),
            " twitch/coreano ": ("twitch", "coreano"),
        }
        for link, esperado in casos.items():
            self.assertEqual(parse_channel_ref(link), esperado, link)

    def test_vod_e_lixo(self):
        from clipador.config import ConfigError, parse_channel_ref
        for ruim in ("https://www.twitch.tv/videos/123456", "https://kick.com/gaules/videos/abc",
                     "https://youtube.com/watch?v=x", "", "gaules"):
            with self.assertRaises(ConfigError, msg=ruim):
                parse_channel_ref(ruim)


if __name__ == "__main__":
    unittest.main()
