name: Monitoramento Diário Oficial FUNED

on:
  schedule:
    # 10:15 UTC = 07:15 horário de Brasília (BRT, UTC-3 o ano todo, sem
    # horário de verão), de terça a sábado.
    # OBS: de propósito NÃO é hora cheia (o GitHub recomenda evitar isso,
    # é o horário de maior concorrência entre workflows agendados). Além
    # disso, o horário foi ADIANTADO de propósito (era 08:07): na prática
    # o GitHub tem atrasado o disparo agendado em até ~50 minutos em dias
    # de pico (observado em 18/08 e 19/08/2026), então rodar às 07:15 dá
    # margem pra ainda chegar perto das 08h mesmo com esse atraso.
    # Ajuste o horário aqui se precisar (lembre-se: o valor é sempre em UTC).
    - cron: "15 10 * * 2-6"
  workflow_dispatch: {}  # permite rodar manualmente pela aba "Actions" no GitHub

jobs:
  monitorar:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout do repositório
        uses: actions/checkout@v4

      - name: Configurar Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Instalar dependências
        run: pip install -r requirements.txt

      - name: Rodar monitoramento e enviar e-mail
        env:
          RENDER_BASE_URL: ${{ secrets.RENDER_BASE_URL }}
          SERVICE_API_KEY: ${{ secrets.SERVICE_API_KEY }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          GMAIL_USER: ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          DESTINATARIOS: ${{ secrets.DESTINATARIOS }}
        run: python main.py
