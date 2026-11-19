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
    estatisticas_triagem - placar dos atendimentos

Recurso:
    playbook://triagem   - regras de decisão do time de suporte
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("OPS_DETECTIVE_DB", BASE_DIR / "suporte.db"))

CATEGORIAS = (
    "credencial_expirada",
    "permissao_escopo",
    "limite_plano",
    "pagamento_recusado",
    "conta_suspensa",
    "erro_cadastral",
    "duvida_uso",
    "bug_aplicacao",
    "infraestrutura",
    "outro",
)

INSTRUCTIONS = """Você é o Ops Detective, agente de triagem de suporte nível 1 -> nível 2.

Objetivo: descobrir a causa raiz de cada ticket antes de escalar. Nunca escale para N2
sem evidência técnica. Regras:
- HTTP 4xx (401, 402, 403, 429, 400...) = problema de cadastro, credencial, plano ou uso do
  cliente -> devolva ao N1 com a resposta pronta para o cliente.
- HTTP 5xx (500, 502, 503...) com stack trace = bug real -> escale ao N2 anexando o stack trace.
- Nenhum erro nos logs = problema do lado do cliente ou dúvida de uso -> N1.
Use `analisar_causa_raiz` antes de decidir e `triar_ticket` para registrar a conclusão.
"""

mcp = FastMCP("ops-detective", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Infraestrutura
# --------------------------------------------------------------------------- #

def _connect(readonly: bool = True) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Banco não encontrado em {DB_PATH}. Rode `python setup_db.py` primeiro."
        )
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _erro(mensagem: str) -> dict[str, Any]:
    return {"ok": False, "erro": mensagem}


def _janela_do_ticket(criado_em: str | None) -> tuple[str | None, str | None]:
    """Janela do incidente: 6h antes da abertura do ticket até 1h depois."""
    criado = _parse_ts(criado_em)
    if criado is None:
        return None, None
    return (criado - timedelta(hours=6)).isoformat(), (criado + timedelta(hours=1)).isoformat()


RESUMO_TICKET = """
    SELECT t.id, t.protocolo, t.titulo, t.status, t.atribuido_para,
           t.urgencia_reportada, t.categoria, t.criado_em, t.triado_em,
           c.id AS cliente_id, c.nome AS cliente, c.empresa, c.plano, c.status_conta
      FROM tickets t
      JOIN clientes c ON c.id = t.cliente_id
"""


# --------------------------------------------------------------------------- #
# Ferramentas
# --------------------------------------------------------------------------- #

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
    if status and status not in ("aberto", "em_triagem", "devolvido_n1", "escalado_n2", "resolvido"):
        return _erro(f"status inválido: {status}")
    if urgencia and urgencia not in ("baixa", "media", "alta", "critica"):
        return _erro(f"urgência inválida: {urgencia}")

    limite = max(1, min(int(limite), 100))
    where, params = [], []
    if status:
        where.append("t.status = ?")
        params.append(status)
    if urgencia:
        where.append("t.urgencia_reportada = ?")
        params.append(urgencia)
    if cliente_id is not None:
        where.append("t.cliente_id = ?")
        params.append(int(cliente_id))

    sql = RESUMO_TICKET
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE t.urgencia_reportada WHEN 'critica' THEN 0 WHEN 'alta' THEN 1"\
           " WHEN 'media' THEN 2 ELSE 3 END, t.criado_em ASC LIMIT ?"
    params.append(limite)

    with _connect() as conn:
        tickets = _rows(conn.execute(sql, params).fetchall())

    return {"ok": True, "total": len(tickets), "tickets": tickets}


@mcp.tool()
def detalhar_ticket(ticket_id: int) -> dict[str, Any]:
    """Retorna o ticket completo, o cliente associado e o volume de logs do cliente.

    Args:
        ticket_id: id numérico do ticket (não é o protocolo TK-xxxx)
    """
    with _connect() as conn:
        ticket = _row(
            conn.execute(
                """
                SELECT t.*, c.nome AS cliente, c.email, c.empresa, c.plano,
                       c.status_conta, c.limite_requisicoes_min
                  FROM tickets t
                  JOIN clientes c ON c.id = t.cliente_id
                 WHERE t.id = ?
                """,
                (int(ticket_id),),
            ).fetchone()
        )
        if not ticket:
            return _erro(f"ticket {ticket_id} não encontrado")

        total_logs, erros = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(status_code >= 400), 0)
              FROM logs_http WHERE cliente_id = ?
            """,
            (ticket["cliente_id"],),
        ).fetchone()

    ticket["logs_do_cliente"] = {"total": total_logs, "com_erro": erros}
    return {"ok": True, "ticket": ticket}


@mcp.tool()
def consultar_cliente(cliente_id: int) -> dict[str, Any]:
    """Consulta o cadastro do cliente: plano, status da conta, API key e cartão.

    Útil para detectar credencial expirada, conta suspensa, limite do plano e
    cartão vencido antes de escalar um ticket.

    Args:
        cliente_id: id numérico do cliente
    """
    with _connect() as conn:
        cliente = _row(conn.execute("SELECT * FROM clientes WHERE id = ?", (int(cliente_id),)).fetchone())
        if not cliente:
            return _erro(f"cliente {cliente_id} não encontrado")

        cliente["total_tickets"] = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE cliente_id = ?", (cliente["id"],)
        ).fetchone()[0]

    expira = _parse_ts(cliente.get("api_key_expira_em"))
    alertas: list[str] = []
    if expira and expira < _now():
        alertas.append(f"API key expirada desde {expira.date().isoformat()}")
    if cliente["status_conta"] != "ativo":
        alertas.append(f"Conta com status '{cliente['status_conta']}'")
    if cliente["plano"] == "free" and not cliente.get("cartao_final"):
        alertas.append("Plano free sem cartão cadastrado (limite baixo: %s req/min)" % cliente["limite_requisicoes_min"])

    cliente["alertas"] = alertas
    return {"ok": True, "cliente": cliente}


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
    limite = max(1, min(int(limite), 200))
    where = ["cliente_id = ?"]
    params: list[Any] = [int(cliente_id)]

    if endpoint:
        where.append("endpoint LIKE ?")
        params.append(f"%{endpoint}%")
    if status_code is not None:
        where.append("status_code = ?")
        params.append(int(status_code))
    if apenas_erros:
        where.append("status_code >= 400")
    if minutos and minutos > 0:
        where.append("timestamp >= ?")
        params.append((_now() - timedelta(minutes=int(minutos))).isoformat())

    sql = "SELECT * FROM logs_http WHERE " + " AND ".join(where) + " ORDER BY timestamp DESC LIMIT ?"
    params.append(limite)

    with _connect() as conn:
        logs = _rows(conn.execute(sql, params).fetchall())

    if not incluir_stack_trace:
        for log in logs:
            log.pop("stack_trace", None)

    return {"ok": True, "total": len(logs), "logs": logs}


@mcp.tool()
def analisar_causa_raiz(ticket_id: int) -> dict[str, Any]:
    """Monta o pacote de evidências do ticket e sugere o destino (N1 ou N2).

    Cruza os logs HTTP da janela do chamado com o cadastro do cliente e
    classifica automaticamente: 4xx -> problema do cliente (N1),
    5xx -> bug de aplicação/infra (N2).

    Args:
        ticket_id: id numérico do ticket
    """
    with _connect() as conn:
        ticket = _row(
            conn.execute(
                """
                SELECT t.*, c.nome AS cliente, c.empresa, c.plano, c.status_conta,
                       c.api_key_expira_em, c.limite_requisicoes_min, c.cartao_final
                  FROM tickets t JOIN clientes c ON c.id = t.cliente_id
                 WHERE t.id = ?
                """,
                (int(ticket_id),),
            ).fetchone()
        )
        if not ticket:
            return _erro(f"ticket {ticket_id} não encontrado")

        logs = _rows(
            conn.execute(
                """
                SELECT timestamp, metodo, endpoint, status_code, latencia_ms,
                       request_id, mensagem_erro, stack_trace
                  FROM logs_http
                 WHERE cliente_id = ? AND timestamp BETWEEN ? AND ?
                 ORDER BY timestamp DESC
                 LIMIT 500
                """,
                (
                    ticket["cliente_id"],
                    *(_janela_do_ticket(ticket["criado_em"])),
                ),
            ).fetchall()
        )

    # Janela do incidente: do ticket até 6h antes dele (captura o que gerou a reclamação)
    inicio, _fim = _janela_do_ticket(ticket["criado_em"])

    por_status: dict[str, int] = {}
    por_endpoint: dict[str, int] = {}
    mensagens: list[str] = []
    stack_traces: list[str] = []
    latencias = []

    for log in logs:
        chave = str(log["status_code"])
        por_status[chave] = por_status.get(chave, 0) + 1
        por_endpoint[log["endpoint"]] = por_endpoint.get(log["endpoint"], 0) + 1
        latencias.append(log["latencia_ms"])
        if log["mensagem_erro"] and log["mensagem_erro"] not in mensagens:
            mensagens.append(log["mensagem_erro"])
        if log["stack_trace"] and log["stack_trace"] not in stack_traces:
            stack_traces.append(log["stack_trace"])

    tem_5xx = any(l["status_code"] >= 500 for l in logs)
    tem_4xx = any(400 <= l["status_code"] < 500 for l in logs)
    detalhes_4xx = {
        400: "payload/formato de requisição inválido (uso incorreto da API)",
        401: "credencial inválida ou expirada (API key)",
        402: "pagamento recusado (cartão ou gateway)",
        403: "permissão/escopo insuficiente ou conta bloqueada",
        404: "endpoint inexistente (integração desatualizada)",
        429: "limite de requisições do plano excedido",
    }

    hipotese = "Indefinida"
    destino = "N1"
    if tem_5xx:
        hipotese = "Bug de aplicação ou falha de infraestrutura (HTTP 5xx com stack trace)"
        destino = "N2"
    elif tem_4xx:
        codigos = sorted({l["status_code"] for l in logs if l["status_code"] >= 400})
        descricoes = [f"{c}: {detalhes_4xx.get(c, 'erro de cliente')}" for c in codigos]
        hipotese = "Falso alarme: erro do cliente / configuração — " + "; ".join(descricoes)
    elif logs:
        hipotese = "Nenhuma falha nos logs: requisições bem-sucedidas (cliente ou dúvida de uso)"
    else:
        hipotese = "Sem logs no período: verificar se o cliente está autenticando corretamente na API"

    alertas: list[str] = []
    expira = _parse_ts(ticket.get("api_key_expira_em"))
    if expira and expira < _now():
        alertas.append(f"API key expirada desde {expira.date().isoformat()}")
    if ticket["status_conta"] != "ativo":
        alertas.append(f"Conta '{ticket['status_conta']}'")
    if len(latencias) and max(latencias) >= 20000:
        alertas.append(f"Latência máxima de {max(latencias)} ms (possível timeout/sobrecarga)")

    return {
        "ok": True,
        "ticket": {
            "id": ticket["id"],
            "protocolo": ticket["protocolo"],
            "titulo": ticket["titulo"],
            "urgencia_reportada": ticket["urgencia_reportada"],
            "cliente": ticket["cliente"],
            "empresa": ticket["empresa"],
            "plano": ticket["plano"],
        },
        "janela_analisada": {
            "de": inicio,
            "ate": _fim,
            "logs_analisados": len(logs),
        },
        "evidencias": {
            "por_status_code": por_status,
            "por_endpoint": por_endpoint,
            "mensagens_distintas": mensagens[:10],
            "stack_traces": stack_traces[:3],
            "latencia_ms": {
                "min": min(latencias) if latencias else None,
                "max": max(latencias) if latencias else None,
                "media": round(sum(latencias) / len(latencias)) if latencias else None,
            },
        },
        "hipotese": hipotese,
        "destino_sugerido": destino,
        "alertas_cadastro": alertas,
        "proximo_passo": (
            "Escalar para N2 com o stack trace e os request_ids."
            if destino == "N2"
            else "Devolver ao N1 com orientação ao cliente e registrar a triagem."
        ),
    }


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
    destino = (destino or "").upper()
    if destino not in ("N1", "N2"):
        return _erro("destino deve ser 'N1' ou 'N2'")
    if categoria not in CATEGORIAS:
        return _erro(f"categoria inválida. Use uma de: {', '.join(CATEGORIAS)}")
    campos = {
        "diagnostico": diagnostico,
        "acao_recomendada": acao_recomendada,
        "resposta_cliente": resposta_cliente,
    }
    for nome, valor in campos.items():
        if not valor or len(valor.strip()) < 10:
            return _erro(f"o campo '{nome}' precisa ter pelo menos 10 caracteres")

    with _connect(readonly=False) as conn:
        ticket = _row(
            conn.execute(
                "SELECT t.*, c.nome AS cliente FROM tickets t JOIN clientes c ON c.id = t.cliente_id WHERE t.id = ?",
                (int(ticket_id),),
            ).fetchone()
        )
        if not ticket:
            return _erro(f"ticket {ticket_id} não encontrado")
        if ticket["status"] in ("devolvido_n1", "escalado_n2", "resolvido"):
            return _erro(
                f"ticket já triado (status atual: {ticket['status']}). "
                "Reabra ou crie um novo ticket para nova análise."
            )

        status_novo = "devolvido_n1" if destino == "N1" else "escalado_n2"
        agora = _now().replace(microsecond=0).isoformat()
        conn.execute(
            """
            UPDATE tickets
               SET status = ?, atribuido_para = ?, categoria = ?, diagnostico = ?,
                   acao_recomendada = ?, resposta_cliente = ?, atualizado_em = ?, triado_em = ?
             WHERE id = ?
            """,
            (
                status_novo,
                destino,
                categoria,
                diagnostico.strip(),
                acao_recomendada.strip(),
                resposta_cliente.strip(),
                agora,
                agora,
                int(ticket_id),
            ),
        )
        conn.commit()

        tem_5xx = conn.execute(
            """
            SELECT COALESCE(SUM(status_code >= 500), 0)
              FROM logs_http
             WHERE cliente_id = ? AND timestamp BETWEEN ? AND ?
            """,
            (ticket["cliente_id"], *(_janela_do_ticket(ticket["criado_em"]))),
        ).fetchone()[0]

    avisos: list[str] = []
    if destino == "N2" and not tem_5xx:
        avisos.append("Não há HTTP 5xx nos logs deste cliente: confirme a evidência antes de escalar.")
    if destino == "N1" and tem_5xx:
        avisos.append("Existem HTTP 5xx nos logs deste cliente: possível escalação indevida ao N1.")

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "protocolo": ticket["protocolo"],
        "cliente": ticket["cliente"],
        "status": status_novo,
        "atribuido_para": destino,
        "categoria": categoria,
        "avisos": avisos,
    }


@mcp.tool()
def estatisticas_triagem() -> dict[str, Any]:
    """Mostra o placar da triagem: volume por status, destino e categoria."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        por_status = _rows(
            conn.execute("SELECT status, COUNT(*) AS total FROM tickets GROUP BY status ORDER BY total DESC").fetchall()
        )
        por_destino = _rows(
            conn.execute(
                "SELECT atribuido_para, COUNT(*) AS total FROM tickets GROUP BY atribuido_para"
            ).fetchall()
        )
        por_categoria = _rows(
            conn.execute(
                "SELECT categoria, COUNT(*) AS total FROM tickets "
                "WHERE categoria IS NOT NULL GROUP BY categoria ORDER BY total DESC"
            ).fetchall()
        )
        pendentes = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'aberto'").fetchone()[0]
        rebaixados = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'devolvido_n1'"
        ).fetchone()[0]

    triados = total - pendentes

    return {
        "ok": True,
        "total_tickets": total,
        "pendentes_de_triagem": pendentes,
        "triados": triados,
        "por_status": por_status,
        "por_destino": por_destino,
        "por_categoria": por_categoria,
        "taxa_rebaixamento_para_n1": round(rebaixados / triados * 100, 1) if triados else 0.0,
    }


@mcp.resource("playbook://triagem")
def playbook_triagem() -> str:
    """Regras de decisão do time de suporte (N1 x N2)."""
    return json.dumps(
        {
            "titulo": "Playbook de triagem N1 -> N2",
            "regras": [
                {
                    "evidencia": "HTTP 401 / token_expirado",
                    "causa": "Credencial expirada ou revogada",
                    "destino": "N1",
                    "acao": "Orientar o cliente a gerar nova API key no painel.",
                },
                {
                    "evidencia": "HTTP 402 / card_declined",
                    "causa": "Cartão recusado pelo emissor",
                    "destino": "N1",
                    "acao": "Pedir atualização do cartão; não é falha do gateway.",
                },
                {
                    "evidencia": "HTTP 403 / missing_scope",
                    "causa": "Permissão insuficiente na credencial",
                    "destino": "N1",
                    "acao": "Ajustar escopos da API key ou permissões do usuário.",
                },
                {
                    "evidencia": "HTTP 403 / account_suspended",
                    "causa": "Conta inadimplente ou suspensa",
                    "destino": "N1",
                    "acao": "Encaminhar ao financeiro e orientar regularização.",
                },
                {
                    "evidencia": "HTTP 429 / rate_limit_exceeded",
                    "causa": "Limite do plano excedido",
                    "destino": "N1",
                    "acao": "Sugerir upgrade de plano ou backoff na integração.",
                },
                {
                    "evidencia": "HTTP 500+ com stack trace",
                    "causa": "Bug de aplicação ou falha de infraestrutura",
                    "destino": "N2",
                    "acao": "Escalar com stack trace, endpoint e request_ids.",
                },
                {
                    "evidencia": "Somente HTTP 2xx nos logs",
                    "causa": "Falha do lado do cliente ou dúvida de uso",
                    "destino": "N1",
                    "acao": "Validar ambiente do cliente (rede, spam, configuração).",
                },
            ],
            "categorias_validas": list(CATEGORIAS),
        },
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
