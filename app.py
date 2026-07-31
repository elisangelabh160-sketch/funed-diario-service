import json
import os
from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

PORTAL = "https://www.jornalminasgerais.mg.gov.br"
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "").strip()

app = FastAPI(
    title="FUNED Diário Oficial Service",
    version="1.1.0",
)


class EdicaoRequest(BaseModel):
    id_jornal: int = Field(..., gt=0)
    data_publicacao: date
    texto_pesquisa: str = "Fundação Ezequiel Dias"


def verificar_chave(valor: str | None) -> None:
    if SERVICE_API_KEY and valor != SERVICE_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Chave da API do serviço inválida.",
        )


def localizar_token_em_valor(valor: Any) -> str | None:
    if isinstance(valor, str):
        texto = valor.strip()

        if texto.startswith("Bearer "):
            return texto

        if texto.startswith("eyJ") and texto.count(".") >= 2:
            return f"Bearer {texto}"

        try:
            convertido = json.loads(texto)
            return localizar_token_em_valor(convertido)
        except Exception:
            return None

    if isinstance(valor, dict):
        for item in valor.values():
            token = localizar_token_em_valor(item)
            if token:
                return token

    if isinstance(valor, list):
        for item in valor:
            token = localizar_token_em_valor(item)
            if token:
                return token

    return None


@app.get("/")
async def raiz() -> dict[str, str]:
    return {
        "servico": "FUNED Diário Oficial",
        "status": "online",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/edicao")
async def obter_edicao(
    carga: EdicaoRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    verificar_chave(x_api_key)

    token_capturado: str | None = None

    async with async_playwright() as p:
        navegador = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        contexto = await navegador.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
        )

        pagina = await contexto.new_page()

        def capturar_token(requisicao) -> None:
            nonlocal token_capturado

            authorization = requisicao.headers.get("authorization")

            if (
                authorization
                and authorization.startswith("Bearer ")
            ):
                token_capturado = authorization

        pagina.on("request", capturar_token)

        dados_pesquisa = {
            "PaginaAtual": 1,
            "TamanhoPagina": 20,
            "textoPesquisa": carga.texto_pesquisa,
            "dataPublicacaoInicial": carga.data_publicacao.isoformat(),
            "dataPublicacaoFinal": carga.data_publicacao.isoformat(),
            "DiarioExecutivo": True,
            "Municipios": False,
            "Terceiros": False,
            "EdicaoExtra": False,
        }

        url_pesquisa = (
            f"{PORTAL}/pesquisa?dadosPesquisa="
            f"{quote(json.dumps(dados_pesquisa, ensure_ascii=False))}"
        )

        try:
            # Abre primeiro a página inicial para permitir que o portal
            # inicialize cookies, scripts e mecanismos de autenticação.
            await pagina.goto(
                PORTAL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            await pagina.wait_for_timeout(3_000)

            # Depois abre a pesquisa da FUNED.
            await pagina.goto(
                url_pesquisa,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            await pagina.wait_for_timeout(8_000)

            # Tenta obter o token das requisições por até 30 segundos.
            for _ in range(60):
                if token_capturado:
                    break

                await pagina.wait_for_timeout(500)

            # Caso o token não tenha aparecido nos headers,
            # procura no localStorage e no sessionStorage.
            if not token_capturado:
                armazenamentos = await pagina.evaluate(
                    """
                    () => {
                      const local = {};
                      const session = {};

                      for (let i = 0; i < localStorage.length; i++) {
                        const chave = localStorage.key(i);
                        local[chave] = localStorage.getItem(chave);
                      }

                      for (let i = 0; i < sessionStorage.length; i++) {
                        const chave = sessionStorage.key(i);
                        session[chave] = sessionStorage.getItem(chave);
                      }

                      return { local, session };
                    }
                    """
                )

                token_capturado = localizar_token_em_valor(
                    armazenamentos
                )

            # Última tentativa: procura o JWT nos cookies.
            if not token_capturado:
                cookies = await contexto.cookies()

                token_capturado = localizar_token_em_valor(
                    cookies
                )

            if not token_capturado:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "O portal foi aberto, mas nenhum Bearer Token "
                        "foi localizado nos headers, no armazenamento "
                        "do navegador ou nos cookies."
                    ),
                )

            resposta = await pagina.evaluate(
                """
                async ({ portal, idJornal, bearerToken }) => {
                  const url =
                    `${portal}/api/v1/Jornal/ObterEdicaoPorId/${idJornal}`;

                  const response = await fetch(url, {
                    method: "GET",
                    headers: {
                      "Authorization": bearerToken,
                      "Accept": "application/json"
                    }
                  });

                  const texto = await response.text();

                  if (!response.ok) {
                    throw new Error(
                      `${response.status} - ${texto}`
                    );
                  }

                  return JSON.parse(texto);
                }
                """,
                {
                    "portal": PORTAL,
                    "idJornal": carga.id_jornal,
                    "bearerToken": token_capturado,
                },
            )

            return resposta

        except PlaywrightTimeoutError as erro:
            raise HTTPException(
                status_code=504,
                detail="O portal demorou demais para responder.",
            ) from erro

        except HTTPException:
            raise

        except Exception as erro:
            raise HTTPException(
                status_code=502,
                detail=f"Falha ao obter a edição: {erro}",
            ) from erro

        finally:
            await contexto.close()
            await navegador.close()
