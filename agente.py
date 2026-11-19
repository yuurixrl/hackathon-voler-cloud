"""Agente de IA do Ops Detective: loop de tool-use sobre as tools do MCP.

Duas formas de operar, escolhidas por variável de ambiente:

- **modo demo (padrão, offline)**: sem chave de API, um roteador determinístico interpreta
  a intenção da mensagem e chama as mesmas tools do `server.py`. Mantém a apresentação
  funcional sem internet e sem custo.
- **modo Anthropic**: com `ANTHROPIC_API_KEY` definida, um Claude recebe as 8 tools do
  MCP (descobertas em runtime via `list_tools`) e decide sozinho quais chamar.

Em ambos os casos o agente **nunca escreve direto no SQLite**: toda ação passa pelo
servidor MCP em processo separado. Os eventos `agente_*` publicados no barramento SSE
alimentam o painel de chat com a trilha de ferramentas.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from typing import Any, Callable

import triagem

Evento = Callable[[dict[str, Any]], None]

MODELO_PADRAO = "claude-3-5-sonnet-latest"
MAX_ITERACOES = 6
MAX_TOKENS = 1024
LIMITE_MENSAGEM = 2000

AJUDA = (
    "Posso investigar e triar chamados usando as tools do MCP. Exemplos:\n"
    "• “Analise o TK-1005” — monta o pacote de evidências e sugere N1 ou N2.\n"
    "• “Trie o TK-1005” — analisa, grava a triagem e gera o pacote se for bug real.\n"
    "• “Gere o pacote de engenharia do TK-1011” — card Jira/Linear para 5xx.\n"
    "• “Me mostre as estatísticas” ou “Quais tickets estão na fila?”."
)


def modo() -> str:
    """Devolve `anthropic` quando há chave configurada; senão `demo`."""
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "demo"


def modelo() -> str:
    return os.environ.get("OPS_AGENTE_MODELO", MODELO_PADRAO)


def _normalizar(texto: str) -> str:
    """Minúsculas sem acento, para casar palavras digitadas de qualquer jeito."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto.lower())
        if not unicodedata.combining(c)
    )


def _contem(texto: str, termos: tuple[str, ...]) -> bool:
    return any(termo in texto for termo in termos)


def _extrair_ticket_id(mensagem: str, explicito: int | None) -> int | None:
    """Tira o id do ticket da mensagem (TK-1005 -> 5, #5, 'ticket 5')."""
    if explicito:
        return int(explicito)
    texto = _normalizar(mensagem)
    encontrado = re.search(r"tk-?\s*(\d{4})", texto)
    if encontrado:
        return int(encontrado.group(1)) - 1000
    encontrado = re.search(r"(?:ticket|chamado|id)\s*#?\s*(\d{1,4})", texto)
    if encontrado:
        return int(encontrado.group(1))
    encontrado = re.search(r"#(\d{1,4})", texto)
    return int(encontrado.group(1)) if encontrado else None


def _categoria_da_analise(analise: dict[str, Any]) -> str:
    """Traduz os códigos HTTP da análise para uma categoria válida no domínio."""
    evidencias = analise.get("evidencias") or {}
    codigos = [int(c) for c in (evidencias.get("por_status_code") or {}) if str(c).isdigit()]
    if any(c >= 500 for c in codigos):
        return "infraestrutura" if any(c in (502, 503, 504) for c in codigos) else "bug_aplicacao"
    if 401 in codigos:
        return "credencial_expirada"
    if 402 in codigos:
        return "pagamento_recusado"
    if 429 in codigos:
        return "limite_plano"
    if 403 in codigos:
        mensagens = " ".join(evidencias.get("mensagens_distintas") or []).lower()
        if _contem(_normalizar(mensagens), ("conta", "fatura", "suspens", "inadimpl")):
            return "conta_suspensa"
        return "permissao_escopo"
    return "duvida_uso"


async def _tool(
    session: Any,
    nome: str,
    argumentos: dict[str, Any],
    publicar: Evento,
    turno: str,
) -> dict[str, Any]:
    """Chama uma tool MCP e registra a chamada no SSE com nome, args e latência."""
    inicio = time.perf_counter()
    try:
        resposta = await asyncio.wait_for(
            session.call_tool(nome, argumentos), timeout=30
        )
        texto = resposta.content[0].text if resposta.content else ""
        if resposta.isError or not texto:
            dados: dict[str, Any] = {"ok": False, "erro": texto or "tool MCP retornou vazio"}
        else:
            dados = json.loads(texto)
    except Exception as exc:  # noqa: BLE001 - erro vai como dado, nunca 500
        dados = {"ok": False, "erro": str(exc)}
    tempo_ms = round((time.perf_counter() - inicio) * 1000)
    publicar(
        {
            "tipo": "agente_tool",
            "id": turno,
            "ferramenta": nome,
            "argumentos": argumentos,
            "tempo_ms": tempo_ms,
            "ok": bool(dados.get("ok")),
        }
    )
    return dados


def _finalizar(
    texto: str,
    ferramentas: list[str],
    publicar: Evento,
    turno: str,
    modo: str = "demo",
) -> dict[str, Any]:
    publicar(
        {
            "tipo": "agente_resposta",
            "id": turno,
            "texto": texto,
            "modo": modo,
            "ferramentas": ferramentas,
        }
    )
    return {
        "ok": True,
        "id": turno,
        "modo": modo,
        "resposta": texto,
        "ferramentas": ferramentas,
    }


async def _modo_demo(
    mensagem: str,
    ticket_id: int | None,
    session: Any,
    publicar: Evento,
    turno: str,
) -> dict[str, Any]:
    """Roteador determinístico: intenção -> sequência de tools MCP."""
    tid = _extrair_ticket_id(mensagem, ticket_id)
    texto = _normalizar(mensagem)
    usadas: list[str] = []

    def registrar(nome: str) -> None:
        if nome not in usadas:
            usadas.append(nome)

    if _contem(
        texto,
        ("triar", "trie", "triando", "triagem", "encaminhar", "classificar", "decidir"),
    ):
        if tid is None:
            return _finalizar(
                "Informe o ticket para eu triar — por exemplo: “Trie o TK-1005”.",
                usadas, publicar, turno,
            )
        analise = await _tool(session, "analisar_causa_raiz", {"ticket_id": tid}, publicar, turno)
        registrar("analisar_causa_raiz")
        if not analise.get("ok"):
            return _finalizar(
                f"Não consegui analisar o ticket {tid}: {analise.get('erro')}",
                usadas, publicar, turno,
            )

        destino = analise["destino_sugerido"]
        dados_triagem = await _tool(
            session,
            "triar_ticket",
            {
                "ticket_id": tid,
                "destino": destino,
                "categoria": _categoria_da_analise(analise),
                "diagnostico": analise["hipotese"],
                "acao_recomendada": analise["proximo_passo"],
                "resposta_cliente": analise["resposta_cliente_sugerida"],
            },
            publicar,
            turno,
        )
        registrar("triar_ticket")
        if not dados_triagem.get("ok"):
            return _finalizar(
                f"A análise apontou {destino}, mas a gravação falhou: {dados_triagem.get('erro')}",
                usadas, publicar, turno,
            )

        publicar(
            {
                "tipo": "triagem",
                "via": "agente",
                "id": turno,
                "ferramenta": "triar_ticket",
                "ticket_id": tid,
                "protocolo": dados_triagem.get("protocolo"),
                "status": dados_triagem.get("status"),
            }
        )
        resposta = (
            f"Triagem gravada: {dados_triagem.get('protocolo')} → {destino} "
            f"({dados_triagem.get('categoria')}). {analise['hipotese']}"
        )
        if destino == "N2":
            pacote = await _tool(
                session, "gerar_pacote_engenharia", {"ticket_id": tid}, publicar, turno
            )
            registrar("gerar_pacote_engenharia")
            if pacote.get("ok"):
                card = pacote["card_engenharia"]
                resposta += (
                    f"\n\nPacote para engenharia: {card['titulo_issue']} · "
                    f"{card['resumo_tecnico']} · {card['criterio_aceite']}"
                )
        return _finalizar(resposta, usadas, publicar, turno)

    if _contem(texto, ("analis", "diagnost", "causa", "evid", "por que", "porque", "investig")):
        if tid is None:
            return _finalizar(
                "Informe o ticket para eu analisar — por exemplo: “Analise o TK-1005”.",
                usadas, publicar, turno,
            )
        analise = await _tool(session, "analisar_causa_raiz", {"ticket_id": tid}, publicar, turno)
        registrar("analisar_causa_raiz")
        if not analise.get("ok"):
            return _finalizar(f"Falha na análise: {analise.get('erro')}", usadas, publicar, turno)
        return _finalizar(
            f"{analise['ticket']['protocolo']} — {analise['hipotese']} "
            f"Destino sugerido: **{analise['destino_sugerido']}**. {analise['proximo_passo']}",
            usadas, publicar, turno,
        )

    if _contem(texto, ("pacote", "engenharia", "jira", "card")):
        if tid is None:
            return _finalizar(
                "Informe o ticket do bug — por exemplo: “Gere o pacote do TK-1005”.",
                usadas, publicar, turno,
            )
        pacote = await _tool(
            session, "gerar_pacote_engenharia", {"ticket_id": tid}, publicar, turno
        )
        registrar("gerar_pacote_engenharia")
        if not pacote.get("ok"):
            return _finalizar(
                f"Não há pacote para esse ticket: {pacote.get('erro')}", usadas, publicar, turno
            )
        card = pacote["card_engenharia"]
        return _finalizar(
            f"{card['titulo_issue']} · severidade {card['severidade']}\n"
            f"Endpoint: {card['endpoint_afetado']}\n"
            f"Resumo técnico: {card['resumo_tecnico']}\n"
            f"Critério de aceite: {card['criterio_aceite']}",
            usadas, publicar, turno,
        )

    if _contem(texto, ("estat", "placar", "kpi", "numeros", "resumo da fila")):
        stats = await _tool(session, "estatisticas_triagem", {}, publicar, turno)
        registrar("estatisticas_triagem")
        if not stats.get("ok"):
            return _finalizar(f"Falha ao ler estatísticas: {stats.get('erro')}", usadas, publicar, turno)
        return _finalizar(
            f"Placar: {stats['total_tickets']} tickets, "
            f"{stats['pendentes_de_triagem']} aguardando triagem, "
            f"{stats['triados']} triados. "
            f"Taxa de devolução ao N1: {stats['taxa_rebaixamento_para_n1']}%.",
            usadas, publicar, turno,
        )

    if _contem(texto, ("log", "telemetria", "http", "request")):
        cliente_id = None
        if tid is not None:
            detalhe = await _tool(session, "detalhar_ticket", {"ticket_id": tid}, publicar, turno)
            registrar("detalhar_ticket")
            if detalhe.get("ok"):
                cliente_id = detalhe["ticket"]["cliente_id"]
        argumentos = {"cliente_id": cliente_id} if cliente_id else {}
        logs = await _tool(session, "buscar_logs_http", argumentos, publicar, turno)
        registrar("buscar_logs_http")
        if not logs.get("ok"):
            return _finalizar(f"Falha ao buscar logs: {logs.get('erro')}", usadas, publicar, turno)
        return _finalizar(
            f"Encontrei {logs.get('total', 0)} log(s) HTTP. "
            "Abra a aba **Logs HTTP (Telemetria)** para ver método, status, latência e stack trace.",
            usadas, publicar, turno,
        )

    if _contem(texto, ("fila", "listar", "chamados", "pendentes", "tickets")):
        lista = await _tool(session, "listar_tickets", {"limite": 20}, publicar, turno)
        registrar("listar_tickets")
        if not lista.get("ok"):
            return _finalizar(f"Falha ao listar tickets: {lista.get('erro')}", usadas, publicar, turno)
        pendentes = [t for t in lista.get("tickets", []) if t["status"] in ("aberto", "em_triagem")]
        resumo = ", ".join(t["protocolo"] for t in pendentes[:8]) or "nenhum"
        return _finalizar(
            f"{len(pendentes)} chamado(s) aguardando triagem: {resumo}.",
            usadas, publicar, turno,
        )

    return _finalizar(AJUDA, usadas, publicar, turno)


def _ferramentas_anthropic(ferramentas_mcp: list[Any]) -> list[dict[str, Any]]:
    """Converte `list_tools()` do MCP para o formato de tools da API Anthropic."""
    return [
        {
            "name": ferramenta.name,
            "description": ferramenta.description or ferramenta.name,
            "input_schema": ferramenta.inputSchema,
        }
        for ferramenta in ferramentas_mcp
    ]


async def _modo_anthropic(
    mensagem: str,
    session: Any,
    publicar: Evento,
    turno: str,
) -> dict[str, Any]:
    """Loop real de tool-use: o Claude escolhe quais tools chamar e quando parar."""
    try:
        from anthropic import AsyncAnthropic  # import tardio: modo demo não exige o pacote
    except ImportError:
        return _finalizar(
            "O modo Anthropic precisa do pacote `anthropic` (`pip install -r requirements.txt`).",
            [], publicar, turno, modo="anthropic",
        )

    client = AsyncAnthropic()
    ferramentas_mcp = (await session.list_tools()).tools
    ferramentas = _ferramentas_anthropic(ferramentas_mcp)
    system = (
        server_instructions()
        + "\n\nRegras do domínio (playbook):\n"
        + triagem.playbook_json()
        + "\n\nResponda sempre em português e curto. Use as tools para toda afirmação sobre tickets."
    )
    mensagens: list[dict[str, Any]] = [{"role": "user", "content": mensagem}]
    usadas: list[str] = []
    texto_final = ""

    for _ in range(MAX_ITERACOES):
        resposta = await asyncio.wait_for(
            client.messages.create(
                model=modelo(),
                max_tokens=MAX_TOKENS,
                system=system,
                tools=ferramentas,
                messages=mensagens,
            ),
            timeout=60,
        )
        mensagens.append({"role": "assistant", "content": resposta.content})
        blocos_tool: list[dict[str, Any]] = []

        for bloco in resposta.content:
            if getattr(bloco, "type", None) == "text" and bloco.text.strip():
                texto_final += ("\n" if texto_final else "") + bloco.text.strip()
                publicar({"tipo": "agente_texto", "id": turno, "texto": bloco.text})
            elif getattr(bloco, "type", None) == "tool_use":
                argumentos = bloco.input if isinstance(bloco.input, dict) else {}
                # `_tool` publica o evento com nome, argumentos e latência real.
                usadas.append(bloco.name)
                resultado = await _tool(session, bloco.name, argumentos, publicar, turno)
                blocos_tool.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": bloco.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                        "is_error": not resultado.get("ok", False),
                    }
                )
                if bloco.name == "triar_ticket" and resultado.get("ok"):
                    publicar(
                        {
                            "tipo": "triagem",
                            "via": "agente",
                            "id": turno,
                            "ferramenta": "triar_ticket",
                            "ticket_id": argumentos.get("ticket_id"),
                            "protocolo": resultado.get("protocolo"),
                            "status": resultado.get("status"),
                        }
                    )

        if resposta.stop_reason != "tool_use" or not blocos_tool:
            break
        mensagens.append({"role": "user", "content": blocos_tool})

    if not texto_final:
        texto_final = "Tool calls executadas, mas o modelo não devolveu texto final."
    publicar(
        {
            "tipo": "agente_resposta",
            "id": turno,
            "texto": texto_final,
            "modo": "anthropic",
            "ferramentas": usadas,
        }
    )
    return {
        "ok": True,
        "id": turno,
        "modo": "anthropic",
        "resposta": texto_final,
        "ferramentas": usadas,
    }


def server_instructions() -> str:
    """Importa o prompt do servidor MCP sem criar dependência circular com a API."""
    try:
        from server import INSTRUCTIONS

        return INSTRUCTIONS
    except Exception:  # noqa: BLE001
        return "Você é o Ops Detective, agente de triagem N1 -> N2."


async def conversar(
    mensagem: str,
    ticket_id: int | None,
    session: Any,
    publicar: Evento,
    turno: str,
) -> dict[str, Any]:
    """Executa um turno do agente sobre uma sessão MCP já aberta."""
    if not mensagem.strip():
        return {"ok": False, "erro": "mensagem vazia", "id": turno}
    if len(mensagem) > LIMITE_MENSAGEM:
        return {
            "ok": False,
            "erro": f"mensagem muito longa (máx. {LIMITE_MENSAGEM} caracteres)",
            "id": turno,
        }

    publicar(
        {
            "tipo": "agente_inicio",
            "id": turno,
            "modo": modo(),
            "mensagem": mensagem,
        }
    )
    if modo() == "anthropic":
        return await _modo_anthropic(mensagem, session, publicar, turno)
    return await _modo_demo(mensagem, ticket_id, session, publicar, turno)
