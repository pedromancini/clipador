"""Cada streamer posta no SEU canal do YouTube (alanzoka -> GTA VI Clips, allanstag -> @dantecllips).

Rodar:  python -m unittest tests.test_contas -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipador.config import ConfigError, load_config
from clipador.postar_lote import conta_do
from clipador.youtube import YouTube

ENV = {"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "sec",
       "YOUTUBE_REFRESH_TOKEN": "token-gta", "YOUTUBE_REFRESH_TOKEN_DANTE": "token-dante"}


def cfg(canais):
    d = tempfile.mkdtemp()
    p = Path(d) / "channels.json"
    p.write_text(json.dumps({"settings": {}, "channels": canais}), encoding="utf-8")
    return load_config(p)


class TestContas(unittest.TestCase):
    def test_config(self):
        c = cfg([{"url": "https://www.twitch.tv/alanzoka"},
                 {"url": "https://kick.com/allanstag", "youtube_conta": "Dante", "hashtags": ["#allanstag"]}])
        self.assertEqual(c.find("twitch", "alanzoka").youtube_conta, "")
        self.assertEqual(c.find("kick", "allanstag").youtube_conta, "dante")
        self.assertEqual(c.find("kick", "allanstag").hashtags, ["#allanstag"])

    def test_conta_invalida(self):
        with self.assertRaises(ConfigError):
            cfg([{"url": "https://kick.com/allanstag", "youtube_conta": "dan te"}])

    @mock.patch.dict(os.environ, ENV, clear=True)
    def test_cada_um_no_seu_canal(self):
        self.assertEqual(YouTube.from_env().refresh_token, "token-gta")
        d = YouTube.from_env(conta="dante")
        self.assertEqual(d.refresh_token, "token-dante")
        self.assertEqual(d.client_id, "id")  # mesmo app do Google

    @mock.patch.dict(os.environ, {k: v for k, v in ENV.items() if "DANTE" not in k}, clear=True)
    def test_sem_token_do_dante_nao_posta_no_gta(self):
        d = YouTube.from_env(conta="dante")
        self.assertFalse(d.configured)
        self.assertEqual(d.refresh_token, "")

    def test_channels_json_do_repo(self):
        c = load_config()
        # allanstag posta no canal principal (GTA VI Clips); o Dante virou o canal gringo (Kai Cenat)
        self.assertEqual(c.find("kick", "allanstag").youtube_conta, "")
        self.assertEqual(c.find("twitch", "kaicenat").youtube_conta, "dante")
        self.assertEqual(c.find("twitch", "alanzoka").youtube_conta, "")
        self.assertEqual(conta_do({"arquivo": "twitch-kaicenat_20261118-210000.mp4"}), "dante")
        self.assertEqual(conta_do({"arquivo": "kick-allanstag_20261118-210000.mp4"}), "")
        self.assertEqual(conta_do({"arquivo": "twitch-alanzoka_20261118-210000.mp4"}), "")


if __name__ == "__main__":
    unittest.main()
