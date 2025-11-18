# Telegram Social Stats Bot

Bot privado de Telegram escrito em Python para monitorar em tempo real YouTube (canal e Shorts), TikTok, Kwai e Facebook Reels/Página. Utiliza `python-telegram-bot` v20, cache com TTL configurável, Selenium headless com `undetected-chromedriver` e bibliotecas auxiliares como `yt-dlp` e `cloudscraper` para contornar bloqueios (Cloudflare, CAPTCHA e afins).

## Funcionalidades
- `/start`: mensagem de boas-vindas, instruções e lembrete de que o bot é privado.
- `/stats`: coleta imediata dos números (ou usa cache de até 10 min) e responde com formatação amigável.
- `/config`: permite ajustar URLs/usernames diretamente pelo Telegram, salvando em `config.json`.
- Tratamento de erros: quando um site bloqueia ou não responde, aparece “indisponível/bloqueado” em vez de travar.
- Cache com TTL configurável (`cache_ttl_seconds`), padrão 600 s.
- Envio automático das estatísticas para o chat configurado (`stats_chat_id`) usando a mesma cadência do cache (padrão 600 s).
- Restringe o acesso ao `authorized_user_id`.

## Pré-requisitos
- Python 3.10+ (recomendado).
- Google Chrome instalado no servidor (o `undetected-chromedriver` fará o download do driver automaticamente).
- Token do bot obtido com o [BotFather](https://t.me/BotFather).
- Conhecer seu ID numérico no Telegram (use o bot [@userinfobot](https://t.me/userinfobot)).

## Instalação
```bash
git clone https://example.com/telegramscrap.git
cd telegramscrap
python -m venv .venv
.venv\Scripts\activate  # Windows
# ou source .venv/bin/activate no Linux/macOS
pip install --upgrade pip
pip install -r requirements.txt
```
> O `requirements.txt` já instala o extra `python-telegram-bot[job-queue]`, que habilita o `JobQueue` usado pelo envio automático de estatísticas.

## Configuração
1. Crie o `.env` com o token do BotFather:
   ```bash
   copy .env.example .env  # Windows
   # ou cp .env.example .env no Linux/macOS
   ```
   Abra `.env` e defina TELEGRAM_TOKEN=seu-token-do-bot.
2. Abra `config.json` e configure:
   - `authorized_user_id`: seu ID numérico.
   - `cache_ttl_seconds`: TTL do cache em segundos (máx. 600 recomendado para evitar bloqueios).
   - `stats_chat_id`: ID numérico do chat/grupo onde as estatísticas devem ser enviadas automaticamente (prefixe com `-100` para supergrupos/canais).
   - `broadcast_interval_seconds`: opcional, altera a frequência do envio automático (deve ser >= 1; padrão é o valor de `cache_ttl_seconds` ou 600 s se ausente).
   - `profiles`: atualize cada URL/username conforme necessário. Use handles completos (com https://) e usernames sem @ quando especificado.
3. Inicie o bot para validar:
   ```bash
   python bot.py
   ```
4. No Telegram, abra o chat com o bot, envie `/start` e teste `/stats`. Ajustes podem ser feitos pelo comando `/config` (ex.: `tiktok_username=seudominio`).

## Como o bot coleta dados
- **YouTube (canal e Shorts)**: `yt-dlp` para pegar inscritos, views totais e somar views dos Shorts.
- **TikTok/Kwai**: tenta primeiro `cloudscraper` + parsing do JSON embedado; se detectar palavras como “Cloudflare”/“captcha”, lança `TemporaryBlockError`. O Kwai possui fallback automático para Selenium headless.
- **Facebook Reels/Página**: Selenium headless (`undetected-chromedriver`) abre a página, aguarda o HTML carregar e extrai seguidores/views com expressões regulares.
- Quando o site aciona CAPTCHA ou outra barreira, o bot retorna “bloqueado” em vez de quebrar o fluxo. Os logs deixam claro o motivo (`TemporaryBlockError`).

## Rodando 24/7

### Systemd (Ubuntu/Debian)
1. Crie um serviço:
   ```ini
   # /etc/systemd/system/telegram-stats-bot.service
   [Unit]
   Description=Telegram Social Stats Bot
   After=network-online.target

   [Service]
   Type=simple
   WorkingDirectory=/opt/telegramscrap
   ExecStart=/opt/telegramscrap/.venv/bin/python bot.py
   Restart=always
   RestartSec=10
   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=multi-user.target
   ```
2. Ative:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-stats-bot
   sudo journalctl -u telegram-stats-bot -f
   ```

### Replit / Render / Heroku / VPS
1. **Replit**: importe o repositório, defina o comando de execução (`python bot.py`) e marque “Always On”.
2. **Render**: crie um “Background Worker”, conecte ao GitHub e defina `Start Command: python bot.py`. Configure variáveis de ambiente se preferir (ou suba o `config.json` via volumes/Secret Files).
3. **Heroku**: crie um `Procfile` (type worker), suba via Git, configure o `config.json` através de variáveis (p. ex., `CONFIG_JSON_BASE64`) e carregue no `release` script antes de iniciar.
4. **VPS**: além de systemd, você pode usar `tmux` ou `pm2 start python --name stats-bot -- bot.py`.

> Dica: mantenha Chrome/Chromium atualizados e habilite swap/monitoramento da VPS para evitar que o Selenium seja encerrado por falta de memória.

## Segurança e privacidade
- O bot ignora qualquer usuário cujo `id` seja diferente de `authorized_user_id`.
- Para dar acesso a outra pessoa, ajuste `authorized_user_id` ou implemente uma lista (o comando `/config` pode ser extendido facilmente).
- Nunca exponha seu `telegram_token`. Mantenha `.env` fora do controle de versão e `config.json` fora de repositórios públicos.

## Logs e tratamento de erros
- Logs vão para stdout (veja `logging.basicConfig` em `bot.py`). Em produção, use `journalctl -u telegram-stats-bot -f`.
- Quando uma plataforma erra/bloqueia, a resposta do `/stats` mostra o estado (“Erro” ou “Bloqueio temporário”), e o log detalha a exceção.
- Ajuste o TTL se notar muitos bloqueios (valores menores que 600 s podem aumentar o risco de CAPTCHA).

## Atualizações futuras
- Caso surjam APIs oficiais que não exijam Selenium, basta substituir o método correspondente em `PlatformScraper`.
- Novas redes podem ser integradas adicionando uma nova chave em `CONFIG_KEYS`, um método em `PlatformScraper` e incluindo no formatador da mensagem.

Pronto! Depois de configurar o token, seu ID e as URLs, o `/stats` responderá exatamente no formato solicitado, exibindo seguidores, views e likes. Qualquer dúvida, abra uma issue ou adapte o código diretamente no `bot.py`.
