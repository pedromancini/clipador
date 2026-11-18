# ✂️ Clipador

Monitora lives da **Twitch** e da **Kick**, detecta os melhores momentos pelo **hype do chat**, corta
**clips verticais 9:16 com legenda** e **posta os melhores no YouTube Shorts sozinho**. Todos os clips ficam
disponíveis no GitHub (e, se você quiser, no Telegram), com título e hashtags prontos pro TikTok.

- 100% gratuito: roda no **GitHub Actions** (repositório público = minutos ilimitados), sem precisar do seu PC ligado.
- **YouTube Shorts: automático** pela API oficial. Só posta os picos mais fortes, com limite de posts por dia (opcional).
- **TikTok: semiautomático.** A API do TikTok não permite postar em público com ferramenta pessoal
  (veja "Limites"), então você baixa o clip e posta, com o título e as hashtags já prontos.

```
chat (Twitch IRC / Kick Pusher) ──> detector de hype ──pico──> monta o clip ──┬─> YouTube Shorts (picos fortes)
stream (Streamlink) ──> ffmpeg ──> buffer de 3 min em segmentos ──┘         └─> resumo do job + artifact (+ Telegram, opcional)
                                              (layout 9:16 + legenda Whisper)
```

> Telegram é **opcional**. Sem ele, os clips aparecem no **resumo do job** (aba Actions) e no **artifact**.

---

## Uso rápido: colar o link de uma live

**No GitHub (sem PC):** aba **Actions → clipador → Run workflow** → cole o link da live no campo
(ex.: `https://kick.com/gaules` ou `https://www.twitch.tv/gaules`) → **Run workflow**.
Ele grava e vai clipando os picos do chat até a live acabar (ou ~5h40). O canal não precisa estar no `channels.json`;
se não estiver, usa as configurações padrão (layout `blur`).

**No seu PC:**

```bash
python -m clipador https://kick.com/gaules          # monitora e clipa os picos até a live acabar
python -m clipador https://kick.com/gaules --once   # teste: gera 1 clip na hora e sai
```

> Só funciona com live **ao vivo**, porque precisa do chat em tempo real. Links de VOD/vídeo (`/videos/...`) e de clip não funcionam.
> Pra monitorar um canal **sempre**, sozinho a cada 10 min, coloque ele no `channels.json` (passo 5).

---

## Como funciona

1. **Chat:** lê o chat ao vivo. Na Twitch usa IRC anônimo, na Kick usa o WebSocket público. Não precisa de conta.
2. **Detecção:** conta as mensagens por segundo. KKKK, CLIP, `!clip`, KEKW, LUL, "caralho", "mds",
   CAPS LOCK e emote repetido valem mais. Quando a soma dos últimos ~8s passa de `multiplicador ×` o normal
   do chat (a mediana dos últimos ~5 min), é um **pico**.
3. **Gravação:** a live fica gravando o tempo todo num buffer dos últimos ~3 min, em pedaços de 5s com o
   horário no nome do arquivo.
4. **Clip:** num pico, espera ~12s, junta o trecho de 25s antes até 12s depois, transcreve com o
   Whisper e gera um vídeo 1080x1920 com legenda estilo TikTok (MAIÚSCULA, fonte grossa, palavra da vez em amarelo).
5. **YouTube (opcional):** se o pico foi forte (ex.: ≥ 4x o normal do chat) e o limite do dia não foi atingido,
   posta como Short com título, hashtags e crédito/link do streamer na descrição.
6. **Entrega:** cada clip aparece no **resumo da execução** no GitHub, com horário, score do hype, as mensagens
   mais repetidas do chat, o link do Short (se postado) e uma **sugestão de título + hashtags** pra colar no TikTok.
   O vídeo fica no artifact. Se o Telegram estiver configurado, ele também manda tudo isso no celular.

---

## Passo a passo

### 1. (Opcional) Criar o bot do Telegram

> Pode pular os passos 1 e 2 se não quiser receber os clips no celular. Veja "Onde ficam os clips" abaixo.

1. No Telegram, abra o **[@BotFather](https://t.me/BotFather)** e mande `/newbot`.
2. Escolha um nome (ex.: `Meu Clipador`) e um usuário terminado em `bot` (ex.: `meu_clipador_bot`).
3. O BotFather te dá um **token** parecido com `123456789:AAH...`. Guarde: é o `TELEGRAM_BOT_TOKEN`.

### 2. (Opcional) Pegar o seu chat_id

1. Abra o seu bot no Telegram e mande qualquer mensagem pra ele (ex.: `oi`).
2. No navegador, abra (trocando `<TOKEN>` pelo token do passo 1):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Procure `"chat":{"id":123456789,...}`. Esse número é o `TELEGRAM_CHAT_ID`.
   - Quer receber num **grupo**? Adicione o bot no grupo, mande uma mensagem lá e repita o passo 2. O id de grupo começa com `-`.

### 3. Subir o projeto no GitHub

1. Crie um repositório **público** no GitHub (ex.: `clipador`). Precisa ser público pro Actions ser ilimitado e grátis.
2. Envie estes arquivos pra ele (`git push`).

### 4. Colocar os secrets

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Nome | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | *(opcional)* o token do BotFather |
| `TELEGRAM_CHAT_ID` | *(opcional)* o seu chat_id |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` | *(opcional)* veja o passo 7 |
| `YOUTUBE_REFRESH_TOKEN_DANTE` | *(opcional)* token do 2º canal do YouTube (passo 7d) |
| `GEMINI_API_KEY` | *(recomendado)* chave grátis em [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

> Os secrets ficam escondidos mesmo com o repositório público. Nunca coloque o token direto no código.

**Títulos e legendas com IA (`GEMINI_API_KEY`):** o Gemini ouve o áudio de cada clip e lê o que o chat
mandou. Com isso ele escreve o título do Short, a legenda do TikTok (gancho + pergunta), as hashtags do assunto
(`#gta6` só quando o clip é mesmo sobre GTA 6) e uma nota de viralidade de 0 a 10. Essa nota decide quais clips
ficam com os melhores horários do TikTok. A faixa grátis do Gemini aguenta com folga ~100 clips por dia. Sem a
chave, ou se a IA falhar, entra o plano B, que escreve uma chamada conforme a reação do chat (risada, choque,
derrota). Ele nunca cita a transcrição automática, porque ela erra muito em live.

### 5. Editar o `channels.json`

```json
{
  "settings": { "...": "..." },
  "channels": [
    { "platform": "twitch", "name": "gaules", "layout": "blur", "enabled": true },
    { "platform": "kick", "name": "gaules", "layout": "facecam", "enabled": true,
      "facecam": { "x": 0.0, "y": 0.0, "w": 0.25, "h": 0.28 }, "min_msgs": 8 }
  ]
}
```

**Campos de cada canal:**

| Campo | O que é |
|---|---|
| `url` | o link do canal (ex.: `"https://kick.com/gaules"`), **ou** use `platform` + `name` abaixo |
| `platform` | `"twitch"` ou `"kick"` |
| `name` | o slug do canal, como aparece na URL (`twitch.tv/`**`gaules`**, `kick.com/`**`gaules`**) |
| `layout` | `"blur"` (padrão), `"facecam"` ou `"crop"` (veja abaixo) |
| `enabled` | `true` pra monitorar, `false` pra pausar sem apagar |
| `facecam` | *(opcional)* **`"auto"`** = acha a câmera sozinho em cada clip (recomendado: funciona mesmo quando o streamer muda a cam de lugar). Ou a posição fixa em **frações da tela** (0 a 1): `{"x","y","w","h"}` |
| `min_msgs` | *(opcional)* mínimo de "pontos de hype" em ~8s pra disparar. **Abaixe pra chats pequenos** (ex.: 5–8) |
| `youtube` | *(opcional)* `false` = este canal nunca vai pro YouTube, só pro Telegram (padrão: `true`) |
| `kick_chatroom_id` | *(opcional, só Kick)* reserva manual se a API da Kick falhar (veja "Problemas comuns") |

**Layouts:**

| `blur` | `facecam` | `crop` |
|---|---|---|
| jogo inteiro no centro, fundo desfocado | câmera em cima (1/3), gameplay embaixo (2/3) | corte central 9:16 |
| funciona em qualquer live (padrão) | melhor pra streamer com cam fixa | bom pra IRL / conteúdo centralizado |

> **`"facecam": "auto"`:** o clipador procura o rosto do streamer (o que fica parado no mesmo lugar) e a moldura da
> câmera em volta dele. Se não achar um rosto (tela de loading, streamer saiu da cam), aquele clip sai no layout `blur`.
>
> **Como achar a posição fixa da facecam:** tire um print da live (1920x1080). Se a cam fica de x=0 a x=480 e
> de y=0 a y=300 pixels, então `x = 0/1920 = 0`, `y = 0/1080 = 0`, `w = 480/1920 = 0.25`, `h = 300/1080 = 0.28`.

**Settings globais (principais):**

| Campo | Padrão | O que faz |
|---|---|---|
| `clip_before_seconds` / `clip_after_seconds` | 25 / 12 | quanto do clip vem antes/depois do pico (máx. 60s no total) |
| `peak_multiplier` | 3.5 | quantas vezes acima do normal o chat precisa ficar. **Maior = menos clips, mais seletivo** |
| `min_msgs` | 12 | mínimo absoluto (vale pra todos os canais sem `min_msgs` próprio) |
| `cooldown_seconds` | 90 | intervalo mínimo entre dois clips |
| `warmup_seconds` | 60 | não clipa no primeiro minuto (o detector ainda está aprendendo o "normal" do chat) |
| `subtitle_size` | 80 | tamanho da legenda (124 = grande, 80 = médio/pequeno) |
| `subtitle_position` | `middle` | `middle` = no meio (no `facecam`, na base da câmera, sem cobrir o jogo) ou `bottom` = embaixo |
| `whisper_model` | `base` | modelo da legenda (`tiny` = mais rápido, `small` = melhor e mais lento) |
| `hashtags` | `#gta6 #gta #clips...` | hashtags padrão da sugestão (o canal e a categoria entram sozinhos) |
| `max_runtime_minutes` | 340 | quanto tempo cada job monitora antes de passar a vez |

### 6. Ativar o GitHub Actions

1. No repositório, abra a aba **Actions**. Se aparecer um botão pra habilitar os workflows, clique.
2. Teste na hora: **Actions → clipador → Run workflow**. Deixe o campo vazio (todos os canais do
   `channels.json`) ou cole o link de uma live (ex.: `https://kick.com/gaules`).
3. A partir daí ele roda sozinho **a cada 10 minutos**. Se o canal estiver offline, o job termina em ~1 min.
   Se estiver ao vivo, ele grava e vai gerando os clips.

**Onde ficam os clips:** aba **Actions** → clique na execução → no **resumo** aparece cada clip com o link do
Short e o texto pro TikTok. No fim da página, em **Artifacts**, fica o `.zip` com os vídeos (guardado por 3 dias;
dá pra mudar `retention-days` no workflow, até 90). Se um job der erro, o GitHub te avisa por e-mail.

### 7. (Opcional) Postagem automática no YouTube Shorts

Configuração feita uma vez só, leva uns 10 minutos. **Use um canal do YouTube separado pros cortes**, não o seu canal principal (veja "Limites").

**a) Criar o projeto no Google Cloud**

1. Abra o [console.cloud.google.com](https://console.cloud.google.com), clique no seletor de projetos e depois em **Novo projeto** (nome: `clipador`).
2. Abra **APIs e serviços → Biblioteca**, procure **YouTube Data API v3** e clique em **Ativar**.
3. Abra **APIs e serviços → Tela de consentimento OAuth** (ou **Google Auth Platform**):
   - tipo **Externo**, nome do app `clipador`, seu e-mail como suporte e contato;
   - em **Público-alvo**, clique em **Publicar app**, até o status ficar **"Em produção"**.
     ⚠️ Esse passo é importante: se ficar em **"Teste"**, o Google invalida o token a cada 7 dias e a postagem para.
     Você **não** precisa enviar o app pra verificação.
4. Abra **APIs e serviços → Credenciais → Criar credenciais → ID do cliente OAuth**:
   - tipo **App para computador**, nome `clipador`;
   - clique em **Baixar JSON** e salve como `client_secret.json`. **Não suba esse arquivo pro GitHub.**

**b) Autorizar o seu canal (no seu PC)**

```bash
pip install -r requirements.txt
python -m clipador.youtube_auth client_secret.json
```

O navegador abre. Entre na conta, **escolha o canal dos cortes** e autorize. O Google vai mostrar
"O Google não verificou este app": clique em **Avançado → Acessar clipador (não seguro)**. É normal, porque o app é seu.
No fim, o terminal mostra 3 valores.

**c) Colocar os secrets e ligar**

1. Crie no GitHub os secrets `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` e `YOUTUBE_REFRESH_TOKEN` com os valores do passo b.
2. No `channels.json`, ligue em `settings.youtube`:

```json
"youtube": {
  "enabled": true,
  "privacy": "public",
  "min_hype_ratio": 4.0,
  "posts_per_hour": 2,
  "min_minutes_between_posts": 20,
  "extreme_hype_ratio": 8.0,
  "extreme_posts_per_hour": 3,
  "max_posts_per_day": 50,
  "category_id": "20",
  "made_for_kids": false
}
```

| Campo | O que faz |
|---|---|
| `enabled` | liga/desliga a postagem automática |
| `privacy` | `public`, `unlisted` (não listado, bom pra testar) ou `private` |
| `min_hype_ratio` | só posta se o auge do pico for ≥ N vezes o normal do chat. **Maior = só os momentos mais insanos** |
| `posts_per_hour` | ritmo normal: até N Shorts por hora (contando a última hora corrida) |
| `min_minutes_between_posts` | espaço mínimo entre dois posts normais (evita soltar 2 seguidos) |
| `extreme_hype_ratio` | quando o chat passa de N vezes o normal, é um momento fora da curva... |
| `extreme_posts_per_hour` | ...e libera um post extra na hora (ex.: 3 em vez de 2), sem esperar o espaçamento |
| `max_posts_per_day` | teto de Shorts por dia **por canal monitorado** (conta pelo fuso de `timezone`) |
| `category_id` | `20` = Jogos, `24` = Entretenimento |

Quer que um canal nunca vá pro YouTube (só Telegram)? Coloque `"youtube": false` nele.

**d) Um streamer em outro canal do YouTube**

Cada streamer pode postar num canal diferente. Ex.: alanzoka vai pro canal principal (de GTA) e allanstag vai
para outro canal:

1. Rode a autorização de novo, dando um nome pra conta, e escolha o **outro** canal na tela do Google:
   ```bash
   python -m clipador.youtube_auth client_secret.json dante
   ```
2. Crie o secret que ele mostrar (`YOUTUBE_REFRESH_TOKEN_DANTE`). O client id/secret são os mesmos do principal.
3. No canal do `channels.json`, coloque `"youtube_conta": "dante"` (e, se quiser, `"hashtags"` próprias dele).

Sem o secret dessa conta, os clips desse streamer **não são postados**: eles nunca vão parar no canal principal.
Para um nome novo (ex.: `"youtube_conta": "outro"`) é preciso também adicionar a linha
`YOUTUBE_REFRESH_TOKEN_OUTRO: ${{ secrets.YOUTUBE_REFRESH_TOKEN_OUTRO }}` no `env` dos workflows.

Os clips que não passam no filtro continuam chegando no Telegram, com o motivo (ex.: `⏭️ YouTube: hype 3.2x < 4x`).

### 8. Baixar os clips no PC, prontos pra postar no TikTok

Dê dois cliques em **`baixar_clips_tiktok.bat`** (ou rode `python -m clipador.tiktok_baixar`). Ele baixa do
GitHub os clips que ainda não estão no seu PC e organiza em `Vídeos\Clips TikTok`, uma pasta por dia de postagem:

```
Clips TikTok\
  2026-11-19 (quinta)\
    19h30 - GTA 6 alanzoka foi preso.mp4
    19h30 - GTA 6 alanzoka foi preso.txt    <- horário pra postar + legenda pronta pra copiar
    extras\                                  <- os que sobraram, se quiser postar mais
```

- Os clips com o chat mais agitado ficam com os horários mais cedo. Os outros vão para `extras`.
- Os horários ficam em `settings.tiktok.horarios` no `channels.json` (padrão: 12h, 15h, 18h, 19h30, 21h, 22h30).
- Ele lembra o que já baixou e quais horários já estão ocupados, então pode rodar quantas vezes quiser.
- Os clips só aparecem quando a execução no GitHub termina, e ficam lá por 3 dias.
- Precisa do [GitHub CLI](https://cli.github.com) logado (`gh auth login`). Outra pasta: `--pasta "D:\Clips"`.

---

## Testar no seu PC (opcional)

Precisa de **Python 3.10+**, **ffmpeg** (com libass) e, pra Kick, do **Google Chrome** instalado.

```bash
pip install -r requirements.txt

# testes rápidos (detector, legenda, parsers)
python -m unittest discover -s tests -p "test_*.py"

# teste completo SEM live: vídeo sintético + chat falso -> detecção, corte e legenda nos 3 layouts
python -m tests.synthetic

# teste com live de verdade: grava ~40s, gera 1 clip forçado, manda no Telegram e sai
#   (Windows PowerShell: $env:TELEGRAM_BOT_TOKEN="..."; $env:TELEGRAM_CHAT_ID="...")
export TELEGRAM_BOT_TOKEN="123:AAH..." TELEGRAM_CHAT_ID="123456789"
python -m clipador --platform kick --channel gaules --once

# mesmo teste, mas postando o clip forçado no YouTube também (precisa do YouTube configurado;
# dica: use "privacy": "unlisted" pra testar sem aparecer no canal)
python -m clipador --platform kick --channel gaules --once --post-youtube

# só checar se está ao vivo
python -m clipador --platform twitch --channel gaules --check-live
```

Os clips ficam em `clips/`, com um `clips/RESUMO.md` (título e hashtags de cada um). Sem os secrets do Telegram, o envio só aparece no log.

---

## Limites e avisos

- **O cron do GitHub atrasa.** "A cada 10 min" na prática pode virar 10 a 30 min em horário de pico.
  Então o começo de uma live pode passar sem monitoramento.
- **Workflows agendados são desativados depois de 60 dias sem commits** no repositório. O GitHub avisa por
  e-mail. Pra evitar, faça qualquer commit (até editar o `channels.json`) ou reative na aba Actions.
- **Um job dura no máximo 6h.** O clipador monitora por ~340 min, encerra limpo e, se a live continuar,
  dispara o próximo job sozinho (fica um buraco de alguns minutos na troca).
- **Execuções "Cancelled" na aba Actions são normais.** Enquanto um canal está sendo gravado, os disparos
  agendados seguintes ficam na fila do mesmo canal (`concurrency`) e o GitHub cancela os excedentes. Isso não é erro.
- **A Kick não tem API pública oficial pra isso.** O clipador usa os mesmos endpoints que o site:
  - `kick.com/api/v2/channels/{slug}` (tem Cloudflare; usamos o `curl_cffi` imitando o Chrome);
  - `web.kick.com/api/v1/realtime/connection` (fornece a key atual do Pusher);
  - o WebSocket do Pusher, canal `chatrooms.{id}.v2`.

  A Kick já trocou a key do Pusher uma vez (out/2026). O código busca a key atual sozinho, mas a Kick pode
  mudar tudo de novo sem aviso.
- **IPs do GitHub às vezes são bloqueados** (Cloudflare na Kick, anúncios na Twitch). Normalmente funciona,
  mas se um canal falhar sempre, é provável que seja isso.
- **Tamanho:** os clips ficam abaixo de 50 MB (limite do bot do Telegram); o clipador ajusta a qualidade pra caber.
- **TikTok não posta sozinho.** Pelas [regras oficiais da API do TikTok](https://developers.tiktok.com/docs/en/content-sharing-guidelines),
  um app sem auditoria só consegue postar como **privado**, e o TikTok **não aprova** ferramentas pessoais
  ("utility tool to help upload contents to the account(s) you or your team manages"). Robôs que fingem
  ser você no navegador violam os termos do TikTok e podem banir a conta. Por isso o TikTok fica semiautomático.
- **YouTube:** a API permite 100 uploads por dia por projeto. O ritmo padrão (2 por hora, espaçados, com um
  extra quando o chat explode) fica abaixo disso de propósito: rajadas de vídeos derrubam o alcance do canal.
  O YouTube também tem um limite de uploads por canal por dia que não é divulgado (canal verificado por
  telefone aguenta mais).
- **Direitos e Content ID:** o conteúdo é dos streamers. Música tocando na live pode gerar reivindicação de
  direitos autorais no YouTube, e o streamer pode pedir remoção (strike). Por isso: **use um canal separado**, mantenha o
  crédito e o link na descrição (o clipador já faz isso) e respeite quem não quer cortes.

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| Não chega nada no Telegram | Confira os secrets. Mande uma mensagem pro bot antes, porque bot não puxa conversa. Veja o log do job na aba Actions |
| `Não consegui descobrir o chatroom_id da Kick` | Abra `https://kick.com/api/v2/channels/SLUG` no navegador, procure `"chatroom":{"id":NUMERO` e coloque `"kick_chatroom_id": NUMERO` no canal |
| Clips demais | Aumente `peak_multiplier` (ex.: 4.5) e/ou `cooldown_seconds` |
| Nenhum clip num canal pequeno | Abaixe o `min_msgs` do canal (ex.: 5) |
| Legenda errada/ruim | Troque `whisper_model` pra `small` (mais lento, mas o runner aguenta) |
| Facecam cortada errada | Ajuste as frações `x`, `y`, `w`, `h` (veja "Como achar a posição da facecam") |
| `⚠️ YouTube falhou: refresh token inválido` | O app do Google ficou em "Teste" (o token expira em 7 dias) ou o acesso foi removido. Publique o app ("Em produção") e rode `python -m clipador.youtube_auth` de novo |
| `⚠️ YouTube ligado... faltam os secrets` | Crie os 3 secrets `YOUTUBE_*` no GitHub |
| Short postado ficou privado | Confira `privacy` no `channels.json`. Se estiver `public` e mesmo assim ficar privado, o Google está tratando o projeto como não verificado: preencha o formulário de auditoria da API do YouTube (é grátis) |
| Nenhum Short postado | Os picos não chegaram no `min_hype_ratio` (o motivo aparece no Telegram). Abaixe pra 3 se quiser mais posts |

## Estrutura

```
clipador/
├── channels.json              # canais + configurações
├── requirements.txt
├── .github/workflows/clipador.yml
├── fonts/Anton-Regular.ttf    # fonte da legenda (licença OFL)
├── clipador/
│   ├── __main__.py   # linha de comando
│   ├── config.py     # leitura/validação do channels.json
│   ├── matrix.py     # lista de canais pro Actions
│   ├── stream.py     # Streamlink: ao vivo?, título, categoria, stream
│   ├── recorder.py   # gravação em buffer (segmentos com horário)
│   ├── chat/         # twitch.py (IRC) e kick.py (Pusher)
│   ├── detector.py   # detecção de picos de hype
│   ├── subtitles.py  # Whisper -> legenda .ass estilo TikTok
│   ├── clipper.py    # layouts 9:16, legenda queimada, limite de 50 MB
│   ├── facecam.py    # acha a câmera do streamer sozinho ("facecam": "auto")
│   ├── summary.py    # resumo dos clips na página do job
│   ├── telegram.py   # envio pelo Telegram (opcional)
│   ├── youtube.py    # postagem automática no YouTube Shorts
│   ├── youtube_auth.py # autorização do canal (rodar 1x no PC)
│   └── runner.py     # junta tudo
└── tests/            # testes unitários + teste sintético ponta a ponta
```
