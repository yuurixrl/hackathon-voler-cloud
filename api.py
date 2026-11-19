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
    POST /api/tickets/{id}/triagem  -> grava a triagem
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
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
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
