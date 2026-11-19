# Ops Detective

Servidor **MCP (Model Context Protocol)** local **e interface web** que dão ao time de
suporte uma visão de raio-X sobre tickets, cadastro de clientes e logs HTTP de uma
empresa SaaS fictícia.

**Objetivo:** acabar com o *"escala tudo como URGENTE"*. O agente investiga a causa raiz,
devolve falsos alarmes (HTTP `4xx`) ao N1 já com a resposta pronta para o cliente e isola
apenas bugs reais (`HTTP 5xx` com stack trace) para a engenharia N2.

- **Stack:** Python 3.12+ · FastAPI + uvicorn · FastMCP (`mcp` 1.x) · SQLite · Tailwind CSS v4 · SSE
- **Transportes:** stdio (Claude Desktop) e HTTP (navegador, mesma origem)
- **Autenticação:** fora de escopo nesta versão (uso local/demo)

---

## Estrutura

```text
ops-detective/
├── setup_db.py                    # cria e popula o suporte.db
├── triagem.py                     # camada de domínio: SQLite + regras de triagem
├── server.py                      # adaptador MCP (FastMCP, stdio)
├── api.py                         # API HTTP + SSE + arquivos estáticos
├── index.html                     # interface web
├── app.css                        # entrada do Tailwind v4 (tokens do design system)
├── static/app.css                 # CSS compilado (versionado)
├── package.json                   # scripts build:css / watch:css
├── suporte.db                     # banco gerado (não versionado)
├── requirements.txt
├── claude_desktop_config.example.json
├── CONTEXTO_HACKATHON.md          # especificação do projeto
├── AGENTS.md                      # guia para agentes/devs que mexem no repo
└── .venv/                         # ambiente virtual (não versionado)
```

A lógica de banco e classificação vive **uma única vez** em `triagem.py`. `server.py`
(MCP) e `api.py` (web) são apenas adaptadores — os dois veem exatamente os mesmos dados.

## Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_db.py

# front-end (Tailwind v4)
npm install
npm run build:css
```

O `setup_db.py` recria o banco do zero (7 clientes, 8 tickets, 38 logs HTTP).
Use `--keep` para não apagar um banco existente.

## Rodar a interface web

```powershell
.\.venv\Scripts\python.exe api.py
```

Abra **http://127.0.0.1:8000**. Para trocar endereço/porta:

```powershell
$env:OPS_DETECTIVE_PORT = "8011"   # padrão: 8000
$env:OPS_DETECTIVE_HOST = "127.0.0.1"
.\.venv\Scripts\python.exe api.py
```

A tela traz KPIs (bloqueios N2, falsos alarmes N1 e economia estimada), quatro visões
da fila, um painel lateral com evidências (status codes, endpoints, mensagens, stack
traces e logs) e o formulário para registrar a triagem. O badge **“Dados ao Vivo”**
indica que o canal SSE está conectado: triagens e resets feitos em qualquer aba (ou
pelo Claude Desktop, no mesmo banco) atualizam a interface automaticamente.

### Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | interface web |
| `GET` | `/api/health` | estado do banco e contadores |
| `GET` | `/api/tickets` | fila (`status`, `urgencia`, `cliente_id`, `limite`) |
| `GET` | `/api/tickets/{id}` | ticket + cliente + volume de logs |
| `GET` | `/api/tickets/{id}/analise` | evidências, hipótese e destino sugerido |
| `GET` | `/api/tickets/{id}/logs` | logs HTTP (`limite`, `apenas_erros`, `incluir_stack_trace`) |
| `POST` | `/api/tickets/{id}/triagem` | grava a triagem (JSON: `destino`, `categoria`, `diagnostico`, `acao_recomendada`, `resposta_cliente`) |
| `GET` | `/api/estatisticas` | placar por status/destino/categoria |
| `GET` | `/api/playbook` | regras e categorias válidas |
| `POST` | `/api/reset` | recria o banco de demonstração |
| `GET` | `/api/events` | SSE (`triagem`, `reset`, `conectado`) |

Erros de entrada devolvem `{"ok": false, "erro": "..."}` com HTTP 400; recurso
inexistente retorna 404.

> Se editar `index.html` ou `app.css`, rode `npm run build:css` (ou `npm run watch:css`).
> O navegador carrega somente `static/app.css`.

## Conectar ao Claude Desktop

O servidor MCP continua disponível e aponta para a mesma camada `triagem.py`.

1. Abra (ou crie) o arquivo `%APPDATA%\Claude\claude_desktop_config.json`.
   Na instalação via Claude Store, o caminho pode ser
   `%LOCALAPPDATA%\Claude-Data\<perfil>\claude_desktop_config.json`.
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

Na web: clique em **Analisar Próximo** para abrir o primeiro ticket pendente, confira as
evidências no drawer e registre a triagem. **Resetar Fila (Demo)** devolve tudo ao estado
inicial.

## Testes rápidos

```powershell
# 1) banco
.\.venv\Scripts\python.exe setup_db.py

# 2) API (Ctrl+C para sair)
.\.venv\Scripts\python.exe api.py

# 3) em outro terminal, verificações simples
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/tickets/5/analise

# 4) servidor MCP sobe sem erro? (Ctrl+C para sair)
.\.venv\Scripts\python.exe server.py
```

O MCP se comunica por stdio — sem o Claude Desktop ele fica aguardando uma mensagem
JSON-RPC. Para um teste programático, veja o exemplo de client em
[`AGENTS.md`](AGENTS.md).

## Decisões técnicas

- **Camada de domínio única:** `triagem.py` é usada por MCP e API; evita divergência de
  regras entre o Claude e a interface.
- **`mcp<2` fixado no `requirements.txt`:** o SDK 2.x renomeou `FastMCP` para `MCPServer`
  e quebrou imports; o projeto usa a API FastMCP clássica.
- **Banco somente leitura nas consultas:** as leituras abrem o SQLite com `mode=ro`;
  apenas `triar_ticket` e o reset escrevem.
- **Janela de análise:** `analisar_causa_raiz` olha de 6h antes até 1h depois da abertura
  do ticket, evitando que logs de outros incidentes do mesmo cliente contaminem o diagnóstico.
- **SSE em vez de polling:** o servidor mantém filas por navegador e publica `triagem` e
  `reset`; reconexão automática via `EventSource` (`retry: 3000`).
- **Tailwind v4 compilado:** `app.css` é a entrada com os tokens do design system; o
  resultado vai para `static/app.css` e é versionado (não é preciso Node para rodar a API).
