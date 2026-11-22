# Tech News Discord Bot

Bot de Discord que entrega um Radar Tech matinal (resumo de 24 horas) com as principais noticias de tecnologia em PT-BR e EN, curadas e traduzidas pelo Google Gemini e exibidas em embeds agrupadas por categoria com sistema de cores.

Projetado para rodar 24/7 com baixo consumo de memoria e pouca escrita em disco em SBCs ARM64 rodando Armbian (Rockchip).

## Categorias e Identidade Visual

O briefing agrupa os destaques em 1 embed por categoria (evitando flood no canal):

| Codigo | Categoria | Cor | Descricao |
|---|---|---|---|
| `critico` | Urgente / Zero-Day | `#F97316` | Destaque dinamico no topo para Zero-Day, RCE, CVEs criticos e exploracao ativa |
| `sec` | Ciberseguranca e Vulnerabilidades | `#EF4444` | Threat intel, vulnerabilidades, vazamentos e avisos oficiais (ex: CISA) |
| `ai` | Inteligencia Artificial e LLMs | `#8B5CF6` | Lancamentos de modelos, pesquisa e ferramentas de IA |
| `dev` | Codificacao, Arquitetura e DevOps | `#3B82F6` | Engenharia de software, cloud, GitHub, linguagens e DevOps |
| `tech` | Tech Geral, Hardware e Mercado | `#10B981` | Hardware, chips, mercado, big techs e tecnologia no Brasil |

## Arquitetura e Escada de Fallback

1. **Coleta de candidatos (janela 24h):** feeds RSS segmentados por categoria em `feeds.yaml` + top stories do Hacker News.
2. **Curadoria com Gemini (`google-genai`):**
   - `AI_MODE=candidates` (padrao para chave gratuita): envia os candidatos em 1 chamada ao Gemini (`gemini-3.5-flash-lite`), que seleciona os melhores, funde duplicatas, detecta alertas criticos, traduz titulos para PT-BR e gera TL;DR.
   - `AI_MODE=auto`: tenta busca na web (`google_search` + schema -> `google_search` sem schema) e desce automaticamente para `candidates` se o plano nao tiver grounding.
   - **Modo C (fallback RSS puro):** se a API do Gemini falhar, o bot ainda publica o Radar Tech agrupado por categoria a partir dos feeds.
3. **Validacao pos-IA:** valida URLs via GET (HTTPS, resolve redirects, bloqueia IPs/encurtadores, exige URL exata dos candidatos no Modo B), bloqueia duplicatas por `corroborating_sources` e similaridade de titulo, filtra por idade e sanitiza HTML/mencoes.
4. **Persistencia SQLite (`data/sent.db`):** cache diario (`daily_digest`, retencao 30 dias) e historico de enviados (`sent_articles`, retencao 60 dias).

## Comandos Slash

- `/noticias [forcar: bool]`: publica o Radar Tech do dia no canal configurado (exige `Gerenciar Servidor`). Reaproveita o cache do dia sem gastar cota; com `forcar:True`, regenera respeitando `MANUAL_COOLDOWN_MINUTES`.
- `/status`: mostra proximo envio, fontes ativas, modo IA configurado, ultimo modo efetivamente usado, ultima geracao, total de itens e status do envio (resposta efemera).

## Requisitos no Discord

1. No Discord Developer Portal, crie uma aplicacao e um bot.
2. Nenhum intent privilegiado e necessario (mantenha `Presence`, `Server Members` e `Message Content` desativados).
3. Na aba **Bot**, clique em **Reset Token** para gerar o `DISCORD_TOKEN`.
4. Em **OAuth2 > URL Generator**, gere o convite com:
   - Escopos: `bot`, `applications.commands`
   - Permissoes: `View Channel`, `Send Messages`, `Embed Links`
5. Convide o bot para o servidor e copie `DISCORD_GUILD_ID` e `DISCORD_CHANNEL_ID`.

## Configuracao

Copie `.env.example` para `.env` e preencha os valores:

```bash
cp .env.example .env
chmod 600 .env
```

Variaveis principais:

| Variavel | Obrigatoria | Padrao | Descricao |
|---|---|---|---|
| `DISCORD_TOKEN` | Sim | - | Token do bot no Discord (aba Bot) |
| `DISCORD_CHANNEL_ID` | Sim | - | ID do canal onde o Radar Tech sera postado |
| `DISCORD_GUILD_ID` | Sim | - | ID do servidor para sincronizacao dos slash commands |
| `POST_TIME` | Nao | `08:00` | Horario diario de envio no formato `HH:MM` (24h) |
| `TIMEZONE` | Nao | `America/Fortaleza` | Fuso horario IANA (`zoneinfo`) |
| `MAX_ARTICLES` | Nao | `12` | Maximo de noticias no fallback RSS |
| `MAX_AGE_HOURS` | Nao | `24` | Janela de coleta RSS (em horas) |
| `DB_PATH` | Nao | `data/sent.db` | Caminho do banco SQLite |
| `LOG_LEVEL` | Nao | `INFO` | Nivel de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `AI_MODE` | Nao | `candidates` | `candidates`, `auto`, `grounded` ou `off` |
| `GEMINI_API_KEY` | Se `AI_MODE != off` | - | Chave da API do Google Gemini |
| `GEMINI_MODEL` | Nao | `gemini-3.5-flash-lite` | Modelo Gemini utilizado na curadoria |
| `AI_TOP_N` | Nao | `12` | Total desejado de noticias (1 a 20, agrupadas por categoria) |
| `AI_MIN_ITEMS` | Nao | `4` | Minimo de itens validados antes de completar com RSS |
| `AI_MAX_CANDIDATES` | Nao | `50` | Maximo de candidatos enviados ao Gemini no Modo B |
| `ALREADY_SENT_DAYS` | Nao | `3` | Janela de dias de titulos enviados passada ao modelo |
| `AI_TIMEOUT_SECONDS` | Nao | `90` | Timeout da chamada ao Gemini |
| `AI_MAX_RETRIES` | Nao | `3` | Tentativas em caso de erro transitorio (429/503/timeout) |
| `AI_MAX_AGE_HOURS` | Nao | `48` | Descarta item da IA mais antigo que esse limite |
| `MANUAL_COOLDOWN_MINUTES` | Nao | `60` | Cooldown para regeneracao manual via `/noticias forcar:True` |

As fontes de coleta podem ser editadas diretamente em `feeds.yaml`.

## Smoke Test do Gemini

```bash
./venv/bin/python scripts/check_gemini.py
```

## Deploy no Armbian (Rockchip)

Considerando o Armbian atualizado e com Python 3 ja instalado:

### 1. Clonar e criar ambiente virtual

Caso o modulo `venv` ainda nao esteja presente, instale apenas o pacote `python3-venv`:

```bash
sudo apt install -y python3-venv
```

Em seguida, configure o projeto:

```bash
git clone <url-do-repo> ~/tech-news-bot
cd ~/tech-news-bot
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edite `.env` com suas credenciais e teste a execucao manual antes de instalar o servico:

```bash
./venv/bin/python -m bot.main
```

### 2. Configurar servico systemd

Edite `tech-news-bot.service` substituindo `SEU_USUARIO` pelo seu usuario no Armbian e instale a unit:

```bash
sudo cp tech-news-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tech-news-bot
```

Acompanhe os logs pelo `journalctl`:

```bash
journalctl -u tech-news-bot -f
```

### 3. Checklist do host

- Verifique se o relogio e o NTP estao ativos (essencial para o horario agendado):

```bash
timedatectl status
```

- Garanta que `.env` tenha permissao restrita (`chmod 600 .env`).
- Nunca versione `.env` nem a pasta `data/` (ambos ja estao no `.gitignore`).

## Testes

Para rodar a suite de testes localmente (sem chamar a API real do Gemini):

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/pytest
```
