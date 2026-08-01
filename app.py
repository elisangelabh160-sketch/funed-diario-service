import asyncio
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
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field
from pypdf import PdfReader


PORTAL = "https://www.jornalminasgerais.mg.gov.br"
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "").strip()

app = FastAPI(
    title="FUNED Diário Oficial Service",
    version="3.2.0",
)


class MonitoramentoRequest(BaseModel):
    data_publicacao: date
    texto_pesquisa: str = "Fundação Ezequiel Dias"


class EdicaoRequest(MonitoramentoRequest):
    id_jornal: int = Field(..., gt=0)


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
            return localizar_token_em_valor(json.loads(texto))
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
    return re.sub(r"\s+", " ", texto.lower()).strip()


def limpar_texto_pagina(texto: str) -> str:
    texto = texto.replace("\x00", "")
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n[ \t]+", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def extrair_pdf_embutido(conteudo: bytes) -> bytes | None:
    inicio_pdf = conteudo.find(b"%PDF-")
    if inicio_pdf < 0:
        return None

    fim_pdf = conteudo.rfind(b"%%EOF")
    if fim_pdf >= inicio_pdf:
        fim_pdf += len(b"%%EOF")
        return conteudo[inicio_pdf:fim_pdf]

    return conteudo[inicio_pdf:]


def extrair_pdf_de_zip(conteudo: bytes) -> bytes | None:
    if not conteudo.startswith(b"PK"):
        return None

    try:
        with zipfile.ZipFile(BytesIO(conteudo)) as arquivo_zip:
            arquivos_pdf = [
                nome
                for nome in arquivo_zip.namelist()
                if nome.lower().endswith(".pdf")
            ]
            if not arquivos_pdf:
                return None
            return arquivo_zip.read(arquivos_pdf[0])
    except Exception:
        return None


def validar_bytes_pdf(conteudo: bytes) -> bytes:
    pdf_embutido = extrair_pdf_embutido(conteudo)
    if pdf_embutido:
        return pdf_embutido

    pdf_zip = extrair_pdf_de_zip(conteudo)
    if pdf_zip:
        pdf_embutido_zip = extrair_pdf_embutido(pdf_zip)
        if pdf_embutido_zip:
            return pdf_embutido_zip

    raise HTTPException(
        status_code=502,
        detail={
            "mensagem": "O conteúdo recebido não contém um PDF válido.",
            "tamanhoBytes": len(conteudo),
            "inicioHexadecimal": conteudo[:80].hex(),
        },
    )


def tentar_decodificar_base64(valor: str) -> bytes | None:
    texto = re.sub(
        r"^data:application/pdf;base64,",
        "",
        valor.strip(),
        flags=re.IGNORECASE,
    )
    texto = re.sub(r"\s+", "", texto)

    if len(texto) < 100:
        return None

    restante = len(texto) % 4
    if restante:
        texto += "=" * (4 - restante)

    try:
        return base64.b64decode(texto, validate=True)
    except (binascii.Error, ValueError):
        return None


def coletar_candidatos_arquivo(
    valor: Any,
    caminho: str = "resposta",
) -> list[dict[str, str]]:
    candidatos: list[dict[str, str]] = []

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
                    {"caminho": novo_caminho, "valor": item.strip()}
                )

            candidatos.extend(
                coletar_candidatos_arquivo(item, novo_caminho)
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
            "Accept": "application/pdf,application/octet-stream,application/zip,*/*",
        },
        timeout=120_000,
        fail_on_status_code=False,
    )

    if not resposta.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "mensagem": "O download do arquivo foi recusado.",
                "url": url_absoluta,
                "status": resposta.status,
                "resposta": (await resposta.text())[:500],
            },
        )

    conteudo = await resposta.body()
    pdf_bytes = validar_bytes_pdf(conteudo)

    return pdf_bytes, {
        "origem": "url",
        "url": url_absoluta,
        "status": resposta.status,
        "contentType": resposta.headers.get("content-type", ""),
        "tamanhoRecebidoBytes": len(conteudo),
        "tamanhoPdfExtraidoBytes": len(pdf_bytes),
    }


async def localizar_e_obter_pdf(
    resposta_json: dict[str, Any],
    requisicoes: APIRequestContext,
    bearer_token: str,
) -> tuple[bytes, dict[str, Any]]:
    candidatos = coletar_candidatos_arquivo(resposta_json)

    if not candidatos:
        raise HTTPException(
            status_code=502,
            detail="Nenhum campo candidato a PDF foi localizado.",
        )

    candidatos = sorted(
        candidatos,
        key=lambda item: 0 if parece_url(item["valor"]) else 1,
    )

    erros: list[dict[str, Any]] = []

    for candidato in candidatos:
        caminho = candidato["caminho"]
        valor = candidato["valor"]

        try:
            if parece_url(valor):
                pdf_bytes, diagnostico = await baixar_arquivo(
                    requisicoes,
                    valor,
                    bearer_token,
                )
                diagnostico["campoOrigem"] = caminho
                return pdf_bytes, diagnostico

            decodificado = tentar_decodificar_base64(valor)
            if decodificado:
                pdf_bytes = validar_bytes_pdf(decodificado)
                return pdf_bytes, {
                    "origem": "base64",
                    "campoOrigem": caminho,
                    "tamanhoRecebidoBytes": len(decodificado),
                    "tamanhoPdfExtraidoBytes": len(pdf_bytes),
                }

        except Exception as erro:
            erros.append(
                {
                    "campo": caminho,
                    "erro": str(erro),
                }
            )

    raise HTTPException(
        status_code=502,
        detail={
            "mensagem": "Nenhum candidato resultou em PDF válido.",
            "tentativas": erros[:10],
        },
    )


def extrair_publicacoes_pdf(
    pdf_bytes: bytes,
    termos: list[str],
) -> dict[str, Any]:
    try:
        leitor = PdfReader(BytesIO(pdf_bytes), strict=False)
    except Exception as erro:
        raise HTTPException(
            status_code=502,
            detail=f"O PDF não pôde ser aberto: {erro}",
        ) from erro

    termos_unicos = list(
        dict.fromkeys(
            termo.strip()
            for termo in termos
            if termo and termo.strip()
        )
    )
    termos_normalizados = [
        normalizar_texto(termo)
        for termo in termos_unicos
    ]

    publicacoes: list[dict[str, Any]] = []
    paginas_sem_texto: list[int] = []

    for indice, pagina in enumerate(leitor.pages):
        numero_pagina = indice + 1

        try:
            texto = limpar_texto_pagina(
                pagina.extract_text() or ""
            )
        except Exception:
            texto = ""

        if not texto:
            paginas_sem_texto.append(numero_pagina)
            continue

        texto_normalizado = normalizar_texto(texto)
        termos_encontrados = [
            termo_original
            for termo_original, termo_normalizado
            in zip(termos_unicos, termos_normalizados)
            if termo_normalizado in texto_normalizado
        ]

        if termos_encontrados:
            publicacoes.append(
                {
                    "pagina": numero_pagina,
                    "termosEncontrados": termos_encontrados,
                    "textoPagina": texto,
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
        "paginasSemTextoExtraivel": paginas_sem_texto,
    }


async def localizar_token(
    pagina: Page,
    contexto: BrowserContext,
) -> str:
    token_capturado: str | None = None

    def capturar_token(requisicao) -> None:
        nonlocal token_capturado
        authorization = requisicao.headers.get("authorization")
        if authorization and authorization.startswith("Bearer "):
            token_capturado = authorization

    pagina.on("request", capturar_token)

    await pagina.goto(
        PORTAL,
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    await pagina.wait_for_timeout(3_000)

    for _ in range(20):
        if token_capturado:
            return token_capturado
        await pagina.wait_for_timeout(500)

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

    token_capturado = localizar_token_em_valor(armazenamentos)

    if not token_capturado:
        token_capturado = localizar_token_em_valor(
            await contexto.cookies()
        )

    if not token_capturado:
        raise HTTPException(
            status_code=502,
            detail="O portal foi aberto, mas nenhum Bearer Token foi localizado.",
        )

    return token_capturado


async def pesquisar_id_jornal_pela_interface(
    pagina: Page,
    carga: MonitoramentoRequest,
) -> tuple[int, dict[str, Any], str]:
    """
    Executa a pesquisa pela própria interface pública do portal.

    O Playwright preenche os campos, aciona o botão PESQUISAR e
    intercepta a resposta real de PesquisarJornaisPaginados. Assim,
    o serviço não armazena nem renova manualmente o Bearer do portal.
    """

    token_capturado: str | None = None

    def capturar_token(requisicao) -> None:
        nonlocal token_capturado

        authorization = requisicao.headers.get("authorization")

        if (
            authorization
            and authorization.startswith("Bearer ")
        ):
            token_capturado = authorization

    pagina.on("request", capturar_token)

    try:
        await pagina.goto(
            f"{PORTAL}/pesquisa",
            wait_until="domcontentloaded",
            timeout=90_000,
        )

        await pagina.wait_for_timeout(3_000)

        # Campo de texto: usa rótulos e fallbacks para reduzir
        # dependência de classes ou IDs internos do portal.
        campo_texto = pagina.get_by_label(
            re.compile(
                r"palavra|frase|conte[uú]do",
                re.IGNORECASE,
            )
        )

        if await campo_texto.count() == 0:
            campo_texto = pagina.locator(
                'input[type="text"]'
            ).first

        await campo_texto.fill(carga.texto_pesquisa)

        campos_data = pagina.locator(
            'input[type="date"], input[placeholder*="/"]'
        )

        quantidade_datas = await campos_data.count()

        if quantidade_datas < 2:
            campos_data = pagina.locator(
                'input'
            ).filter(
                has=pagina.locator(
                    '[type="date"]'
                )
            )

        data_br = carga.data_publicacao.strftime("%d/%m/%Y")
        data_iso = carga.data_publicacao.isoformat()

        # Primeiro tenta preencher pelos rótulos.
        data_inicial = pagina.get_by_label(
            re.compile(
                r"data\s*inicial",
                re.IGNORECASE,
            )
        )
        data_final = pagina.get_by_label(
            re.compile(
                r"data\s*final",
                re.IGNORECASE,
            )
        )

        if await data_inicial.count() > 0:
            try:
                await data_inicial.fill(data_iso)
            except Exception:
                await data_inicial.fill(data_br)
        elif await campos_data.count() >= 1:
            try:
                await campos_data.nth(0).fill(data_iso)
            except Exception:
                await campos_data.nth(0).fill(data_br)
        else:
            raise HTTPException(
                status_code=502,
                detail="O campo Data Inicial não foi localizado no portal.",
            )

        if await data_final.count() > 0:
            try:
                await data_final.fill(data_iso)
            except Exception:
                await data_final.fill(data_br)
        elif await campos_data.count() >= 2:
            try:
                await campos_data.nth(1).fill(data_iso)
            except Exception:
                await campos_data.nth(1).fill(data_br)
        else:
            raise HTTPException(
                status_code=502,
                detail="O campo Data Final não foi localizado no portal.",
            )

        # Mantém o Diário do Executivo selecionado. Caso o portal
        # use checkbox, garante o estado marcado.
        executivo = pagina.get_by_text(
            re.compile(
                r"di[aá]rio do executivo",
                re.IGNORECASE,
            )
        )

        if await executivo.count() > 0:
            elemento = executivo.first

            try:
                checkbox = elemento.locator(
                    'xpath=preceding::input[@type="checkbox"][1]'
                )

                if (
                    await checkbox.count() > 0
                    and not await checkbox.is_checked()
                ):
                    await checkbox.check(force=True)
            except Exception:
                # Alguns layouts usam botão visual, não checkbox.
                # Nesse caso, preservamos o estado padrão do portal.
                pass

        botao_pesquisar = pagina.get_by_role(
            "button",
            name=re.compile(
                r"pesquisar",
                re.IGNORECASE,
            ),
        )

        if await botao_pesquisar.count() == 0:
            botao_pesquisar = pagina.locator(
                'button, input[type="submit"]'
            ).filter(
                has_text=re.compile(
                    r"pesquisar",
                    re.IGNORECASE,
                )
            )

        if await botao_pesquisar.count() == 0:
            raise HTTPException(
                status_code=502,
                detail="O botão PESQUISAR não foi localizado no portal.",
            )

        try:
            async with pagina.expect_response(
                lambda resposta: (
                    "PesquisarJornaisPaginados"
                    in resposta.url
                ),
                timeout=90_000,
            ) as resposta_esperada:
                await botao_pesquisar.first.click()

            resposta_pesquisa = await resposta_esperada.value

        except PlaywrightTimeoutError as erro:
            raise HTTPException(
                status_code=504,
                detail=(
                    "O portal não retornou a resposta da pesquisa "
                    "após o clique em PESQUISAR."
                ),
            ) from erro

        if not resposta_pesquisa.ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "mensagem": (
                        "A pesquisa realizada pela interface foi recusada."
                    ),
                    "status": resposta_pesquisa.status,
                    "url": resposta_pesquisa.url,
                    "resposta": (
                        await resposta_pesquisa.text()
                    )[:1000],
                },
            )

        # O token efetivamente usado na pesquisa é a fonte de verdade.
        authorization_real = (
            resposta_pesquisa.request.headers.get(
                "authorization"
            )
        )

        if (
            authorization_real
            and authorization_real.startswith("Bearer ")
        ):
            token_capturado = authorization_real

        if not token_capturado:
            raise HTTPException(
                status_code=502,
                detail=(
                    "A pesquisa funcionou, mas o Authorization usado "
                    "pelo próprio portal não foi capturado."
                ),
            )

        try:
            resposta_json = await resposta_pesquisa.json()
        except Exception as erro:
            raise HTTPException(
                status_code=502,
                detail={
                    "mensagem": (
                        "A resposta da pesquisa não contém JSON válido."
                    ),
                    "resposta": (
                        await resposta_pesquisa.text()
                    )[:1000],
                },
            ) from erro

        resultados = resposta_json.get("dados", [])

        if isinstance(resultados, dict):
            resultados = (
                resultados.get("dados")
                or resultados.get("itens")
                or resultados.get("resultados")
                or []
            )

        if not isinstance(resultados, list) or not resultados:
            raise HTTPException(
                status_code=404,
                detail={
                    "mensagem": (
                        "Nenhuma publicação foi localizada na data "
                        "e com a expressão informadas."
                    ),
                    "dataPublicacao": (
                        carga.data_publicacao.isoformat()
                    ),
                    "textoPesquisa": carga.texto_pesquisa,
                },
            )

        candidatos_executivo = [
            item
            for item in resultados
            if isinstance(item, dict)
            and "executivo" in normalizar_texto(
                str(
                    item.get("tipoCaderno")
                    or item.get("descricaoCaderno")
                    or item.get("caderno")
                    or ""
                )
            )
        ]

        candidatos = candidatos_executivo or resultados

        candidatos_com_id = [
            item
            for item in candidatos
            if isinstance(item, dict)
            and (
                item.get("idJornal")
                or item.get("IdJornal")
                or item.get("id")
            )
        ]

        if not candidatos_com_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "mensagem": (
                        "A pesquisa retornou resultados, mas nenhum "
                        "possui idJornal."
                    ),
                    "resultados": resultados[:10],
                },
            )

        # Prioriza o resultado que contém o termo pesquisado.
        termo = normalizar_texto(carga.texto_pesquisa)

        candidatos_com_id.sort(
            key=lambda item: (
                0
                if termo in normalizar_texto(
                    " ".join(
                        str(valor)
                        for valor in item.values()
                        if valor is not None
                    )
                )
                else 1
            )
        )

        escolhido = candidatos_com_id[0]

        id_jornal = (
            escolhido.get("idJornal")
            or escolhido.get("IdJornal")
            or escolhido.get("id")
        )

        return int(id_jornal), escolhido, token_capturado

    finally:
        pagina.remove_listener("request", capturar_token)


async def processar_edicao(
    contexto: BrowserContext,
    token: str,
    id_jornal: int,
    carga: MonitoramentoRequest,
    resultado_pesquisa: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url_edicao = (
        f"{PORTAL}/api/v1/Jornal/"
        f"ObterEdicaoPorId/{id_jornal}"
    )

    resposta = await contexto.request.get(
        url_edicao,
        headers={
            "Authorization": token,
            "Accept": "application/json",
        },
        timeout=120_000,
        fail_on_status_code=False,
    )

    if not resposta.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "mensagem": "O portal recusou a consulta da edição.",
                "status": resposta.status,
                "resposta": (await resposta.text())[:500],
            },
        )

    resposta_json = await resposta.json()

    pdf_bytes, diagnostico = await localizar_e_obter_pdf(
        resposta_json,
        contexto.request,
        token,
    )

    resultado = extrair_publicacoes_pdf(
        pdf_bytes,
        [
            carga.texto_pesquisa,
            "Fundação Ezequiel Dias",
            "FUNED",
            "Funed",
        ],
    )

    dados_originais = resposta_json.get("dados", {})
    cadernos = (
        dados_originais.get("cadernos", [])
        if isinstance(dados_originais, dict)
        else []
    )

    return {
        "dados": {
            "idJornal": id_jornal,
            "dataPublicacao": carga.data_publicacao.isoformat(),
            "textoPesquisa": carga.texto_pesquisa,
            "resultadoPesquisa": resultado_pesquisa,
            "cadernos": cadernos,
            **resultado,
            "diagnosticoArquivo": diagnostico,
        },
        "erros": [],
    }


async def executar_monitoramento(
    carga: MonitoramentoRequest,
    id_jornal: int | None = None,
) -> dict[str, Any]:
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
            viewport={"width": 1440, "height": 1000},
        )

        pagina = await contexto.new_page()

        try:
            resultado_pesquisa = None

            if id_jornal is None:
                (
                    id_jornal,
                    resultado_pesquisa,
                    token,
                ) = await pesquisar_id_jornal_pela_interface(
                    pagina,
                    carga,
                )
            else:
                token = await localizar_token(
                    pagina,
                    contexto,
                )

            return await processar_edicao(
                contexto,
                token,
                id_jornal,
                carga,
                resultado_pesquisa,
            )

        except PlaywrightTimeoutError as erro:
            raise HTTPException(
                status_code=504,
                detail="O portal demorou demais para responder.",
            ) from erro

        finally:
            await contexto.close()
            await navegador.close()


@app.get("/")
async def raiz() -> dict[str, str]:
    return {
        "servico": "FUNED Diário Oficial",
        "status": "online",
        "versao": "3.2.0",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "versao": "3.2.0",
    }


@app.post("/monitoramento")
async def monitoramento(
    carga: MonitoramentoRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    verificar_chave(x_api_key)
    return await executar_monitoramento(carga)


@app.post("/edicao")
async def obter_edicao(
    carga: EdicaoRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    verificar_chave(x_api_key)
    return await executar_monitoramento(
        carga,
        id_jornal=carga.id_jornal,
    )
