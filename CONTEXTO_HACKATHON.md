# Projeto: Ops Detective (MCP Server para Triagem Inteligente N1 → N2)

**Evento:** Hackathon Voler Claude (Duração: 4 horas)
**Stack:** Python 3.12+ + FastMCP (`mcp` 1.x) + SQLite + Claude Desktop

> Documento original importado do Gemini estava truncado na seção 2 (árvore de arquivos).
> As seções 3-8 abaixo completam a especificação conforme a implementação.

---

## 1. Contexto para a IA (Leia antes de gerar código)

Estamos desenvolvendo um servidor **MCP (Model Context Protocol)** local em Python que conecta
o **Claude Desktop** a um banco de dados SQLite simulando o ambiente de suporte e
infraestrutura de uma empresa SaaS.

### O Problema (A Dor)

O suporte N1 recebe dezenas de chamados genéricos (ex: *"Sistema fora do ar!"*, *"Não consigo
pagar!"*). Sem acesso técnico aos logs da API ou ao banco de dados, o N1 escala tudo como
**URGENTE/CRÍTICO** para o time de Engenharia (N2). Isso gera *context switching* constante
nos desenvolvedores sêniores, que perdem horas investigando "bugs" que na verdade são apenas
erros cadastrais ou de uso do cliente (erros HTTP `4xx`).

### A Solução Prática

O MCP `ops-detective` dá ao Claude visão de raio-X controlada sobre os tickets, cadastros de
usuários e logs técnicos HTTP. O agente investiga a causa raiz sozinho, rebaixa falsos alarmes
para o N1 (já gerando a resposta pronta para o cliente) e isola apenas bugs reais (`HTTP 500`)
para a Engenharia N2 com o stack trace anexado.

---

## 2. Arquitetura Técnica Atual

### Estrutura de Arquivos

```text
ops-detective/
├── .venv/                        # Ambiente virtual Python
├── setup_db.py                   # Script que cria e popula o banco 'suporte.db'
├── server.py                     # Servidor MCP usando FastMCP (transporte stdio)
├── suporte.db                    # Banco SQLite gerado pelo setup_db.py
├── requirements.txt              # Dependências (mcp fixado em 1.x)
├── claude_desktop_config.example.json
├── README.md                     # Instalação, tools e roteiro de demo
├── AGENTS.md                     # Guia técnico do repositório
└── CONTEXTO_HACKATHON.md         # Este arquivo
```

---

## 3. Modelo de dados (`suporte.db`)

### `clientes`

| Campo | Tipo | Observação |
|---|---|---|
| `id` | INTEGER PK | |
| `nome`, `email`, `empresa` | TEXT | `email` único |
| `plano` | TEXT | `free` / `pro` / `enterprise` |
| `status_conta` | TEXT | `ativo` / `suspenso` / `inadimplente` / `cancelado` |
| `api_key`, `api_key_expira_em` | TEXT | permite detectar credencial expirada |
| `cartao_final`, `cartao_validade` | TEXT | permite detectar cartão vencido |
| `limite_requisicoes_min` | INTEGER | base para o `429` |
| `ultimo_login_em`, `criado_em` | TEXT (ISO 8601 UTC) | |

### `tickets`

| Campo | Tipo | Observação |
|---|---|---|
| `id`, `protocolo` | INTEGER PK / TEXT | protocolo `TK-1001`… |
| `cliente_id` | INTEGER FK | |
| `titulo`, `descricao` | TEXT | texto livre do cliente |
| `canal` | TEXT | `email` / `chat` / `telefone` / `whatsapp` / `formulario` |
| `urgencia_reportada` | TEXT | `baixa` / `media` / `alta` / `critica` |
| `status` | TEXT | `aberto` → `devolvido_n1` \| `escalado_n2` → `resolvido` |
| `atribuido_para` | TEXT | `N1` / `N2` |
| `categoria` | TEXT | causa raiz (lista fechada, ver §5) |
| `diagnostico`, `acao_recomendada`, `resposta_cliente` | TEXT | preenchidos na triagem |
| `criado_em`, `atualizado_em`, `triado_em` | TEXT | |

### `logs_http`

| Campo | Tipo | Observação |
|---|---|---|
| `id` | INTEGER PK | |
| `cliente_id` | INTEGER FK | |
| `timestamp` | TEXT | ISO 8601 UTC |
| `metodo`, `endpoint` | TEXT | ex: `GET /v1/relatorios/vendas` |
| `status_code` | INTEGER | `200`, `401`, `402`, `403`, `429`, `500`… |
| `latencia_ms` | INTEGER | permite detectar timeout |
| `request_id` | TEXT | correlação para anexar no N2 |
| `ip`, `user_agent` | TEXT | |
| `mensagem_erro` | TEXT | ex: `token_expirado`, `card_declined` |
| `stack_trace` | TEXT | preenchido só em erros `5xx` |

---

## 4. Ferramentas MCP (`server.py`)

| Tool | Entrada | Saída |
|---|---|---|
| `listar_tickets` | `status`, `urgencia`, `cliente_id`, `limite` | lista ordenada por urgência e tempo |
| `detalhar_ticket` | `ticket_id` | ticket completo + cliente + contagem de logs |
| `consultar_cliente` | `cliente_id` | cadastro + `alertas` (key expirada, conta suspensa, etc.) |
| `buscar_logs_http` | `cliente_id`, `endpoint`, `status_code`, `apenas_erros`, `minutos`, `limite`, `incluir_stack_trace` | logs filtrados |
| `analisar_causa_raiz` | `ticket_id` | evidências agregadas + `hipotese` + `destino_sugerido` |
| `triar_ticket` | `ticket_id`, `destino`, `categoria`, `diagnostico`, `acao_recomendada`, `resposta_cliente` | grava a triagem e muda o status |
| `estatisticas_triagem` | — | volume por status/destino/categoria + taxa de rebaixamento |

Recurso `playbook://triagem` (JSON): regras de decisão resumidas para o modelo.

### Regras de decisão

1. `401` / `token_expirado` → **N1** (renovar API key).
2. `402` / `card_declined` → **N1** (atualizar cartão).
3. `403` / `missing_scope` ou `account_suspended` → **N1** (escopo/permissão/financeiro).
4. `429` / `rate_limit_exceeded` → **N1** (upgrade de plano ou backoff).
5. `400` / payload inválido → **N1** (corrigir integração).
6. `5xx` com stack trace → **N2** (bug/infra) — anexar endpoint, stack trace e `request_id`.
7. Somente `2xx` → **N1** (problema do cliente ou dúvida de uso).
8. Sem logs → **N1** (validar autenticação/integração antes de escalar).

A janela de correlação é de **6h antes até 1h depois** da abertura do ticket, para não
misturar incidentes diferentes do mesmo cliente.

---

## 5. Categorias válidas

`credencial_expirada`, `permissao_escopo`, `limite_plano`, `pagamento_recusado`,
`conta_suspensa`, `erro_cadastral`, `duvida_uso`, `bug_aplicacao`, `infraestrutura`, `outro`.

---

## 6. Massa de dados de demonstração

7 clientes, 8 tickets, ~40 logs HTTP. Cenários:

| Ticket | Cliente | Reclamação | Causa real | Destino |
|---|---|---|---|---|
| TK-1001 | Loja Verão | "Sistema fora do ar" (crítica) | 6× `401` API key expirada | N1 |
| TK-1002 | Fintech X | "Não consigo pagar" | 3× `402` cartão recusado | N1 |
| TK-1003 | Startup Cloud | "API lenta/travando" | 9× `429` limite do plano free | N1 |
| TK-1004 | LogExpress | "Erro ao gerar relatório" | 4× `403` escopo ausente | N1 |
| TK-1005 | HealthData | "Erro 500 nos relatórios" | 4× `500` + `KeyError: 'segmento'` | **N2** |
| TK-1006 | Construtora Alfa | "Dashboard não carrega" | 5× `500` pool esgotado (30s) | **N2** |
| TK-1007 | Fintech X | "Sem e-mails de notificação" | só `202` — nada falhou | N1 |
| TK-1008 | Boutique Estilo | "Conta bloqueada" | 3× `403` conta inadimplente | N1 |

---

## 7. Critérios de aceite

- [x] `setup_db.py` recria o banco do zero e imprime as contagens.
- [x] `server.py` sobe por stdio e expõe 8 tools + 1 resource.
- [x] Para TK-1001/1002/1003/1004/1007/1008, `analisar_causa_raiz` sugere **N1**.
- [x] Para TK-1005/1006, sugere **N2** e inclui stack trace.
- [x] `triar_ticket` valida entrada, grava diagnóstico/resposta e não permite triagem dupla.
- [x] Claude Desktop lista o servidor e executa as tools.
- [x] O agente devolve ao N1 com resposta pronta ao cliente nos falsos alarmes.
- [x] V2: `/api/logs` expõe a telemetria global; o front tem a aba de Logs HTTP e o feed
  de auditoria derivado de `triado_em`.
- [x] V2: `POST /api/injetar-chamado` cria o TK-1009+ com 3 logs `429` e publica SSE.
- [x] V2: `gerar_pacote_engenharia` devolve o card Jira/Linear para 5xx e recusa 4xx.

---

## 8. Roteiro de apresentação (15 minutos — versão V2)

1. **Problema (40s):** mostrar `listar_tickets` com 8 chamados "críticos" e a fila do N2.
2. **Investigação (90s):** pedir análise do TK-1001 ("sistema fora do ar") → logs `401` +
   API key expirada → devolver ao N1 com resposta pronta.
3. **Contraste (60s):** pedir análise do TK-1005 → `500` + stack trace → escalar ao N2.
4. **Placar (40s):** `estatisticas_triagem` exibindo a taxa de rebaixamento para N1.
5. **Fechamento (30s):** "o N2 só recebe o que é bug de verdade, já com evidência".

O roteiro coreografado de 15 minutos (com aba de logs, auditoria, pacote de engenharia
e injeção ao vivo) está em [`APRESENTACAO.md`](APRESENTACAO.md).
