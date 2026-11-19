"""Servidor MCP `ops-detective` — triagem inteligente N1 -> N2.

Expõe ao Claude Desktop (via stdio) uma visão controlada do banco `suporte.db`:
tickets abertos, cadastro dos clientes e logs HTTP da API.

Ferramentas:
    listar_tickets       - fila de chamados com filtros
    detalhar_ticket      - ticket completo + cliente
    consultar_cliente    - plano, status, validade da API key, cartão
    buscar_logs_http     - rastro técnico por cliente/endpoint/status
    analisar_causa_raiz  - pacote de evidências + hipótese (4xx x 5xx)
    triar_ticket         - devolve ao N1 ou escala ao N2 (grava a triagem)
    gerar_pacote_engenharia - card técnico Jira/Linear para bugs reais (N2)
    estatisticas_triagem - placar dos atendimentos

Recurso:
    playbook://triagem   - regras de decisão do time de suporte

A lógica de banco e classificação vive em `triagem.py`; este arquivo é apenas o
adaptador MCP (o mesmo domínio alimenta a API web em `api.py`).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import triagem

INSTRUCTIONS = """Você é o Ops Detective, agente de triagem de suporte nível 1 -> nível 2.

Objetivo: descobrir a causa raiz de cada ticket antes de escalar. Nunca escale para N2
sem evidência técnica. Regras:
- HTTP 4xx (401, 402, 403, 429, 400...) = problema de cadastro, credencial, plano ou uso do
  cliente -> devolva ao N1 com a resposta pronta para o cliente.
- HTTP 5xx (500, 502, 503...) com stack trace = bug real -> escale ao N2 anexando o stack trace.
- Nenhum erro nos logs = problema do lado do cliente ou dúvida de uso -> N1.
Use `analisar_causa_raiz` antes de decidir e `triar_ticket` para registrar a conclusão.
Quando a conclusão for N2, chame também `gerar_pacote_engenharia(ticket_id)` para entregar
ao time de desenvolvimento um card com título, severidade, endpoint, request_ids,
resumo técnico, stack trace e critério de aceite — e anexe esse card ao escalonamento.
"""

mcp = FastMCP("ops-detective", instructions=INSTRUCTIONS)


@mcp.tool()
def listar_tickets(
    status: str | None = None,
    urgencia: str | None = None,
    cliente_id: int | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    """Lista a fila de chamados com filtros opcionais.

    Args:
        status: aberto | em_triagem | devolvido_n1 | escalado_n2 | resolvido
        urgencia: baixa | media | alta | critica (urgência alegada pelo cliente)
        cliente_id: filtra os tickets de um cliente específico
        limite: máximo de tickets retornados (1-100)
    """
    return triagem.listar_tickets(
        status=status, urgencia=urgencia, cliente_id=cliente_id, limite=limite
    )


@mcp.tool()
def detalhar_ticket(ticket_id: int) -> dict[str, Any]:
    """Retorna o ticket completo, o cliente associado e o volume de logs do cliente.

    Args:
        ticket_id: id numérico do ticket (não é o protocolo TK-xxxx)
    """
    return triagem.detalhar_ticket(ticket_id)


@mcp.tool()
def consultar_cliente(cliente_id: int) -> dict[str, Any]:
    """Consulta o cadastro do cliente: plano, status da conta, API key e cartão.

    Útil para detectar credencial expirada, conta suspensa, limite do plano e
    cartão vencido antes de escalar um ticket.

    Args:
        cliente_id: id numérico do cliente
    """
    return triagem.consultar_cliente(cliente_id)


@mcp.tool()
def buscar_logs_http(
    cliente_id: int,
    endpoint: str | None = None,
    status_code: int | None = None,
    apenas_erros: bool = True,
    minutos: int = 1440,
    limite: int = 50,
    incluir_stack_trace: bool = False,
) -> dict[str, Any]:
    """Busca os logs HTTP de um cliente para investigar a causa raiz.

    Args:
        cliente_id: id numérico do cliente
        endpoint: filtra por trecho do endpoint (ex: /v1/relatorios)
        status_code: filtra por código exato (ex: 500)
        apenas_erros: se True, retorna somente status >= 400
        minutos: janela de tempo para trás (padrão 1440 = 24h; use 0 para tudo)
        limite: máximo de linhas (1-200)
        incluir_stack_trace: inclui o stack trace completo (arquivos maiores)
    """
    return triagem.buscar_logs_http(
        cliente_id=cliente_id,
        endpoint=endpoint,
        status_code=status_code,
        apenas_erros=apenas_erros,
        minutos=minutos,
        limite=limite,
        incluir_stack_trace=incluir_stack_trace,
    )


@mcp.tool()
def analisar_causa_raiz(ticket_id: int) -> dict[str, Any]:
    """Monta o pacote de evidências do ticket e sugere o destino (N1 ou N2).

    Cruza os logs HTTP da janela do chamado com o cadastro do cliente e
    classifica automaticamente: 4xx -> problema do cliente (N1),
    5xx -> bug de aplicação/infra (N2).

    Args:
        ticket_id: id numérico do ticket
    """
    return triagem.analisar_causa_raiz(ticket_id)


@mcp.tool()
def triar_ticket(
    ticket_id: int,
    destino: str,
    categoria: str,
    diagnostico: str,
    acao_recomendada: str,
    resposta_cliente: str,
) -> dict[str, Any]:
    """Registra a triagem do ticket e o encaminha ao destino correto.

    Args:
        ticket_id: id numérico do ticket
        destino: N1 (falso alarme, volta ao suporte) ou N2 (bug real, engenharia)
        categoria: natureza da causa raiz (ver lista de categorias)
        diagnostico: explicação técnica da causa raiz
        acao_recomendada: o que o time deve fazer a seguir
        resposta_cliente: mensagem pronta para enviar ao cliente
    """
    return triagem.triar_ticket(
        ticket_id=ticket_id,
        destino=destino,
        categoria=categoria,
        diagnostico=diagnostico,
        acao_recomendada=acao_recomendada,
        resposta_cliente=resposta_cliente,
    )


@mcp.tool()
def gerar_pacote_engenharia(ticket_id: int) -> dict[str, Any]:
    """Gera um card técnico estruturado (estilo Jira/Linear) com evidências e stack trace para bugs reais (N2).

    Só funciona quando há HTTP 5xx na janela do chamado. Para falsos alarmes (4xx)
    a tool devolve `{"ok": false, "erro": ...}` — não há o que enviar à engenharia.

    Args:
        ticket_id: id numérico do ticket (ex: 5 para TK-1005, 6 para TK-1006)
    """
    return triagem.gerar_pacote_engenharia(ticket_id)


@mcp.tool()
def estatisticas_triagem() -> dict[str, Any]:
    """Mostra o placar da triagem: volume por status, destino e categoria."""
    return triagem.estatisticas_triagem()


@mcp.resource("playbook://triagem")
def playbook_triagem() -> str:
    """Regras de decisão do time de suporte (N1 x N2)."""
    return triagem.playbook_json()


if __name__ == "__main__":
    mcp.run()
