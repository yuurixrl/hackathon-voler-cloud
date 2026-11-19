# AGENTS.md — guia de desenvolvimento do Ops Detective

## O que é

Aplicação local de triagem N1 → N2 com **duas interfaces sobre a mesma camada de domínio**:

- `server.py` — servidor MCP (FastMCP + stdio) usado pelo Claude Desktop;
- `api.py` — API HTTP (FastAPI) que serve a interface web com SSE.

Ambas consomem `triagem.py`, que concentra SQLite e regras de classificação. O
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
| `triagem.py` | domínio: conexão SQLite, consultas, análise de causa raiz, triagem, stats, playbook, health e reset |
| `server.py` | adaptador MCP: 7 tools + 1 resource (`playbook://triagem`) |
| `api.py` | adaptador HTTP: rotas `/api/*`, SSE, arquivos estáticos |
| `painel_ao_vivo.py` | lançador do painel de apresentação na porta 8080 (reusa `api.app`) |
| `index.html` | front-end (KPIs, abas, drawer, evidências, formulário, SSE + polling 1s) |
| `app.css` | entrada Tailwind v4 (`@import`, `@source`, tokens do design system) |
| `static/app.css` | CSS compilado, servido ao navegador — **versionado** |
| `package.json` | scripts `build:css` / `watch:css` (Tailwind v4) |
| `suporte.db` | SQLite gerado; ignorado no git |

## Convenções

- **Código e mensagens em português**; nomes de funções/tabelas em `snake_case`.
- **SQL sempre parametrizado** (`?`) — nada de f-string com entrada do usuário.
- Consultas abrem o banco em **modo read-only** (`mode=ro`); só `triar_ticket` e
  `reset_demo` escrevem.
- `triagem.py` devolve `{"ok": true, ...}` ou `{"ok": false, "erro": "..."}` — nunca
  levanta exceção para erro de entrada, para o agente conseguir se corrigir.
- `api.py` usa `_json()` para traduzir isso: `ok: true` → 200; erro com "não encontrado"
  → 404; demais erros → 400.
- Timestamps em **ISO 8601 UTC** (strings `TEXT` no SQLite).
- Filtros/abas do front usam `status` (`aberto`, `em_triagem`, `devolvido_n1`,
  `escalado_n2`, `resolvido`), **não** `atribuido_para` (os tickets semeados já vêm com
  `atribuido_para='N1'` mesmo estando abertos).
- SSE publica `{"tipo": "triagem", ...}` e `{"tipo": "reset"}` no barramento em memória;
  o front recarrega fila/estatísticas ao receber. Eventos de clientes lentos são
  descartados (`QueueFull`) para não travar a API.
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

Se mudar a massa de dados, atualize esta tabela e o roteiro no `README.md`.

## Rotas da API

`GET /` · `GET /api/health` · `GET /api/tickets` · `GET /api/tickets/{id}` ·
`GET /api/tickets/{id}/analise` · `GET /api/tickets/{id}/logs` ·
`POST /api/tickets/{id}/triagem` · `GET /api/estatisticas` · `GET /api/playbook` ·
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
- O navegador carrega `static/app.css`, não `app.css`: após alterar HTML/CSS, rode
  `npm run build:css`, senão as mudanças não aparecem.
- `npm install` só é necessário para gerar o CSS; a API em si roda só com o `requirements.txt`.
- `POST /api/reset` e qualquer teste de `POST /triagem` alteram o banco; rode
  `setup_db.py` ao final para devolver a demo aos 8 tickets abertos.
- Config real do Claude Desktop Store fica em
  `%LOCALAPPDATA%\Claude-Data\<perfil>\claude_desktop_config.json`, não em `%APPDATA%\Claude`.
