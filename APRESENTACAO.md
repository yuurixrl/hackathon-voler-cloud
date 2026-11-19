# Ops Detective — Roteiro de Apresentação Ao Vivo (Split-Screen)

Guia de palco para provar, em 3 minutos, que o **MCP está conectando o modelo a uma
infraestrutura real** — e não é só um chat que responde texto.

## A ideia

| Metade da tela | O que roda | O que a banca vê |
|---|---|---|
| **Esquerda — Sistema da empresa** | `painel_ao_vivo.py` → `http://127.0.0.1:8080` | Fila real lida do `suporte.db`, com destaque verde/vermelho no instante da escrita |
| **Direita — O Agente** | Claude Desktop com as 7 tools do `server.py` (MCP/stdio) | O modelo investigando logs e chamando `analisar_causa_raiz` / `triar_ticket` |

```text
 Claude / Codex (chat)  -- triar_ticket() -->  server.py (MCP)
                                                     │ UPDATE no SQLite
 painel_ao_vivo.py (8080) -- GET /api/* --------> suporte.db
        │
        └── SSE + polling 1s --> index.html destaca o card e recalcula os KPIs
```

O painel **não** é um chat: ele lê o mesmo banco que o MCP escreve. Quando o agente
triagem um chamado, o card pisca (**verde** = devolvido ao N1, **vermelho** = escalado
ao N2), ganha o selo **`ATUALIZADO AGORA`**, mostra o diagnóstico gravado e libera o
botão de copiar resposta. O badge no topo passa a exibir
**`SQLite Conectado (Ao Vivo) · N aguardando triagem`**.

> O SSE entrega a mudança na hora; o polling de 1 segundo é a rede de segurança caso a
> conexão SSE caia. Os dois caminhos terminam na mesma função `triagem.py`.

## Preparação (antes de subir)

**Terminal 1 — devolve o caos inicial (8 chamados abertos):**

```powershell
.\.venv\Scripts\python.exe setup_db.py
```

**Terminal 2 — sobe o painel na porta 8080:**

```powershell
.\.venv\Scripts\python.exe painel_ao_vivo.py
```

Abra `http://127.0.0.1:8080` na **esquerda** e deixe o Claude Desktop na **direita**.
Confira o selo verde no topo e os 8 chamados aguardando triagem.

## Roteiro coreografado (3 minutos)

### Ato 1 — O cenário de caos (30 s)

Aponte para a esquerda:

> "Aqui está a fila real do suporte: 8 chamados abertos, vários críticos, todos
> aguardando investigação. No fluxo tradicional, o N1 não enxerga os logs da API e
> joga tudo para os desenvolvedores."

### Ato 2 — Barrando um falso alarme ao vivo (1 min)

Digite no chat da direita:

> "Analise a causa raiz do ticket TK-1001 da Loja Verão e registre a triagem no banco de dados."

O que a banca vê:

- o agente consulta os logs e encontra 6 erros `HTTP 401` (API key expirada há 2 dias);
- à esquerda, o card **TK-1001 pisca em verde**, muda para **DEVOLVIDO N1 (FALSO ALARME)**,
  mostra o diagnóstico vindo do SQLite e libera **Copiar Resposta N1**.

### Ato 3 — Isolando um bug real com stack trace (1 min)

Digite no chat da direita:

> "Agora investigue o TK-1005 da HealthData e faça a triagem."

O que a banca vê:

- o agente detecta `HTTP 500` com `KeyError: 'segmento'` em `report_service.py`;
- à esquerda, o card **TK-1005 fica vermelho** como **ESCALADO N2 (BUG REAL)** —
  vai para engenharia só o que exige código, já com arquivo e linha do erro.

### Ato 4 — Limpando a fila e placar final (30 s)

Digite no chat da direita:

> "Faça a triagem de todos os tickets restantes da fila e me mostre as estatísticas finais."

Fechamento: os cards se organizam sozinhos — **6 devolvidos ao N1 e apenas 2 bugs
reais para o N2** — e o KPI **Economia Estimada de Dev** chega a **3h 30m**
(6 × 35 min), atualizado sem recarregar a página.

## Dicas de palco

- Se o modelo não usar as ferramentas, cite os nomes: `analisar_causa_raiz`,
  `buscar_logs_http`, `triar_ticket` e `estatisticas_triagem`.
- O destino correto é determinado pela regra em `triagem.py`: `4xx → N1`,
  `5xx + stack trace → N2`, sem erro → N1. O agente pode discordar, e o sistema
  avisa quando isso acontece — isso também é bom material de defesa.
- Ao final, clique em **Resetar Fila (Demo)** no painel (ou rode `setup_db.py`) para
  repetir a apresentação do zero.
- Se o badge ficar vermelho, o banco não foi encontrado: rode `setup_db.py`. Se ficar
  âmbar, o SSE caiu; o polling de 1 s continua funcionando e o `EventSource` reconecta.
