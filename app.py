import base64
import binascii
import json
import os
import re
import unicodedata
import zipfile
from datetime import date
from io import BytesIO
from typing import Any
from urllib.parse import quote, urljoin

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import (
    APIRequestContext,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field
from pypdf import PdfReader


PORTAL = "https://www.jornalminasgerais.mg.gov.br"
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "").strip()

app = FastAPI(
    title="FUNED Diário Oficial Service",
    version="2.1.0",
)


class EdicaoRequest(BaseModel):
    id_jornal: int = Field(..., gt=0)
    data_publicacao: date
    texto_pesquisa: str = "Fundação Ezequiel Dias"


# ==========================================================
# AUTENTICAÇÃO DO SERVIÇO
# ==========================================================

def verificar_chave(valor: str | None) -> None:
    if SERVICE_API_KEY and valor != SERVICE_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Chave da API do serviço inválida.",
        )


# ==========================================================
# LOCALIZAÇÃO DO TOKEN DO PORTAL
# ==========================================================

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


# ==========================================================
# TRATAMENTO DE TEXTO
# ==========================================================

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

    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n[ \t]+", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)

    return texto.strip()


# ==========================================================
# VALIDAÇÃO E ABERTURA DO PDF
# ==========================================================

def possui_assinatura_pdf(conteudo: bytes) -> bool:
    inicio = conteudo[:1024]

    return b"%PDF-" in inicio


def extrair_pdf_de_zip(conteudo: bytes) -> bytes | None:
    if not conteudo.startswith(b"PK"):
        return None

    try:
        with zipfile.ZipFile(BytesIO(conteudo)) as arquivo_zip:
            nomes = arquivo_zip.namelist()

            arquivos_pdf = [
                nome
                for nome in nomes
                if nome.lower().endswith(".pdf")
            ]

            if not arquivos_pdf:
                return None

            return arquivo_zip.read(arquivos_pdf[0])

    except Exception:
        return None


def validar_bytes_pdf(conteudo: bytes) -> bytes:
    if possui_assinatura_pdf(conteudo):
        return conteudo

    pdf_zip = extrair_pdf_de_zip(conteudo)

    if pdf_zip and possui_assinatura_pdf(pdf_zip):
        return pdf_zip

    inicio_legivel = conteudo[:200].decode(
        "utf-8",
        errors="replace",
    )

    raise HTTPException(
        status_code=502,
        detail={
            "mensagem": (
                "O conteúdo obtido não possui a assinatura de um PDF válido."
            ),
            "tamanhoBytes": len(conteudo),
            "inicioConteudo": inicio_legivel,
        },
    )


def tentar_decodificar_base64(valor: str) -> bytes | None:
    texto = valor.strip()

    texto = re.sub(
        r"^data:application/pdf;base64,",
        "",
        texto,
        flags=re.IGNORECASE,
    )

    texto = re.sub(r"\s+", "", texto)

    if len(texto) < 100:
        return None

    tentativas = [texto]

    # Base64 pode chegar sem o preenchimento "=".
    restante = len(texto) % 4

    if restante:
        tentativas.append(
            texto + ("=" * (4 - restante))
        )

    for candidato in tentativas:
        try:
            conteudo = base64.b64decode(
                candidato,
                validate=True,
            )

            if conteudo:
                return conteudo

        except (binascii.Error, ValueError):
            continue

    return None


# ==========================================================
# BUSCA RECURSIVA DE CANDIDATOS NO JSON
# ==========================================================

def coletar_candidatos_arquivo(
    valor: Any,
    caminho: str = "resposta",
) -> list[dict[str, Any]]:
    candidatos: list[dict[str, Any]] = []

    chaves_relevantes = {
        "arquivo",
        "arquivoCadernoPrincipal",
        "base64",
        "pdf",
        "pdfBase64",
        "conteudo",
        "file",
        "fileData",
        "data",
        "url",
        "link",
        "download",
        "downloadUrl",
        "urlArquivo",
        "caminho",
    }

    if isinstance(valor, dict):
        for chave, item in valor.items():
            novo_caminho = f"{caminho}.{chave}"

            if (
                chave in chaves_relevantes
                and isinstance(item, str)
                and item.strip()
            ):
                candidatos.append(
                    {
                        "caminho": novo_caminho,
                        "valor": item.strip(),
                    }
                )

            candidatos.extend(
                coletar_candidatos_arquivo(
                    item,
                    novo_caminho,
                )
            )

    elif isinstance(valor, list):
        for indice, item in enumerate(valor):
            candidatos.extend(
                coletar_candidatos_arquivo(
                    item,
                    f"{caminho}[{indice}]",
                )
            )

    return candidatos


def parece_url(valor: str) -> bool:
    texto = valor.strip().lower()

    return (
        texto.startswith("http://")
        or texto.startswith("https://")
        or texto.startswith("/")
        or texto.startswith("api/")
    )


# ==========================================================
# DOWNLOAD DO PDF
# ==========================================================

async def baixar_arquivo(
    requisicoes: APIRequestContext,
    url: str,
    bearer_token: str,
) -> tuple[bytes, dict[str, Any]]:
    url_absoluta = urljoin(PORTAL, url)

    resposta = await requisicoes.get(
        url_absoluta,
        headers={
            "Authorization": bearer_token,
            "Accept": (
                "application/pdf,"
                "application/octet-stream,"
                "application/zip,"
                "*/*"
            ),
        },
        timeout=120_000,
        fail_on_status_code=False,
    )

    content_type = resposta.headers.get(
        "content-type",
        "",
    )

    if not resposta.ok:
        texto_erro = await resposta.text()

        raise HTTPException(
            status_code=502,
            detail={
                "mensagem": "O download do arquivo foi recusado.",
                "url": url_absoluta,
                "status": resposta.status,
                "contentType": content_type,
                "resposta": texto_erro[:500],
            },
        )

    conteudo = await resposta.body()

    diagnostico = {
        "origem": "url",
        "url": url_absoluta,
        "status": resposta.status,
        "contentType": content_type,
        "tamanhoBytes": len(conteudo),
    }

    return validar_bytes_pdf(conteudo), diagnostico


async def localizar_e_obter_pdf(
    resposta_json: dict[str, Any],
    requisicoes: APIRequestContext,
    bearer_token: str,
) -> tuple[bytes, dict[str, Any]]:
    candidatos = coletar_candidatos_arquivo(
        resposta_json
    )

    if not candidatos:
        raise HTTPException(
            status_code=502,
            detail={
                "mensagem": (
                    "Nenhum campo candidato a PDF ou URL foi "
                    "localizado na resposta do portal."
                ),
                "chavesResposta": list(
                    resposta_json.keys()
                ),
            },
        )

    erros_candidatos: list[dict[str, Any]] = []

    # Primeiro tenta URLs, pois o portal pode devolver um link assinado.
    candidatos_ordenados = sorted(
        candidatos,
        key=lambda item: (
            0 if parece_url(item["valor"]) else 1
        ),
    )

    for candidato in candidatos_ordenados:
        caminho = candidato["caminho"]
        valor = candidato["valor"]

        try:
            if parece_url(valor):
                pdf_bytes, diagnostico = (
                    await baixar_arquivo(
                        requisicoes=requisicoes,
                        url=valor,
                        bearer_token=bearer_token,
                    )
                )

                diagnostico["campoOrigem"] = caminho

                return pdf_bytes, diagnostico

            bytes_decodificados = (
                tentar_decodificar_base64(valor)
            )

            if bytes_decodificados:
                pdf_bytes = validar_bytes_pdf(
                    bytes_decodificados
                )

                return pdf_bytes, {
                    "origem": "base64",
                    "campoOrigem": caminho,
                    "tamanhoBytes": len(pdf_bytes),
                }

        except HTTPException as erro:
            erros_candidatos.append(
                {
                    "campo": caminho,
                    "erro": erro.detail,
                }
            )

        except Exception as erro:
            erros_candidatos.append(
                {
                    "campo": caminho,
                    "erro": str(erro),
                }
            )

    raise HTTPException(
        status_code=502,
        detail={
            "mensagem": (
                "Foram encontrados campos candidatos, mas nenhum "
                "deles resultou em um PDF válido."
            ),
            "totalCandidatos": len(candidatos),
            "tentativas": erros_candidatos[:10],
        },
    )


# ==========================================================
# EXTRAÇÃO DO TEXTO DO PDF
# ==========================================================

def extrair_publicacoes_pdf(
    pdf_bytes: bytes,
    termos: list[str],
) -> dict[str, Any]:
    try:
        leitor = PdfReader(
            BytesIO(pdf_bytes),
            strict=False,
        )

    except Exception as erro:
        raise HTTPException(
            status_code=502,
            detail={
                "mensagem": (
                    "O arquivo possui assinatura PDF, mas não "
                    "pôde ser aberto pelo pypdf."
                ),
                "erro": str(erro),
                "tamanhoBytes": len(pdf_bytes),
            },
        ) from erro

    termos_unicos: list[str] = []

    for termo in termos:
        termo_limpo = termo.strip()

        if termo_limpo and termo_limpo not in termos_unicos:
            termos_unicos.append(termo_limpo)

    termos_normalizados = [
        normalizar_texto(termo)
        for termo in termos_unicos
    ]

    publicacoes: list[dict[str, Any]] = []
    paginas_sem_texto: list[int] = []

    for indice, pagina in enumerate(leitor.pages):
        numero_pagina = indice + 1

        try:
            texto_extraido = (
                pagina.extract_text() or ""
            )

        except Exception:
            texto_extraido = ""

        texto_extraido = limpar_texto_pagina(
            texto_extraido
        )

        if not texto_extraido:
            paginas_sem_texto.append(
                numero_pagina
            )
            continue

        texto_normalizado = normalizar_texto(
            texto_extraido
        )

        termos_encontrados = [
            termo_original
            for termo_original, termo_normalizado
            in zip(
                termos_unicos,
                termos_normalizados,
            )
            if (
                termo_normalizado
                and termo_normalizado
                in texto_normalizado
            )
        ]

        if termos_encontrados:
            publicacoes.append(
                {
                    "pagina": numero_pagina,
                    "termosEncontrados": (
                        termos_encontrados
                    ),
                    "textoPagina": texto_extraido,
                }
            )

    return {
        "totalPaginas": len(leitor.pages),
        "paginasLocalizadas": [
            item["pagina"]
            for item in publicacoes
        ],
        "totalPublicacoes": len(publicacoes),
        "publicacoes": publicacoes,
        "paginasSemTextoExtraivel": (
            paginas_sem_texto
        ),
    }


# ==========================================================
# ROTAS
# ==========================================================

@app.get("/")
async def raiz() -> dict[str, str]:
    return {
        "servico": "FUNED Diário Oficial",
        "status": "online",
        "versao": "2.1.0",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "versao": "2.1.0",
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
                (
                    "--disable-blink-features="
                    "AutomationControlled"
                ),
            ],
        )

        contexto = await navegador.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
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

            authorization = (
                requisicao.headers.get(
                    "authorization"
                )
            )

            if (
                authorization
                and authorization.startswith(
                    "Bearer "
                )
            ):
                token_capturado = authorization

        pagina.on("request", capturar_token)

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

        dados_pesquisa_json = json.dumps(
            dados_pesquisa,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        url_pesquisa = (
            f"{PORTAL}/pesquisa?dadosPesquisa="
            f"{quote(dados_pesquisa_json)}"
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
                        const chave =
                          localStorage.key(i);

                        local[chave] =
                          localStorage.getItem(chave);
                      }

                      for (
                        let i = 0;
                        i < sessionStorage.length;
                        i++
                      ) {
                        const chave =
                          sessionStorage.key(i);

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

                token_capturado = (
                    localizar_token_em_valor(
                        armazenamentos
                    )
                )

            if not token_capturado:
                cookies = await contexto.cookies()

                token_capturado = (
                    localizar_token_em_valor(
                        cookies
                    )
                )

            if not token_capturado:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "O portal foi aberto, mas nenhum "
                        "Bearer Token foi localizado."
                    ),
                )

            url_edicao = (
                f"{PORTAL}/api/v1/Jornal/"
                f"ObterEdicaoPorId/"
                f"{carga.id_jornal}"
            )

            resposta_portal = (
                await contexto.request.get(
                    url_edicao,
                    headers={
                        "Authorization": (
                            token_capturado
                        ),
                        "Accept": (
                            "application/json"
                        ),
                    },
                    timeout=120_000,
                    fail_on_status_code=False,
                )
            )

            if not resposta_portal.ok:
                texto_erro = (
                    await resposta_portal.text()
                )

                raise HTTPException(
                    status_code=502,
                    detail={
                        "mensagem": (
                            "O portal recusou a "
                            "consulta da edição."
                        ),
                        "status": (
                            resposta_portal.status
                        ),
                        "resposta": texto_erro[:500],
                    },
                )

            try:
                resposta_json = (
                    await resposta_portal.json()
                )

            except Exception as erro:
                texto_resposta = (
                    await resposta_portal.text()
                )

                raise HTTPException(
                    status_code=502,
                    detail={
                        "mensagem": (
                            "O portal não retornou "
                            "um JSON válido."
                        ),
                        "resposta": (
                            texto_resposta[:500]
                        ),
                    },
                ) from erro

            pdf_bytes, diagnostico_arquivo = (
                await localizar_e_obter_pdf(
                    resposta_json=resposta_json,
                    requisicoes=contexto.request,
                    bearer_token=token_capturado,
                )
            )

            termos_busca = [
                carga.texto_pesquisa,
                "Fundação Ezequiel Dias",
                "FUNED",
                "Funed",
            ]

            resultado_extracao = (
                extrair_publicacoes_pdf(
                    pdf_bytes=pdf_bytes,
                    termos=termos_busca,
                )
            )

            dados_originais = (
                resposta_json.get("dados", {})
            )

            cadernos = []

            if isinstance(dados_originais, dict):
                cadernos = dados_originais.get(
                    "cadernos",
                    [],
                )

            return {
                "dados": {
                    "idJornal": carga.id_jornal,
                    "dataPublicacao": (
                        carga.data_publicacao
                        .isoformat()
                    ),
                    "textoPesquisa": (
                        carga.texto_pesquisa
                    ),
                    "cadernos": cadernos,
                    "totalPaginas": (
                        resultado_extracao[
                            "totalPaginas"
                        ]
                    ),
                    "paginasLocalizadas": (
                        resultado_extracao[
                            "paginasLocalizadas"
                        ]
                    ),
                    "totalPublicacoes": (
                        resultado_extracao[
                            "totalPublicacoes"
                        ]
                    ),
                    "publicacoes": (
                        resultado_extracao[
                            "publicacoes"
                        ]
                    ),
                    "paginasSemTextoExtraivel": (
                        resultado_extracao[
                            "paginasSemTextoExtraivel"
                        ]
                    ),
                    "diagnosticoArquivo": (
                        diagnostico_arquivo
                    ),
                },
                "erros": [],
            }

        except PlaywrightTimeoutError as erro:
            raise HTTPException(
                status_code=504,
                detail=(
                    "O portal demorou demais "
                    "para responder."
                ),
            ) from erro

        except HTTPException:
            raise

        except Exception as erro:
            raise HTTPException(
                status_code=502,
                detail={
                    "mensagem": (
                        "Falha inesperada ao "
                        "processar a edição."
                    ),
                    "erro": str(erro),
                    "tipo": type(erro).__name__,
                },
            ) from erro

        finally:
            await contexto.close()
            await navegador.close()
