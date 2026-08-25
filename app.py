CÓDIGO ANTIGO (com o bug) — procure por isso no app.py:

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


CÓDIGO NOVO (corrigido) — substitua pelo trecho abaixo:

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


O QUE MUDOU:
A busca por rótulo agora exige que o elemento encontrado seja necessariamente
um <input> ou <textarea> (usando .and_(), que cruza os dois critérios: "é um
campo de texto" E "tem esse rótulo"). Assim, o botão do VLibras (que é um
<button>, não um campo de texto) nunca mais será escolhido, mesmo que o
rótulo dele contenha a palavra "conteúdo" — e o código vai automaticamente
usar o campo de busca de verdade (ou cair no fallback do input[type="text"]
se nada bater).

COMO APLICAR NO GITHUB:
1. Abra o repositório do serviço Render (funed-diario-service) no GitHub.
2. Abra o arquivo app.py e clique no ícone de lápis (Edit).
3. Localize a função pesquisar_id_jornal_pela_interface (use Ctrl+F na
   página para achar "get_by_label" rapidamente).
4. Selecione exatamente o bloco do "CÓDIGO ANTIGO" acima e substitua pelo
   "CÓDIGO NOVO".
5. Role até o fim da página e clique em "Commit changes...", depois
   "Commit changes" de novo para confirmar.
6. Isso vai disparar automaticamente um novo deploy no Render (porque
   qualquer commit no repositório reconstrói o serviço). Espere uns 2-3
   minutos para o deploy terminar (aba "Events" no Render deve mostrar
   "Deploy live").
7. Depois disso, vá na aba "Actions" do repositório do monitoramento e
   rode o workflow manualmente ("Run workflow") pra confirmar que o erro
   502 sumiu.
