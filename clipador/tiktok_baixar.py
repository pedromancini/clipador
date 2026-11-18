"""Baixa os clips do GitHub pro seu PC, já organizados pra postar no TikTok.

Uso (no Windows, dentro da pasta do projeto):
    python -m clipador.tiktok_baixar
    python -m clipador.tiktok_baixar --pasta "D:\\Clips TikTok"

Ou dê dois cliques em `baixar_clips_tiktok.bat`.

O que ele faz:
1. Procura no GitHub os clips que ainda não foram baixados (backups das execuções, ficam 3 dias lá).
2. Escolhe os melhores (chat mais agitado) e distribui nos horários de postagem livres
   (settings.tiktok.horarios no channels.json), a partir de agora até o fim de amanhã.
3. Salva em uma pasta por dia de postagem:

    Clips TikTok/
      2026-11-17 (terça)/
        19h30 - GTA 6 - alanzoka foi preso.mp4
        19h30 - GTA 6 - alanzoka foi preso.txt   <- horário + legenda pronta pra copiar
        extras/                                   <- clips que sobraram (se quiser postar mais)

Precisa do GitHub CLI (`gh`) instalado e logado (`gh auth login`). Não usa nenhuma biblioteca extra.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

PASTA_PADRAO = Path.home() / "Videos" / "Clips TikTok"
HORARIOS_PADRAO = ["12:00", "15:00", "18:00", "19:30", "21:00", "22:30"]
ESTADO = ".clipador_estado.json"
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


@dataclass
class Clip:
    nome: str          # nome do .mp4 no artifact
    video: bytes
    dados: dict        # conteúdo do .json (pode ser vazio em clips antigos)

    @property
    def viral(self) -> float:
        v = self.dados.get("viral")
        return float(v) if isinstance(v, (int, float)) else -1.0

    @property
    def hype(self) -> float:
        h = self.dados.get("hype")
        return float(h) if isinstance(h, (int, float)) else 0.0

    @property
    def quando(self) -> datetime | None:
        """Horário do pico (pelo json ou pelo nome do arquivo: canal_AAAAMMDD-HHMMSS.mp4)."""
        if self.dados.get("pico_epoch"):
            return datetime.fromtimestamp(float(self.dados["pico_epoch"]))
        m = re.search(r"_(\d{8}-\d{6})", self.nome)
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else None


# ------------------------------------------------------------------ GitHub
def gh(*args: str, binary: bool = False):
    try:
        p = subprocess.run(["gh", *args], capture_output=True, timeout=600)
    except FileNotFoundError:
        sys.exit("Não achei o GitHub CLI (gh). Instale: https://cli.github.com e rode `gh auth login`.")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:500])
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def repo_padrao() -> str:
    try:
        return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
    except Exception:  # noqa: BLE001
        return "pedromancini/clipador"


def listar_artifacts(repo: str) -> list[dict]:
    d = json.loads(gh("api", f"repos/{repo}/actions/artifacts?per_page=100"))
    return [a for a in d.get("artifacts", []) if a["name"].startswith("clips-") and not a.get("expired")]


def baixar_artifact(repo: str, art_id: int) -> list[Clip]:
    # grava direto no disco (pacotes de ~200 MB em conexão lenta passam de 10 min)
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"clipador_{art_id}.zip"
    with open(tmp, "wb") as f:
        p = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{art_id}/zip"], stdout=f,
                           stderr=subprocess.PIPE, timeout=3 * 3600)
    if p.returncode != 0:
        raise RuntimeError(f"falha ao baixar o artifact {art_id}: {p.stderr.decode(errors='replace')[:300]}")
    clips: list[Clip] = []
    with zipfile.ZipFile(tmp) as z:
        nomes = set(z.namelist())
        for n in sorted(nomes):
            if not n.lower().endswith(".mp4") or "/" in n or "\\" in n:
                continue
            j = n[:-4] + ".json"
            dados = {}
            if j in nomes:
                try:
                    dados = json.loads(z.read(j).decode("utf-8"))
                except ValueError:
                    dados = {}
            clips.append(Clip(n, z.read(n), dados))
    tmp.unlink(missing_ok=True)
    return clips


# ------------------------------------------------------------------ agenda
def proximos_horarios(agora: datetime, horarios: list[str], ocupados: set[str], dias: int = 2,
                      antecedencia_min: int = 20) -> list[datetime]:
    """Horários de postagem livres de agora até o fim de amanhã (dias=2)."""
    livres = []
    for d in range(dias):
        dia = (agora + timedelta(days=d)).date()
        for h in sorted(horarios):
            hh, mm = (int(x) for x in h.split(":"))
            t = datetime(dia.year, dia.month, dia.day, hh, mm)
            if t >= agora + timedelta(minutes=antecedencia_min) and t.strftime("%Y-%m-%d %H:%M") not in ocupados:
                livres.append(t)
    return livres


def agendar(clips: list[Clip], livres: list[datetime]) -> list[tuple[Clip, datetime | None]]:
    """O melhor clip vai no horário livre mais cedo (notícia velha esfria). Sobra = None (extras).

    Ordem: nota de viralidade da IA (quando tem), depois o quanto o chat agitou."""
    ordem = sorted(clips, key=lambda c: (-c.viral, -c.hype, c.quando or datetime.min))
    out = []
    for i, c in enumerate(ordem):
        out.append((c, livres[i] if i < len(livres) else None))
    return out


# ------------------------------------------------------------------ arquivos
def nome_seguro(txt: str, limite: int = 60) -> str:
    txt = re.sub(r"#\w+", "", txt)  # hashtags não entram no nome do arquivo
    txt = re.sub(r'[<>:"/\|?*\x00-\x1f]', "", txt)
    txt = re.sub(r"[^\w\s\-.,!áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" .")
    return (txt[:limite].rstrip(" .") or "clip")


def pasta_do_dia(raiz: Path, dia: datetime) -> Path:
    return raiz / f"{dia:%Y-%m-%d} ({DIAS[dia.weekday()]})"


def canal_do(c: Clip) -> str:
    return (c.dados.get("canal") or c.nome.split("_")[0].partition("-")[2] or "outros").lower()


def _tags_obrigatorias(canal: str) -> list[str]:
    try:
        from .config import load_config
        for ch in load_config().channels:
            if ch.name == canal:
                return list(ch.title_tags or [])
    except Exception:  # noqa: BLE001
        pass
    return []


def legenda(c: Clip) -> str:
    d = c.dados
    plataforma, _, canal_arq = c.nome.split("_")[0].partition("-")   # twitch-alanzoka_2026....mp4
    canal = d.get("canal") or canal_arq or "streamer"
    titulo = d.get("titulo") or f"Clip da live de {canal}"
    tags = d.get("hashtags") or [*carregar_hashtags(), f"#{canal}"][:8]
    url = d.get("url_canal") or {"twitch": f"twitch.tv/{canal}", "kick": f"kick.com/{canal}"}.get(plataforma, "")
    credito = f"Créditos: {url.replace('https://', '').replace('www.', '')}" if url else f"Créditos: {canal}"
    gancho = (d.get("legenda") or "").strip()
    corpo = gancho if gancho and gancho != titulo else titulo
    faltam = [t for t in _tags_obrigatorias(canal) if t.lower() not in corpo.lower()]
    if faltam:  # regra do campeonato: a hashtag tem que estar no texto principal
        corpo = f"{corpo} {' '.join(faltam)}"
    return f"{corpo}\n\n{credito}\n\n{' '.join(tags)}"


def texto_txt(c: Clip, quando: datetime | None) -> str:
    linhas = []
    if quando:
        linhas.append(f"POSTAR: {DIAS[quando.weekday()]} {quando:%d/%m} às {quando:%Hh%M}")
    else:
        linhas.append("EXTRA: sem horário (poste se quiser mais um no dia)")
    linhas += ["", "LEGENDA (copie e cole no TikTok):", "-" * 40, legenda(c), "-" * 40, ""]
    q = c.quando
    if q:
        linhas.append(f"Momento da live: {q:%d/%m às %H:%M}")
    if c.hype:
        linhas.append(f"Chat: {c.hype:.1f}x mais agitado que o normal")
    if c.viral >= 0:
        linhas.append(f"Nota de viralidade (IA): {c.viral:g}/10")
    if c.dados.get("youtube"):
        linhas.append(f"Também postado no YouTube: {c.dados['youtube']}")
    if c.dados.get("fala"):
        linhas += ["", f"O que ele fala: {c.dados['fala']}"]
    return "\n".join(linhas) + "\n"


def salvar(raiz: Path, c: Clip, quando: datetime | None) -> Path:
    titulo = nome_seguro(c.dados.get("titulo") or Path(c.nome).stem)
    if quando:
        pasta = pasta_do_dia(raiz, quando)
        base = f"{quando:%Hh%M} - {titulo}"
    else:
        pasta = pasta_do_dia(raiz, c.quando or datetime.now()) / "extras"
        base = f"{(c.quando or datetime.now()):%Hh%M} (live) - {titulo}"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"{base}.mp4"
    n = 2
    while destino.exists():
        destino = pasta / f"{base} ({n}).mp4"
        n += 1
    destino.write_bytes(c.video)
    destino.with_suffix(".txt").write_text(texto_txt(c, quando), encoding="utf-8-sig")  # BOM: Bloco de Notas ok
    return destino


# ------------------------------------------------------------------ principal
def _settings() -> dict:
    try:
        return json.loads((ROOT / "channels.json").read_text(encoding="utf-8")).get("settings") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def carregar_hashtags() -> list[str]:
    h = _settings().get("hashtags")
    return [str(x) for x in h] if isinstance(h, list) and h else ["#clips", "#cortes", "#fyp"]


def carregar_horarios() -> list[str]:
    try:
        h = (_settings().get("tiktok") or {}).get("horarios")
        if h and all(re.fullmatch(r"\d{1,2}:\d{2}", x) for x in h):
            return list(h)
    except (OSError, ValueError, AttributeError):
        pass
    return HORARIOS_PADRAO


def agora_local() -> datetime:
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            pass
    return datetime.now()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Baixa os clips do GitHub organizados por dia pra postar no TikTok")
    ap.add_argument("--pasta", default=str(PASTA_PADRAO), help=f"onde salvar (padrão: {PASTA_PADRAO})")
    ap.add_argument("--repo", default=None, help="usuario/repositorio (padrão: o deste projeto)")
    a = ap.parse_args(argv)

    raiz = Path(a.pasta)
    raiz.mkdir(parents=True, exist_ok=True)
    est_path = raiz / ESTADO
    try:
        estado = json.loads(est_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}
    baixados: set[int] = set(estado.get("artifacts_baixados", []))
    ocupados: set[str] = set(estado.get("horarios_ocupados", []))

    repo = a.repo or repo_padrao()
    print(f"Procurando clips novos em {repo}...")
    novos = [x for x in listar_artifacts(repo) if x["id"] not in baixados]
    if not novos:
        print("Nenhum clip novo. (Os clips aparecem quando a execução no GitHub termina.)")
        return 0

    clips: list[Clip] = []
    for art in sorted(novos, key=lambda x: x["created_at"]):
        print(f"  baixando {art['name']} ({art['size_in_bytes'] / 1e6:.0f} MB)...")
        clips += baixar_artifact(repo, art["id"])

    # cada streamer tem a sua pasta e os seus horários (podem ser contas de TikTok diferentes)
    por_canal: dict[str, list[Clip]] = {}
    for c in clips:
        por_canal.setdefault(canal_do(c), []).append(c)
    agenda: list[tuple[Clip, datetime | None]] = []
    for canal, lista in sorted(por_canal.items()):
        print(f"\n{canal}:")
        ocup_c = {h.split("|", 1)[1] for h in ocupados if h.startswith(f"{canal}|")}
        livres = proximos_horarios(agora_local(), carregar_horarios(), ocup_c)
        ag = agendar(lista, livres)
        agenda += ag
        for c, quando in sorted(ag, key=lambda x: (x[1] is None, x[1] or datetime.min)):
            destino = salvar(raiz / canal, c, quando)
            if quando:
                ocupados.add(f"{canal}|{quando:%Y-%m-%d %H:%M}")
                print(f"  {quando:%d/%m %Hh%M}  {destino.name}")

    baixados.update(x["id"] for x in novos)
    hoje = agora_local().strftime("%Y-%m-%d")
    estado = {"artifacts_baixados": sorted(baixados),
              "horarios_ocupados": sorted(h for h in ocupados if h.split("|")[-1][:10] >= hoje)}
    est_path.write_text(json.dumps(estado, indent=1), encoding="utf-8")

    agendados = sum(1 for _, q in agenda if q)
    print(f"\nPronto: {len(clips)} clips baixados, {agendados} com horário marcado, "
          f"{len(clips) - agendados} em 'extras'.\nPasta: {raiz} (uma subpasta por streamer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
