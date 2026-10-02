"""
Monitoramento do Diário Oficial - FUNED
Versão 100% gratuita (substitui o fluxo n8n).

Pipeline:
  1. Chama o endpoint /monitoramento do serviço Python+Playwright já publicado no Render
     (mesmos parâmetros do fluxo n8n atual: data de hoje, busca "Funed", Diário do Executivo).
  2. Manda o texto das páginas para um modelo GRATUITO do OpenRouter, pedindo um JSON
     estruturado (mesmo "shape" que o fluxo n8n já produzia).
  3. Renderiza esse JSON no MESMO layout HTML do e-mail atual (cabeçalho azul, box de
     resumo, card por publicação, box bege com o conteúdo oficial, resumo objetivo).
  4. Envia por e-mail via Gmail (SMTP + App Password), para a lista fixa da equipe SDC.

Segredos esperados como variáveis de ambiente (configure como GitHub Actions Secrets):
  RENDER_BASE_URL        -> ex: https://funed-diario-service.onrender.com (sem barra no final)
  SERVICE_API_KEY        -> a mesma chave já configurada no ambiente do seu serviço no Render
  OPENROUTER_API_KEY     -> chave gratuita do OpenRouter (openrouter.ai/keys)
  GMAIL_USER              -> conta Gmail remetente (ex: elisangelabh160@gmail.com)
  GMAIL_APP_PASSWORD     -> senha de app do Gmail (não é a senha normal da conta)
  DESTINATARIOS           -> lista de e-mails separados por vírgula

Contrato real do endpoint (confirmado lendo app.py/README do repositório
funed-diario-service):
  POST {RENDER_BASE_URL}/monitoramento
  Header: X-API-Key: <SERVICE_API_KEY>
  Body: {"data_publicacao": "YYYY-MM-DD", "texto_pesquisa": "Fundação Ezequiel Dias"}
  Resposta: {"dados": {"totalPublicacoes": N, "publicacoes": [{"pagina": 8, "textoPagina": "..."}]}}

--------------------------------------------------------------------------
HISTÓRICO DE CORREÇÕES
--------------------------------------------------------------------------
18/09/2026 — Diagnóstico: a página 19 (Portaria FUNED nº 75/2026) sumiu do
e-mail. Log da execução mostrou que as 5 tentativas de chamar a OpenRouter
para essa página bateram em "429 Too Many Requests" seguidas, sem nunca
conseguir uma resposta — não foi a IA "decidindo" que não havia nada da
FUNED, foi rate limit do plano gratuito. Duas correções aplicadas:
  1. A espera entre tentativas em caso de 429 agora CRESCE a cada tentativa
     (20s, 40s, 60s, 80s) em vez de ficar fixa em 20s — dá mais chance do
     limite por minuto da OpenRouter resetar antes da tentativa seguinte.
  2. Rede de segurança: comparamos as páginas que o app.py já confirmou (por
     busca de texto simples, sem IA) contra as páginas que a IA efetivamente
     extraiu. Qualquer página que mencione "FUNED"/"Fundação Ezequiel Dias"
     mas não tenha virado publicação agora aparece em um aviso explícito no
     e-mail, em vez de simplesmente desaparecer sem ninguém notar.
"""

import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

RENDER_BASE_URL = os.environ["RENDER_BASE_URL"].rstrip("/")
SERVICE_API_KEY = os.environ["SERVICE_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
DESTINATARIOS = [e.strip() for e in os.environ["DESTINATARIOS"].split(",") if e.strip()]

# Modelo gratuito do OpenRouter (sem custo, $0/token). Se este deixar de existir,
# veja outros modelos ":free" em https://openrouter.ai/models?max_price=0
#
# Trocado de "nvidia/nemotron-3.5-lightning:free" pra este porque aquele é um
# modelo de "raciocínio" que insiste em escrever um textão de pensamento em
# voz alta (tipo "Here's a thinking process...") junto da resposta, estourando
# o limite de tokens antes de chegar no JSON de verdade. Este aqui é um
# modelo de chat/instrução direta (sem essa etapa de raciocínio longo) com
# contexto grande, o que deve dar resultado bem mais consistente.
MODELO_LLM = "poolside/laguna-s-2.1:free"

# Texto de busca: mesmo padrão default do serviço. Pode sobrescrever com a env
# var TEXTO_PESQUISA se preferir usar "Funed" como no fluxo n8n antigo.
TEXTO_BUSCA = os.environ.get("TEXTO_PESQUISA", "Fundação Ezequiel Dias")

DATA_HOJE_ISO = date.today().isoformat()          # formato exigido pela API: YYYY-MM-DD
DATA_HOJE_BR = date.today().strftime("%d/%m/%Y")  # formato usado no e-mail

MAX_TENTATIVAS = 5
ESPERA_ENTRE_TENTATIVAS_MS = 5000
# Aumentado de 3 para 5: em 25/08/2026 vimos o serviço no Render cair
# ("Instance failed") e se recuperar sozinho pouco depois — instâncias
# gratuitas do Render podem reiniciar por falta de memória (o Playwright/
# navegador consome bastante RAM) e demorar até recuperar de vez. Mais
# tentativas com espera crescente dão tempo pro serviço se estabilizar
# antes de desistirmos de vez.
# (Esse MAX_TENTATIVAS é usado só no loop de extração por página com a
# IA, mais abaixo — chamadas rápidas, então 5 tentativas curtas fazem
# sentido ali.)

# --------------------------------------------------------------------------
# Chamada ao /monitoramento do Render (busca das páginas via Playwright):
# usa constantes PRÓPRIAS, diferentes do MAX_TENTATIVAS acima, porque essa
# chamada é fundamentalmente diferente das outras: o app.py do Render JÁ
# tenta de novo sozinho, internamente, até 4 vezes por chamada (com pausas
# de 5s/10s/20s entre elas) antes de responder — e cada uma dessas
# tentativas internas pode legitimamente levar minutos (abrir o portal,
# esperar a pesquisa, buscar a edição, baixar o PDF). Em 28/08/2026 vimos
# isso na prática: com um timeout de 180s por chamada e 5 tentativas
# externas aqui, todas as tentativas estouravam o tempo (Read timed out)
# mesmo com o serviço funcionando normalmente — o portal só estava mais
# lento naquele dia, e a soma das tentativas internas do Render facilmente
# passa de 180s. Resultado: menos tentativas por fora, mas cada uma com
# tempo de sobra pra deixar o Render terminar sozinho o que já está
# tentando fazer, em vez de cortar a chamada no meio e tentar de novo do
# zero (o que só reinicia o relógio sem resolver nada).
MAX_TENTATIVAS_RENDER = 2
TIMEOUT_RENDER_S = 600  # 10 min: cobre com folga o pior caso das 4 tentativas internas do app.py


# --------------------------------------------------------------------------
# 1. Buscar as páginas do Diário no serviço Render (Python + Playwright)
# --------------------------------------------------------------------------

# Tempo máximo esperando o serviço free do Render "acordar" (instância dorme
# após 15 min sem tráfego; o Render avisa que pode levar 50s ou mais).
ACORDAR_TIMEOUT_S = 120
ACORDAR_INTERVALO_S = 5


def aguardar_servico_acordar():
    """Faz ping em /health até o serviço responder, ou desiste após ACORDAR_TIMEOUT_S."""
    url = f"{RENDER_BASE_URL}/health"
    inicio = time.monotonic()
    tentativa = 0
    while time.monotonic() - inicio < ACORDAR_TIMEOUT_S:
        tentativa += 1
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                print(f"Serviço acordado (tentativa {tentativa}, {time.monotonic() - inicio:.0f}s).")
                return True
        except Exception as e:  # noqa: BLE001
            print(f"[wake-up tentativa {tentativa}] ainda não respondeu: {e}", file=sys.stderr)
        time.sleep(ACORDAR_INTERVALO_S)

    print("Serviço não confirmou estar acordado a tempo; seguindo mesmo assim.", file=sys.stderr)
    return False


def buscar_paginas_diario():
    """Chama POST /monitoramento no serviço funed-diario-service (Render)."""
    aguardar_servico_acordar()

    url = f"{RENDER_BASE_URL}/monitoramento"
    payload = {
        "data_publicacao": DATA_HOJE_ISO,
        "texto_pesquisa": TEXTO_BUSCA,
    }
    headers = {"X-API-Key": SERVICE_API_KEY}

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS_RENDER + 1):
        try:
            # timeout alto: o app.py já tenta de novo internamente (até 4x),
            # então essa chamada por fora precisa de tempo de sobra pra
            # deixar ele terminar sozinho (ver comentário acima, perto da
            # definição de MAX_TENTATIVAS_RENDER e TIMEOUT_RENDER_S).
            resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_RENDER_S)

            # O serviço responde 404 (com um corpo JSON próprio, não uma
            # página de erro genérica) quando ele rodou normalmente mas
            # simplesmente não existe edição do Diário — ou não há nenhuma
            # publicação com o termo buscado — na data pedida (ex: quando o
            # workflow é disparado manualmente numa segunda-feira, dia em que
            # o Diário Oficial de MG não é publicado). Isso NÃO é uma falha:
            # é um resultado válido de "nada para relatar hoje", então não
            # deve ser tratado como erro nem tentar de novo — basta seguir
            # em frente sem páginas, e o e-mail final vai informar
            # corretamente que não há atos da FUNED na data.
            if resp.status_code == 404 and resp.headers.get("content-type", "").startswith("application/json"):
                try:
                    detalhe = resp.json()
                except ValueError:
                    detalhe = {}
                print(
                    f"Nenhuma publicação encontrada para {DATA_HOJE_ISO} "
                    f"(resposta do serviço: {detalhe}). Seguindo sem páginas.",
                    file=sys.stderr,
                )
                return []

            resp.raise_for_status()
            corpo = resp.json()
            dados = corpo.get("dados", {})
            publicacoes_brutas = dados.get("publicacoes", [])

            paginas_normalizadas = [
                {
                    "numero": p.get("pagina"),
                    "texto": p.get("textoPagina") or "",
                    # CORREÇÃO 18/09/2026: final da página anterior, usado
                    # como contexto pra resolver tabelas de licença
                    # (DEFERIDA/INDEFERIDA) que começam na página de trás.
                    "texto_pagina_anterior": p.get("textoPaginaAnterior") or "",
                    # CORREÇÃO 02/10/2026: categoria DEFERIDA/INDEFERIDA já
                    # resolvida de forma determinística pelo app.py (ele
                    # rastreia o cabeçalho vigente por todo o documento, não
                    # só na página anterior — teste real mostrou que o
                    # cabeçalho válido às vezes está 2+ páginas atrás, sem
                    # nenhum cabeçalho na página imediatamente anterior).
                    # Quando presente, isso é a fonte da verdade e substitui
                    # qualquer tentativa da IA de adivinhar pelo texto.
                    "categoria_licenca_vigente": p.get("categoriaLicencaVigente"),
                    # CORREÇÃO 02/10/2026 (2ª): contagem determinística de
                    # quantas menções distintas a "FUNED"/"Fundação Ezequiel
                    # Dias" existem nesta página — usada pra avisar a IA
                    # quantas ela precisa justificar, e pra conferir depois
                    # se cada uma virou publicação (ver _verificar_cobertura_mencoes).
                    "total_mencoes_funed": p.get("totalMencoesFuned") or 0,
                    "trechos_mencoes_funed": p.get("trechosMencoesFuned") or [],
                }
                for p in publicacoes_brutas
            ]
            return paginas_normalizadas
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            print(f"[tentativa {tentativa}] erro ao chamar Render: {e}", file=sys.stderr)
            # Mostra o corpo do erro (o app.py do Render manda um "detalhe"
            # explicando qual etapa da automação falhou — ex: portal fora
            # do ar, botão/campo não encontrado, PDF recusado). Sem isso só
            # vemos "502 Bad Gateway" genérico e não sabemos onde travou.
            resp_erro = getattr(e, "response", None)
            if resp_erro is not None:
                try:
                    print(f"[debug] corpo do erro: {resp_erro.json()}", file=sys.stderr)
                except ValueError:
                    print(f"[debug] corpo do erro (texto): {resp_erro.text[:1000]!r}", file=sys.stderr)
            if tentativa < MAX_TENTATIVAS_RENDER:
                # espera mais a cada tentativa (15s, 30s, 45s...) — dá mais
                # tempo pra instância terminar de acordar entre as tentativas.
                time.sleep((ESPERA_ENTRE_TENTATIVAS_MS / 1000) * tentativa * 3)

    raise RuntimeError(f"Falha ao buscar páginas do Diário após {MAX_TENTATIVAS_RENDER} tentativas: {ultimo_erro}")


# --------------------------------------------------------------------------
# 2. Extrair/estruturar as publicações com um modelo gratuito do OpenRouter
# --------------------------------------------------------------------------

# IMPORTANTE: este prompt processa UMA página por vez (ver extrair_publicacoes
# mais abaixo). Isso é proposital — quando mandávamos várias páginas juntas
# numa única chamada, o modelo gratuito às vezes "embaralhava" o número da
# página entre publicações (ex: pegava o conteúdo real da página 27 e
# etiquetava como "página 23") e chegou a esquecer de extrair o conteúdo de
# uma página inteira. Processando uma página por vez, o número da página nunca
# precisa ser "adivinhado" pelo modelo — o código já sabe qual é e o preenche
# depois, então esse tipo de erro fica impossível.
PROMPT_SISTEMA = """detailed thinking off

Você é um assistente que analisa UMA página do Diário Oficial de Minas Gerais \
(Diário do Executivo) em busca de publicações relacionadas à Fundação Ezequiel \
Dias (FUNED). Você recebe o texto de uma única página e deve devolver APENAS um \
JSON válido (sem markdown, sem texto fora do JSON, sem explicar seu raciocínio, \
sem escrever "thinking process" ou qualquer texto antes/depois do JSON), no \
seguinte formato exato:

{
  "publicacoes": [
    {
      "categoria": "ato próprio da FUNED" | "menção indireta",
      "tipo_do_ato": "string curta descrevendo o tipo do ato",
      "data_periodo": "data(s) ou período do ato, como aparece no texto",
      "pessoas": [
        {"nome": "NOME COMPLETO EM MAIÚSCULAS", "masp": "número do MASP", "adm": "Adm. N ou null"}
      ],
      "conteudo_oficial": "trecho oficial extraído literalmente do texto da página, sem corrigir acentuação nem reescrever",
      "resumo_objetivo": "1-3 frases em linguagem simples explicando o que foi decidido/autorizado"
    }
  ]
}

Regras importantes:
- Considere APENAS publicações relacionadas à FUNED (Fundação Ezequiel Dias), diretas ou indiretas, que estejam NESTA página.
- IMPORTANTE: releia a página INTEIRA antes de responder e procure TODAS as
  ocorrências das palavras "Funed" ou "Fundação Ezequiel Dias" no texto — elas
  podem aparecer mais de uma vez, em tabelas diferentes ou em partes distantes
  da página (ex: uma tabela de licenças DEFERIDAS em um trecho e outra de
  licenças INDEFERIDAS em outro trecho da mesma página). NÃO pare na primeira
  ocorrência encontrada: cada ocorrência distinta deve virar uma publicação
  (ou ser agrupada com outras do mesmo tipo, conforme a regra abaixo).
- ATENÇÃO: uma mesma página do Diário costuma trazer atos de VÁRIOS órgãos diferentes do
  governo de Minas Gerais (Secretaria de Educação, Secretaria de Saúde, IPSEMG, FHEMIG,
  FUNED, etc.), muitas vezes em tabelas ou listas genéricas compartilhadas por vários
  órgãos ao mesmo tempo (ex: uma lista única de "licenças para tratamento de saúde
  indeferidas" que junta servidores de vários órgãos diferentes). A palavra "FUNED" ou
  "Fundação Ezequiel Dias" aparecer EM ALGUM LUGAR da página NÃO significa que a página
  inteira (ou a tabela inteira) seja da FUNED. Antes de incluir qualquer item, confirme
  que aquele item específico está de fato atribuído à FUNED — porque está sob um
  cabeçalho/seção com o nome "Fundação Ezequiel Dias" ou "FUNED", ou porque o próprio
  texto do item cita a FUNED como o órgão responsável por aquele ato. Se uma tabela tiver
  uma coluna "Órgão" (ou equivalente) e ela não disser FUNED para aquela linha, NÃO inclua
  essa linha, mesmo que a palavra FUNED apareça em outro lugar da mesma página.
- Se restar dúvida real se um item é ou não da FUNED (ambiguidade genuína, não apenas
  "a palavra apareceu na página"), prefira NÃO incluir a ficar incluindo itens errados.
- Se a página tiver uma tabela repetitiva com muitos registros do mesmo tipo da FUNED
  (ex: vários servidores da FUNED com licença indeferida na mesma seção),
  AGRUPE tudo em UMA única publicação (mesma "categoria"/"tipo_do_ato"), listando todas
  as pessoas em "pessoas" — não crie uma publicação separada pra cada pessoa. Isso evita
  gastar espaço de resposta com dezenas de itens repetidos e ajuda a garantir espaço pra
  outros atos (como portarias completas) que também estejam na mesma página. Mas se
  houver tabelas SEPARADAS de tipos diferentes (ex: uma de licenças DEFERIDAS e outra de
  licenças INDEFERIDAS), cada uma é uma publicação diferente — não junte as duas.
- "conteudo_oficial" deve ser um recorte fiel do texto original da página (não invente, não resuma aqui).
- IMPORTANTE: o campo "pessoas" de uma publicação deve conter SOMENTE pessoas que também
  apareçam no texto de "conteudo_oficial" DESSA MESMA publicação. Nunca copie nomes de uma
  tabela maior (ex: de outros órgãos, ou de antes de você filtrar quem é da FUNED) para
  dentro de "pessoas" se esses nomes não estiverem no trecho de "conteudo_oficial" que você
  realmente extraiu. As duas listas têm que bater.
- "resumo_objetivo" é o único campo que deve estar em linguagem simplificada.
- Se uma publicação citar múltiplas pessoas, liste todas em "pessoas".
- Se não houver NENHUMA publicação relacionada à FUNED nesta página, devolva:
  {"publicacoes": []}
- Nunca invente MASP ou datas que não estejam no texto fornecido.
- ATENÇÃO ESPECIAL às tabelas de "Licenças para tratamento de saúde DEFERIDAS" ou
  "INDEFERIDAS": essas tabelas às vezes COMEÇAM em uma página anterior (que você não
  está vendo) e simplesmente CONTINUAM no topo desta página, sem repetir o cabeçalho
  "DEFERIDAS"/"INDEFERIDAS" no início. Uma mesma página pode conter DUAS OU MAIS dessas
  tabelas em sequência, cada uma com seu próprio cabeçalho aparecendo ANTES dela no texto.
  Para decidir se um item é DEFERIDA ou INDEFERIDA, use APENAS o cabeçalho "Licença(s)
  para tratamento de saúde DEFERIDA(S)" ou "INDEFERIDA(S)" que aparece IMEDIATAMENTE
  ANTES daquele item na ORDEM do texto (de cima para baixo). NUNCA use um cabeçalho que
  aparece DEPOIS do item no texto — isso pertence a uma tabela diferente e ainda não
  começou. Se um item da FUNED aparecer LOGO NO INÍCIO do texto da página, ANTES de
  qualquer cabeçalho "DEFERIDA(S)"/"INDEFERIDA(S)" aparecer nesta página, significa que a
  tabela começou na página anterior e você NÃO tem como saber com certeza se é DEFERIDA ou
  INDEFERIDA só com o texto desta página.
- Se um bloco "--- CATEGORIA DA TABELA DE LICENÇA (FATO CONFIRMADO, NÃO TENTE ADIVINHAR) ---"
  for fornecido, ele já diz a categoria correta (DEFERIDA ou INDEFERIDA) pra qualquer item
  da FUNED que apareça nesta página antes de um cabeçalho próprio — isso foi calculado de
  forma determinística a partir do documento inteiro (não é um palpite). Use exatamente essa
  categoria em "tipo_do_ato" e NUNCA escreva "categoria não visível" quando esse bloco
  estiver presente.
- Se esse bloco de categoria confirmada NÃO for fornecido, e um bloco "--- CONTEXTO: final
  da página anterior ---" estiver presente, use-o para tentar localizar o cabeçalho
  "DEFERIDA(S)"/"INDEFERIDA(S)" que se aplica ao item do início desta página. Esse bloco de
  contexto serve SOMENTE pra essa desambiguação: nunca crie uma publicação nova com base em
  conteúdo que apareça só nesse bloco — ele é de outra página, não desta.
- Só na ausência de QUALQUER uma dessas duas fontes (categoria confirmada ou contexto da
  página anterior), e se mesmo assim não for possível determinar a categoria com segurança,
  use "tipo_do_ato": "Licença para tratamento de saúde (categoria DEFERIDA/INDEFERIDA não
  visível nesta página — tabela iniciada na página anterior)".
"""


def montar_prompt_usuario_pagina(pagina):
    # CORREÇÃO 02/10/2026: o app.py agora resolve a categoria DEFERIDA/
    # INDEFERIDA de forma determinística (rastreando o cabeçalho vigente por
    # todo o documento) e manda o resultado em "categoria_licenca_vigente".
    # Quando presente, isso é mandado como um FATO confirmado — a IA não
    # precisa (e não deve) tentar adivinhar pelo texto.
    categoria_confirmada = pagina.get("categoria_licenca_vigente")
    bloco_categoria = ""
    if categoria_confirmada:
        bloco_categoria = (
            "\n\n--- CATEGORIA DA TABELA DE LICENÇA (FATO CONFIRMADO, NÃO "
            "TENTE ADIVINHAR) ---\n"
            f"Se esta página contiver uma tabela de \"Licença(s) para "
            f"tratamento de saúde\" sem cabeçalho visível no início (ou seja, "
            f"um item da FUNED aparece ANTES de qualquer cabeçalho "
            f"\"DEFERIDA(S)\"/\"INDEFERIDA(S)\" nesta página), a categoria "
            f"correta e já confirmada é: {categoria_confirmada}. Use "
            f"\"tipo_do_ato\": \"Licença para tratamento de saúde "
            f"{categoria_confirmada}\" nesse caso — NÃO escreva mais "
            f"\"categoria não visível\"."
        )

    # CORREÇÃO 18/09/2026: anexa o final da página anterior como um bloco de
    # CONTEXTO separado — mantido como apoio adicional (ex.: pra outros
    # cabeçalhos de seção que não sejam DEFERIDA/INDEFERIDA), mas a
    # categoria DEFERIDA/INDEFERIDA em si já vem resolvida acima.
    texto_anterior = pagina.get("texto_pagina_anterior") or ""
    bloco_contexto = ""
    if texto_anterior:
        final_pagina_anterior = texto_anterior[-2000:]
        bloco_contexto = (
            "\n\n--- CONTEXTO: final da página anterior (apoio geral; NUNCA "
            "crie uma publicação com base em conteúdo que apareça só neste "
            "bloco de contexto — ele não pertence a esta página) ---\n"
            f"{final_pagina_anterior}"
        )

    # CORREÇÃO 02/10/2026 (2ª): página 32 da edição de 02/10/2026 tinha 5
    # atos distintos da FUNED e o modelo só extraiu 1 — a página era grande
    # (dezenas de milhares de caracteres) e misturava publicações de vários
    # outros órgãos. Esse bloco dá um número EXATO (contado por código, não
    # pela IA) de quantas menções distintas existem, como checklist.
    total_mencoes = pagina.get("total_mencoes_funed") or 0
    bloco_checklist = ""
    if total_mencoes >= 2:
        bloco_checklist = (
            "\n\n--- CONTAGEM AUTOMÁTICA (checklist obrigatório) ---\n"
            f"Esta página contém {total_mencoes} menções DISTINTAS a "
            f"\"FUNED\"/\"Fundação Ezequiel Dias\" (contadas por código, não "
            f"é uma estimativa). Isso NÃO significa que existam {total_mencoes} "
            f"publicações — várias menções podem pertencer à mesma publicação "
            f"(ex: o nome do órgão aparece no título E de novo no corpo do "
            f"mesmo ato), ou podem ser de uma tabela repetitiva que deve ser "
            f"agrupada numa única publicação (ver regra de agrupamento "
            f"acima). Mas releia a página e confirme que cada uma das "
            f"{total_mencoes} menções foi considerada — incluída em alguma "
            f"publicação, agrupada em uma tabela, ou avaliada e descartada "
            f"por não ser da FUNED de fato. NÃO pare depois de achar só a "
            f"primeira ou segunda menção."
        )

    return (
        f"--- PÁGINA {pagina['numero']} ---\n{pagina['texto']}"
        f"{bloco_categoria}{bloco_contexto}{bloco_checklist}"
    )


def _fim_do_objeto(texto, inicio):
    """A partir de um índice onde texto[inicio] == '{', devolve o índice do
    '}' que fecha esse mesmo objeto (respeitando strings/escapes), ou None
    se as chaves nunca fecharem."""
    profundidade = 0
    dentro_de_string = False
    escapando = False
    for i in range(inicio, len(texto)):
        ch = texto[i]
        if dentro_de_string:
            if escapando:
                escapando = False
            elif ch == "\\":
                escapando = True
            elif ch == '"':
                dentro_de_string = False
            continue
        if ch == '"':
            dentro_de_string = True
        elif ch == "{":
            profundidade += 1
        elif ch == "}":
            profundidade -= 1
            if profundidade == 0:
                return i
    return None


def _reparar_json_truncado(texto):
    """Tenta salvar o que dá de uma resposta cortada no meio (o modelo parou
    de escrever antes de fechar o JSON, geralmente por estourar o limite de
    tokens da resposta).

    Caminha pelo texto controlando quais chaves/colchetes estão abertos. Toda
    vez que um "}" fecha um item e o nível logo acima é uma lista (ex: acabou
    de fechar um objeto dentro de "publicacoes": [...]), isso é um "ponto
    seguro" pra cortar — o item anterior está completo. Guardamos o último
    ponto seguro e, no final, cortamos o texto ali e fechamos à mão o que
    ainda estava aberto (array/objeto), pra virar um JSON válido só com as
    publicações que já tinham vindo por inteiro antes do corte.
    """
    pilha = []
    dentro_de_string = False
    escapando = False
    ultimo_corte_seguro = None
    pilha_no_corte = None
    for i, ch in enumerate(texto):
        if dentro_de_string:
            if escapando:
                escapando = False
            elif ch == "\\":
                escapando = True
            elif ch == '"':
                dentro_de_string = False
            continue
        if ch == '"':
            dentro_de_string = True
        elif ch in "{[":
            pilha.append(ch)
        elif ch in "}]":
            if pilha:
                pilha.pop()
            if pilha and pilha[-1] == "[":
                ultimo_corte_seguro = i + 1
                # guarda uma cópia da pilha NESSE momento — o que vier
                # depois desse ponto no texto vai ser descartado, então as
                # chaves que abrirem depois não contam pra fechar no final.
                pilha_no_corte = list(pilha)

    if ultimo_corte_seguro is None or not pilha_no_corte:
        return None

    fechamento = "".join("]" if c == "[" else "}" for c in reversed(pilha_no_corte))
    candidato = texto[:ultimo_corte_seguro] + fechamento
    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        return None


def _extrair_json(texto_resposta):
    """Extrai o objeto JSON da resposta do modelo.

    Modelos gratuitos às vezes:
      - envolvem o JSON em ```json ... ```;
      - escrevem todo um "raciocínio" em texto livre antes da resposta, e
        esse texto pode conter chaves { } soltas (ex: um placeholder tipo
        "{lista de nomes}") que não são JSON de verdade;
      - cortam a resposta no meio por falta de espaço, antes de chegar no
        JSON de verdade.

    Por isso: em vez de simplesmente pegar da primeira "{" até a última "}",
    encontramos TODOS os blocos com chaves balanceadas no texto e escolhemos
    o que (a) é JSON válido, (b) tem a cara do formato pedido (contém
    "publicacoes" ou "paginas_com_atos") e (c), se houver mais de um assim,
    o que tiver mais publicações — pra não cair num rascunho vazio que o
    modelo tenha escrito antes da resposta de verdade. Se nada disso achar
    nada bom (ex: a resposta foi cortada no meio), tenta reparar o JSON
    truncado pra pelo menos salvar as publicações que já vieram completas.
    """
    texto = texto_resposta.strip()

    # remove bloco de código markdown (```json ... ``` ou ``` ... ```), se houver
    texto = re.sub(r"^```(?:json)?\s*", "", texto)
    texto = re.sub(r"\s*```\s*$", "", texto)
    texto = texto.strip()

    candidatos = []
    i = 0
    while i < len(texto):
        if texto[i] == "{":
            fim = _fim_do_objeto(texto, i)
            if fim is not None:
                candidatos.append(texto[i:fim + 1])
                i = fim + 1
                continue
        i += 1

    melhor = None
    primeiro_valido = None
    for bloco in candidatos:
        try:
            obj = json.loads(bloco)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if primeiro_valido is None:
            primeiro_valido = obj
        if "publicacoes" in obj or "paginas_com_atos" in obj:
            if melhor is None or len(obj.get("publicacoes", [])) > len(melhor.get("publicacoes", [])):
                melhor = obj

    if melhor is not None and melhor.get("publicacoes"):
        return melhor

    reparado = _reparar_json_truncado(texto)
    if reparado is not None and isinstance(reparado, dict) and reparado.get("publicacoes"):
        return reparado

    if melhor is not None:
        return melhor
    if primeiro_valido is not None:
        return primeiro_valido

    if not candidatos:
        raise ValueError(f"Resposta do modelo não contém JSON reconhecível: {texto[:300]}")
    raise ValueError(f"Nenhum bloco JSON válido (com o formato esperado) na resposta do modelo: {texto[:300]}")


def _normalizar(texto):
    """Remove acentos e baixa a caixa, pra comparação de texto ser tolerante a
    pequenas diferenças de acentuação/maiúsculas entre 'pessoas' e 'conteudo_oficial'."""
    if not texto:
        return ""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def _pessoa_aparece_no_conteudo(pessoa, conteudo_normalizado, conteudo_digitos):
    """Confere se a pessoa (por MASP ou por nome) realmente aparece no trecho de
    'conteudo_oficial' dessa mesma publicação."""
    masp_digitos = re.sub(r"\D", "", str(pessoa.get("masp") or ""))
    if masp_digitos and masp_digitos in conteudo_digitos:
        return True

    nome = _normalizar(pessoa.get("nome") or "")
    if not nome:
        return False
    if nome in conteudo_normalizado:
        return True

    # Às vezes o "conteudo_oficial" tem o nome com espaçamento/quebra de linha
    # diferente do campo "pessoas". Aceita também se o PRIMEIRO nome e o
    # ÚLTIMO sobrenome baterem os dois — reduz bastante falso positivo de
    # nomes "roubados" de outra tabela/órgão, que dificilmente vão bater os
    # dois pedaços por coincidência.
    partes = nome.split()
    if len(partes) >= 2:
        primeiro_nome, sobrenome = partes[0], partes[-1]
        if len(sobrenome) >= 4 and sobrenome in conteudo_normalizado and primeiro_nome in conteudo_normalizado:
            return True

    return False


def _filtrar_pessoas_consistentes(dados):
    """Pós-processamento (na unha, sem depender só da instrução no prompt) pra
    corrigir um problema recorrente do modelo gratuito: o campo 'pessoas' de uma
    publicação às vezes vem com dezenas de nomes copiados de uma tabela
    maior/compartilhada entre vários órgãos, mesmo esses nomes não aparecendo no
    trecho de 'conteudo_oficial' que o modelo realmente extraiu como sendo da
    FUNED. Pedir isso só via prompt não foi suficiente em testes reais (o mesmo
    problema se repetiu em rodadas seguidas mesmo com a instrução no prompt), então
    aqui filtramos com certeza: só mantém em 'pessoas' quem realmente aparece (por
    nome ou MASP) no 'conteudo_oficial' da mesma publicação."""
    for pub in dados.get("publicacoes", []):
        pessoas = pub.get("pessoas") or []
        if not pessoas:
            continue
        conteudo_normalizado = _normalizar(pub.get("conteudo_oficial") or "")
        if not conteudo_normalizado:
            continue
        conteudo_digitos = re.sub(r"\D", "", conteudo_normalizado)
        pessoas_filtradas = [
            p for p in pessoas if _pessoa_aparece_no_conteudo(p, conteudo_normalizado, conteudo_digitos)
        ]
        # Se o filtro zerasse TODAS as pessoas, é mais provável que o texto de
        # "conteudo_oficial" esteja num formato inesperado do que todas as
        # pessoas estarem erradas — nesse caso, mantém a lista original pra não
        # perder informação real por causa de um falso negativo do filtro.
        if pessoas_filtradas:
            removidas = len(pessoas) - len(pessoas_filtradas)
            if removidas:
                print(
                    f"[filtro pessoas] página {pub.get('pagina')}: removida(s) {removidas} "
                    f"pessoa(s) que não aparecia(m) no conteúdo oficial dessa publicação.",
                    file=sys.stderr,
                )
            pub["pessoas"] = pessoas_filtradas
    return dados


def _chamar_llm_para_pagina(pagina):
    """Faz a chamada à OpenRouter para o texto de UMA única página e devolve o
    JSON já extraído (dict com "publicacoes"). Repete em caso de erro."""
    corpo = {
        "model": MODELO_LLM,
        "messages": [
            {"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": montar_prompt_usuario_pagina(pagina)},
        ],
        "temperature": 0.1,
        # Como agora é só uma página por chamada, a resposta tende a ser bem
        # menor que antes — mas deixamos uma folga generosa pra páginas com
        # tabelas grandes da FUNED (esse modelo aceita até 32768).
        "max_tokens": 16000,
        "response_format": {"type": "json_object"},
        # Caso o modelo escolhido tenha uma etapa de "raciocínio" opcional,
        # isso pede pra ele não gastar tokens de resposta com isso. Modelos
        # sem essa capacidade simplesmente ignoram esse parâmetro.
        "reasoning": {"enabled": False},
    }

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=corpo,
                timeout=180,
            )
            resp.raise_for_status()
            corpo_resposta = resp.json()
            if "choices" not in corpo_resposta:
                # A OpenRouter respondeu 200 OK mas sem o formato esperado
                # (ex: bloqueio de conteúdo, erro do provedor). Mostra o
                # corpo inteiro no log pra dar pra entender o motivo.
                raise ValueError(f"resposta da OpenRouter sem 'choices': {json.dumps(corpo_resposta)[:1000]}")
            conteudo = corpo_resposta["choices"][0]["message"]["content"]
            print(
                f"  [página {pagina['numero']} / tentativa {tentativa}] resposta recebida "
                f"({len(conteudo)} caractere(s)).",
                file=sys.stderr,
            )
            try:
                resultado = _extrair_json(conteudo)
                if not resultado.get("publicacoes"):
                    # O JSON veio válido, mas "vazio" — registra a resposta
                    # bruta do modelo mesmo sem erro, pra dar pra conferir
                    # depois se ele realmente não achou nada ou se ignorou
                    # conteúdo que devia ter pego.
                    print(
                        f"  [página {pagina['numero']} / tentativa {tentativa}] modelo devolveu JSON "
                        f"válido mas SEM publicações (resposta bruta): {conteudo[:2000]!r}",
                        file=sys.stderr,
                    )
                return resultado
            except Exception as erro_parse:  # noqa: BLE001
                # Mostra a resposta bruta do modelo no log, pra dar pra ver
                # exatamente o que veio quando o parse falha.
                print(
                    f"  [página {pagina['numero']} / tentativa {tentativa}] resposta bruta do modelo "
                    f"(não foi possível extrair JSON): {conteudo[:2000]!r}",
                    file=sys.stderr,
                )
                raise erro_parse
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            print(f"  [página {pagina['numero']} / tentativa {tentativa}] erro ao chamar OpenRouter: {e}", file=sys.stderr)
            if tentativa < MAX_TENTATIVAS:
                # CORREÇÃO 18/09/2026: "429 muitas requisições" agora espera
                # progressivamente mais a cada tentativa (20s, 40s, 60s, 80s)
                # em vez de sempre 20s fixos. Foi exatamente essa espera fixa
                # que não deu tempo do limite por minuto da OpenRouter resetar
                # e fez a página 19 (Portaria FUNED nº 75/2026) tomar 429 em
                # TODAS as 5 tentativas seguidas e sumir do e-mail daquele dia.
                espera = (20 * tentativa) if "429" in str(e) else (ESPERA_ENTRE_TENTATIVAS_MS / 1000)
                time.sleep(espera)

    raise RuntimeError(f"Falha ao extrair publicações da página {pagina['numero']} após {MAX_TENTATIVAS} tentativas: {ultimo_erro}")



# CORREÇÃO 18/09/2026 (2ª rodada): em 18/09/2026, depois da correção do rate
# limit, a página 19 passou a ser analisada — mas a IA atribuiu à FUNED um
# indeferimento de pensão que, pela ordem real do texto da página, pertence
# ao IPSEMG (Instituto de Previdência dos Servidores do Estado de MG), uma
# autarquia diferente que só compartilha a página com a FUNED. O trecho de
# "conteudo_oficial" dessa publicação não citava "FUNED"/"Fundação Ezequiel
# Dias" em lugar nenhum. Por isso, valida-se aqui — não só no prompt — que
# cada publicação realmente cita a FUNED dentro do próprio conteúdo oficial
# extraído; quem não citar é descartada (silenciosamente, por pedido).
TERMOS_ATRIBUICAO_FUNED = ["Fundação Ezequiel Dias", "FUNED", "Funed"]


def _publicacao_atribuida_a_funed(pub):
    """Confere se o trecho de 'conteudo_oficial' realmente cita a FUNED (por
    nome completo ou sigla) em algum lugar dele — não basta a palavra ter
    aparecido em outro ponto da página."""
    conteudo_normalizado = _normalizar(pub.get("conteudo_oficial") or "")
    if not conteudo_normalizado:
        return False
    termos = TERMOS_ATRIBUICAO_FUNED + [TEXTO_BUSCA]
    return any(_normalizar(termo) in conteudo_normalizado for termo in termos if termo)


def _verificar_cobertura_mencoes(pagina, publicacoes_validas):
    """Confere, de forma determinística, se cada menção distinta a
    'FUNED'/'Fundação Ezequiel Dias' contada pelo app.py (campo
    'trechos_mencoes_funed') está representada em algum 'conteudo_oficial'
    das publicações que a IA extraiu pra essa página. Devolve True se
    alguma menção ficou sem cobertura (sinal de que a IA pode ter deixado
    passar algum ato) — usado só como ALERTA pro e-mail, não bloqueia nada.

    CORREÇÃO 02/10/2026 (2ª): a página 32 da edição de 02/10/2026 tinha 5
    atos distintos da FUNED e a IA só extraiu 1 (provavelmente porque a
    página era enorme — ~36 mil caracteres — e misturava publicações de
    vários outros órgãos). O aviso de "verificação manual" existente só
    disparava quando a página ficava com ZERO publicações; esse caso
    passou batido porque 1 publicação foi extraída com sucesso. Essa função
    fecha essa lacuna."""
    trechos = pagina.get("trechos_mencoes_funed") or []
    if not trechos:
        return False

    conteudo_unido = _normalizar(
        " ".join(
            pub.get("conteudo_oficial") or ""
            for pub in publicacoes_validas
        )
    )
    if not conteudo_unido:
        return True

    mencoes_sem_cobertura = 0
    for trecho in trechos:
        # usa um miolo do trecho (não o trecho inteiro) pra tolerar pequenas
        # diferenças de espaçamento/quebra de linha entre o texto bruto da
        # página e o "conteudo_oficial" que a IA recortou.
        miolo = _normalizar(trecho)[10:70].strip()
        if not miolo:
            continue
        if miolo not in conteudo_unido:
            mencoes_sem_cobertura += 1

    return mencoes_sem_cobertura > 0


def _processar_uma_pagina(pagina):
    """Chama a IA para UMA página e devolve (publicacoes_validas, falhou,
    cobertura_incompleta). Função auxiliar usada tanto na primeira passada
    quanto na rodada extra de retentativas no final (ver extrair_publicacoes)."""
    try:
        resultado_pagina = _chamar_llm_para_pagina(pagina)
    except Exception as e:  # noqa: BLE001
        print(f"Falha ao analisar a página {pagina['numero']}: {e}", file=sys.stderr)
        return [], True, False

    publicacoes_pagina = resultado_pagina.get("publicacoes") or []
    if not publicacoes_pagina:
        cobertura_incompleta = bool(pagina.get("trechos_mencoes_funed"))
        return [], False, cobertura_incompleta

    # Força o número da página com o valor que a GENTE já sabe (veio do
    # serviço de raspagem), em vez de confiar no que o modelo eventualmente
    # tenha tentado inventar/repetir — é exatamente isso que evita o bug de
    # páginas trocadas entre publicações.
    for pub in publicacoes_pagina:
        pub["pagina"] = pagina["numero"]

    publicacoes_validas = [
        pub for pub in publicacoes_pagina if _publicacao_atribuida_a_funed(pub)
    ]
    descartadas = len(publicacoes_pagina) - len(publicacoes_validas)
    if descartadas:
        print(
            f"[validação de atribuição] página {pagina['numero']}: descartada(s) "
            f"{descartadas} publicação(ões) cujo conteúdo oficial não citava a FUNED "
            f"(provável atribuição incorreta a outro órgão, ex: IPSEMG/DETRAN/Hemominas).",
            file=sys.stderr,
        )

    cobertura_incompleta = _verificar_cobertura_mencoes(pagina, publicacoes_validas)
    if cobertura_incompleta:
        print(
            f"[cobertura de menções] página {pagina['numero']}: nem toda menção "
            f"distinta a FUNED/Fundação Ezequiel Dias ficou representada nas "
            f"publicações extraídas — possível ato perdido. Vai entrar no aviso "
            f"de verificação manual do e-mail.",
            file=sys.stderr,
        )

    return publicacoes_validas, False, cobertura_incompleta


def extrair_publicacoes(paginas):
    """Analisa cada página separadamente (uma chamada à IA por página) e junta
    os resultados. Ver o comentário grande acima de PROMPT_SISTEMA pra
    entender por que isso é feito página a página, e não tudo de uma vez."""
    if not paginas:
        return {
            "paginas_com_atos": [],
            "publicacoes": [],
            "paginas_com_falha": [],
            "paginas_cobertura_incompleta": [],
        }

    todas_publicacoes = []
    paginas_com_atos = []
    paginas_com_falha = []
    paginas_cobertura_incompleta = []

    for i, pagina in enumerate(paginas):
        print(f"Analisando página {pagina['numero']} com a IA ({i + 1}/{len(paginas)})...", file=sys.stderr)
        publicacoes_validas, falhou, cobertura_incompleta = _processar_uma_pagina(pagina)
        if falhou:
            paginas_com_falha.append(pagina["numero"])
        elif publicacoes_validas:
            todas_publicacoes.extend(publicacoes_validas)
            paginas_com_atos.append(pagina["numero"])
        if cobertura_incompleta:
            paginas_cobertura_incompleta.append(pagina["numero"])

        # pausa entre chamadas pra não estourar o limite de requisições por
        # minuto do plano gratuito da OpenRouter (aumentada de 3s pra 5s em
        # 18/09/2026, já que 3s não bastou pra evitar 429 seguidos).
        if i < len(paginas) - 1:
            time.sleep(5)

    # CORREÇÃO 18/09/2026 (3ª rodada): rodada extra, ao FINAL de tudo, só
    # para as páginas que falharam por completo (ex: rate limit em todas as
    # tentativas). Como o processo já gastou um tempo considerável
    # processando as outras páginas, essa espera extra dá uma chance real do
    # limite por minuto da OpenRouter ter resetado de vez — foi exatamente
    # essa falta de uma segunda chance, mais tarde, que fez a página 19
    # (Portaria FUNED nº 75/2026) sumir do e-mail de 18/09/2026.
    # CORREÇÃO 02/10/2026 (2ª): além das páginas que falharam por completo,
    # a rodada extra agora também retenta páginas com COBERTURA INCOMPLETA
    # (teve publicação extraída, mas sobrou menção à FUNED sem representação
    # — ver _verificar_cobertura_mencoes). Nesses casos, NUNCA substitui o
    # que já foi encontrado na 1ª passada: só ACRESCENTA publicações novas
    # que a retentativa conseguir achar (comparando por conteúdo oficial,
    # pra não duplicar a mesma publicação duas vezes).
    paginas_para_retentar = list(
        dict.fromkeys(paginas_com_falha + paginas_cobertura_incompleta)
    )
    if paginas_para_retentar:
        print(
            f"Rodada extra ao final para {len(paginas_para_retentar)} página(s) que "
            f"falharam ou ficaram com cobertura incompleta: {paginas_para_retentar}. "
            f"Aguardando 45s antes de retentar...",
            file=sys.stderr,
        )
        time.sleep(45)
        paginas_por_numero = {p["numero"]: p for p in paginas}
        paginas_com_falha_definitiva = []
        paginas_cobertura_incompleta_definitiva = []
        for numero in paginas_para_retentar:
            pagina = paginas_por_numero[numero]
            print(f"Retentando página {numero} (rodada extra)...", file=sys.stderr)
            publicacoes_validas, falhou, cobertura_incompleta = _processar_uma_pagina(pagina)
            if falhou:
                paginas_com_falha_definitiva.append(numero)
            elif publicacoes_validas:
                ja_capturadas = _normalizar(
                    " ".join(
                        pub.get("conteudo_oficial") or ""
                        for pub in todas_publicacoes
                        if pub.get("pagina") == numero
                    )
                )
                novas = [
                    pub for pub in publicacoes_validas
                    if _normalizar(pub.get("conteudo_oficial") or "")[:80]
                    not in ja_capturadas
                ]
                if novas:
                    todas_publicacoes.extend(novas)
                    print(
                        f"[rodada extra] página {numero}: {len(novas)} publicação(ões) "
                        f"nova(s) encontrada(s) na retentativa.",
                        file=sys.stderr,
                    )
                if numero not in paginas_com_atos:
                    paginas_com_atos.append(numero)
                # reconfere cobertura com o total acumulado (1ª passada + retentativa)
                publicacoes_da_pagina = [
                    pub for pub in todas_publicacoes if pub.get("pagina") == numero
                ]
                if _verificar_cobertura_mencoes(pagina, publicacoes_da_pagina):
                    paginas_cobertura_incompleta_definitiva.append(numero)
            elif numero in paginas_cobertura_incompleta:
                # não achou nada de novo na retentativa — mantém o aviso.
                paginas_cobertura_incompleta_definitiva.append(numero)
            time.sleep(8)
        paginas_com_falha = paginas_com_falha_definitiva
        paginas_cobertura_incompleta = paginas_cobertura_incompleta_definitiva
        if paginas_com_falha or paginas_cobertura_incompleta:
            print(
                f"Após a rodada extra — falharam por completo: {paginas_com_falha}; "
                f"cobertura ainda incompleta: {paginas_cobertura_incompleta}. "
                f"Vão aparecer no aviso de verificação manual do e-mail.",
                file=sys.stderr,
            )

    resultado = {
        "paginas_com_atos": paginas_com_atos,
        "publicacoes": todas_publicacoes,
        "paginas_com_falha": paginas_com_falha,
        "paginas_cobertura_incompleta": paginas_cobertura_incompleta,
    }
    return _filtrar_pessoas_consistentes(resultado)


# --------------------------------------------------------------------------
# 3. Renderizar o e-mail no mesmo layout visual do fluxo n8n atual
# --------------------------------------------------------------------------

def _card_publicacao(idx, pub):
    pessoas_html = "<br>".join(
        f"{p['nome']} — MASP {p['masp']}" + (f" — {p['adm']}" if p.get("adm") else "")
        for p in pub.get("pessoas", [])
    ) or "Não informado"

    return f"""
    <div style="border:1px solid #e2e8f0; border-left:4px solid #1e3a5f; border-radius:6px; padding:16px; margin-bottom:16px; background:#ffffff;">
      <h3 style="margin:0 0 12px 0; color:#1e3a5f; font-size:17px;">
        <span style="display:inline-block; background:#1e3a5f; color:#c9a24b; border-radius:50%; width:22px; height:22px; text-align:center; line-height:22px; font-size:12px; font-weight:bold; margin-right:6px;">{idx}</span>
        Página {pub.get('pagina', '?')}
      </h3>
      <p style="margin:6px 0;"><strong>Categoria:</strong> {pub.get('categoria', 'não informado')}</p>
      <p style="margin:6px 0;"><strong>Tipo do ato:</strong> {pub.get('tipo_do_ato', 'não informado')}</p>
      <p style="margin:6px 0;"><strong>Data ou período:</strong> {pub.get('data_periodo', 'não informado')}</p>
      <p style="margin:6px 0;"><strong>Pessoa(s) relacionada(s):</strong><br>{pessoas_html}</p>
      <div style="background:#faf3e0; border:1px solid #e6d5a8; border-radius:6px; padding:12px; margin:12px 0;">
        <p style="margin:0 0 6px 0; color:#8a6d3b; font-weight:bold; font-size:12px; letter-spacing:0.5px;">CONTEÚDO OFICIAL IDENTIFICADO</p>
        <p style="margin:0; white-space:pre-wrap;">{pub.get('conteudo_oficial', '')}</p>
      </div>
      <p style="margin:6px 0;"><strong style="color:#1e3a5f;">Resumo objetivo</strong></p>
      <p style="margin:0;">{pub.get('resumo_objetivo', '')}</p>
    </div>
    """


def renderizar_email_html(dados):
    paginas_com_atos = dados.get("paginas_com_atos", [])
    publicacoes = dados.get("publicacoes", [])
    paginas_sem_ato_extraido = dados.get("paginas_sem_ato_extraido", [])

    if not publicacoes:
        aviso_sem_resultado = """
        <div style="background:#faf6ec; border-left:4px solid #c9a24b; border-radius:6px; padding:16px;">
          <p style="margin:0;">Nenhuma publicação relacionada à FUNED foi identificada na edição de hoje.</p>
        </div>
        """
        cards_html = ""
    else:
        aviso_sem_resultado = ""
        cards_html = "".join(
            _card_publicacao(i + 1, pub) for i, pub in enumerate(publicacoes)
        )

    # CORREÇÃO 18/09/2026 — Rede de segurança: se alguma página que o app.py
    # já confirmou conter "FUNED"/"Fundação Ezequiel Dias" (busca de texto
    # simples, sem IA) não virou nenhuma publicação (seja porque a IA falhou
    # nela, seja porque decidiu — certa ou erradamente — que não havia ato
    # da FUNED ali), isso agora aparece como um aviso explícito no e-mail,
    # em vez de a página simplesmente desaparecer sem ninguém perceber. Foi
    # assim que a Portaria FUNED nº 75/2026 (página 19) sumiu do e-mail de
    # 18/09/2026: a página tomou "429 Too Many Requests" da OpenRouter em
    # TODAS as 5 tentativas e foi pulada silenciosamente.
    aviso_paginas_nao_confirmadas = ""
    if paginas_sem_ato_extraido:
        lista_paginas = ", ".join(str(p) for p in paginas_sem_ato_extraido)
        aviso_paginas_nao_confirmadas = f"""
        <div style="background:#fdf3e7; border-left:4px solid #d9822b; border-radius:6px; padding:16px; margin:20px 0;">
          <p style="margin:0; color:#8a5a1e;"><strong>⚠️ Atenção — verificação manual recomendada:</strong>
          a(s) página(s) {lista_paginas} menciona(m) "FUNED"/"Fundação Ezequiel Dias" no texto do Diário Oficial de hoje,
          mas o resumo automático pode não ter capturado TODOS os atos presentes nela(s)
          — seja porque nenhum ato foi identificado, seja porque a página tem mais menções
          ao termo do que publicações extraídas (pode ter sido uma falha temporária da IA,
          uma página muito extensa/com muitos órgãos misturados, ou um caso ambíguo).
          Recomenda-se conferir essa(s) página(s) diretamente no Diário Oficial.</p>
        </div>
        """

    resumo_box = f"""
    <div style="background:#faf6ec; border-left:4px solid #c9a24b; border-radius:6px; padding:16px; margin:20px 0;">
      <p style="margin:6px 0;"><strong style="color:#1e3a5f;">Data da edição:</strong> {DATA_HOJE_BR}</p>
      <p style="margin:6px 0;"><strong style="color:#1e3a5f;">Páginas com atos identificados:</strong> {', '.join(str(p) for p in paginas_com_atos) or 'nenhuma'}</p>
      <p style="margin:6px 0;"><strong style="color:#1e3a5f;">Total de atos identificados:</strong> {len(publicacoes)}</p>
    </div>
    """

    # CORREÇÃO 02/10/2026 (3ª): layout do cabeçalho redesenhado a pedido —
    # selo circular "SDC" (anel dourado sobre fundo azul-marinho), no mesmo
    # estilo visual usado em outras peças da equipe, em vez do título simples
    # de antes. Construído com uma <table> (não só <div>) pra renderizar como
    # círculo de forma confiável também no Outlook/Word, que ignora
    # border-radius em <div> mas respeita em células de tabela.
    selo_sdc = """
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto 18px auto;">
          <tr>
            <td style="width:64px; height:64px; border-radius:50%; border:2px solid #c9a24b; background:#1e3a5f; text-align:center; vertical-align:middle; font-family:Arial, Helvetica, sans-serif;">
              <span style="display:inline-block; color:#c9a24b; font-size:14px; font-weight:bold; letter-spacing:1.5px;">SDC</span>
            </td>
          </tr>
        </table>
    """

    return f"""
    <div style="max-width:600px; margin:0 auto; font-family:Arial, Helvetica, sans-serif; color:#1a1a1a;">
      <div style="background:#1e3a5f; border-radius:8px 8px 0 0; padding:28px 24px 24px 24px; text-align:center;">
        {selo_sdc}
        <h1 style="margin:0; color:#ffffff; font-size:22px; letter-spacing:0.3px;">Monitoramento do Diário Oficial</h1>
        <p style="margin:8px 0 0 0; color:#c9a24b; font-size:13px; text-transform:uppercase; letter-spacing:1px;">Fundação Ezequiel Dias – FUNED</p>
      </div>
      <div style="border:1px solid #e2e8f0; border-top:none; border-radius:0 0 8px 8px; padding:24px;">
        {resumo_box}
        {aviso_paginas_nao_confirmadas}
        {aviso_sem_resultado}
        {cards_html}
        <hr style="border:none; border-top:1px solid #e2e8f0; margin:24px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto 10px auto;">
          <tr>
            <td style="width:30px; height:30px; border-radius:50%; border:1.5px solid #c9a24b; background:#1e3a5f; text-align:center; vertical-align:middle;">
              <span style="display:inline-block; color:#c9a24b; font-size:8px; font-weight:bold; letter-spacing:0.5px;">SDC</span>
            </td>
          </tr>
        </table>
        <p style="margin:0; color:#8a94a3; font-size:12px; text-align:center;">
          Relatório gerado automaticamente para apoio ao monitoramento institucional da FUNED.<br>
          <strong style="color:#1e3a5f;">Serviço de Desenvolvimento e Capacitação — SDC</strong>
        </p>
      </div>
    </div>
    """


# --------------------------------------------------------------------------
# 4. Enviar por e-mail (Gmail SMTP + App Password)
# --------------------------------------------------------------------------

def enviar_email(html, destinatarios):
    assunto = f"Resumo Diário Oficial FUNED - {DATA_HOJE_BR}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = f"Elisangela Ferreira da Silva <{GMAIL_USER}>"
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
        servidor.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        servidor.sendmail(GMAIL_USER, destinatarios, msg.as_string())

    print(f"E-mail enviado para: {', '.join(destinatarios)}")


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------

def main():
    print(f"Iniciando monitoramento do Diário Oficial FUNED - {DATA_HOJE_BR}")

    paginas = buscar_paginas_diario()
    print(f"{len(paginas)} página(s) recebida(s) do serviço Render.")
    for p in paginas:
        tamanho = len(p.get("texto") or "")
        inicio_texto = (p.get("texto") or "")[:120].replace("\n", " ")
        print(f"  -> página {p.get('numero')}: {tamanho} caractere(s) — início: {inicio_texto!r}")

    dados = extrair_publicacoes(paginas)
    print(f"{len(dados.get('publicacoes', []))} publicação(ões) identificada(s).")

    # CORREÇÃO 18/09/2026 — Rede de segurança (ver comentário em
    # renderizar_email_html): compara as páginas que o app.py já confirmou
    # conter o termo buscado contra as páginas que a IA efetivamente
    # transformou em publicação. A diferença vira aviso no e-mail.
    paginas_recebidas_numeros = {p["numero"] for p in paginas}
    paginas_confirmadas_numeros = set(dados.get("paginas_com_atos", []))
    paginas_zero_publicacoes = sorted(paginas_recebidas_numeros - paginas_confirmadas_numeros)

    # CORREÇÃO 02/10/2026 (2ª): a rede de segurança acima só pegava páginas
    # com ZERO publicações extraídas — mas a página 32 de 02/10/2026 teve 1
    # publicação extraída (de 5 atos reais), então passava batido. Agora o
    # aviso também inclui páginas com COBERTURA INCOMPLETA (teve publicação,
    # mas sobrou menção à FUNED sem representar nenhuma publicação).
    paginas_cobertura_incompleta = dados.get("paginas_cobertura_incompleta", [])
    dados["paginas_sem_ato_extraido"] = sorted(
        set(paginas_zero_publicacoes) | set(paginas_cobertura_incompleta)
    )
    if dados["paginas_sem_ato_extraido"]:
        print(
            f"⚠️ Página(s) com termo encontrado mas sem cobertura completa pela IA "
            f"(zero publicações: {paginas_zero_publicacoes}; cobertura incompleta: "
            f"{paginas_cobertura_incompleta}) — incluindo aviso no e-mail.",
            file=sys.stderr,
        )

    html = renderizar_email_html(dados)
    enviar_email(html, DESTINATARIOS)

    print("Concluído com sucesso.")


if __name__ == "__main__":
    main()
