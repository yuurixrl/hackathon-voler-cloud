r"""Painel visual ao vivo para a apresentacao do Ops Detective (porta 8080).

Este arquivo e apenas um lancador: reaproveita a aplicacao FastAPI de `api.py`
(rotas, SSE e arquivos estaticos) e sobe na porta usada no roteiro de palco.
Nao duplica logica de banco nem de triagem — tudo continua em `triagem.py`.

Uso:
    .\.venv\Scripts\python.exe setup_db.py      # estado inicial: 8 chamados abertos
    .\.venv\Scripts\python.exe painel_ao_vivo.py
    # navegador: http://127.0.0.1:8080  (painel a esquerda, Claude a direita)

Variaveis de ambiente:
    OPS_PAINEL_PORT  (padrao 8080)
    OPS_DETECTIVE_HOST (padrao 127.0.0.1)
"""

from __future__ import annotations

import os

import uvicorn

from api import app

HOST = os.environ.get("OPS_DETECTIVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PAINEL_PORT", "8080"))


if __name__ == "__main__":
    print(f"Painel ao vivo em http://{HOST}:{PORT}")
    print("Deixe o Claude Desktop na metade direita da tela e este painel na esquerda.")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
