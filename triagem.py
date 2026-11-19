"""Camada de domínio do Ops Detective.

Concentra o acesso ao SQLite e as regras de triagem N1 -> N2. É consumida por
dois adaptadores:

- `server.py`  -> servidor MCP (stdio) usado pelo Claude Desktop;
- `api.py`     -> API HTTP (FastAPI) usada pela interface web.

Nenhuma função aqui depende de MCP ou HTTP: todas devolvem dicionários
`{"ok": true, ...}` ou `{"ok": false, "erro": "..."}`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

STATUS_VALIDOS = ("aberto", "em_triagem", "devolvido_n1", "escalado_n2", "resolvido")
URGENCIAS_VALIDAS = ("baixa", "media", "alta", "critica")

RESUMO_TICKET = """
    SELECT t.id, t.protocolo, t.titulo, t.status, t.atribuido_para,
           t.urgencia_reportada, t.categoria, t.criado_em, t.triado_em,
           c.id AS cliente_id, c.nome AS cliente, c.empresa, c.plano, c.status_conta
      FROM tickets t
      JOIN clientes c ON c.id = t.cliente_id
"""


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


# --------------------------------------------------------------------------- #
# Consultas
# --------------------------------------------------------------------------- #

def listar_tickets(
    status: str | None = None,
    urgencia: str | None = None,
    cliente_id: int | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    """Lista a fila de chamados com filtros opcionais."""
    if status and status not in STATUS_VALIDOS:
        return _erro(f"status inválido: {status}")
    if urgencia and urgencia not in URGENCIAS_VALIDAS:
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


def detalhar_ticket(ticket_id: int) -> dict[str, Any]:
    """Retorna o ticket completo, o cliente associado e o volume de logs do cliente."""
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


def consultar_cliente(cliente_id: int) -> dict[str, Any]:
    """Consulta o cadastro do cliente: plano, status da conta, API key e cartão."""
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
        alertas.append(
            "Plano free sem cartão cadastrado (limite baixo: %s req/min)"
            % cliente["limite_requisicoes_min"]
        )

    cliente["alertas"] = alertas
    return {"ok": True, "cliente": cliente}


def buscar_logs_http(
    cliente_id: int,
    endpoint: str | None = None,
    status_code: int | None = None,
    apenas_erros: bool = True,
    minutos: int = 1440,
    limite: int = 50,
    incluir_stack_trace: bool = False,
) -> dict[str, Any]:
    """Busca os logs HTTP de um cliente para investigar a causa raiz."""
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


def analisar_causa_raiz(ticket_id: int) -> dict[str, Any]:
    """Monta o pacote de evidências do ticket e sugere o destino (N1 ou N2).

    4xx -> problema do cliente (N1); 5xx -> bug de aplicação/infra (N2).
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

    proximo_passo = (
        "Escalar para N2 com o stack trace e os request_ids."
        if destino == "N2"
        else "Devolver ao N1 com orientação ao cliente e registrar a triagem."
    )
    resposta_sugerida = (
        f"Olá, {ticket['cliente']}. Identificamos a causa do ocorrido: {hipotese}. "
        f"{proximo_passo} Estamos à disposição para qualquer dúvida neste chamado."
    )

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
        "proximo_passo": proximo_passo,
        "resposta_cliente_sugerida": resposta_sugerida,
    }


def triar_ticket(
    ticket_id: int,
    destino: str,
    categoria: str,
    diagnostico: str,
    acao_recomendada: str,
    resposta_cliente: str,
) -> dict[str, Any]:
    """Registra a triagem do ticket e o encaminha ao destino correto."""
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


# --------------------------------------------------------------------------- #
# Telemetria, injeção ao vivo e pacote de engenharia
# --------------------------------------------------------------------------- #

def logs_globais(limite: int = 50) -> dict[str, Any]:
    """Últimos logs HTTP de todos os clientes, com empresa e plano (somente leitura)."""
    limite = max(1, min(int(limite), 200))
    with _connect() as conn:
        logs = _rows(
            conn.execute(
                """
                SELECT l.id, l.timestamp, l.metodo, l.endpoint, l.status_code,
                       l.latencia_ms, l.request_id, l.mensagem_erro, l.stack_trace,
                       c.empresa, c.plano
                  FROM logs_http l
                  JOIN clientes c ON c.id = l.cliente_id
                 ORDER BY l.timestamp DESC
                 LIMIT ?
                """,
                (limite,),
            ).fetchall()
        )
    return {"ok": True, "total": len(logs), "logs": logs}


# Cada injeção ao vivo usa um incidente diferente. O rodízio é determinístico
# (baseado no próximo protocolo), então não é preciso guardar estado no banco:
# TK-1009 repete o cenário original da demo e os seguintes caminham pelo catálogo.
TRACE_FATURAMENTO_NOVO = """Traceback (most recent call last):
  File "/app/api/v1/atendimentos.py", line 63, in faturar_lote
    total = billing_service.fechar_lote(atendimentos=body["ids"])
  File "/app/services/billing_service.py", line 87, in fechar_lote
    valor = item["cobranca"]["valor"]
KeyError: 'cobranca'
"""

TRACE_POOL_EXPORTACAO = """Traceback (most recent call last):
  File "/app/api/v1/dashboard.py", line 96, in exportar_obra
    dados = await repo.relatorio_obra(obra_id=body["obra_id"])
  File "/app/infra/database.py", line 121, in acquire
    raise TimeoutError("pool de conexões esgotado (max=20)")
TimeoutError: pool de conexões esgotado (max=20)
"""

CENARIOS_INJECAO = (
    {
        "chave": "rate_limit",
        "rotulo": "Rate limit do plano free (HTTP 429)",
        "destino": "N1",
        "cliente_id": 3,
        "cliente": "Startup Cloud",
        "titulo": "Bloqueio total na integração de vendas",
        "descricao": (
            "Nossa integração de pedidos parou de sincronizar. Todas as chamadas "
            "retornam 429 e as vendas do dia estão travadas. Precisamos de ajuda urgente!"
        ),
        "urgencia": "critica",
        "metodo": "POST",
        "endpoint": "/v1/pedidos/sincronizar",
        "status_code": 429,
        "mensagem_erro": "rate_limit_exceeded: plano=free limite=60 req/min",
        "latencias": (980, 1120, 1260),
    },
    {
        "chave": "api_key_webhook",
        "rotulo": "Credencial expirada em webhook (HTTP 401)",
        "destino": "N1",
        "cliente_id": 1,
        "cliente": "Loja Verão",
        "titulo": "Webhooks de catálogo pararam de chegar",
        "descricao": (
            "Desde que trocamos a chave da API os webhooks de catálogo não chegam mais. "
            "Nossa precificação está desatualizada e precisamos corrigir ainda hoje."
        ),
        "urgencia": "alta",
        "metodo": "POST",
        "endpoint": "/v1/webhooks/catalogo",
        "status_code": 401,
        "mensagem_erro": "invalid_api_key: credencial expirada (sk_live_9f2c...4e5f)",
        "latencias": (18, 24, 31),
    },
    {
        "chave": "bug_faturamento",
        "rotulo": "Bug no faturamento em lote (HTTP 500)",
        "destino": "N2",
        "cliente_id": 5,
        "cliente": "HealthData",
        "titulo": "Faturamento em lote de atendimentos falhando",
        "descricao": (
            "O job noturno de faturamento quebrou depois da última deploy. Nenhuma "
            "nota foi emitida e o erro 500 aparece para todos os atendimentos."
        ),
        "urgencia": "critica",
        "metodo": "POST",
        "endpoint": "/v1/atendimentos/faturar",
        "status_code": 500,
        "mensagem_erro": "internal_server_error: KeyError: 'cobranca'",
        "stack_trace": TRACE_FATURAMENTO_NOVO,
        "latencias": (2210, 2380, 2540),
    },
    {
        "chave": "escopo_transportadoras",
        "rotulo": "Escopo de API ausente (HTTP 403)",
        "destino": "N1",
        "cliente_id": 4,
        "cliente": "LogExpress",
        "titulo": "Integração com transportadoras fora do ar",
        "descricao": (
            "Ao consultar as transportadoras recebemos 403 dizendo que falta permissão. "
            "A mesma credencial funcionava até ontem de manhã."
        ),
        "urgencia": "alta",
        "metodo": "GET",
        "endpoint": "/v1/integracoes/transportadoras",
        "status_code": 403,
        "mensagem_erro": "missing_scope: a credencial não possui o escopo 'integracoes:read'",
        "latencias": (21, 26, 33),
    },
    {
        "chave": "pool_exportacao",
        "rotulo": "Pool de conexões esgotado (HTTP 500)",
        "destino": "N2",
        "cliente_id": 6,
        "cliente": "Construtora Alfa",
        "titulo": "Exportação de obra falha com erro 500",
        "descricao": (
            "A exportação do relatório de obra para o financeiro falha há uma hora. "
            "A tela fica carregando e depois aparece 500. Precisamos do arquivo hoje."
        ),
        "urgencia": "critica",
        "metodo": "POST",
        "endpoint": "/v1/dashboard/obra/exportar",
        "status_code": 500,
        "mensagem_erro": "internal_server_error: TimeoutError ao adquirir conexão do pool",
        "stack_trace": TRACE_POOL_EXPORTACAO,
        "latencias": (30010, 30480, 30950),
    },
    {
        "chave": "cartao_assinatura",
        "rotulo": "Cartão recusado na renovação (HTTP 402)",
        "destino": "N1",
        "cliente_id": 2,
        "cliente": "Fintech X",
        "titulo": "Renovação da assinatura sendo recusada",
        "descricao": (
            "A renovação automática da assinatura está sendo recusada pelo gateway. "
            "Já contestamos a cobrança com o banco e precisamos que verifiquem."
        ),
        "urgencia": "alta",
        "metodo": "POST",
        "endpoint": "/v1/checkout/assinatura",
        "status_code": 402,
        "mensagem_erro": "card_declined: do_not_honor (cartão de débito)",
        "latencias": (510, 545, 580),
    },
    {
        "chave": "conta_inadimplente",
        "rotulo": "Conta bloqueada por inadimplência (HTTP 403)",
        "destino": "N1",
        "cliente_id": 7,
        "cliente": "Boutique Estilo",
        "titulo": "Bloqueio ao emitir nota fiscal",
        "descricao": (
            "Ao tentar emitir a nota fiscal do cliente a conta é bloqueada novamente. "
            "Já fizemos o pagamento da fatura e precisamos liberar o acesso."
        ),
        "urgencia": "critica",
        "metodo": "POST",
        "endpoint": "/v1/notas-fiscais/emitir",
        "status_code": 403,
        "mensagem_erro": "account_suspended: fatura em atraso (verificar cadastro)",
        "latencias": (22, 28, 35),
    },
)


def injetar_chamado() -> dict[str, Any]:
    """Cria um ticket inédito (TK-1009, TK-1010...) com um incidente rotacionado.

    Usado ao vivo na apresentação para simular um cliente abrindo um chamado crítico.
    O protocolo é sempre o próximo disponível e o cenário vem de `CENARIOS_INJECAO`,
    escolhido por rodízio: cada injeção é um problema diferente da anterior. O
    primeiro continua sendo o 429 da Startup Cloud, como no roteiro original.
    """
    agora = _now().replace(microsecond=0)
    with _connect(readonly=False) as conn:
        numeros = []
        for (protocolo,) in conn.execute("SELECT protocolo FROM tickets").fetchall():
            encontrado = re.match(r"TK-(\d+)$", protocolo or "")
            if encontrado:
                numeros.append(int(encontrado.group(1)))
        proximo = max(numeros, default=1000) + 1
        novo_protocolo = f"TK-{proximo:04d}"
        cenario = CENARIOS_INJECAO[(proximo - 1009) % len(CENARIOS_INJECAO)]

        conn.execute(
            """
            INSERT INTO tickets
                (protocolo, cliente_id, titulo, descricao, canal, urgencia_reportada,
                 status, atribuido_para, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, 'chat', ?, 'aberto', 'N1', ?, ?)
            """,
            (
                novo_protocolo,
                cenario["cliente_id"],
                cenario["titulo"],
                cenario["descricao"],
                cenario["urgencia"],
                agora.isoformat(),
                agora.isoformat(),
            ),
        )
        ticket_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        total_logs = len(cenario["latencias"])
        for indice, latencia in enumerate(cenario["latencias"]):
            momento = (agora - timedelta(minutes=total_logs - 1 - indice)).isoformat()
            conn.execute(
                """
                INSERT INTO logs_http
                    (cliente_id, timestamp, metodo, endpoint, status_code,
                     latencia_ms, request_id, mensagem_erro, stack_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cenario["cliente_id"],
                    momento,
                    cenario["metodo"],
                    cenario["endpoint"],
                    cenario["status_code"],
                    latencia,
                    f"req_live_{proximo}_{indice + 1:02d}",
                    cenario["mensagem_erro"],
                    cenario.get("stack_trace"),
                ),
            )
        conn.commit()

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "protocolo": novo_protocolo,
        "logs_inseridos": total_logs,
        "cliente": cenario["cliente"],
        "titulo": cenario["titulo"],
        "cenario": cenario["chave"],
        "rotulo": cenario["rotulo"],
        "status_code": cenario["status_code"],
        "destino_esperado": cenario["destino"],
        "mensagem": (
            f"Chamado {novo_protocolo} injetado: {cenario['rotulo']} em "
            f"{cenario['endpoint']} ({cenario['cliente']})."
        ),
    }


def gerar_pacote_engenharia(ticket_id: int) -> dict[str, Any]:
    """Monta um card técnico (Jira/Linear) para bugs reais com HTTP 5xx."""
    with _connect() as conn:
        ticket = _row(
            conn.execute(
                """
                SELECT t.id, t.cliente_id, t.protocolo, t.titulo, t.descricao,
                       t.urgencia_reportada, t.diagnostico, t.criado_em,
                       c.nome AS cliente, c.empresa, c.plano
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
                SELECT metodo, endpoint, status_code, latencia_ms, request_id,
                       mensagem_erro, stack_trace
                  FROM logs_http
                 WHERE cliente_id = ? AND status_code >= 500 AND timestamp BETWEEN ? AND ?
                 ORDER BY timestamp DESC
                 LIMIT 200
                """,
                (ticket["cliente_id"], *(_janela_do_ticket(ticket["criado_em"]))),
            ).fetchall()
        )

    if not logs:
        return _erro(
            f"não há HTTP 5xx na janela do ticket {ticket['protocolo']}: "
            "este não é um bug real para N2."
        )

    por_endpoint: dict[str, int] = {}
    request_ids: list[str] = []
    stack_trace = ""
    for log in logs:
        chave = f"{log['metodo']} {log['endpoint']}"
        por_endpoint[chave] = por_endpoint.get(chave, 0) + 1
        if log["request_id"] not in request_ids:
            request_ids.append(log["request_id"])
        if not stack_trace and log["stack_trace"]:
            stack_trace = log["stack_trace"]

    endpoint_afetado = max(por_endpoint, key=por_endpoint.get)
    latencias = [l["latencia_ms"] for l in logs]

    linhas = [linha.strip() for linha in stack_trace.splitlines() if linha.strip()] if stack_trace else []
    excecao = next(
        (linha for linha in linhas if re.match(r"^[A-Za-z_][A-Za-z0-9_]*Error:", linha)),
        logs[0]["mensagem_erro"] or "Falha HTTP 500",
    )
    # O "File" relevante é o último antes da exceção (o ponto onde ela foi lançada),
    # não o primeiro do traceback (que é só o entrypoint da API).
    indice_excecao = linhas.index(excecao) if excecao in linhas else len(linhas)
    local = next((linha for linha in reversed(linhas[:indice_excecao]) if linha.startswith("File ")), "")
    origem = re.search(r'File "([^"]+)", line (\d+)', local)
    resumo = f"{excecao} em {origem.group(1)}:{origem.group(2)}" if origem else excecao

    chave_perdida = re.search(r"KeyError:\s*'([^']+)'", excecao)
    criterio = (
        f"Tratar ausência da chave '{chave_perdida.group(1)}' no payload e validar retorno HTTP 200."
        if chave_perdida
        else f"Tratar {excecao} em {endpoint_afetado} e validar retorno HTTP 200 com payload válido."
    )

    urgencia = ticket["urgencia_reportada"]
    plano = ticket["plano"]
    severidade = (
        f"P1 - Crítica (Cliente {plano.capitalize()})"
        if urgencia in ("critica", "alta")
        else "P2 - Alta"
    )

    return {
        "ok": True,
        "ticket_id": ticket["id"],
        "card_engenharia": {
            "titulo_issue": (
                f"[BUG N2] {ticket['protocolo']} — Falha 500 em "
                f"{endpoint_afetado.split(' ', 1)[-1]} ({ticket['empresa']})"
            ),
            "severidade": severidade,
            "endpoint_afetado": endpoint_afetado,
            "request_ids_correlacionados": request_ids,
            "resumo_tecnico": resumo,
            "stack_trace": stack_trace,
            "criterio_aceite": criterio,
            "logs_5xx": len(logs),
            "latencia_maxima_ms": max(latencias) if latencias else None,
            "empresa": ticket["empresa"],
            "plano": plano,
            "diagnostico": ticket["diagnostico"],
        },
    }


# --------------------------------------------------------------------------- #
# Playbook, saúde e demo
# --------------------------------------------------------------------------- #

def playbook() -> dict[str, Any]:
    """Regras de decisão do time de suporte (N1 x N2)."""
    return {
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
    }


def playbook_json() -> str:
    """Playbook serializado (formato usado pelo recurso MCP)."""
    return json.dumps(playbook(), ensure_ascii=False, indent=2)


def reset_demo() -> dict[str, Any]:
    """Recria o banco de demonstração (7 clientes, 8 tickets, ~40 logs)."""
    import setup_db

    try:
        caminho = setup_db.criar_banco(DB_PATH)
    except Exception as exc:  # noqa: BLE001 - devolvido como erro de entrada
        return _erro(f"falha ao recriar o banco: {exc}")

    return {"ok": True, "banco": str(caminho), "mensagem": "Banco de demonstração recriado."}


def health() -> dict[str, Any]:
    """Estado do banco e contadores rápidos (usado pela interface web)."""
    if not DB_PATH.exists():
        return {"ok": False, "banco": False, "erro": f"banco ausente: {DB_PATH}"}

    try:
        with _connect() as conn:
            tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            pendentes = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE status IN ('aberto', 'em_triagem')"
            ).fetchone()[0]
    except sqlite3.Error as exc:
        return {"ok": False, "banco": False, "erro": str(exc)}

    return {
        "ok": True,
        "banco": True,
        "banco_caminho": str(DB_PATH),
        "tickets": tickets,
        "pendentes": pendentes,
    }
