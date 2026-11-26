# Tech News Discord Bot

Bot assíncrono para Discord que realiza curadoria, deduplicação e publicação diária de notícias de tecnologia (PT-BR e EN), utilizando modelos do Google Gemini para seleção e síntese, com fallback resiliente para RSS/Hacker News e formatação em embeds por categoria.

## Destaques de Engenharia

- Pipeline assíncrono (`asyncio`, `httpx`, `discord.py`) projetado para baixo consumo de memória (~40 MB de RAM em produção).
- Curadoria inteligente em chamada única ao Google Gemini (`google-genai`), com tradução de manchetes e geração de TL;DR objetivo.
- Escada de fallback multicamadas: Grounding Web -> Curadoria sobre candidatos RSS -> Pipeline determinístico RSS puro.
- Validação pós-IA rigorosa: verificação HTTP de links, resolução de redirects, bloqueio de encurtadores/IPs e detecção de duplicatas cruzadas com fontes corroborantes.
- Destaque dinâmico de criticidade: promoção automática para categoria de alerta caso sejam identificados Zero-Days, CVEs críticos ou RCEs ativos.
- Persistência assíncrona em SQLite (`aiosqlite`) com expiração automática de cache e registros antigos.
- Cobertura de testes unitários e de integração com mocks completos de rede e LLM (`pytest`, `pytest-asyncio`).

## Categorias e Identidade Visual

As notícias são organizadas e agregadas em embeds temáticas com paleta de cores dedicada:

| Código | Categoria | Cor | Descrição |
|---|---|---|---|
| `critico` | Urgente / Zero-Day | `#F97316` | Destaque dinâmico no topo para Zero-Day, RCE e vulnerabilidades com exploração ativa |
| `sec` | Cibersegurança & Vulnerabilidades | `#EF4444` | Threat intel, avisos de agências (CISA), vazamentos e correções |
| `ai` | Inteligência Artificial & LLMs | `#8B5CF6` | Lançamentos de modelos, pesquisas e ecossistema de machine learning |
| `dev` | Codificação, Arquitetura & DevOps | `#3B82F6` | Engenharia de software, cloud, GitHub, linguagens e infraestrutura |
| `tech` | Tech Geral, Hardware & Mercado | `#10B981` | Notícias de mercado, hardware, semicondutores e ecossistema tech nacional |

## Arquitetura do Sistema

```
[Fontes RSS / Hacker News API]
             │
             ▼
[Filtro Temporal & Dedup Inicial (SQLite)]
             │
             ▼
[Curadoria Gemini (LLM)] ──(Se falhar)──> [Fallback Heurístico RSS]
             │
             ▼
[Validador Pós-IA] (HTTP Check, Anti-Hallucination, Limpeza HTML, og:image)
             │
             ▼
[Formatter & Rate Limiter] (Embeds agrupadas, limites Discord 6k chars/10 embeds)
             │
             ▼
[Discord Channel via Bot API]
```

1. **Ingestão (Janela 24h):** Coleta paralela das fontes configuradas em `feeds.yaml`.
2. **Deduplicação de Primeiro Nível:** Checagem contra histórico no SQLite e descarte de notícias com alta similaridade de título (`difflib`).
3. **Curadoria via LLM:**
   - `AI_MODE=candidates` (Recomendado/Free Tier): Envia candidatos filtrados ao Gemini em lote único com schema Pydantic estruturado.
   - `AI_MODE=auto`: Tenta grounding com busca na web (`google_search`) e desce automaticamente os degraus em caso de indisponibilidade de cota.
   - `AI_MODE=off`: Operação direta por relevância de feed e pontuação do Hacker News.
4. **Validação Estrita:** Inspeção de status code de URLs canônicas, verificação de idade e sanitização contra menções indevidas (`@everyone`, `@here`).
5. **Persistência:** Registro transacional em `data/sent.db` apenas após confirmação de entrega no canal do Discord.

## Comandos Slash

- `/noticias [forcar: bool]`: Dispara a publicação imediata do briefing diário no canal configurado (restrito a usuários com permissão `Gerenciar Servidor`). Se o digest do dia já estiver gerado, reutiliza o cache; com `forcar:True`, regenera respeitando o cooldown configurado.
- `/status`: Exibe status operacional do bot, fontes ativas, modo de IA em uso, timestamp da última execução e contagem de itens (resposta efêmera).

## Configuração do Discord

1. No [Discord Developer Portal](https://discord.com/developers/applications), crie uma nova aplicação.
2. Na aba **Bot**, gere o token de autenticação (`Reset Token`). Nenhuma Privileged Gateway Intent é necessária (mantenha `Presence`, `Server Members` e `Message Content` desativados).
3. Na aba **OAuth2 > URL Generator**, selecione os escopos `bot` e `applications.commands`, com as permissões:
   - `View Channels`
   - `Send Messages`
   - `Embed Links`
4. Adicione o bot ao seu servidor e copie o `Guild ID` e o `Channel ID`.

## Instalação e Execução Local

### Pré-requisitos

- Python 3.10 ou superior
- Git

### Passo a Passo

```bash
git clone https://github.com/SEU_USUARIO/tech-news-bot.git
cd tech-news-bot

python3 -m venv venv
source venv/bin/activate  # No Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
```

Edite o arquivo `.env` com suas credenciais:

```dotenv
DISCORD_TOKEN=seu_bot_token_aqui
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_GUILD_ID=123456789012345678
GEMINI_API_KEY=sua_chave_gemini_aqui
```

Inicie o bot:

```bash
python -m bot.main
```

## Variáveis de Ambiente

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `DISCORD_TOKEN` | Sim | - | Token de autenticação do bot no Discord |
| `DISCORD_CHANNEL_ID` | Sim | - | ID do canal de destino das postagens |
| `DISCORD_GUILD_ID` | Sim | - | ID do servidor (Guild) para sincronização instantânea de slash commands |
| `POST_TIME` | Não | `08:00` | Horário do envio diário programado (formato 24h `HH:MM`) |
| `TIMEZONE` | Não | `America/Fortaleza` | Fuso horário IANA válido para agendamento |
| `MAX_ARTICLES` | Não | `12` | Quantidade máxima de matérias em modo de fallback RSS |
| `MAX_AGE_HOURS` | Não | `24` | Janela máxima de publicação considerada na coleta |
| `DB_PATH` | Não | `data/sent.db` | Caminho do banco de dados SQLite local |
| `LOG_LEVEL` | Não | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `AI_MODE` | Não | `candidates` | Estratégia de curadoria: `candidates`, `auto`, `grounded` ou `off` |
| `GEMINI_API_KEY` | Se `AI_MODE != off` | - | Chave de API do Google AI Studio |
| `GEMINI_MODEL` | Não | `gemini-3.5-flash-lite` | Identificador do modelo Gemini |
| `AI_TOP_N` | Não | `12` | Quantidade alvo de notícias selecionadas pela IA |
| `AI_MIN_ITEMS` | Não | `4` | Mínimo aceitável de itens validados antes de complementar com RSS |
| `AI_MAX_CANDIDATES` | Não | `50` | Limite de matérias brutas enviadas no payload da LLM |
| `ALREADY_SENT_DAYS` | Não | `3` | Janela retroativa de títulos enviada à LLM para evitar repetições de assunto |
| `AI_TIMEOUT_SECONDS` | Não | `90` | Timeout por requisição com o modelo de IA |
| `AI_MAX_RETRIES` | Não | `3` | Retentativas com backoff exponencial para erros transientes |
| `AI_MAX_AGE_HOURS` | Não | `48` | Descarte de matérias validadas com publicação além desse limite |
| `MANUAL_COOLDOWN_MINUTES` | Não | `60` | Cooldown mínimo para forçar nova geração no comando `/noticias` |

## Deploy em Produção (Linux / systemd)

O projeto inclui um arquivo de serviço configurado para ambientes Linux com inicialização automática e recuperação de falhas:

1. Ajuste os caminhos e usuário em `tech-news-bot.service`.
2. Instale e ative a unit:

```bash
sudo cp tech-news-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tech-news-bot
```

3. Acompanhe os logs via journal:

```bash
journalctl -u tech-news-bot -f
```

## Testes Automatizados

O projeto conta com suíte de testes cobrindo parsing, deduplicação, formatação de mensagens, resiliência do agendador e validação de contratos com o modelo de IA.

```bash
pip install -r requirements-dev.txt
pytest -v
```

## Licença

Distribuído sob licença MIT. Consulte `LICENSE` para mais informações.
