# Política de Privacidade — Clipador / gtaviclips

*Última atualização: 16 de novembro de 2026*

O **Clipador** é uma ferramenta pessoal, de código aberto, usada pelo dono deste repositório para gerar cortes
(clips) de lives públicas da Twitch e da Kick e publicá-los no **próprio canal do YouTube do dono** (gtaviclips).

## Quem usa
O aplicativo é usado **somente pelo dono deste repositório**, na conta Google dele. Não é oferecido ao público
e não pede login de outras pessoas.

## Dados do Google/YouTube acessados
Com a autorização do dono, o aplicativo usa a YouTube Data API v3 com os escopos:
- `youtube.upload`: enviar os clips gerados para o canal do dono;
- `youtube.readonly`: listar os envios recentes do próprio canal, só para contar quantos Shorts foram postados
  no dia (limite diário configurado).

O aplicativo **não** lê, coleta, vende nem compartilha dados de outros usuários, nem dados pessoais do dono
além do necessário para essas duas funções.

## Armazenamento
- O token de acesso (refresh token) fica guardado como *secret* criptografado no GitHub Actions do repositório do
  dono e é usado apenas para as chamadas acima.
- Nenhum dado é enviado a servidores de terceiros além das APIs do Google/YouTube, da Twitch e da Kick (para ler as
  lives públicas) e, opcionalmente, do Telegram do próprio dono.

## Conteúdo de terceiros
Os clips são trechos de transmissões públicas. O crédito e o link do canal original são incluídos na descrição de
cada vídeo. Streamers que não quiserem cortes podem pedir a remoção pelo contato abaixo.

## Revogar o acesso
O acesso pode ser revogado a qualquer momento em <https://myaccount.google.com/permissions>.

## Contato
Abra uma *issue* neste repositório: <https://github.com/pedromancini/clipador/issues>

O uso das APIs do YouTube segue os [Termos de Serviço do YouTube](https://www.youtube.com/t/terms) e a
[Política de Privacidade do Google](https://policies.google.com/privacy).
