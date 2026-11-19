# Ops Detective — Roteiro de Apresentação Ao Vivo (15 minutos)

Guia de palco para provar que o **MCP está conectando o modelo a uma infraestrutura
real** — com telemetria, auditoria, injeção de incidente e entrega pronta para a engenharia.

## A ideia

| Metade da tela | O que roda | O que a banca vê |
|---|---|---|
| **Esquerda — Sistema da empresa** | `painel_ao_vivo.py` → `http://127.0.0.1:8080` | Fila real do `suporte.db`, aba de logs HTTP, auditoria e cards que piscam no instante da escrita |
| **Direita — O Agente** | Claude Desktop (com chave) **ou** a aba **Agente IA** do próprio painel (offline) — ambos sobre as 8 tools do `server.py` via MCP/stdio | O modelo/roteador investigando logs e chamando `analisar_causa_raiz`, `triar_ticket` e `gerar_pacote_engenharia` em tempo real |

```text
 Claude Desktop (direita) -- tools MCP ------------------+
 aba Agente IA (esquerda) -- POST /api/agente/chat ------+--> server.py (stdio) -- UPDATE --> suporte.db
                                                        ▲       ▲
 painel (8080) -- POST /api/agente/* --> api.py --+       | leitura mode=ro
         │                                                         |
         ├── /api/logs --------------------------------------------+
         └── SSE (agente_*, triagem, ...) <--- suporte.db ---> card pisca, KPIs mudam, auditoria cresce
```

O painel **agora também é um chat**: a 6ª aba **Agente IA** interpreta a mensagem e chama
as mesmas tools do `server.py`. Sem `ANTHROPIC_API_KEY` ela roda em **modo demo offline**
(roteador determinístico em `agente.py`); com a chave, faz tool-use real com Claude. Nos
dois casos o agente **nunca escreve direto no SQLite** — toda ação passa pelo MCP. O chat
mostra o modo ativo, a trilha de tools (`agente_inicio` → `agente_tool` →
`agente_resposta`, via SSE) e a latência de cada chamada.

Mesmo fora do chat, o painel prova que a resposta não está decorada no HTML: ao abrir
um chamado, ele mostra **“Agente MCP analisando…”**, dispara a tool
`analisar_causa_raiz` em `server.py` (processo separado, ~2 s de subida) e só depois
exibe o diagnóstico, a hipótese e a resposta sugerida — com a **trilha das tool calls**
e a latência de cada uma. O selo **`MCP · 8 tools prontas`** fica verde no topo quando
o servidor responde; fica âmbar enquanto uma tool está em execução.

Quando o agente triar um chamado, o card pisca (**verde** = devolvido ao N1,
**vermelho** = escalado ao N2), ganha o selo **`ATUALIZADO AGORA`**, mostra o diagnóstico
gravado e libera o botão de copiar resposta. O outro badge exibe
**`SQLite Conectado (Ao Vivo) · N aguardando triagem`**. Toda triagem — venha do chat ou
do painel — aparece também no painel **Atividade Recente do Agente MCP**.

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
Se o Claude ainda mostrar 7 tools, reinicie-o (sair pelo ícone da bandeja) para carregar
a 8ª, `gerar_pacote_engenharia`. No navegador, use Ctrl+F5 se o CSS estiver em cache.
Confira os dois selos verdes no topo e os 8 chamados aguardando triagem.

## Roteiro coreografado (15 minutos)

### 00:00 – 03:00 — A dor do suporte N1 → N2

Aponte para a esquerda:

> "Aqui está a fila real do suporte: 8 chamados abertos, vários críticos, todos
> aguardando investigação. O N1 não enxerga os logs da API e, para não assumir risco,
> joga tudo para os desenvolvedores. Cada falso alarme custa uma troca de contexto de
> ~35 minutos para a engenharia."

Mostre os KPIs: **Travando Engenharia (N2)**, **Falsos Alarmes Barrados (N1)** e
**Economia Estimada de Dev**.

### 03:00 – 05:00 — Arquitetura MCP (por que é seguro)

Explique o desenho no quadro, junto ao diagrama acima:

- `triagem.py` é a única camada de domínio — regras e SQLite em um só lugar.
- `server.py` é o adaptador MCP (stdio); `api.py` é o adaptador HTTP; o painel 8080
  reaproveita a mesma `api.app`.
- **Leitura sempre em `mode=ro`.** Escrita só em três pontos controlados:
  `triar_ticket`, `injetar_chamado` e `reset_demo`.
- A ponte MCP do painel sobe `server.py` em **outro processo** via `stdio_client` —
  o mesmo protocolo do Claude Desktop. por isso o spinner dura ~2 s: é a prova visual
  de que a resposta está sendo gerada sob demanda, não servida de um arquivo estático.

### 05:00 – 12:00 — Demo ao vivo

**1) Falso alarme (≈2 min).** Digite no chat da direita:

> "Analise a causa raiz do ticket TK-1001 da Loja Verão e registre a triagem no banco de dados."

- O agente encontra 6 erros `HTTP 401` (API key expirada há 2 dias).
- Abra o TK-1001 no painel: selo MCP âmbar, `analisar_causa_raiz(ticket_id=1)`,
  diagnóstico aparecendo depois da latência medida.
- O card **pisca em verde**, vira **DEVOLVIDO N1**, libera **Copiar Resposta N1**.
- Role até **Atividade Recente do Agente MCP**: `[Hora] TK-1001 → Devolvido N1
  (credencial_expirada)` já está lá.

**2) Agente IA dentro do painel (≈1 min).** Clique na 6ª aba **Agente IA**. O selo mostra
**"Demo offline (roteador determinístico) · tools via MCP"** — sem chave paga e sem
internet. Digite:

> "Me mostre as estatísticas da fila" e depois "Analise o TK-1003"

- A trilha aparece em tempo real: `estatisticas_triagem({})` e
  `analisar_causa_raiz(ticket_id=3)`, cada uma com sua latência.
- O agente responde em linguagem natural, sem escrever no banco (só leitura via MCP).
- Para ver o agente **escrever** pelo chat, digite "Trie o TK-1005 e gere o pacote de
  engenharia": o card pisca N2 e a auditoria cresce exatamente como no passo 4.
- Reforce o contraste para a banca: é o mesmo `server.py`, a mesma tool e a mesma regra
  em `triagem.py`; muda só quem decide (roteador offline vs. Claude com chave).

**3) Logs HTTP ao vivo (≈1 min).** Clique na 5ª aba **Logs HTTP (Telemetria)**.

- Mostre os 38 eventos reais do banco: empresa, `POST /v1/relatorios/atendimentos`,
  badges verdes/âmbar/vermelhos, latência e `request_id`.
- Abra um `<details>` de stack trace e conecte com o que o agente acabou de dizer.

**4) Bug real + pacote de engenharia (≈2 min).** Digite no chat:

> "Agora investigue o TK-1005 da HealthData, faça a triagem e gere o pacote para engenharia."

- O agente detecta `HTTP 500` com `KeyError: 'segmento'` em `report_service.py:142`.
- O card **fica vermelho** como **ESCALADO N2** e a auditoria ganha a linha N2.
- No drawer do TK-1005, clique em **Gerar pacote engenharia**: o botão mostra
  `gerar_pacote_engenharia(ticket_id=5)` e aparece o card estilo Jira/Linear com
  severidade `P1 - Crítica (Cliente Enterprise)`, endpoint, 4 request IDs,
  `KeyError: 'segmento' em /app/services/report_service.py:142`, stack trace completo e
  critério de aceite. Clique em **Copiar card para o Jira**.

**5) Injeção de chamado ao vivo (≈1 min).** Clique em **⚡ Injetar Chamado Ao Vivo**.

- Toast: **"Novo chamado TK-1009: Rate limit do plano free (HTTP 429)"**
- O `TK-1009` da Startup Cloud (plano free, urgência **CRÍTICA**) entra na fila com
  3 logs `429 rate_limit_exceeded: plano=free limite=60 req/min` em
  `POST /v1/pedidos/sincronizar`.
- Abra o chamado no painel e deixe o agente analisar na hora: o destino sugerido é
  **N1** — é um limite de plano, não um bug. Trie pelo chat ou pelo formulário e mostre
  a nova linha aparecendo na auditoria.
- **Repita o botão:** o `TK-1010` já chega com outro problema (401 da Loja Verão em
  `/v1/webhooks/catalogo`), o `TK-1011` com um 500 de faturamento (N2, com stack trace e
  pacote de engenharia disponível) e assim por diante — 7 cenários distintos antes de
  repetir. O toast e a aba **Logs HTTP** mostram a diferença na hora.

### 12:00 – 15:00 — Impacto de negócio e perguntas

Se ainda houver tickets abertos, digite:

> "Faça a triagem de todos os tickets restantes da fila e me mostre as estatísticas finais."

Fechamento: os cards se organizam sozinhos — **6 devolvidos ao N1 e apenas 2 bugs
reais para o N2** — e o KPI **Economia Estimada de Dev** chega a **3h 30m**
(6 × 35 min), atualizado sem recarregar a página. Reforce:

- falso alarme barrado antes de consumir engenharia;
- bug real chega ao dev com evidência e card pronto, sem ida e volta;
- injeção ao vivo prova que o sistema reage a incidente novo, não só a massa de teste;
- auditoria responde "quem decidiu o quê e quando" — inclusive para ações do agente.

## Dicas de palco

- Se o modelo não usar as ferramentas, cite os nomes: `analisar_causa_raiz`,
  `buscar_logs_http`, `triar_ticket`, `gerar_pacote_engenharia` e
  `estatisticas_triagem`.
- A aba **Agente IA** funciona sem `ANTHROPIC_API_KEY` (modo demo offline). Se o
  organizador configurar a chave, o selo muda para **Anthropic (LLM real)** e o Claude
  escolhe as tools sozinho; a trilha SSE e as regras de negócio são as mesmas.
- O destino correto é determinado pela regra em `triagem.py`: `4xx → N1`,
  `5xx + stack trace → N2`, sem erro → N1. O agente pode discordar, e o sistema
  avisa quando isso acontece — isso também é bom material de defesa.
- `gerar_pacote_engenharia` recusa tickets N1 com `{"ok": false, ...}`: se alguém
  perguntar, responda que não faz sentido abrir issue de engenharia para erro de cliente.
- Ao final, clique em **Resetar Fila (Demo)** no painel (ou rode `setup_db.py`) para
  repetir a apresentação do zero. O reset também apaga os tickets injetados.
- Se o badge SQLite ficar vermelho, rode `setup_db.py`. Se ficar âmbar, o SSE caiu;
  o polling de 1 s continua funcionando e o `EventSource` reconecta.
- Se o badge MCP ficar vermelho, o `server.py` não subiu: use sempre o Python do
  `.venv` (o global não tem o pacote `mcp`).
