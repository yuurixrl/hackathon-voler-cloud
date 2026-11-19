r"""Chatbot de terminal: Gemini com tool-use sobre o servidor MCP do Ops Detective.

Uso:
    .\.venv\Scripts\python.exe chat_gemini.py

A chave fica no `.env` (veja `.env.example`). O modelo padrão é `gemini-3.7-flash`.
O agente web (`agente.py`) usa o mesmo cliente Gemini e as mesmas tools MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from gemini_cliente import carregar_env, criar_cliente, modelos_candidatos, modelo
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

carregar_env()

RAIZ = os.path.dirname(os.path.abspath(__file__))
MAX_ITERACOES = 6


def criar_cliente_gemini() -> Any:
    """Compatível com o guia de instalação: devolve o cliente configurado no `.env`."""
    return criar_cliente()


def _parametros_mcp() -> StdioServerParameters:
    return StdioServerParameters(
        command=os.path.join(RAIZ, ".venv", "Scripts", "python.exe"),
        args=[os.path.join(RAIZ, "server.py")],
        cwd=RAIZ,
    )


async def _conversar(
    cliente: Any,
    types: Any,
    session: ClientSession,
    contents: list[Any],
    config: Any,
    mensagem: str,
) -> str:
    """Executa um turno com histórico e devolve o texto final do Gemini."""
    contents.append(types.Content(role="user", parts=[types.Part(text=mensagem)]))
    texto_final = ""

    modelos = modelos_candidatos()
    for _ in range(MAX_ITERACOES):
        resposta = None
        for tentativa in range(3):
            try:
                resposta = await asyncio.wait_for(
                    cliente.aio.models.generate_content(
                        model=modelos[tentativa % len(modelos)],
                        contents=contents,
                        config=config,
                    ),
                    timeout=30,
                )
                break
            except Exception as erro:
                if tentativa == 2:
                    raise
                await asyncio.sleep(0.5 * (tentativa + 1))
        if not resposta.candidates or not resposta.candidates[0].content:
            break
        parte = resposta.candidates[0].content

        for pedaco in parte.parts or []:
            if pedaco.text and pedaco.text.strip():
                texto_final += ("\n" if texto_final else "") + pedaco.text.strip()

        chamadas = [p.function_call for p in (parte.parts or []) if p.function_call]
        if not chamadas:
            break

        contents.append(parte)
        respostas: list[Any] = []
        for chamada in chamadas:
            argumentos = dict(chamada.args or {})
            print("  [MCP -> {}] {}".format(
                chamada.name,
                json.dumps(argumentos, ensure_ascii=False),
            ))
            resposta_tool = await session.call_tool(chamada.name, argumentos)
            dados = json.loads(resposta_tool.content[0].text) if resposta_tool.content else {
                "ok": False, "erro": "tool MCP retornou vazio"
            }
            respostas.append(
                types.Part.from_function_response(name=chamada.name, response=dados)
            )
        contents.append(types.Content(role="user", parts=respostas))

    return texto_final or "Não consegui gerar uma resposta final."


async def main() -> None:
    cliente = criar_cliente_gemini()
    from google.genai import types

    print("Conectando ao servidor MCP Ops Detective...")
    async with stdio_client(_parametros_mcp()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            ferramentas_mcp = (await session.list_tools()).tools
            ferramentas = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description or t.name,
                            parameters_json_schema=t.inputSchema,
                        )
                    ]
                )
                for t in ferramentas_mcp
            ]

            resource = await session.read_resource("playbook://triagem")
            regras = resource.contents[0].text
            system = (
                "Você é o Ops Detective, chatbot especialista em triagem de suporte "
                "técnico N1 -> N2, conectado ao banco da empresa via MCP.\n"
                "- Sempre use as ferramentas MCP para consultar tickets, clientes e logs "
                "antes de responder.\n"
                "- Só grave a triagem no banco (`triar_ticket`) quando o usuário pedir "
                "para resolver/triar o chamado ou confirmar a ação.\n"
                "- Responda sempre em português e de forma curta.\n"
                "- Siga rigorosamente as regras do playbook oficial abaixo:\n\n"
                + regras
            )
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
                tools=ferramentas,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )

            print("MCP ativo ({} tools): {}".format(
                len(ferramentas_mcp), ", ".join(t.name for t in ferramentas_mcp)
            ))
            print("Modelo Gemini: " + modelo())
            print("Converse com o Ops Detective (digite 'sair' para encerrar).\n" + "-" * 65)

            contents: list[Any] = []
            while True:
                try:
                    mensagem = await asyncio.to_thread(input, "\nVocê: ")
                except (EOFError, KeyboardInterrupt):
                    print("\nEncerrando chatbot...")
                    break

                texto = mensagem.strip()
                if not texto:
                    continue
                if texto.lower() in ("sair", "exit", "quit"):
                    print("Encerrando chatbot...")
                    break

                try:
                    resposta = await _conversar(
                        cliente, types, session, contents, config, texto
                    )
                    print("\nOps Detective:\n" + resposta)
                except Exception as erro:  # noqa: BLE001
                    print("\n[Erro na requisição]: {}".format(erro))


if __name__ == "__main__":
    asyncio.run(main())
