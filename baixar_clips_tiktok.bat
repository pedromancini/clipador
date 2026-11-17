@echo off
rem Baixa os clips novos do GitHub para a pasta Videos\Clips TikTok, organizados por dia de postagem.
chcp 65001 >nul
cd /d "%~dp0"
python -m clipador.tiktok_baixar %*
echo.
pause
