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
from starlette.concurrency import run_in_threadpool
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

# CORREÇÃO 02/10/2026: as tabelas de "Licenças para tratamento de saúde
# DEFERIDAS/INDEFERIDAS" são organizadas em blocos que ficam valendo até o
# próximo cabeçalho aparecer — e esse cabeçalho pode estar MUITO antes da
# página atual (o teste real mostrou um caso em que a página imediatamente
# anterior não tinha cabeçalho NENHUM, e o cabeçalho que realmente valia
# estava 2 páginas atrás). Por isso o contexto de "só a página anterior" não
# é suficiente — precisa de um estado que atravesse o documento inteiro.
RE_CATEGORIA_LICENCA = re.compile(
    r"Licenç(?:a|as)\s+para\s+tratamento\s+de\s+sa[úu]de\s+(DEFERIDA|INDEFERIDA)S?",
    re.IGNORECASE,
)

# CORREÇÃO 02/10/2026 (2ª): detectado no teste real da edição de 02/10/2026 —
# a página 32 tinha 5 atos distintos da FUNED (quinquênio, retificação de
# férias prêmio, duas dispensas/designações de função gratificada, e uma
# portaria criando uma unidade), mas o modelo gratuito só extraiu 1 deles. A
# página tinha ~36 mil caracteres com publicações de VÁRIOS outros órgãos
# misturadas (SES, CIB-SUS/MG etc.) — é "agulha no palheiro" demais pra um
# modelo gratuito garantir que viu tudo. Em vez de confiar só na atenção do
# modelo, contamos aqui, de forma determinística, quantas menções distintas
# ao termo existem na página, pra (a) avisar o modelo quantas ele precisa
# justificar e (b) conferir depois, no main.py, se cada uma foi mesmo
# coberta por alguma publicação extraída.
RE_MENCAO_FUNED = re.compile(
    r"Funda[cç][aã]o\s+Ezequiel\s+Dias|\bFUNED\b",
    re.IGNORECASE,
)


def _agrupar_mencoes_funed(texto: str, raio_cluster: int = 80) -> list[str]:
    """Encontra todas as ocorrências de 'FUNED'/'Fundação Ezequiel Dias' no
    texto da página e agrupa as que estão muito próximas (ex: 'Fundação
    Ezequiel Dias - FUNED' escritos juntos, que são a mesma menção, não
    duas) num único "trecho". Devolve um trecho de texto (um pedaço do
    conteúdo original ao redor de cada grupo) por menção distinta — usado
    depois pra conferir se cada uma virou alguma publicação."""
    posicoes = [m.start() for m in RE_MENCAO_FUNED.finditer(texto)]
    if not posicoes:
        return []

    grupos: list[int] = [posicoes[0]]
    for pos in posicoes[1:]:
        if pos - grupos[-1] > raio_cluster:
            grupos.append(pos)
        # senão, é a mesma menção (ex: "Fundação Ezequiel Dias" seguido de
        # "FUNED" logo depois) — não conta como uma segunda ocorrência.

    return [
        texto[max(0, pos - 30): pos + 150]
        for pos in grupos
    ]

MAX_TENTATIVAS_MONITORAMENTO = 4
ESPERAS_MONITORAMENTO_SEGUNDOS = [5, 10, 20]
STATUS_REPETIVEIS = {401, 408, 429, 500, 502, 503, 504}

app = FastAPI(
    title="FUNED Diário Oficial Service",
    version="3.6.0",
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
    pdf_bytes = await run_in_threadpool(validar_bytes_pdf, conteudo)

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
                pdf_bytes = await run_in_threadpool(validar_bytes_pdf, decodificado)
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
    # CORREÇÃO 18/09/2026: guarda o texto da página anterior (a que acabou de
    # ser processada, tenha ou não tido menção à FUNED) pra anexar como
    # contexto em qualquer página que dê match. Isso resolve o caso de
    # tabelas de licença (DEFERIDA/INDEFERIDA) que começam numa página e
    # continuam na seguinte sem repetir o cabeçalho — sem esse contexto, não
    # tinha como saber a categoria correta olhando só a página do match.
    texto_pagina_anterior = ""

    # CORREÇÃO 02/10/2026: estado que atravessa TODAS as páginas (não só a
    # anterior), guardando a última categoria "DEFERIDA"/"INDEFERIDA" vista
    # em qualquer página já processada. Testado com a edição de 02/10/2026:
    # a tabela da página 29 (Edilene De Jesus Ferreira, Sandra Teresinha Dos
    # Santos Gomes) não tinha cabeçalho nem na própria página 29 nem na 28
    # (a anterior) — o cabeçalho que realmente valia estava na página 27.
    # Por isso o estado precisa ser global ao documento, não só da página
    # anterior.
    categoria_licenca_vigente: str | None = None

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
            texto_pagina_anterior = ""
            continue

        texto_normalizado = normalizar_texto(texto)
        termos_encontrados = [
            termo_original
            for termo_original, termo_normalizado
            in zip(termos_unicos, termos_normalizados)
            if termo_normalizado in texto_normalizado
        ]

        cabecalhos_na_pagina = list(RE_CATEGORIA_LICENCA.finditer(texto))

        if termos_encontrados:
            # Posição do primeiro trecho que bateu com algum termo buscado,
            # pra saber qual cabeçalho de categoria (se algum) vem ANTES dele
            # nesta mesma página, na ordem real do texto extraído.
            posicao_termo = None
            for termo_original in termos_encontrados:
                pos = texto_normalizado.find(
                    normalizar_texto(termo_original)
                )
                if pos != -1 and (posicao_termo is None or pos < posicao_termo):
                    posicao_termo = pos

            categoria_nesta_pagina = categoria_licenca_vigente
            if posicao_termo is not None:
                cabecalhos_antes = [
                    m for m in cabecalhos_na_pagina
                    if m.start() < posicao_termo
                ]
                if cabecalhos_antes:
                    categoria_nesta_pagina = (
                        cabecalhos_antes[-1].group(1).upper()
                    )
            # senão, mantém categoria_licenca_vigente (carregada das páginas
            # anteriores) — é o caso em que o item da FUNED aparece antes de
            # qualquer cabeçalho nesta página, ou seja, a tabela começou
            # antes e segue valendo a última categoria vista.

            trechos_mencoes = _agrupar_mencoes_funed(texto)

            publicacoes.append(
                {
                    "pagina": numero_pagina,
                    "termosEncontrados": termos_encontrados,
                    "textoPagina": texto,
                    "textoPaginaAnterior": texto_pagina_anterior,
                    "categoriaLicencaVigente": categoria_nesta_pagina,
                    "totalMencoesFuned": len(trechos_mencoes),
                    "trechosMencoesFuned": trechos_mencoes,
                }
            )

        # Atualiza o estado global com o último cabeçalho visto nesta
        # página (se houve algum) — vale pra próxima página, não importa
        # quantas páginas sem cabeçalho vierem depois.
        if cabecalhos_na_pagina:
            categoria_licenca_vigente = (
                cabecalhos_na_pagina[-1].group(1).upper()
            )

        texto_pagina_anterior = texto

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

        campo_texto = pagina.locator("input, textarea").and_(
            pagina.get_by_label(
                re.compile(
                    r"palavra|frase|conte[uú]do",
                    re.IGNORECASE,
                )
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

    resultado = await run_in_threadpool(
        extrair_publicacoes_pdf,
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


async def executar_monitoramento_uma_vez(
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


def erro_repetivel(erro: Exception) -> bool:
    """Indica se a falha admite nova tentativa automática."""
    if isinstance(erro, HTTPException):
        return erro.status_code in STATUS_REPETIVEIS

    return isinstance(
        erro,
        (
            PlaywrightTimeoutError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def resumir_erro(erro: Exception) -> dict[str, Any]:
    if isinstance(erro, HTTPException):
        return {
            "tipo": type(erro).__name__,
            "status": erro.status_code,
            "detalhe": erro.detail,
        }

    return {
        "tipo": type(erro).__name__,
        "detalhe": str(erro),
    }


async def executar_monitoramento(
    carga: MonitoramentoRequest,
    id_jornal: int | None = None,
) -> dict[str, Any]:
    """
    Executa o monitoramento com backoff progressivo:
    tentativa imediata; depois 5 s, 10 s e 20 s.
    """
    historico_erros: list[dict[str, Any]] = []

    for tentativa in range(1, MAX_TENTATIVAS_MONITORAMENTO + 1):
        try:
            resultado = await executar_monitoramento_uma_vez(
                carga,
                id_jornal=id_jornal,
            )

            dados = resultado.setdefault("dados", {})
            dados["tentativaUtilizada"] = tentativa
            dados["totalTentativasPermitidas"] = (
                MAX_TENTATIVAS_MONITORAMENTO
            )

            if historico_erros:
                resultado["tentativasAnteriores"] = historico_erros

            return resultado

        except Exception as erro:
            historico_erros.append(
                {
                    "tentativa": tentativa,
                    **resumir_erro(erro),
                }
            )

            ultima_tentativa = (
                tentativa >= MAX_TENTATIVAS_MONITORAMENTO
            )

            if ultima_tentativa or not erro_repetivel(erro):
                if isinstance(erro, HTTPException):
                    raise HTTPException(
                        status_code=erro.status_code,
                        detail={
                            "mensagem": (
                                "O monitoramento não pôde ser concluído."
                            ),
                            "erroFinal": erro.detail,
                            "tentativas": historico_erros,
                        },
                    ) from erro

                raise HTTPException(
                    status_code=502,
                    detail={
                        "mensagem": (
                            "O monitoramento falhou após as tentativas "
                            "automáticas."
                        ),
                        "tentativas": historico_erros,
                    },
                ) from erro

            espera = ESPERAS_MONITORAMENTO_SEGUNDOS[
                tentativa - 1
            ]
            await asyncio.sleep(espera)

    raise HTTPException(
        status_code=502,
        detail={
            "mensagem": "Falha inesperada no mecanismo de tentativas.",
            "tentativas": historico_erros,
        },
    )


@app.get("/")
async def raiz() -> dict[str, str]:
    return {
        "servico": "FUNED Diário Oficial",
        "status": "online",
        "versao": "3.6.0",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "versao": "3.6.0",
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
