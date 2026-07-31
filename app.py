import os, json
from datetime import date
from urllib.parse import quote
from typing import Any
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

PORTAL = "https://www.jornalminasgerais.mg.gov.br"
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "").strip()
app = FastAPI(title="FUNED Diário Oficial Service", version="1.0.0")

class EdicaoRequest(BaseModel):
    id_jornal: int = Field(..., gt=0)
    data_publicacao: date
    texto_pesquisa: str = "Fundação Ezequiel Dias"

def check_key(value: str | None) -> None:
    if SERVICE_API_KEY and value != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida.")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/edicao")
async def obter_edicao(payload: EdicaoRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    check_key(x_api_key)
    token = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(locale="pt-BR", timezone_id="America/Sao_Paulo")
        page = await context.new_page()

        def capture_token(request):
            nonlocal token
            authorization = request.headers.get("authorization")
            if request.url.startswith(PORTAL) and authorization and authorization.startswith("Bearer "):
                token = authorization

        page.on("request", capture_token)

        dados = {
            "PaginaAtual": 1,
            "TamanhoPagina": 20,
            "textoPesquisa": payload.texto_pesquisa,
            "dataPublicacaoInicial": payload.data_publicacao.isoformat(),
            "dataPublicacaoFinal": payload.data_publicacao.isoformat(),
            "DiarioExecutivo": True,
            "Municipios": False,
            "Terceiros": False,
            "EdicaoExtra": False
        }
        url = f"{PORTAL}/pesquisa?dadosPesquisa={quote(json.dumps(dados, ensure_ascii=False))}"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            for _ in range(60):
                if token:
                    break
                await page.wait_for_timeout(500)

            if not token:
                raise HTTPException(status_code=502, detail="O portal não gerou o Bearer Token.")

            resposta = await page.evaluate(
                """async ({portal, idJornal, bearerToken}) => {
                    const r = await fetch(`${portal}/api/v1/Jornal/ObterEdicaoPorId/${idJornal}`, {
                      headers: {Authorization: bearerToken, Accept: "application/json"}
                    });
                    const t = await r.text();
                    if (!r.ok) throw new Error(`${r.status} - ${t}`);
                    return JSON.parse(t);
                }""",
                {"portal": PORTAL, "idJornal": payload.id_jornal, "bearerToken": token}
            )
            return resposta
        except PlaywrightTimeoutError as exc:
            raise HTTPException(status_code=504, detail="O portal demorou demais para responder.") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Falha ao obter a edição: {exc}") from exc
        finally:
            await context.close()
            await browser.close()
