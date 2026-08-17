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

MAX_TENTATIVAS = 3
ESPERA_ENTRE_TENTATIVAS_MS = 5000


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

    # --- DIAGNÓSTICO TEMPORÁRIO ---
    # O GitHub Actions censura (***) qualquer log que contenha o valor exato
    # do segredo, então não dá pra simplesmente imprimir a URL para conferir.
    # Aqui imprimimos o valor em hexadecimal (uma codificação diferente), que
    # NÃO bate com o texto original e por isso não é censurada — assim dá
    # pra conferir se há espaços, aspas ou caracteres escondidos no segredo.
    print(
        f"[debug] RENDER_BASE_URL tem {len(RENDER_BASE_URL)} caractere(s); "
        f"em hexadecimal: {RENDER_BASE_URL.encode('utf-8').hex()}",
        file=sys.stderr,
    )
    print(
        f"[debug] URL final tem {len(url)} caractere(s); "
        f"em hexadecimal: {url.encode('utf-8').hex()}",
        file=sys.stderr,
    )
    # --- FIM DO DIAGNÓSTICO TEMPORÁRIO ---
    payload = {
        "data_publicacao": DATA_HOJE_ISO,
        "texto_pesquisa": TEXTO_BUSCA,
    }
    headers = {"X-API-Key": SERVICE_API_KEY}

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            # timeout alto: mesmo acordado, o scraping com Playwright pode demorar.
            resp = requests.post(url, json=payload, headers=headers, timeout=180)
            resp.raise_for_status()
            corpo = resp.json()
            dados = corpo.get("dados", {})
            publicacoes_brutas = dados.get("publicacoes", [])

            paginas_normalizadas = [
                {
                    "numero": p.get("pagina"),
                    "texto": p.get("textoPagina") or "",
                }
                for p in publicacoes_brutas
            ]
            return paginas_normalizadas
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            print(f"[tentativa {tentativa}] erro ao chamar Render: {e}", file=sys.stderr)
            # --- DIAGNÓSTICO TEMPORÁRIO: mostra a resposta HTTP completa (se
            # houver uma) para descobrir se quem respondeu 404 foi o nosso
            # app (FastAPI) ou algo no meio do caminho (proxy/borda do Render).
            resp_erro = getattr(e, "response", None)
            if resp_erro is not None:
                print(
                    f"[debug] status={resp_erro.status_code} "
                    f"headers={dict(resp_erro.headers)} "
                    f"corpo={resp_erro.text[:500]!r}",
                    file=sys.stderr,
                )
            else:
                print(f"[debug] exceção sem resposta HTTP associada (tipo: {type(e).__name__})", file=sys.stderr)
            # --- FIM DO DIAGNÓSTICO TEMPORÁRIO ---
            if tentativa < MAX_TENTATIVAS:
                # espera mais a cada tentativa (15s, 30s, 45s...) — dá mais
                # tempo pra instância terminar de acordar entre as tentativas.
                time.sleep((ESPERA_ENTRE_TENTATIVAS_MS / 1000) * tentativa * 3)

    raise RuntimeError(f"Falha ao buscar páginas do Diário após {MAX_TENTATIVAS} tentativas: {ultimo_erro}")


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
"""


def montar_prompt_usuario_pagina(pagina):
    return f"--- PÁGINA {pagina['numero']} ---\n{pagina['texto']}"


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
                # "429 muitas requisições" precisa de mais tempo de espera do
                # que os outros erros, senão a tentativa seguinte esbarra no
                # mesmo limite de novo.
                espera = 20 if "429" in str(e) else (ESPERA_ENTRE_TENTATIVAS_MS / 1000)
                time.sleep(espera)

    raise RuntimeError(f"Falha ao extrair publicações da página {pagina['numero']} após {MAX_TENTATIVAS} tentativas: {ultimo_erro}")


def extrair_publicacoes(paginas):
    """Analisa cada página separadamente (uma chamada à IA por página) e junta
    os resultados. Ver o comentário grande acima de PROMPT_SISTEMA pra
    entender por que isso é feito página a página, e não tudo de uma vez."""
    if not paginas:
        return {"paginas_com_atos": [], "publicacoes": []}

    todas_publicacoes = []
    paginas_com_atos = []

    for i, pagina in enumerate(paginas):
        print(f"Analisando página {pagina['numero']} com a IA ({i + 1}/{len(paginas)})...", file=sys.stderr)
        try:
            resultado_pagina = _chamar_llm_para_pagina(pagina)
        except Exception as e:  # noqa: BLE001
            # Se UMA página falhar (mesmo após as tentativas), não perde as
            # outras — registra o erro e segue pras próximas páginas.
            print(f"Falha ao analisar a página {pagina['numero']}, pulando essa página: {e}", file=sys.stderr)
            continue

        publicacoes_pagina = resultado_pagina.get("publicacoes") or []
        if publicacoes_pagina:
            # Força o número da página com o valor que a GENTE já sabe (veio
            # do serviço de raspagem), em vez de confiar no que o modelo
            # eventualmente tenha tentado inventar/repetir — é exatamente
            # isso que evita o bug de páginas trocadas entre publicações.
            for pub in publicacoes_pagina:
                pub["pagina"] = pagina["numero"]
            todas_publicacoes.extend(publicacoes_pagina)
            paginas_com_atos.append(pagina["numero"])

        # pequena pausa entre chamadas pra não estourar o limite de
        # requisições por minuto do plano gratuito da OpenRouter.
        if i < len(paginas) - 1:
            time.sleep(3)

    resultado = {"paginas_com_atos": paginas_com_atos, "publicacoes": todas_publicacoes}
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
    <div style="border:1px solid #e2e8f0; border-left:4px solid #2563a8; border-radius:6px; padding:16px; margin-bottom:16px; background:#ffffff;">
      <h3 style="margin:0 0 12px 0; color:#1e3a5f; font-size:17px;">Publicação {idx} — Página {pub.get('pagina', '?')}</h3>
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

    if not publicacoes:
        aviso_sem_resultado = """
        <div style="background:#eef2f7; border-left:4px solid #2563a8; border-radius:6px; padding:16px;">
          <p style="margin:0;">Nenhuma publicação relacionada à FUNED foi identificada na edição de hoje.</p>
        </div>
        """
        cards_html = ""
    else:
        aviso_sem_resultado = ""
        cards_html = "".join(
            _card_publicacao(i + 1, pub) for i, pub in enumerate(publicacoes)
        )

    resumo_box = f"""
    <div style="background:#eef2f7; border-left:4px solid #2563a8; border-radius:6px; padding:16px; margin:20px 0;">
      <p style="margin:6px 0;"><strong>Data da edição:</strong> {DATA_HOJE_BR}</p>
      <p style="margin:6px 0;"><strong>Páginas com atos identificados:</strong> {', '.join(str(p) for p in paginas_com_atos) or 'nenhuma'}</p>
      <p style="margin:6px 0;"><strong>Total de atos identificados:</strong> {len(publicacoes)}</p>
    </div>
    """

    return f"""
    <div style="max-width:600px; margin:0 auto; font-family:Arial, Helvetica, sans-serif; color:#1a1a1a;">
      <div style="background:#1e3a5f; border-radius:8px 8px 0 0; padding:24px;">
        <h1 style="margin:0; color:#ffffff; font-size:24px;">Monitoramento do Diário Oficial</h1>
        <p style="margin:8px 0 0 0; color:#c9d6e3; font-size:14px;">Fundação Ezequiel Dias – FUNED</p>
      </div>
      <div style="border:1px solid #e2e8f0; border-top:none; border-radius:0 0 8px 8px; padding:24px;">
        {resumo_box}
        {aviso_sem_resultado}
        {cards_html}
        <hr style="border:none; border-top:1px solid #e2e8f0; margin:24px 0;">
        <p style="margin:0; color:#8a94a3; font-size:12px; text-align:center;">
          Relatório gerado automaticamente para apoio ao monitoramento institucional da FUNED.<br>
          <strong>Automatização SDC</strong>
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

    html = renderizar_email_html(dados)
    enviar_email(html, DESTINATARIOS)

    print("Concluído com sucesso.")


if __name__ == "__main__":
    main()
