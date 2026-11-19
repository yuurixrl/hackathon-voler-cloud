# AGENTS.md — guia de desenvolvimento do Ops Detective

## O que é

Servidor MCP local (FastMCP + SQLite) que apoia a triagem de tickets N1 → N2. Ver
[`CONTEXTO_HACKATHON.md`](CONTEXTO_HACKATHON.md) para a especificação e
[`README.md`](README.md) para instalação e demo.

## Comandos

```powershell
# instalar
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# recriar banco de demonstração
.\.venv\Scripts\python.exe setup_db.py

# rodar o servidor (stdio)
.\.venv\Scripts\python.exe server.py
```

## Mapa do código

| Arquivo | Conteúdo |
|---|---|
| `setup_db.py` | schema + massa de dados (clientes, tickets, logs) |
| `server.py` | instância `FastMCP`, helpers de conexão e as 7 tools + 1 resource |
| `suporte.db` | SQLite gerado; ignorado no git |

## Convenções

- **Código e mensagens em português**; nomes de funções/tabelas em `snake_case`.
- **SQL sempre parametrizado** (`?`) — nada de f-string com entrada do usuário.
- Consultas abrem o banco em **modo read-only** (`mode=ro`); só `triar_ticket` escreve.
- Tools devolvem `{"ok": true, ...}` ou `{"ok": false, "erro": "..."}` — nunca levantam
  exceção para erro de entrada, para o Claude conseguir se corrigir.
- Timestamps em **ISO 8601 UTC** (strings `TEXT` no SQLite).

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

## Armadilhas conhecidas

- O SDK `mcp` **2.x** removeu `mcp.server.fastmcp` (virou `MCPServer`). O
  `requirements.txt` fixa `mcp>=1.9,<2` de propósito.
- Ao apontar o Claude Desktop para o servidor, use o Python **do `.venv`**;
  o Python global não tem o pacote `mcp` instalado.
- `suporte.db` não é versionado: rode `setup_db.py` depois de clonar.
