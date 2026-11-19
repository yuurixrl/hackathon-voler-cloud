"""API HTTP do Ops Detective (interface web).

Serve o front estático (`index.html` + `static/app.css`) e expõe a mesma lógica
de triagem usada pelo servidor MCP, através de `triagem.py`.

Endpoints principais:
    GET  /                          -> interface web
    GET  /api/health                -> estado do banco
    GET  /api/tickets               -> fila (filtros: status, urgencia, cliente_id)
    GET  /api/tickets/{id}          -> ticket + cliente + volume de logs
    GET  /api/tickets/{id}/analise  -> evidências + hipótese + destino sugerido
    GET  /api/tickets/{id}/logs     -> últimos logs HTTP do cliente do ticket
    POST /api/tickets/{id}/triagem  -> grava a triagem (direto, legado)
    POST /api/agente/analisar       -> analise gerada pelo MCP em processo separado
    POST /api/agente/triagem        -> triagem gravada pelo MCP em processo separado
    POST /api/agente/pacote         -> card de engenharia gerado pela tool MCP
    GET  /api/agente/status         -> tools expostas pelo servidor MCP
    GET  /api/logs                  -> telemetria HTTP global (somente leitura)
    POST /api/injetar-chamado       -> cria ticket inédito + 3 logs ao vivo
    GET  /api/estatisticas          -> placar
    GET  /api/playbook              -> regras + categorias válidas
    POST /api/reset                 -> recria o banco de demonstração
    GET  /api/events                -> SSE com atualizações em tempo real

Uso:
    python api.py                   # http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import triagem

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="Ops Detective",
    description="Triagem N1 -> N2 com evidências de logs HTTP.",
    version="2.0.0",
)


# --------------------------------------------------------------------------- #
# SSE: barramento de eventos
# --------------------------------------------------------------------------- #

class EventBus:
    """Distribui eventos para os navegadores conectados via SSE."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        fila: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._subscribers.add(fila)
        return fila

    def unsubscribe(self, fila: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(fila)

    def publish(self, evento: dict[str, Any]) -> None:
        for fila in list(self._subscribers):
            try:
                fila.put_nowait(evento)
            except asyncio.QueueFull:
                # Navegador lento: descarta o evento em vez de travar a API.
                pass


bus = EventBus()


def _sse(evento: dict[str, Any]) -> str:
    return f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# Helpers de resposta
# --------------------------------------------------------------------------- #

def _json(payload: dict[str, Any]) -> JSONResponse:
    """Traduz o retorno de `triagem.py` para o status HTTP adequado."""
    if payload.get("ok"):
        return JSONResponse(payload)
    erro = str(payload.get("erro", ""))
    status = 404 if "não encontrado" in erro else 400
    return JSONResponse(payload, status_code=status)


def _cliente_id_do_ticket(ticket_id: int) -> int | None:
    detalhe = triagem.detalhar_ticket(ticket_id)
    if not detalhe.get("ok"):
        return None
    return int(detalhe["ticket"]["cliente_id"])


# --------------------------------------------------------------------------- #
# Ponte MCP: o front fala com o servidor MCP como o Claude Desktop fala
# --------------------------------------------------------------------------- #

def _parametros_mcp() -> StdioServerParameters:
    """Lanca `server.py` em outro processo, exatamente como o Claude Desktop faz."""
    return StdioServerParameters(
        command=sys.executable,
        args=[str(BASE_DIR / "server.py")],
        cwd=str(BASE_DIR),
    )


@asynccontextmanager
async def _sessao_mcp():
    async with stdio_client(_parametros_mcp()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _chamar_mcp(ferramenta: str, argumentos: dict[str, Any]) -> dict[str, Any]:
    """Executa uma tool do MCP e devolve o JSON com proveniencia e latencia."""
    inicio = time.perf_counter()

    def finalizar(payload: dict[str, Any]) -> dict[str, Any]:
        payload["ferramenta"] = ferramenta
        payload["origem"] = "MCP · server.py (processo separado)"
        payload["tempo_ms"] = round((time.perf_counter() - inicio) * 1000)
        return payload

    try:
        async with _sessao_mcp() as session:
            resposta = await asyncio.wait_for(
                session.call_tool(ferramenta, argumentos), timeout=30
            )
        texto = resposta.content[0].text if resposta.content else ""
        if resposta.isError or not texto:
            return finalizar({"ok": False, "erro": texto or "tool MCP retornou vazio"})
        return finalizar(json.loads(texto))
    except Exception as exc:  # noqa: BLE001 - erro devolvido como JSON, nunca 500
        return finalizar({"ok": False, "erro": f"falha na ponte MCP: {exc}"})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> JSONResponse:
    payload = triagem.health()
    payload["api"] = True
    return _json(payload)


@app.get("/api/tickets")
def listar_tickets(
    status: str | None = None,
    urgencia: str | None = None,
    cliente_id: int | None = None,
    limite: int = 100,
) -> JSONResponse:
    return _json(
        triagem.listar_tickets(
            status=status, urgencia=urgencia, cliente_id=cliente_id, limite=limite
        )
    )


@app.get("/api/tickets/{ticket_id}")
def detalhar_ticket(ticket_id: int) -> JSONResponse:
    return _json(triagem.detalhar_ticket(ticket_id))


@app.get("/api/tickets/{ticket_id}/analise")
def analisar_causa_raiz(ticket_id: int) -> JSONResponse:
    return _json(triagem.analisar_causa_raiz(ticket_id))


@app.get("/api/tickets/{ticket_id}/logs")
def logs_do_ticket(
    ticket_id: int,
    limite: int = 20,
    apenas_erros: bool = True,
    incluir_stack_trace: bool = False,
) -> JSONResponse:
    cliente_id = _cliente_id_do_ticket(ticket_id)
    if cliente_id is None:
        return JSONResponse({"ok": False, "erro": f"ticket {ticket_id} não encontrado"}, status_code=404)

    return _json(
        triagem.buscar_logs_http(
            cliente_id=cliente_id,
            apenas_erros=apenas_erros,
            minutos=0,
            limite=limite,
            incluir_stack_trace=incluir_stack_trace,
        )
    )


class TriagemIn(BaseModel):
    destino: str
    categoria: str
    diagnostico: str
    acao_recomendada: str
    resposta_cliente: str


@app.post("/api/tickets/{ticket_id}/triagem")
def triar_ticket(ticket_id: int, dados: TriagemIn) -> JSONResponse:
    resultado = triagem.triar_ticket(
        ticket_id=ticket_id,
        destino=dados.destino,
        categoria=dados.categoria,
        diagnostico=dados.diagnostico,
        acao_recomendada=dados.acao_recomendada,
        resposta_cliente=dados.resposta_cliente,
    )
    if resultado.get("ok"):
        bus.publish(
            {
                "tipo": "triagem",
                "ticket_id": ticket_id,
                "protocolo": resultado.get("protocolo"),
                "status": resultado.get("status"),
            }
        )
    return _json(resultado)


class AgenteAnaliseIn(BaseModel):
    ticket_id: int


class AgenteTriagemIn(TriagemIn):
    ticket_id: int


@app.get("/api/agente/status")
async def agente_status() -> JSONResponse:
    """Prova que o MCP esta vivo: lista as tools do `server.py` via stdio."""
    inicio = time.perf_counter()
    try:
        async with _sessao_mcp() as session:
            ferramentas = [t.name for t in (await session.list_tools()).tools]
        return _json(
            {
                "ok": True,
                "servidor": "server.py (stdio, processo separado)",
                "ferramentas": ferramentas,
                "tempo_ms": round((time.perf_counter() - inicio) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "erro": f"falha na ponte MCP: {exc}"})


@app.post("/api/agente/analisar")
async def agente_analisar(dados: AgenteAnaliseIn) -> JSONResponse:
    """A analise do drawer vem da tool MCP, nao da funcao local da API."""
    return _json(await _chamar_mcp("analisar_causa_raiz", {"ticket_id": dados.ticket_id}))


@app.post("/api/agente/pacote")
async def agente_pacote(dados: AgenteAnaliseIn) -> JSONResponse:
    """O card da engenharia também nasce dentro de uma tool do MCP separado."""
    return _json(
        await _chamar_mcp("gerar_pacote_engenharia", {"ticket_id": dados.ticket_id})
    )


@app.post("/api/agente/triagem")
async def agente_triar(dados: AgenteTriagemIn) -> JSONResponse:
    """A triagem e gravada pela tool MCP; a API apenas repassa e avisa via SSE."""
    resultado = await _chamar_mcp(
        "triar_ticket",
        {
            "ticket_id": dados.ticket_id,
            "destino": dados.destino,
            "categoria": dados.categoria,
            "diagnostico": dados.diagnostico,
            "acao_recomendada": dados.acao_recomendada,
            "resposta_cliente": dados.resposta_cliente,
        },
    )
    if resultado.get("ok"):
        bus.publish(
            {
                "tipo": "triagem",
                "via": "MCP",
                "ferramenta": "triar_ticket",
                "ticket_id": dados.ticket_id,
                "protocolo": resultado.get("protocolo"),
                "status": resultado.get("status"),
                "tempo_ms": resultado.get("tempo_ms"),
            }
        )
    return _json(resultado)


@app.get("/api/logs")
def logs_globais(limite: int = 50) -> JSONResponse:
    """Telemetria HTTP de todos os clientes, lida em modo=ro."""
    return _json(triagem.logs_globais(limite))


@app.post("/api/injetar-chamado")
async def injetar_chamado() -> JSONResponse:
    """Simula um chamado crítico chegando ao vivo durante a apresentação."""
    resultado = await asyncio.to_thread(triagem.injetar_chamado)
    if resultado.get("ok"):
        bus.publish(
            {
                "tipo": "novo_chamado",
                "ticket_id": resultado.get("ticket_id"),
                "protocolo": resultado.get("protocolo"),
                "cliente": resultado.get("cliente"),
                "titulo": resultado.get("titulo"),
                "rotulo": resultado.get("rotulo"),
                "status_code": resultado.get("status_code"),
            }
        )
    return _json(resultado)


@app.get("/api/estatisticas")
def estatisticas_triagem() -> JSONResponse:
    return _json(triagem.estatisticas_triagem())


@app.get("/api/playbook")
def playbook() -> JSONResponse:
    return _json({"ok": True, **triagem.playbook()})


@app.post("/api/reset")
async def reset_demo() -> JSONResponse:
    # `criar_banco` é bloqueante; roda fora do event loop.
    resultado = await asyncio.to_thread(triagem.reset_demo)
    if resultado.get("ok"):
        bus.publish({"tipo": "reset"})
    return _json(resultado)


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    async def gerar():
        fila = bus.subscribe()
        try:
            yield "retry: 3000\n\n"
            yield _sse({"tipo": "conectado"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evento = await asyncio.wait_for(fila.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(evento)
        finally:
            bus.unsubscribe(fila)

    return StreamingResponse(
        gerar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Front estático
# --------------------------------------------------------------------------- #

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


if __name__ == "__main__":
    host = os.environ.get("OPS_DETECTIVE_HOST", "127.0.0.1")
    port = int(os.environ.get("OPS_DETECTIVE_PORT", "8000"))
    uvicorn.run("api:app", host=host, port=port, log_level="info")
