# Serviço FUNED — Diário Oficial

Serviço FastAPI + Playwright que abre o portal, captura o Bearer Token gerado pelo próprio site e consulta `ObterEdicaoPorId`.

## Deploy no Render
1. Crie um repositório no GitHub.
2. Envie estes arquivos.
3. No Render, escolha **New > Blueprint** e conecte o repositório.
4. Após o deploy, copie a URL pública e o valor de `SERVICE_API_KEY`.

## n8n
Método: `POST`

URL:
`https://SEU-SERVICO.onrender.com/edicao`

Cabeçalhos:
- `X-API-Key`: valor de `SERVICE_API_KEY`
- `Content-Type`: `application/json`

Corpo JSON em modo expressão:

```javascript
{{
({
  id_jornal: $json.idJornal,
  data_publicacao: DateTime.fromISO($json.dataPublicacao)
    .setZone('America/Sao_Paulo')
    .toFormat('yyyy-MM-dd'),
  texto_pesquisa: 'Fundação Ezequiel Dias'
})
}}
```
