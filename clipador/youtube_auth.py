"""Autoriza o clipador a postar no SEU canal do YouTube (rodar UMA vez, no seu PC).

    python -m clipador.youtube_auth caminho/do/client_secret.json            (canal principal)
    python -m clipador.youtube_auth caminho/do/client_secret.json dante      (outro canal: conta "dante")

1. Abre o navegador na tela de login do Google.
2. Você escolhe a conta/canal do YouTube e autoriza (o Google avisa "app não verificado":
   clique em "Avançado" > "Acessar clipador (não seguro)" — o app é seu, isso é normal).
3. Ele imprime os 3 valores pra colocar como secrets no GitHub.

O client_secret.json vem do Google Cloud (credencial OAuth do tipo "App para computador").
Passo a passo completo no README.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import requests

from .youtube import SCOPES, TOKEN_URL, YouTube

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # sem o caminho do client_secret (ou só o nome da conta): usa o client_secret*.json da pasta do projeto
    if not argv or not Path(argv[0]).is_file():
        achados = sorted(Path(".").glob("client_secret*.json")) or sorted(
            Path(__file__).resolve().parent.parent.glob("client_secret*.json"))
        if not achados:
            print(__doc__)
            return 2
        argv = [str(achados[0]), *argv]
    conta = argv[1].strip().lower() if len(argv) > 1 else ""
    suf = f"_{conta.upper()}" if conta else ""
    with open(argv[0], encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw.get("installed") or raw.get("web")
    if not cfg:
        print("client_secret.json inválido: crie a credencial como 'App para computador' (Desktop app).")
        return 2
    client_id, client_secret = cfg["client_id"], cfg["client_secret"]

    porta = _porta_livre()
    redirect = f"http://127.0.0.1:{porta}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline",
        "prompt": "select_account consent",   # sempre mostra a escolha de conta/canal (vários canais na mesma conta)
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })

    resultado: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                resultado["code"] = q.get("code", [""])[0]
                resultado["error"] = q.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Autorizado! Pode fechar esta aba e voltar pro terminal." if resultado.get("code") \
                else f"Falhou: {resultado.get('error') or 'resposta inesperada'}"
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", porta), Handler)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    print("Abrindo o navegador para autorizar... Se não abrir, copie este link:\n")
    print(url + "\n")
    webbrowser.open(url)
    t.join(timeout=300)
    srv.server_close()

    if not resultado.get("code"):
        print(f"Não recebi a autorização ({resultado.get('error') or 'tempo esgotado'}).")
        return 1
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret, "code": resultado["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect}, timeout=30)
    if r.status_code != 200:
        print(f"Falha ao trocar o código pelo token: {r.status_code} {r.text[:300]}")
        return 1
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        print("O Google não devolveu refresh_token. Remova o acesso do app em "
              "https://myaccount.google.com/permissions e rode de novo.")
        return 1

    # confere qual canal foi autorizado
    yt = YouTube(client_id, client_secret, refresh)
    try:
        d = yt._get("channels", {"part": "snippet", "mine": "true"})
        nome = d["items"][0]["snippet"]["title"] if d.get("items") else "(conta sem canal!)"
    except Exception as e:  # noqa: BLE001
        nome = f"(não consegui conferir: {e})"

    print("\n✅ Autorizado! Canal do YouTube:", nome)
    print("   (se não for o canal que você queria, rode de novo e escolha o outro canal na tela do Google)")
    if conta:
        print("\nColoque este secret no GitHub (Settings > Secrets and variables > Actions):\n")
        print(f"  YOUTUBE_REFRESH_TOKEN{suf}  = {refresh}")
        print("\n(usa o mesmo YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET do canal principal)")
    else:
        print("\nColoque estes 3 secrets no GitHub (Settings > Secrets and variables > Actions):\n")
        print(f"  YOUTUBE_CLIENT_ID      = {client_id}")
        print(f"  YOUTUBE_CLIENT_SECRET  = {client_secret}")
        print(f"  YOUTUBE_REFRESH_TOKEN  = {refresh}")
    print("\nNão compartilhe esses valores com ninguém (dão acesso pra postar no seu canal).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
