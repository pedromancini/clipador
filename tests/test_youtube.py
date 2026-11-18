"""Testes da postagem no YouTube (API simulada, sem rede).

Rodar:  python -m unittest tests.test_youtube -v
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from clipador.config import DEFAULT_YOUTUBE
from clipador.youtube import PostStats, YouTube, YouTubeError, YouTubeLimitError, build_metadata, decide, marker


def resp(status=200, body=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    r.headers = headers or {}
    return r


class TestDecisao(unittest.TestCase):
    def ys(self, **kw):
        s = dict(DEFAULT_YOUTUBE, enabled=True)
        s.update(kw)
        return s

    def test_desligado(self):
        self.assertFalse(decide(dict(DEFAULT_YOUTUBE), True, 9, False, 0).post)

    def test_canal_desligado(self):
        self.assertFalse(decide(self.ys(), False, 9, False, 0).post)

    def test_hype_fraco_nao_posta(self):
        d = decide(self.ys(min_hype_ratio=4), True, 3.2, False, 0)
        self.assertFalse(d.post)
        self.assertIn("3.2x < 4x", d.reason)

    def test_hype_forte_posta(self):
        self.assertTrue(decide(self.ys(min_hype_ratio=4), True, 5.0, False, 0).post)
        self.assertTrue(decide(self.ys(), True, float("inf"), False, 0).post)  # baseline 0

    def test_limite_diario(self):
        d = decide(self.ys(max_posts_per_day=6), True, 9, False, 6)
        self.assertFalse(d.post)
        self.assertIn("limite", d.reason)

    def test_ritmo_2_por_hora(self):
        ys = self.ys()
        self.assertTrue(decide(ys, True, 5, False, PostStats(today=3, last_hour=1, minutes_since_last=25)).post)
        d = decide(ys, True, 5, False, PostStats(today=3, last_hour=2, minutes_since_last=25))
        self.assertFalse(d.post)
        self.assertIn("última hora", d.reason)

    def test_espacamento(self):
        d = decide(self.ys(), True, 5, False, PostStats(today=1, last_hour=1, minutes_since_last=8))
        self.assertFalse(d.post)
        self.assertIn("espera 20 min", d.reason)

    def test_chat_explodiu_libera_terceiro(self):
        st = PostStats(today=4, last_hour=2, minutes_since_last=3)
        d = decide(self.ys(), True, 9.5, False, st)
        self.assertTrue(d.post)
        self.assertTrue(d.extreme)
        st.last_hour = 3
        self.assertFalse(decide(self.ys(), True, 9.5, False, st).post)

    def test_teste_once(self):
        self.assertFalse(decide(self.ys(), True, 0, True, 0).post)
        self.assertTrue(decide(self.ys(), True, 0, True, 0, allow_forced=True).post)


class TestMetadata(unittest.TestCase):
    def test_titulo_e_descricao(self):
        m = build_metadata("“Que jogada <insana>” 😂 gaules", "gaules", "Kick", "https://kick.com/gaules",
                           ["#gta6", "#clips", "#gaules"], "kick", "20", "public", False)
        t = m["snippet"]["title"]
        self.assertTrue(t.endswith(" #Shorts"))
        self.assertNotIn("<", t)
        self.assertLessEqual(len(t), 100)
        self.assertIn("https://kick.com/gaules", m["snippet"]["description"])
        self.assertIn(marker("kick", "gaules"), m["snippet"]["description"])
        self.assertEqual(m["status"]["privacyStatus"], "public")
        self.assertFalse(m["status"]["selfDeclaredMadeForKids"])

    def test_titulo_longo_corta(self):
        m = build_metadata("x" * 300, "c", "Twitch", "u", [], "twitch", "20", "unlisted", False)
        self.assertLessEqual(len(m["snippet"]["title"]), 100)


class TestApi(unittest.TestCase):
    def yt(self):
        return YouTube("id", "secret", "refresh")

    @mock.patch("clipador.youtube.requests")
    def test_conta_posts_de_hoje(self, req):
        hoje = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req.post.return_value = resp(body={"access_token": "tok", "expires_in": 3600})
        req.get.side_effect = [
            resp(body={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}),
            resp(body={"items": [
                {"snippet": {"description": "a\nclipador:kick/gaules", "publishedAt": hoje}},
                {"snippet": {"description": "b\nclipador:kick/gaules", "publishedAt": "2020-01-01T00:00:00Z"}},
                {"snippet": {"description": "c\nclipador:twitch/outro", "publishedAt": hoje}},
            ]}),
        ]
        yt = self.yt()
        self.assertEqual(yt.posted_today("kick", "gaules"), 1)
        self.assertEqual(yt.posted_today("kick", "gaules"), 1)  # em cache: não consulta de novo
        self.assertEqual(req.get.call_count, 2)
        st = yt.stats("kick", "gaules")
        self.assertEqual(st.last_hour, 1)
        self.assertLess(st.minutes_since_last, 1)
        yt._increment("kick", "gaules")
        self.assertEqual(yt.stats("kick", "gaules").last_hour, 2)

    @mock.patch("clipador.youtube.requests")
    def test_upload_resumavel(self, req):
        req.post.side_effect = [
            resp(body={"access_token": "tok", "expires_in": 3600}),
            resp(200, headers={"Location": "https://upload/abc"}),
        ]
        req.put.return_value = resp(200, body={"id": "VID123", "status": {"privacyStatus": "public"}})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.mp4"
            f.write_bytes(b"0" * 1000)
            meta = build_metadata("t", "gaules", "Kick", "u", [], "kick", "20", "public", False)
            url = self.yt().upload(f, meta, "kick", "gaules")
        self.assertEqual(url, "https://youtube.com/shorts/VID123")
        init_kwargs = req.post.call_args_list[1].kwargs
        self.assertEqual(init_kwargs["params"]["uploadType"], "resumable")
        self.assertEqual(init_kwargs["headers"]["X-Upload-Content-Length"], "1000")

    @mock.patch("clipador.youtube.requests")
    def test_token_invalido(self, req):
        req.post.return_value = resp(400, body={"error": "invalid_grant"})
        with self.assertRaises(YouTubeError) as ctx:
            self.yt()._token()
        self.assertIn("youtube_auth", str(ctx.exception))

    @mock.patch("clipador.youtube.requests")
    def test_cota_esgotada(self, req):
        req.post.side_effect = [
            resp(body={"access_token": "tok", "expires_in": 3600}),
            resp(403, body={"error": {"errors": [{"reason": "quotaExceeded"}]}}),
        ]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.mp4"
            f.write_bytes(b"0")
            meta = build_metadata("t", "c", "Kick", "u", [], "kick", "20", "public", False)
            with self.assertRaises(YouTubeError) as ctx:
                self.yt().upload(f, meta, "kick", "c")
        self.assertIn("cota", str(ctx.exception))

    @mock.patch("clipador.youtube.requests")
    def test_limite_de_envios_do_canal(self, req):
        req.post.side_effect = [
            resp(body={"access_token": "tok", "expires_in": 3600}),
            resp(400, body={"error": {"errors": [{"reason": "uploadLimitExceeded"}]}}),
        ]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.mp4"
            f.write_bytes(b"0")
            meta = build_metadata("t", "c", "Kick", "u", [], "kick", "20", "public", False)
            with self.assertRaises(YouTubeLimitError):
                self.yt().upload(f, meta, "kick", "c")

    def test_nao_configurado(self):
        self.assertFalse(YouTube(None, None, None).configured)


if __name__ == "__main__":
    unittest.main()
