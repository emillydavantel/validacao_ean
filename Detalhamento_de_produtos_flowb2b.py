import os
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, HttpUrl
import requests
from datetime import date
from typing import Optional

router = APIRouter()

class EmpresaDetalhamento(BaseModel):
    empresa_id: int
    webhook_url: HttpUrl
    data_inicio: Optional[date] = None
    data_fim: Optional[date] = None

API_URL_BASE = "https://asahknimbggpzpoebmej.supabase.co"
os.environ["SUPABASE_ANON_KEY"]  # nunca hardcode: chave sai por variavel de ambiente

def processar_detalhamento(empresa_id: int, webhook_url: str, data_inicio: Optional[date], data_fim: Optional[date]):
    headers = {
        "apikey": API_KEY,
        "Content-Type": "application/json"
    }

    try:
        # Construir filtros
        filtros = f"empresa_id=eq.{empresa_id}"
        if data_inicio:
            filtros += f"&data_emissao=gte.{data_inicio}"
        if data_fim:
            filtros += f"&data_emissao=lte.{data_fim}"

        # Selecionar colunas e incluir junção externa com detalhes_nota_fiscal
        select_cols = "id,chave_acesso,data_emissao,detalhes_nota_fiscal!left(id)"

        # Filtrar notas fiscais que ainda não foram detalhadas (onde detalhes_nota_fiscal.id é null)
        filtros += "&detalhes_nota_fiscal.id=is.null"

        # Construir a URL completa
        notas_url = f"{API_URL_BASE}/rest/v1/notas_fiscais?{filtros}&select={select_cols}"

        response = requests.get(notas_url, headers=headers)

        if response.status_code != 200:
            raise Exception(f"Erro ao buscar notas fiscais: {response.status_code} - {response.text}")

        notas = response.json()
        if not notas:
            result = {"message": "Nenhuma nota fiscal encontrada para processamento", "relevante": False, "empresa_id": empresa_id}
        else:
            for nota in notas:
                chave_acesso = nota.get("chave_acesso")
                nota_id = nota.get("id")
                data_emissao = nota.get("data_emissao")

                # Processar a nota
                payload = {
                    "chave_acesso": chave_acesso,
                    "empresa_id": empresa_id
                }
                detalhes_url = f"{API_URL_BASE}/functions/v1/detalhes_nota_fiscal_chave_acesso"
                detalhes_response = requests.post(detalhes_url, json=payload, headers=headers)

                if detalhes_response.status_code != 200:
                    # Registrar o erro conforme necessário
                    continue

            result = {"message": "Detalhamento de produtos concluído com sucesso", "relevante": True, "empresa_id": empresa_id}

        # Enviar notificação para o webhook_url
        requests.post(webhook_url, json=result)

    except Exception as e:
        error_message = {"error": str(e), "relevante": False, "empresa_id": empresa_id}
        # Enviar notificação de erro para o webhook_url
        requests.post(webhook_url, json=error_message)

@router.post("/")
async def detalhar_produtos(empresa: EmpresaDetalhamento, background_tasks: BackgroundTasks):
    background_tasks.add_task(
        processar_detalhamento,
        empresa_id=empresa.empresa_id,
        webhook_url=str(empresa.webhook_url),
        data_inicio=empresa.data_inicio,
        data_fim=empresa.data_fim
    )
    return {"message": "Processamento iniciado, você será notificado quando concluído via webhook"}
