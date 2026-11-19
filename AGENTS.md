# AGENTS.md — guia de desenvolvimento do Ops Detective

## O que é

Aplicação local de triagem N1 → N2 com **duas interfaces sobre a mesma camada de domínio**:

- `server.py` — servidor MCP (FastMCP + stdio) usado pelo Claude Desktop;
- `api.py` — API HTTP (FastAPI) que serve a interface web com SSE.

Sobre a ponte MCP roda `agente.py`, o agente de IA do chat: interpreta a mensagem e
decide quais tools chamar (roteador determinístico offline por padrão, Claude via
`ANTHROPIC_API_KEY` quando configurada). Ambas consomem `triagem.py`, que concentra
SQLite e regras de classificação. O
`painel_ao_vivo.py` é apenas o lançador da porta 8080 para a apresentação split-screen. Ver
[`CONTEXTO_HACKATHON.md`](CONTEXTO_HACKATHON.md) para a especificação,
[`README.md`](README.md) para instalação e demo e
[`APRESENTACAO.md`](APRESENTACAO.md) para o roteiro de palco.

## Comandos

```powershell
# instalar (Python + front)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install

# recriar banco de demonstração
.\.venv\Scripts\python.exe setup_db.py

# compilar CSS (necessário após mexer em index.html/app.css)
npm run build:css          # ou: npm run watch:css

# rodar a interface web (http://127.0.0.1:8000)
.\.venv\Scripts\python.exe api.py

# rodar o painel de apresentação (http://127.0.0.1:8080)
.\.venv\Scripts\python.exe painel_ao_vivo.py

# rodar o servidor MCP (stdio)
.\.venv\Scripts\python.exe server.py
```

Porta/host da API via `OPS_DETECTIVE_PORT` (padrão `8000`) e `OPS_DETECTIVE_HOST`
(padrão `127.0.0.1`). Banco via `OPS_DETECTIVE_DB`.

## Mapa do código

| Arquivo | Conteúdo |
|---|---|
| `setup_db.py` | schema + massa de dados (clientes, tickets, logs) |
| `triagem.py` | domínio: conexão SQLite, consultas, análise, triagem, telemetria global, injeção, pacote de engenharia, stats, playbook, health e reset |
| `server.py` | adaptador MCP: 8 tools + 1 resource (`playbook://triagem`) |
| `api.py` | adaptador HTTP: rotas `/api/*`, ponte MCP via stdio, SSE, arquivos estáticos |
| `agente.py` | agente de IA: roteador demo offline, tool-use Anthropic, eventos `agente_*` |
| `painel_ao_vivo.py` | lançador do painel de apresentação na porta 8080 (reusa `api.app`) |
| `index.html` | front-end (KPIs, 5 abas, chat flutuante do agente, auditoria, injeção, drawer com pacote N2, SSE + polling 1s) |
| `app.css` | entrada Tailwind v4 (`@import`, `@source`, tokens do design system) |
| `static/app.css` | CSS compilado, servido ao navegador — **versionado** |
| `package.json` | scripts `build:css` / `watch:css` (Tailwind v4) |
| `suporte.db` | SQLite gerado; ignorado no git |

## Convenções

- **Código e mensagens em português**; nomes de funções/tabelas em `snake_case`.
- **SQL sempre parametrizado** (`?`) — nada de f-string com entrada do usuário.
- Consultas abrem o banco em **modo read-only** (`mode=ro`), incluindo `logs_globais` e
  `gerar_pacote_engenharia`; só `triar_ticket`, `injetar_chamado` e `reset_demo` escrevem.
- `triagem.py` devolve `{"ok": true, ...}` ou `{"ok": false, "erro": "..."}` — nunca
  levanta exceção para erro de entrada, para o agente conseguir se corrigir.
- `api.py` usa `_json()` para traduzir isso: `ok: true` → 200; erro com "não encontrado"
  → 404; demais erros → 400.
- Timestamps em **ISO 8601 UTC** (strings `TEXT` no SQLite).
- Filtros/abas do front usam `status` (`aberto`, `em_triagem`, `devolvido_n1`,
  `escalado_n2`, `resolvido`), **não** `atribuido_para` (os tickets semeados já vêm com
  `atribuido_para='N1'` mesmo estando abertos).
- SSE publica `{"tipo": "triagem", ...}`, `{"tipo": "novo_chamado", ...}` e
  `{"tipo": "reset"}` no barramento em memória; o front recarrega fila/estatísticas ao
  receber. Eventos de clientes lentos são descartados (`QueueFull`) para não travar a API.
- A análise e a triagem do drawer passam pela **ponte MCP**: `api.py` inicia
  `server.py` em outro processo via `stdio_client`, chama `analisar_causa_raiz` /
  `triar_ticket` e devolve o JSON com `ferramenta`, `origem` e `tempo_ms`. O front
  mostra o estado “Agente MCP analisando…”, o selo MCP âmbar e a trilha de tool calls.
  A resposta sugerida (`resposta_cliente_sugerida`) é composta dentro da tool, no
  `triagem.py`, não no JavaScript. O card de engenharia segue o mesmo caminho:
  `POST /api/agente/pacote` → tool `gerar_pacote_engenharia`. Rotas legadas
  `/api/tickets/{id}/analise` e `/triagem` continuam funcionando, mas o front não as usa mais.
- A V2 adicionou 3 frentes no front: **aba `logs`** (`/api/logs?limite=50`, tabela com
  status colorido e `<details>` de stack trace), **auditoria** derivada de
  `state.tickets.filter(t => t.triado_em)` ordenado por `triado_em` (inclui escritas do
  MCP no chat) e **botão de injeção** (`POST /api/injetar-chamado` → `TK-1009` ou próximo,
  3 logs HTTP). O cenário vem de `CENARIOS_INJECAO`, escolhido por
  `(proximo - 1009) % len(CENARIOS_INJECAO)`: 7 incidentes distintos (429, 401, 500, 403,
  500, 402, 403), cada um com cliente/endpoint/mensagem próprios; o primeiro segue sendo
  Startup Cloud/`cliente_id=3`, urgência `crítica`, 3 logs 429. O protocolo é calculado
  pelo `MAX` dos `TK-\d+` existentes; o `id` do ticket é o `last_insert_rowid()`. A resposta
  traz `cenario`, `rotulo`, `status_code`, `destino_esperado` e vai no SSE `novo_chamado`.
- O agente (`POST /api/agente/chat`) recebe `mensagem` e `ticket_id?` e roda sobre **uma**
  `_sessao_mcp()` por turno — nunca uma sessão por tool, para não pagar ~2 s de startup do
  `server.py` a cada chamada. `agente.conversar()` publica `agente_inicio`, `agente_tool`
  (com nome/args/latência), `agente_erro` e `agente_resposta`; quando a tool `triar_ticket`
  grava, o próprio agente publica `triagem` com o mesmo `id` do turno. O front usa esse `id`
  para não duplicar a resposta entre SSE e a resposta HTTP.
- `agente.modo()` devolve `anthropic` se `ANTHROPIC_API_KEY` estiver definida; senão `demo`.
  O import de `anthropic` é **tardio** — o modo demo funciona sem o pacote instalado e sem
  rede. Ferramentas são descobertas via `list_tools()` e convertidas para `input_schema`;
  não duplique assinaturas de tools no agente. Teto: 6 iterações / 1024 tokens / 90 s.
- `gerar_pacote_engenharia` exige HTTP `5xx` na janela `_janela_do_ticket()`; para 4xx
  devolve `_erro(...)` (a API traduz para 400). O `resumo_tecnico` pega a última linha
  `File "…", line N` **antes** da exceção — não o entrypoint da API.
- O front também faz **polling de 1s** em `/api/tickets`, `/api/estatisticas` e
  `/api/health` como rede de segurança. Ele compara uma assinatura
  (`id:status:triado_em:categoria:diagnostico`) e, quando detecta uma escrita vinda do
  MCP no SQLite, aplica `flash-n1`/`flash-n2` ao card e o selo `ATUALIZADO AGORA`.
  O badge do topo mostra `SQLite Conectado (Ao Vivo) · N aguardando triagem`.

## Cenários semeado no banco

| Ticket | Cliente | Falha nos logs | Destino correto |
|---|---|---|---|
| TK-1001 | Loja Verão | `401` API key expirada | N1 |
| TK-1002 | Fintech X | `402` cartão recusado | N1 |
| TK-1003 | Startup Cloud | `429` limite do plano free | N1 |
| TK-1004 | LogExpress | `403` escopo faltando | N1 |
| TK-1005 | HealthData | `500` + stack trace (bug real) | N2 |
| TK-1006 | Construtora Alfa | `500` pool esgotado | N2 |
| TK-1007 | Fintech X | só `202` (nada falhou) | N1 |
| TK-1008 | Boutique Estilo | `403` conta inadimplente | N1 |

Além destes, o botão de injeção cria o **TK-1009+** com 3 logs de um catálogo rotacionado
de 7 incidentes: `429` (Startup Cloud), `401` (Loja Verão), `500` (HealthData), `403`
(LogExpress), `500` (Construtora Alfa), `402` (Fintech X) e `403` (Boutique Estilo), sempre
com endpoint e mensagem diferentes. O `TK-1009` repete o cenário original da apresentação.
Se mudar a massa de dados, atualize esta tabela e o roteiro no `README.md`.

## Rotas da API

`GET /` · `GET /api/health` · `GET /api/tickets` · `GET /api/tickets/{id}` ·
`GET /api/tickets/{id}/analise` · `GET /api/tickets/{id}/logs` ·
`POST /api/tickets/{id}/triagem` · `GET /api/agente/status` ·
`POST /api/agente/chat` · `POST /api/agente/analisar` · `POST /api/agente/triagem` ·
`POST /api/agente/pacote` · `GET /api/logs` · `POST /api/injetar-chamado` ·
`GET /api/estatisticas` · `GET /api/playbook` ·
`POST /api/reset` · `GET /api/events`.

## Teste rápido via cliente MCP (stdio)

```python
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=r".\.venv\Scripts\python.exe",
        args=[r"server.py"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print([t.name for t in (await session.list_tools()).tools])
            res = await session.call_tool("analisar_causa_raiz", {"ticket_id": 5})
            print(json.loads(res.content[0].text)["destino_sugerido"])

asyncio.run(main())
```

Teste rápido da API:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/tickets/5/analise
```

## Armadilhas conhecidas

- O SDK `mcp` **2.x** removeu `mcp.server.fastmcp` (virou `MCPServer`). O
  `requirements.txt` fixa `mcp>=1.9,<2` de propósito.
- Ao apontar o Claude Desktop para o servidor, use o Python **do `.venv`**;
  o Python global não tem o pacote `mcp` instalado.
- `suporte.db` não é versionado: rode `setup_db.py` depois de clonar.
- Cada chamada à ponte MCP sobe um `server.py` novo (~1,5–2 s no Windows). Isso é
  intencional na demo — o spinner visível prova que a resposta está sendo gerada —
  mas não use essas rotas para carga: o timeout do tool é 30 s.
- O navegador carrega `static/app.css`, não `app.css`: após alterar HTML/CSS, rode
  `npm run build:css`, senão as mudanças não aparecem.
- `npm install` só é necessário para gerar o CSS; a API em si roda só com o `requirements.txt`.
- `POST /api/reset`, `POST /api/injetar-chamado` e qualquer teste de `POST /triagem`
  alteram o banco; rode `setup_db.py` ao final para devolver a demo aos 8 tickets abertos.
- O Claude Desktop precisa ser reiniciado para carregar a 8ª tool (`gerar_pacote_engenharia`);
  a ponte MCP da API não sofre com isso porque sobe um `server.py` novo por chamada.
- Config real do Claude Desktop Store fica em
  `%LOCALAPPDATA%\Claude-Data\<perfil>\claude_desktop_config.json`, não em `%APPDATA%\Claude`.
