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
MODELO_LLM = "meta-llama/llama-3.3-70b-instruct:free"

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

def buscar_paginas_diario():
    """Chama POST /monitoramento no serviço funed-diario-service (Render)."""
    url = f"{RENDER_BASE_URL}/monitoramento"
    payload = {
        "data_publicacao": DATA_HOJE_ISO,
        "texto_pesquisa": TEXTO_BUSCA,
    }
    headers = {"X-API-Key": SERVICE_API_KEY}

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            # timeout alto: o serviço free do Render pode estar "dormindo" e
            # o primeiro request acorda a instância (pode levar ~1 minuto).
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
            if tentativa < MAX_TENTATIVAS:
                time.sleep(ESPERA_ENTRE_TENTATIVAS_MS / 1000)

    raise RuntimeError(f"Falha ao buscar páginas do Diário após {MAX_TENTATIVAS} tentativas: {ultimo_erro}")


# --------------------------------------------------------------------------
# 2. Extrair/estruturar as publicações com um modelo gratuito do OpenRouter
# --------------------------------------------------------------------------

PROMPT_SISTEMA = """Você é um assistente que analisa o Diário Oficial de Minas Gerais (Diário do \
Executivo) em busca de publicações relacionadas à Fundação Ezequiel Dias (FUNED). \
Você recebe o texto de várias páginas do Diário e deve devolver APENAS um JSON \
válido (sem markdown, sem texto fora do JSON), no seguinte formato exato:

{
  "paginas_com_atos": [8, 31, 33],
  "publicacoes": [
    {
      "pagina": 8,
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
- Considere APENAS publicações relacionadas à FUNED (Fundação Ezequiel Dias), diretas ou indiretas.
- "conteudo_oficial" deve ser um recorte fiel do texto original da página (não invente, não resuma aqui).
- "resumo_objetivo" é o único campo que deve estar em linguagem simplificada.
- Se uma publicação citar múltiplas pessoas, liste todas em "pessoas".
- Se não houver NENHUMA publicação relacionada à FUNED em nenhuma página, devolva:
  {"paginas_com_atos": [], "publicacoes": []}
- Nunca invente números de página, MASP ou datas que não estejam no texto fornecido.
"""


def montar_prompt_usuario(paginas):
    blocos = []
    for p in paginas:
        blocos.append(f"--- PÁGINA {p['numero']} ---\n{p['texto']}")
    return "\n\n".join(blocos)


def _extrair_json(texto_resposta):
    """Modelos gratuitos às vezes envolvem o JSON em ```json ... ```; remove isso."""
    texto = texto_resposta.strip()
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta do modelo não contém JSON reconhecível: {texto[:300]}")
    return json.loads(match.group(0))


def extrair_publicacoes(paginas):
    if not paginas:
        return {"paginas_com_atos": [], "publicacoes": []}

    corpo = {
        "model": MODELO_LLM,
        "messages": [
            {"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": montar_prompt_usuario(paginas)},
        ],
        "temperature": 0.1,
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
                timeout=120,
            )
            resp.raise_for_status()
            conteudo = resp.json()["choices"][0]["message"]["content"]
            return _extrair_json(conteudo)
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            print(f"[tentativa {tentativa}] erro ao chamar OpenRouter: {e}", file=sys.stderr)
            if tentativa < MAX_TENTATIVAS:
                time.sleep(ESPERA_ENTRE_TENTATIVAS_MS / 1000)

    raise RuntimeError(f"Falha ao extrair publicações via LLM após {MAX_TENTATIVAS} tentativas: {ultimo_erro}")


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

    dados = extrair_publicacoes(paginas)
    print(f"{len(dados.get('publicacoes', []))} publicação(ões) identificada(s).")

    html = renderizar_email_html(dados)
    enviar_email(html, DESTINATARIOS)

    print("Concluído com sucesso.")


if __name__ == "__main__":
    main()
