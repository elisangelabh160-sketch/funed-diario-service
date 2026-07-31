import base64
import json
import os
import re
import unicodedata
from datetime import date
from io import BytesIO
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field
from pypdf import PdfReader


PORTAL = "https://www.jornalminasgerais.mg.gov.br"
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "").strip()


app = FastAPI(
    title="FUNED Diário Oficial Service",
    version="2.0.0",
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


def normalizar_texto(valor: str) -> str:
    texto = unicodedata.normalize("NFD", valor)

    texto = "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )

    texto = texto.lower()

    return re.sub(r"\s+", " ", texto).strip()


def limpar_texto_pagina(texto: str) -> str:
    texto = texto.replace("\x00", "")

    texto = re.sub(
        r"[ \t]+",
        " ",
        texto,
    )

    texto = re.sub(
        r"\n[ \t]+",
        "\n",
        texto,
    )

    texto = re.sub(
        r"\n{3,}",
        "\n\n",
        texto,
    )

    return texto.strip()


def localizar_pdf_base64(resposta: dict[str, Any]) -> str:
    dados = resposta.get("dados", resposta)

    arquivo_principal = dados.get(
        "arquivoCadernoPrincipal",
        {},
    )

    if isinstance(arquivo_principal, dict):
        arquivo = arquivo_principal.get("arquivo")

        if isinstance(arquivo, str) and arquivo.strip():
            return arquivo.strip()

    candidatos = [
        dados.get("arquivo"),
        dados.get("pdf"),
        dados.get("base64"),
        resposta.get("arquivo"),
        resposta.get("pdf"),
        resposta.get("base64"),
    ]

    for candidato in candidatos:
        if isinstance(candidato, str) and candidato.strip():
            return candidato.strip()

    raise HTTPException(
        status_code=502,
        detail=(
            "O portal respondeu, mas o PDF da edição não foi "
            "encontrado na resposta."
        ),
    )


def extrair_publicacoes_pdf(
    pdf_base64: str,
    termos: list[str],
) -> dict[str, Any]:
    pdf_limpo = re.sub(
        r"^data:application/pdf;base64,",
        "",
        pdf_base64.strip(),
        flags=re.IGNORECASE,
    )

    pdf_limpo = re.sub(
        r"\s+",
        "",
        pdf_limpo,
    )

    try:
        pdf_bytes = base64.b64decode(
            pdf_limpo,
            validate=True,
        )
    except Exception as erro:
        raise HTTPException(
            status_code=502,
            detail="O PDF retornado pelo portal possui Base64 inválido.",
        ) from erro

    try:
        leitor = PdfReader(BytesIO(pdf_bytes))
    except Exception as erro:
        raise HTTPException(
            status_code=502,
            detail=f"Não foi possível abrir o PDF da edição: {erro}",
        ) from erro

    termos_normalizados = [
        normalizar_texto(termo)
        for termo in termos
        if termo and termo.strip()
    ]

    publicacoes: list[dict[str, Any]] = []
    paginas_sem_texto: list[int] = []

    for indice, pagina in enumerate(leitor.pages):
        numero_pagina = indice + 1

        try:
            texto_extraido = pagina.extract_text() or ""
        except Exception:
            texto_extraido = ""

        texto_extraido = limpar_texto_pagina(
            texto_extraido
        )

        if not texto_extraido:
            paginas_sem_texto.append(numero_pagina)
            continue

        texto_normalizado = normalizar_texto(
            texto_extraido
        )

        termos_encontrados = [
            termo_original
            for termo_original, termo_normalizado in zip(
                termos,
                termos_normalizados,
            )
            if termo_normalizado
            and termo_normalizado in texto_normalizado
        ]

        if termos_encontrados:
            publicacoes.append(
                {
                    "pagina": numero_pagina,
                    "termosEncontrados": termos_encontrados,
                    "textoPagina": texto_extraido,
                }
            )

    return {
        "totalPaginas": len(leitor.pages),
        "paginasLocalizadas": [
            item["pagina"]
            for item in publicacoes
        ],
        "publicacoes": publicacoes,
        "paginasSemTextoExtraivel": paginas_sem_texto,
    }


@app.get("/")
async def raiz() -> dict[str, str]:
    return {
        "servico": "FUNED Diário Oficial",
        "status": "online",
        "versao": "2.0.0",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "versao": "2.0.0",
    }


@app.post("/edicao")
async def obter_edicao(
    carga: EdicaoRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    verificar_chave(x_api_key)

    token_capturado: str | None = None

    async with async_playwright() as playwright:
        navegador = await playwright.chromium.launch(
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
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        pagina = await contexto.new_page()

        def capturar_token(requisicao) -> None:
            nonlocal token_capturado

            authorization = requisicao.headers.get(
                "authorization"
            )

            if (
                authorization
                and authorization.startswith("Bearer ")
            ):
                token_capturado = authorization

        pagina.on(
            "request",
            capturar_token,
        )

        dados_pesquisa = {
            "PaginaAtual": 1,
            "TamanhoPagina": 20,
            "textoPesquisa": carga.texto_pesquisa,
            "dataPublicacaoInicial": (
                carga.data_publicacao.isoformat()
            ),
            "dataPublicacaoFinal": (
                carga.data_publicacao.isoformat()
            ),
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
            await pagina.goto(
                PORTAL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            await pagina.wait_for_timeout(3_000)

            await pagina.goto(
                url_pesquisa,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            await pagina.wait_for_timeout(8_000)

            for _ in range(60):
                if token_capturado:
                    break

                await pagina.wait_for_timeout(500)

            if not token_capturado:
                armazenamentos = await pagina.evaluate(
                    """
                    () => {
                      const local = {};
                      const session = {};

                      for (
                        let i = 0;
                        i < localStorage.length;
                        i++
                      ) {
                        const chave = localStorage.key(i);
                        local[chave] =
                          localStorage.getItem(chave);
                      }

                      for (
                        let i = 0;
                        i < sessionStorage.length;
                        i++
                      ) {
                        const chave = sessionStorage.key(i);
                        session[chave] =
                          sessionStorage.getItem(chave);
                      }

                      return {
                        local,
                        session
                      };
                    }
                    """
                )

                token_capturado = localizar_token_em_valor(
                    armazenamentos
                )

            if not token_capturado:
                cookies = await contexto.cookies()

                token_capturado = localizar_token_em_valor(
                    cookies
                )

            if not token_capturado:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "O portal foi aberto, mas nenhum Bearer "
                        "Token foi localizado."
                    ),
                )

            url_edicao = (
                f"{PORTAL}/api/v1/Jornal/"
                f"ObterEdicaoPorId/{carga.id_jornal}"
            )

            resposta_portal = await contexto.request.get(
                url_edicao,
                headers={
                    "Authorization": token_capturado,
                    "Accept": "application/json",
                },
                timeout=120_000,
            )

            if not resposta_portal.ok:
                texto_erro = await resposta_portal.text()

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "O portal recusou a consulta da edição. "
                        f"Status: {resposta_portal.status}. "
                        f"Resposta: {texto_erro[:500]}"
                    ),
                )

            resposta_json = await resposta_portal.json()
            return resposta_json

            pdf_base64 = localizar_pdf_base64(
                resposta_json
            )

            termos_busca = [
                carga.texto_pesquisa,
                "Fundação Ezequiel Dias",
                "FUNED",
                "Funed",
            ]

            resultado_extracao = extrair_publicacoes_pdf(
                pdf_base64,
                termos_busca,
            )

            dados_originais = resposta_json.get(
                "dados",
                {},
            )

            cadernos = dados_originais.get(
                "cadernos",
                [],
            )

            return {
                "dados": {
                    "idJornal": carga.id_jornal,
                    "dataPublicacao": (
                        carga.data_publicacao.isoformat()
                    ),
                    "textoPesquisa": carga.texto_pesquisa,
                    "cadernos": cadernos,
                    "totalPaginas": resultado_extracao[
                        "totalPaginas"
                    ],
                    "paginasLocalizadas": resultado_extracao[
                        "paginasLocalizadas"
                    ],
                    "totalPublicacoes": len(
                        resultado_extracao["publicacoes"]
                    ),
                    "publicacoes": resultado_extracao[
                        "publicacoes"
                    ],
                    "paginasSemTextoExtraivel": (
                        resultado_extracao[
                            "paginasSemTextoExtraivel"
                        ]
                    ),
                },
                "erros": [],
            }

        except PlaywrightTimeoutError as erro:
            raise HTTPException(
                status_code=504,
                detail=(
                    "O portal demorou demais para responder."
                ),
            ) from erro

        except HTTPException:
            raise

        except Exception as erro:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Falha ao processar a edição: {erro}"
                ),
            ) from erro

        finally:
            await contexto.close()
            await navegador.close()
