"""Cliente Gemini compartilhado pelo agente da web e pelo chat de terminal.

A chave vem de `GEMINI_API_KEY` (arquivo `.env`, ignorado pelo git). O guia de
instalação diz que chaves `AQ.` usam Vertex AI; nesta máquina o endpoint Vertex
está com a API desabilitada no projeto, então o padrão é usar o endpoint do AI
Studio, que aceita a mesma chave. Para forçar Vertex, defina
`GEMINI_USAR_VERTEX=1`.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

MODELO_PADRAO = "gemini-3.5-flash-lite"
MODELOS_FALLBACK = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash")


def carregar_env() -> None:
    """Carrega o `.env` da raiz do projeto sem sobrescrever variáveis reais."""
    load_dotenv()


def chave_api() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def modo_agente() -> str:
    """Força `demo`/`gemini` via env; sem override, usa a presença da chave.

    `OPS_AGENTE_MODO=demo` é útil para testar o fallback offline mesmo com o
    `.env` preenchido na máquina de desenvolvimento.
    """
    forcado = os.environ.get("OPS_AGENTE_MODO", "").strip().lower()
    if forcado in ("demo", "offline"):
        return "demo"
    if forcado in ("gemini", "ia", "llm"):
        return "gemini"
    return "gemini" if chave_api() else "demo"


def usar_vertex() -> bool:
    return os.environ.get("GEMINI_USAR_VERTEX", "").strip().lower() in (
        "1", "true", "sim", "yes"
    )


def modelo() -> str:
    """Modelo usado pelo agente; configurável sem mexer no código."""
    return (
        os.environ.get("OPS_AGENTE_MODELO", "").strip()
        or os.environ.get("GEMINI_MODELO", "").strip()
        or MODELO_PADRAO
    )


def modelos_candidatos() -> list[str]:
    """Modelo configurado primeiro, depois alternativas se houver 503/404."""
    atual = modelo()
    return [atual] + [m for m in MODELOS_FALLBACK if m != atual]


def criar_cliente() -> Any:
    """Cria o cliente `google-genai` a partir do `.env`."""
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError(
            "Pacote google-genai ausente: rode `pip install -r requirements.txt`."
        ) from exc

    chave = chave_api()
    if not chave:
        raise RuntimeError("Defina GEMINI_API_KEY no arquivo .env antes de iniciar.")

    if chave.startswith("AQ.") and usar_vertex():
        return genai.Client(vertexai=True, api_key=chave)
    return genai.Client(api_key=chave)
