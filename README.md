# Ops Detective

Servidor **MCP (Model Context Protocol)** local que dá ao Claude Desktop visão de raio-X
sobre tickets de suporte, cadastro de clientes e logs HTTP de uma empresa SaaS fictícia.

**Objetivo:** acabar com o *"escala tudo como URGENTE"*. O agente investiga a causa raiz,
devolve falsos alarmes (HTTP `4xx`) ao N1 já com a resposta pronta para o cliente e isola
apenas bugs reais (`HTTP 5xx` com stack trace) para a engenharia N2.

- **Stack:** Python 3.12+ · FastMCP (`mcp` 1.x) · SQLite · Claude Desktop
- **Transporte:** stdio

---

## Estrutura

```text
ops-detective/
├── setup_db.py                    # cria e popula o suporte.db
├── server.py                      # servidor MCP (FastMCP, stdio)
├── suporte.db                     # banco gerado (não versionado)
├── requirements.txt
├── claude_desktop_config.example.json
├── CONTEXTO_HACKATHON.md          # especificação do projeto
├── AGENTS.md                      # guia para agentes/devs que mexem no repo
└── .venv/                         # ambiente virtual (não versionado)
```

## Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_db.py
```

O `setup_db.py` recria o banco do zero (7 clientes, 8 tickets, ~40 logs HTTP).
Use `--keep` para não apagar um banco existente.

## Conectar ao Claude Desktop

1. Abra (ou crie) o arquivo `%APPDATA%\Claude\claude_desktop_config.json`.
2. Adicione o servidor apontando para o **Python do `.venv`**:

```json
{
  "mcpServers": {
    "ops-detective": {
      "command": "C:\\Users\\yurig\\hackathon-voler-cloud\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\yurig\\hackathon-voler-cloud\\server.py"]
    }
  }
}
```

3. Reinicie o Claude Desktop (sair pelo ícone da bandeja, não só fechar a janela).
4. No chat, clique no ícone de ferramentas e confirme que `ops-detective` aparece com 7 tools.

> O modelo do arquivo está em [`claude_desktop_config.example.json`](claude_desktop_config.example.json).

## Ferramentas expostas

| Tool | Para que serve |
|---|---|
| `listar_tickets` | Fila de chamados com filtros por status, urgência e cliente |
| `detalhar_ticket` | Ticket completo + dados do cliente + volume de logs |
| `consultar_cliente` | Plano, status da conta, validade da API key, cartão, alertas |
| `buscar_logs_http` | Rastro técnico por cliente, endpoint, status code e janela de tempo |
| `analisar_causa_raiz` | Pacote de evidências + hipótese + destino sugerido (N1/N2) |
| `triar_ticket` | Registra diagnóstico, ação e resposta ao cliente; muda o status |
| `estatisticas_triagem` | Placar por status, destino e categoria + taxa de rebaixamento |

Recurso: `playbook://triagem` — regras de decisão do time de suporte.

## Roteiro de demonstração (3 tickets)

| Ticket | Reclamação | Evidência real | Resultado |
|---|---|---|---|
| `TK-1001` | "Sistema fora do ar, URGENTE!" | 6× HTTP `401` (API key expirada há 2 dias) | **N1** — renovar credencial |
| `TK-1002` | "Não consigo pagar" | 3× HTTP `402` `card_declined` | **N1** — trocar cartão |
| `TK-1005` | "Erro 500 ao gerar relatório" | 4× HTTP `500` + stack trace `KeyError: 'segmento'` | **N2** — bug real |

Prompt para abrir a demo:

> "Sou o N1. Temos 8 tickets na fila e a diretoria está cobrando. Analise cada ticket,
> descubra a causa raiz com os logs e me diga quais voltam para o N1 e quais vão para o N2.
> Para os que voltam, já escreva a resposta ao cliente."

## Testes rápidos

```powershell
# 1) banco
.\.venv\Scripts\python.exe setup_db.py

# 2) servidor sobe sem erro? (Ctrl+C para sair)
.\.venv\Scripts\python.exe server.py
```

O servidor se comunica por stdio — sem o Claude Desktop ele fica aguardando uma
mensagem JSON-RPC. Para um teste programático, veja o exemplo de client em
[`AGENTS.md`](AGENTS.md).

## Decisões técnicas

- **`mcp<2` fixado no `requirements.txt`:** o SDK 2.x renomeou `FastMCP` para `MCPServer`
  e quebrou imports; o projeto usa a API FastMCP clássica.
- **Banco somente leitura nas consultas:** as tools abrem o SQLite com `mode=ro`;
  apenas `triar_ticket` escreve.
- **Janela de análise:** `analisar_causa_raiz` olha de 6h antes até 1h depois da abertura
  do ticket, evitando que logs de outros incidentes do mesmo cliente contaminem o diagnóstico.
