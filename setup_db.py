"""Cria e popula o banco `suporte.db` do projeto Ops Detective.

O banco simula o ambiente de suporte/infra de uma empresa SaaS:
- `clientes`: cadastro, plano, status da conta, validade da API key e do cartão;
- `tickets`: chamados abertos pelo N1 com a urgência reportada pelo cliente;
- `logs_http`: rastro técnico das requisições feitas pela API do cliente.

Uso:
    python setup_db.py            # recria o banco do zero
    python setup_db.py --keep     # não apaga o banco existente
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("OPS_DETECTIVE_DB", BASE_DIR / "suporte.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS logs_http;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS clientes;

CREATE TABLE clientes (
    id                     INTEGER PRIMARY KEY,
    nome                   TEXT NOT NULL,
    email                  TEXT NOT NULL UNIQUE,
    empresa                TEXT NOT NULL,
    plano                  TEXT NOT NULL CHECK (plano IN ('free', 'pro', 'enterprise')),
    status_conta           TEXT NOT NULL CHECK (status_conta IN ('ativo', 'suspenso', 'inadimplente', 'cancelado')),
    api_key                TEXT NOT NULL,
    api_key_expira_em      TEXT,
    cartao_final           TEXT,
    cartao_validade        TEXT,
    limite_requisicoes_min INTEGER NOT NULL,
    ultimo_login_em        TEXT,
    criado_em              TEXT NOT NULL
);

CREATE TABLE tickets (
    id                 INTEGER PRIMARY KEY,
    protocolo          TEXT NOT NULL UNIQUE,
    cliente_id         INTEGER NOT NULL REFERENCES clientes(id),
    titulo             TEXT NOT NULL,
    descricao          TEXT NOT NULL,
    canal              TEXT NOT NULL CHECK (canal IN ('email', 'chat', 'telefone', 'whatsapp', 'formulario')),
    urgencia_reportada TEXT NOT NULL CHECK (urgencia_reportada IN ('baixa', 'media', 'alta', 'critica')),
    status             TEXT NOT NULL DEFAULT 'aberto'
                       CHECK (status IN ('aberto', 'em_triagem', 'devolvido_n1', 'escalado_n2', 'resolvido')),
    atribuido_para     TEXT NOT NULL DEFAULT 'N1' CHECK (atribuido_para IN ('N1', 'N2')),
    categoria          TEXT,
    diagnostico        TEXT,
    acao_recomendada   TEXT,
    resposta_cliente   TEXT,
    criado_em          TEXT NOT NULL,
    atualizado_em      TEXT NOT NULL,
    triado_em          TEXT
);

CREATE TABLE logs_http (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id   INTEGER NOT NULL REFERENCES clientes(id),
    timestamp    TEXT NOT NULL,
    metodo       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    latencia_ms  INTEGER NOT NULL,
    request_id   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    mensagem_erro TEXT,
    stack_trace  TEXT
);

CREATE INDEX idx_logs_cliente_timestamp ON logs_http (cliente_id, timestamp);
CREATE INDEX idx_logs_status_code ON logs_http (status_code);
CREATE INDEX idx_tickets_status ON tickets (status);
"""


def agora() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(referencia: datetime, minutos_atras: int) -> str:
    return (referencia - timedelta(minutes=minutos_atras)).isoformat()


# --------------------------------------------------------------------------- #
# Massa de dados
# --------------------------------------------------------------------------- #

CLIENTES = [
    {
        "id": 1,
        "nome": "Mariana Lopes",
        "email": "mariana@lojaverao.com.br",
        "empresa": "Loja Verão",
        "plano": "pro",
        "status_conta": "ativo",
        "api_key": "sk_live_9f2c4a71b3d84e5f",
        "api_key_expira_em": -2880,  # expirou há 2 dias
        "cartao_final": "4111",
        "cartao_validade": "09/2027",
        "limite_requisicoes_min": 600,
        "ultimo_login_em": 180,
    },
    {
        "id": 2,
        "nome": "Carlos Menezes",
        "email": "carlos@fintechx.com.br",
        "empresa": "Fintech X",
        "plano": "enterprise",
        "status_conta": "ativo",
        "api_key": "sk_live_77aa10ce88b24d90",
        "api_key_expira_em": 43200,  # expira em 30 dias
        "cartao_final": "5500",
        "cartao_validade": "03/2028",
        "limite_requisicoes_min": 3000,
        "ultimo_login_em": 60,
    },
    {
        "id": 3,
        "nome": "Ana Beatriz Souza",
        "email": "ana@startupcloud.io",
        "empresa": "Startup Cloud",
        "plano": "free",
        "status_conta": "ativo",
        "api_key": "sk_test_31bc09de44a0f17a",
        "api_key_expira_em": 20160,
        "cartao_final": None,
        "cartao_validade": None,
        "limite_requisicoes_min": 60,
        "ultimo_login_em": 45,
    },
    {
        "id": 4,
        "nome": "Rafael Torres",
        "email": "rafael@logexpress.com",
        "empresa": "LogExpress",
        "plano": "pro",
        "status_conta": "ativo",
        "api_key": "sk_live_5d10ef2b7c9a4633",
        "api_key_expira_em": 14400,
        "cartao_final": "4012",
        "cartao_validade": "11/2026",
        "limite_requisicoes_min": 600,
        "ultimo_login_em": 300,
    },
    {
        "id": 5,
        "nome": "Juliana Prado",
        "email": "juliana@healthdata.com.br",
        "empresa": "HealthData",
        "plano": "enterprise",
        "status_conta": "ativo",
        "api_key": "sk_live_a0247cd9b1e34f66",
        "api_key_expira_em": 43200,
        "cartao_final": "3782",
        "cartao_validade": "06/2029",
        "limite_requisicoes_min": 3000,
        "ultimo_login_em": 20,
    },
    {
        "id": 6,
        "nome": "Pedro Almeida",
        "email": "pedro@construtora-alfa.com.br",
        "empresa": "Construtora Alfa",
        "plano": "pro",
        "status_conta": "ativo",
        "api_key": "sk_live_88bd13f5a70c49e2",
        "api_key_expira_em": 20160,
        "cartao_final": "6011",
        "cartao_validade": "01/2027",
        "limite_requisicoes_min": 600,
        "ultimo_login_em": 90,
    },
    {
        "id": 7,
        "nome": "Fernanda Lima",
        "email": "fernanda@boutiqueestilo.com",
        "empresa": "Boutique Estilo",
        "plano": "free",
        "status_conta": "inadimplente",
        "api_key": "sk_test_66fae0b28c1d4a77",
        "api_key_expira_em": 5040,
        "cartao_final": None,
        "cartao_validade": None,
        "limite_requisicoes_min": 60,
        "ultimo_login_em": 1440,
    },
]

# minutos_atras é relativo ao momento em que o script roda
TICKETS = [
    {
        "id": 1,
        "protocolo": "TK-1001",
        "cliente_id": 1,
        "titulo": "Sistema fora do ar!!!",
        "descricao": (
            "Bom dia, o sistema de vocês está fora do ar desde ontem! A diretoria está "
            "cobrando e eu preciso dos relatórios de vendas AGORA. Isso é um absurdo, "
            "se não resolverem em 1 hora vamos cancelar o contrato. URGENTE!!!"
        ),
        "canal": "whatsapp",
        "urgencia_reportada": "critica",
        "criado_em": 55,
    },
    {
        "id": 2,
        "protocolo": "TK-1002",
        "cliente_id": 2,
        "titulo": "Não consigo pagar",
        "descricao": (
            "Tentei fazer o pagamento da fatura três vezes e sempre dá erro. "
            "Meu cartão está funcionando, acabei de usar em outro site. Pode ser "
            "problema no gateway de vocês?"
        ),
        "canal": "email",
        "urgencia_reportada": "alta",
        "criado_em": 120,
    },
    {
        "id": 3,
        "protocolo": "TK-1003",
        "cliente_id": 3,
        "titulo": "API muito lenta / travando",
        "descricao": (
            "Desde ontem minha integração está retornando erro e ficando lenta. "
            "Preciso rodar um processamento em lote hoje. Podem verificar?"
        ),
        "canal": "chat",
        "urgencia_reportada": "alta",
        "criado_em": 90,
    },
    {
        "id": 4,
        "protocolo": "TK-1004",
        "cliente_id": 4,
        "titulo": "Erro ao gerar relatório",
        "descricao": (
            "Ao chamar o endpoint de relatórios recebo um erro dizendo que não tenho "
            "permissão, mas sempre funcionou antes. O token continua o mesmo."
        ),
        "canal": "email",
        "urgencia_reportada": "media",
        "criado_em": 200,
    },
    {
        "id": 5,
        "protocolo": "TK-1005",
        "cliente_id": 5,
        "titulo": "Sistema travando e dando erro 500",
        "descricao": (
            "O relatório de atendimentos está falhando desde a atualização de ontem. "
            "Recebo erro 500 Internal Server Error. São 4.000 atendimentos que não "
            "consigo faturar, isso é crítico para nós!"
        ),
        "canal": "telefone",
        "urgencia_reportada": "critica",
        "criado_em": 35,
    },
    {
        "id": 6,
        "protocolo": "TK-1006",
        "cliente_id": 6,
        "titulo": "Dashboard não carrega",
        "descricao": (
            "A tela inicial fica em branco com mensagem de erro 500. Já limpamos o "
            "cache e trocamos de navegador. Outros setores também estão reclamando."
        ),
        "canal": "formulario",
        "urgencia_reportada": "alta",
        "criado_em": 70,
    },
    {
        "id": 7,
        "protocolo": "TK-1007",
        "cliente_id": 2,
        "titulo": "Não recebo os e-mails de notificação",
        "descricao": (
            "Faz duas semanas que não recebo o e-mail de fechamento diário. "
            "Já conferi a caixa de spam e está tudo vazio."
        ),
        "canal": "email",
        "urgencia_reportada": "media",
        "criado_em": 1440,
    },
    {
        "id": 8,
        "protocolo": "TK-1008",
        "cliente_id": 7,
        "titulo": "Não consigo acessar a conta",
        "descricao": (
            "Minha conta foi bloqueada do nada. Preciso entrar para gerar a nota "
            "fiscal de um cliente. Já tentei trocar a senha e nada."
        ),
        "canal": "chat",
        "urgencia_reportada": "critica",
        "criado_em": 25,
    },
]

TRACE_KEY_ERROR = """Traceback (most recent call last):
  File "/app/api/v1/relatorios.py", line 88, in gerar_relatorio
    payload = report_service.build(segmento=body["segmento"], periodo=body["periodo"])
  File "/app/services/report_service.py", line 142, in build
    filtro = Segmento[segmento].value
KeyError: 'segmento'

Durante o tratamento da exceção acima, ocorreu outra exceção:

Traceback (most recent call last):
  File "/app/middleware/errors.py", line 31, in handle_exception
    return JSONResponse(status_code=500, content={"erro": "internal_server_error"})
"""

TRACE_POOL = """Traceback (most recent call last):
  File "/app/api/v1/dashboard.py", line 54, in obter_dashboard
    dados = await repo.resumo(cliente_id=cliente.id)
  File "/app/infra/database.py", line 121, in acquire
    raise TimeoutError("pool de conexões esgotado (max=20)")
TimeoutError: pool de conexões esgotado (max=20)
"""


def gerar_logs(referencia: datetime) -> list[dict]:
    """Monta o rastro HTTP de cada cenário dos tickets."""
    logs: list[dict] = []

    def add(cliente_id, minutos, metodo, endpoint, status, latencia, rid, erro=None, trace=None, ip="177.92.14.10"):
        logs.append(
            {
                "cliente_id": cliente_id,
                "timestamp": ts(referencia, minutos),
                "metodo": metodo,
                "endpoint": endpoint,
                "status_code": status,
                "latencia_ms": latencia,
                "request_id": rid,
                "ip": ip,
                "user_agent": "ops-sdk-python/2.4.1",
                "mensagem_erro": erro,
                "stack_trace": trace,
            }
        )

    # TK-1001 | Loja Verão: "sistema fora do ar" = API key expirada (401)
    for i in range(6):
        add(
            1,
            70 - i * 3,
            "GET",
            "/v1/relatorios/vendas",
            401,
            12 + i,
            f"req_8f1c{i:02d}",
            "token_expirado: API key expirou em %s" % ts(referencia, 2880)[:10],
        )
    add(1, 150, "GET", "/v1/relatorios/vendas", 200, 340, "req_8f1c90")

    # TK-1002 | Fintech X: "não consigo pagar" = cartão recusado (402)
    for i, motivo in enumerate(
        ["card_declined: insufficient_funds", "card_declined: insufficient_funds", "card_declined: do_not_honor"]
    ):
        add(2, 130 - i * 4, "POST", "/v1/checkout/pagamento", 402, 480 + i * 30, f"req_pay{i:02d}", motivo)

    # TK-1003 | Startup Cloud: plano free excedeu limite (429)
    for i in range(9):
        add(
            3,
            110 - i * 2,
            "GET",
            "/v1/relatorios/vendas",
            429,
            8,
            f"req_rl{i:02d}",
            "rate_limit_exceeded: plano=free limite=60 req/min",
            ip="191.55.203.77",
        )

    # TK-1004 | LogExpress: escopo faltando na API key (403)
    for i in range(4):
        add(
            4,
            230 - i * 5,
            "GET",
            "/v1/relatorios/estoque",
            403,
            15,
            f"req_sc{i:02d}",
            "missing_scope: a credencial não possui o escopo 'relatorios:read'",
        )

    # TK-1005 | HealthData: bug real no gerador de relatórios (500)
    for i in range(4):
        add(
            5,
            40 - i * 6,
            "POST",
            "/v1/relatorios/atendimentos",
            500,
            2200 + i * 150,
            f"req_500{i:02d}",
            "internal_server_error: KeyError: 'segmento'",
            TRACE_KEY_ERROR,
            ip="201.17.88.4",
        )

    # TK-1006 | Construtora Alfa: pool de conexões esgotado (500)
    for i in range(5):
        add(
            6,
            80 - i * 4,
            "GET",
            "/v1/dashboard/resumo",
            500,
            30000,
            f"req_db{i:02d}",
            "internal_server_error: TimeoutError ao adquirir conexão do pool",
            TRACE_POOL,
            ip="189.4.120.9",
        )

    # TK-1007 | Fintech X: e-mails saem com sucesso (202) -> problema do lado do cliente
    for i in range(3):
        add(
            2,
            1500 - i * 60,
            "POST",
            "/v1/notificacoes/email",
            202,
            95,
            f"req_mail{i:02d}",
            None,
        )

    # TK-1008 | Boutique Estilo: conta inadimplente bloqueada (403)
    for i in range(3):
        add(
            7,
            30 - i * 2,
            "POST",
            "/v1/auth/login",
            403,
            20,
            f"req_lg{i:02d}",
            "account_suspended: fatura em atraso desde %s" % ts(referencia, 4320)[:10],
            ip="177.140.55.21",
        )

    return logs


def criar_banco(db_path: Path = DB_PATH) -> Path:
    referencia = agora()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)

        for c in CLIENTES:
            expira = c["api_key_expira_em"]
            conn.execute(
                """
                INSERT INTO clientes (
                    id, nome, email, empresa, plano, status_conta, api_key,
                    api_key_expira_em, cartao_final, cartao_validade,
                    limite_requisicoes_min, ultimo_login_em, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c["id"],
                    c["nome"],
                    c["email"],
                    c["empresa"],
                    c["plano"],
                    c["status_conta"],
                    c["api_key"],
                    ts(referencia, -expira) if expira is not None else None,
                    c["cartao_final"],
                    c["cartao_validade"],
                    c["limite_requisicoes_min"],
                    ts(referencia, c["ultimo_login_em"]),
                    ts(referencia, 43200),
                ),
            )

        for t in TICKETS:
            criado = ts(referencia, t["criado_em"])
            conn.execute(
                """
                INSERT INTO tickets (
                    id, protocolo, cliente_id, titulo, descricao, canal,
                    urgencia_reportada, status, atribuido_para, criado_em, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'aberto', 'N1', ?, ?)
                """,
                (
                    t["id"],
                    t["protocolo"],
                    t["cliente_id"],
                    t["titulo"],
                    t["descricao"],
                    t["canal"],
                    t["urgencia_reportada"],
                    criado,
                    criado,
                ),
            )

        for log in gerar_logs(referencia):
            conn.execute(
                """
                INSERT INTO logs_http (
                    cliente_id, timestamp, metodo, endpoint, status_code,
                    latencia_ms, request_id, ip, user_agent, mensagem_erro, stack_trace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log["cliente_id"],
                    log["timestamp"],
                    log["metodo"],
                    log["endpoint"],
                    log["status_code"],
                    log["latencia_ms"],
                    log["request_id"],
                    log["ip"],
                    log["user_agent"],
                    log["mensagem_erro"],
                    log["stack_trace"],
                ),
            )

        conn.commit()
    finally:
        conn.close()

    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria e popula o banco do Ops Detective.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Não recria o banco caso ele já exista.",
    )
    args = parser.parse_args()

    if DB_PATH.exists() and args.keep:
        print(f"Banco já existe em {DB_PATH} (use sem --keep para recriar).")
        return

    caminho = criar_banco()
    tamanho_kb = caminho.stat().st_size / 1024

    conn = sqlite3.connect(caminho)
    try:
        clientes = conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
        tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        logs = conn.execute("SELECT COUNT(*) FROM logs_http").fetchone()[0]
    finally:
        conn.close()

    print(f"Banco criado em: {caminho} ({tamanho_kb:.1f} KB)")
    print(f"  clientes:  {clientes}")
    print(f"  tickets:   {tickets}")
    print(f"  logs_http: {logs}")


if __name__ == "__main__":
    main()
