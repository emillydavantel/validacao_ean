from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import requests
from datetime import datetime, timedelta
import math
import os
import logging
from dateutil import parser
import traceback

router = APIRouter()

# Configurações da API do Supabase
API_URL_BASE = "https://asahknimbggpzpoebmej.supabase.co"
API_KEY = os.getenv("SUPABASE_API_KEY")

# Headers comuns para as requisições
HEADERS = {
    "apikey": API_KEY,
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Configurar o logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Anti-pico: se a janela de medicao (desde a ultima compra) for menor que isto,
# recua para a PENULTIMA compra (NF), diluindo picos de periodo curto.
GATILHO_PENULTIMA_DIAS = 7

# Janela MINIMA de medicao da media diaria (dias).
#
# Motivo (caso real, 20/jul/2026 — GOLDEN GATO AD CAST PEIXE 3KG, Henriqueta):
# a janela vai da ultima NF de entrada ate a referencia. Quando o fornecedor
# entrega com frequencia, ela COLAPSA: NFs em 06/07 e 08/07, ultima venda 08/07
# => janela de 2 dias. As 2 vendas do periodo viraram media de 1,0/dia num
# produto que vende ~3 por MES (0,1/dia) — media 10x inflada. Multiplicada pela
# cobertura de 35 dias, sugeriu 44 unidades (o correto era ~5).
#
# O anti-pico da penultima NF nao resolvia: a penultima tambem era de 2 dias
# antes. Media diaria so tem significado com periodo suficiente, entao a janela
# nunca pode ser menor que isto, por mais recente que seja a ultima entrada.
JANELA_MINIMA_DIAS = 30

class FornecedorID(BaseModel):
    fornecedor_id: int
    empresa_id: int

class CalculationRules:
    """Classe para armazenar e documentar as regras aplicadas durante o cálculo"""
    
    def __init__(self):
        self.rules_applied = []
        self.warnings = []
        self.errors = []
    
    def add_rule(self, rule_type: str, description: str, produto_id: Optional[int] = None, data: Optional[Dict] = None):
        self.rules_applied.append({
            "tipo": rule_type,
            "descricao": description,
            "produto_id": produto_id,
            "dados": data,
            "timestamp": datetime.now().isoformat()
        })
    
    def add_warning(self, warning: str, produto_id: Optional[int] = None):
        self.warnings.append({
            "aviso": warning,
            "produto_id": produto_id,
            "timestamp": datetime.now().isoformat()
        })
    
    def add_error(self, error: str, produto_id: Optional[int] = None):
        self.errors.append({
            "erro": error,
            "produto_id": produto_id,
            "timestamp": datetime.now().isoformat()
        })

def round_up_to_multiple(value: float, multiple: int) -> float:
    """Arredonda value para cima para o próximo múltiplo de multiple."""
    if multiple <= 1:
        return round(value)
    return math.ceil(value / multiple) * multiple


# =========================================================================
# REGRA UNICA DE SUGESTAO DE COMPRA POR PRODUTO
# Doc: Flowb2b_v2/docs/regra-sugestao-compra.md
# Substitui as copias divergentes (esta e a fonte de verdade da PRODUCAO).
# =========================================================================

def teto_caixa(valor: float, item_por_caixa: int) -> int:
    """Arredonda SEMPRE para cima ao proximo multiplo da caixa (ceil)."""
    cx = item_por_caixa if item_por_caixa and item_por_caixa > 0 else 1
    return int(math.ceil(valor / cx) * cx)


def calcular_sugestao_produto(item_por_caixa, data_ultima_compra, data_hoje,
                              estoque_atual, qtd_vendida_periodo,
                              prazo_entrega, prazo_estoque,
                              confiabilidade_min_dias=7, dias_periodo_opcional=None,
                              teto_venda_dia=None, fator_seguranca=1.25, pedido_minimo=2):
    """
    ETAPA 2 (quantidade). Funcao PURA (sem IO).
    - qtd_vendida_periodo = venda REAL do periodo (NUNCA compras - estoque).
    - cobertura = prazo_entrega + prazo_estoque.
    - fator de seguranca so quando estoque = 0; pedido minimo quando zerado com demanda.
    """
    dias_venda = (data_hoje - data_ultima_compra).days
    dias_periodo = dias_periodo_opcional if dias_periodo_opcional else dias_venda

    media_venda_dia = 0 if dias_periodo == 0 else qtd_vendida_periodo / dias_periodo
    if teto_venda_dia:
        media_venda_dia = min(media_venda_dia, teto_venda_dia)

    confiabilidade = "Janela curta - revisar" if dias_periodo < confiabilidade_min_dias else "OK"
    periodo_cobertura = (prazo_entrega or 0) + (prazo_estoque or 0)

    sugestao_qtd = media_venda_dia * periodo_cobertura - estoque_atual
    if estoque_atual < 1:
        sugestao_qtd = sugestao_qtd * fator_seguranca
    if sugestao_qtd < 0:
        sugestao_qtd = 0

    teto_sugestao = teto_caixa(sugestao_qtd, item_por_caixa)
    teto_minimo = teto_caixa(pedido_minimo, item_por_caixa) if (estoque_atual < 1 and sugestao_qtd > 0) else 0
    sugestao_final = max(teto_sugestao, teto_minimo)

    dias_estoque_atual = 0 if media_venda_dia == 0 else estoque_atual / media_venda_dia

    return {
        "sugestao_quantidade": sugestao_final,
        "media_venda_dia": media_venda_dia,
        "periodo_venda": dias_periodo,
        "quantidade_vendida": qtd_vendida_periodo,
        "confiabilidade": confiabilidade,
        "periodo_cobertura": periodo_cobertura,
        "dias_estoque_atual": dias_estoque_atual,
        "multiplicacao_aplicada": teto_minimo > teto_sugestao,
    }


def fetch_fornecedor_mais_barato(produto_ids, empresa_id) -> Dict:
    """
    ETAPA 1: para cada produto, retorna o fornecedor_id de MENOR preco efetivo
    = valor_de_compra * (1 - desconto/100). Empate -> menor prazo_entrega.
    Serve para comprar de UM so fornecedor (o mais barato) e nunca duplicar o
    pedido quando o produto tem varios fornecedores. Retorna {produto_id: fornecedor_id}.
    """
    if not produto_ids:
        return {}
    try:
        ids = ",".join(str(p) for p in produto_ids)
        url_fp = f"{API_URL_BASE}/rest/v1/fornecedores_produtos?produto_id=in.({ids})&select=produto_id,fornecedor_id,valor_de_compra,precocusto"
        resp = requests.get(url_fp, headers=HEADERS)
        if resp.status_code != 200:
            logger.warning(f"fetch_fornecedor_mais_barato: erro ao buscar precos: {resp.text}")
            return {}
        rows = resp.json()

        url_pol = f"{API_URL_BASE}/rest/v1/politica_compra?empresa_id=eq.{empresa_id}&select=fornecedor_id,desconto,prazo_entrega"
        resp_pol = requests.get(url_pol, headers=HEADERS)
        politicas_por_forn = {}
        if resp_pol.status_code == 200:
            for p in resp_pol.json():
                politicas_por_forn[p.get("fornecedor_id")] = p

        melhor = {}  # produto_id -> (preco_efetivo, prazo_entrega, fornecedor_id)
        for r in rows:
            pid = r.get("produto_id")
            fid = r.get("fornecedor_id")
            base = r.get("valor_de_compra")
            if base is None:
                base = r.get("precocusto")
            if base is None:
                continue
            pol = politicas_por_forn.get(fid, {})
            desconto = float(pol.get("desconto") or 0)
            prazo = pol.get("prazo_entrega")
            prazo = prazo if prazo is not None else 9999
            preco_efetivo = float(base) * (1 - desconto / 100)
            atual = melhor.get(pid)
            if atual is None or (preco_efetivo, prazo) < (atual[0], atual[1]):
                melhor[pid] = (preco_efetivo, prazo, fid)
        return {pid: v[2] for pid, v in melhor.items()}
    except Exception as e:
        logger.warning(f"fetch_fornecedor_mais_barato falhou: {e}")
        return {}

@router.post("/calcular")
async def calcular_pedido(fornecedor: FornecedorID):
    """Endpoint original mantido para compatibilidade"""
    try:
        fornecedor_id = fornecedor.fornecedor_id
        empresa_id = fornecedor.empresa_id
        logger.info(f"Iniciando cálculo para fornecedor_id: {fornecedor_id}, empresa_id: {empresa_id}")

        # Passo 1: Buscar políticas e produtos
        politicas = fetch_policies(fornecedor_id)
        if not politicas:
            logger.warning("Nenhuma política de compra encontrada.")
            return {"message": "Nenhuma política de compra encontrada para o fornecedor"}

        produtos = fetch_products(fornecedor_id)
        if not produtos:
            logger.warning("Nenhum produto encontrado.")
            return {"message": "Nenhum produto encontrado para o fornecedor"}

        # Passo 2: Buscar dados de última venda e compra
        produto_ids = [p['produto_id'] for p in produtos]
        produtos_datas = fetch_produto_datas(produto_ids)

        # Passo 2.1: Buscar dados do penúltimo pedido para detecção de anomalias
        penultimo_map = fetch_penultimo_pedido(produto_ids, empresa_id)

        # Passo 3: Processar o cálculo
        resultado = process_calculation(politicas, produtos, produtos_datas, penultimo_map, empresa_id, fornecedor_id)

        logger.info("Cálculo concluído com sucesso.")
        return resultado

    except HTTPException as e:
        logger.error(f"HTTPException: {e.detail}")
        raise e
    except Exception as e:
        logger.exception("Erro desconhecido.")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/monitoramento/{fornecedor_id}")
async def monitoramento_calculo(fornecedor_id: int, empresa_id: int = None):
    """
    Endpoint de monitoramento completo que mostra:
    - Todas as informações coletadas
    - Regras aplicadas durante o cálculo
    - Warnings e erros
    - Resultado final detalhado
    """
    calculation_rules = CalculationRules()

    try:
        calculation_rules.add_rule("INICIO", f"Iniciando monitoramento para fornecedor_id: {fornecedor_id}, empresa_id: {empresa_id}")

        # === COLETA DE DADOS BASE ===
        logger.info(f"[MONITORAMENTO] Iniciando para fornecedor_id: {fornecedor_id}, empresa_id: {empresa_id}")

        # 1. Buscar políticas
        calculation_rules.add_rule("FETCH_POLICIES", "Buscando políticas de compra")
        politicas = fetch_policies_with_monitoring(fornecedor_id, calculation_rules)

        if not politicas:
            calculation_rules.add_error("Nenhuma política de compra encontrada")
            return build_monitoring_response(calculation_rules, None, None, None, None)

        # Extrair empresa_id das políticas se não foi fornecido
        if empresa_id is None and politicas:
            empresa_id = politicas[0].get('empresa_id')
            if empresa_id:
                calculation_rules.add_rule("EMPRESA_ID_EXTRACTED", f"empresa_id extraído das políticas: {empresa_id}")

        # 2. Buscar produtos
        calculation_rules.add_rule("FETCH_PRODUCTS", "Buscando produtos do fornecedor")
        produtos = fetch_products_with_monitoring(fornecedor_id, calculation_rules)

        if not produtos:
            calculation_rules.add_error("Nenhum produto encontrado")
            return build_monitoring_response(calculation_rules, politicas, None, None, None)

        # 3. Buscar histórico de vendas/compras
        produto_ids = [p['produto_id'] for p in produtos]
        calculation_rules.add_rule("FETCH_HISTORY", f"Buscando histórico para {len(produto_ids)} produtos")
        produtos_datas = fetch_produto_datas_with_monitoring(produto_ids, calculation_rules)

        # 3.1 Buscar dados do penúltimo pedido para detecção de anomalias
        penultimo_map = {}
        if empresa_id:
            calculation_rules.add_rule("FETCH_PENULTIMO", "Buscando dados do penúltimo pedido para detecção de anomalias")
            penultimo_map = fetch_penultimo_pedido_with_monitoring(produto_ids, empresa_id, calculation_rules)
        else:
            calculation_rules.add_warning("empresa_id não disponível - detecção de anomalias desativada")

        # === PROCESSAMENTO DETALHADO ===
        calculation_rules.add_rule("START_PROCESSING", "Iniciando processamento detalhado")
        resultado_detalhado = process_calculation_with_monitoring(
            politicas, produtos, produtos_datas, calculation_rules, penultimo_map
        )
        
        calculation_rules.add_rule("FINISH", "Cálculo concluído com sucesso")
        
        return build_monitoring_response(
            calculation_rules, 
            politicas, 
            produtos, 
            produtos_datas, 
            resultado_detalhado
        )
        
    except Exception as e:
        calculation_rules.add_error(f"Erro crítico: {str(e)}")
        logger.exception("Erro no monitoramento")
        return build_monitoring_response(calculation_rules, None, None, None, None)

def build_monitoring_response(rules: CalculationRules, politicas, produtos, produtos_datas, resultado):
    """Constrói a resposta completa do monitoramento"""
    return {
        "timestamp": datetime.now().isoformat(),
        "resumo": {
            "total_regras_aplicadas": len(rules.rules_applied),
            "total_warnings": len(rules.warnings),
            "total_errors": len(rules.errors),
            "total_politicas": len(politicas) if politicas else 0,
            "total_produtos": len(produtos) if produtos else 0,
            "sucesso": len(rules.errors) == 0
        },
        "dados_coletados": {
            "politicas": politicas,
            "produtos": produtos,
            "historico_produtos": produtos_datas
        },
        "regras_aplicadas": rules.rules_applied,
        "warnings": rules.warnings,
        "errors": rules.errors,
        "resultado_final": resultado
    }

def fetch_policies_with_monitoring(fornecedor_id: int, rules: CalculationRules) -> List[Dict]:
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/flowb2b_fetch_politica_compra"
        payload = {"f_id": fornecedor_id}
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            error_msg = f"Erro ao buscar políticas: {response.text}"
            rules.add_error(error_msg)
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar políticas")

        politicas = response.json()
        rules.add_rule("POLICIES_FOUND", f"Encontradas {len(politicas)} políticas", data={"count": len(politicas)})
        
        for politica in politicas:
            rules.add_rule("POLICY_DETAIL", "Política encontrada", data={
                "id": politica.get('id'),
                "desconto": politica.get('desconto'),
                "valor_minimo": politica.get('valor_minimo'),
                "prazo_estoque": politica.get('prazo_estoque')
            })
        
        return politicas
    except Exception as e:
        rules.add_error(f"Exceção ao buscar políticas: {str(e)}")
        raise

def fetch_products_with_monitoring(fornecedor_id: int, rules: CalculationRules) -> List[Dict]:
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/flowb2b_get_produtos_detalhados"
        payload = {"f_id": fornecedor_id}
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            error_msg = f"Erro ao buscar produtos: {response.text}"
            rules.add_error(error_msg)
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar produtos")

        produtos = response.json()
        rules.add_rule("PRODUCTS_FOUND", f"Encontrados {len(produtos)} produtos", data={"count": len(produtos)})
        
        for produto in produtos:
            produto_id = produto.get('produto_id')
            rules.add_rule("PRODUCT_DETAIL", "Produto encontrado", produto_id=produto_id, data={
                "codigo_produto": produto.get('codigo_produto'),
                "estoque_atual": produto.get('estoque_atual'),
                "valor_de_compra": produto.get('valor_de_compra'),
                "precocusto": produto.get('precocusto'),
                "itens_por_caixa": produto.get('itens_por_caixa')
            })
        
        return produtos
    except Exception as e:
        rules.add_error(f"Exceção ao buscar produtos: {str(e)}")
        raise

def fetch_produto_datas_with_monitoring(produto_ids: list, rules: CalculationRules) -> Dict:
    try:
        rules.add_rule("FETCH_SALES_HISTORY", "Buscando histórico de vendas")
        vendas_data = fetch_max_data_saida_with_monitoring(produto_ids, rules)
        
        rules.add_rule("FETCH_PURCHASE_HISTORY", "Buscando histórico de compras")
        compras_data = fetch_max_data_compra_with_monitoring(produto_ids, rules)

        produtos_datas = {}
        for produto_id in produto_ids:
            data_venda = vendas_data.get(produto_id)
            data_compra = compras_data.get(produto_id)
            
            produtos_datas[produto_id] = {
                "data_ultima_venda": data_venda,
                "data_ultima_compra": data_compra
            }
            
            # Log das datas encontradas
            rules.add_rule("PRODUCT_DATES", "Datas coletadas", produto_id=produto_id, data={
                "data_ultima_venda": data_venda,
                "data_ultima_compra": data_compra
            })
        
        return produtos_datas
    except Exception as e:
        rules.add_error(f"Erro ao buscar datas dos produtos: {str(e)}")
        raise

def fetch_max_data_saida_with_monitoring(produto_ids: list, rules: CalculationRules) -> Dict:
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/get_max_data_saida"
        payload = {"produto_ids": produto_ids}
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            error_msg = f"Erro ao buscar datas de última venda: {response.text}"
            rules.add_error(error_msg)
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar datas de última venda")

        vendas_data = response.json()
        result = {item['produto_id']: item['max_data_saida'] for item in vendas_data}
        
        rules.add_rule("SALES_DATA_COLLECTED", f"Dados de venda coletados para {len(result)} produtos")
        return result
    except Exception as e:
        rules.add_error(f"Exceção ao buscar datas de venda: {str(e)}")
        raise

def fetch_max_data_compra_with_monitoring(produto_ids: list, rules: CalculationRules) -> Dict:
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/get_max_data_compra"
        payload = {"produto_ids": produto_ids}
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            error_msg = f"Erro ao buscar datas de última compra: {response.text}"
            rules.add_error(error_msg)
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar datas de última compra")

        compras_data = response.json()
        result = {item['produto_id']: item['max_data_compra'] for item in compras_data}
        
        rules.add_rule("PURCHASE_DATA_COLLECTED", f"Dados de compra coletados para {len(result)} produtos")
        return result
    except Exception as e:
        rules.add_error(f"Exceção ao buscar datas de compra: {str(e)}")
        raise

def fetch_penultimo_pedido_with_monitoring(produto_ids: list, empresa_id: int, rules: CalculationRules) -> Dict:
    """Busca dados do penúltimo pedido de compra para detecção de anomalias"""
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/get_penultimo_pedido_compra"
        payload = {"p_produto_ids": produto_ids, "p_empresa_id": empresa_id}
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            rules.add_warning(f"Não foi possível buscar penúltimo pedido: {response.text}")
            return {}

        penultimo_data = response.json()
        result = {}
        for item in penultimo_data:
            produto_id = item.get('produto_id')
            if produto_id:
                result[produto_id] = {
                    'qtd_penultima': item.get('qtd_penultima_compra'),
                    'intervalo': item.get('intervalo_dias'),
                    'data_penultima': item.get('data_penultima_compra')
                }

        rules.add_rule("PENULTIMO_PEDIDO_COLLECTED", f"Dados do penúltimo pedido coletados para {len(result)} produtos")
        return result
    except Exception as e:
        rules.add_warning(f"Exceção ao buscar penúltimo pedido: {str(e)}")
        return {}

def process_calculation_with_monitoring(politicas: List[Dict], produtos: List[Dict],
                                      produtos_datas: Dict, rules: CalculationRules, penultimo_map: Dict = None) -> list:
    resultado = []
    penultimo_map = penultimo_map or {}

    # Cache para id_produto_bling
    id_produto_bling_cache = {}

    for politica in politicas:
        politica_id = politica['id']
        rules.add_rule("PROCESSING_POLICY", f"Processando política {politica_id}", data=politica)
        
        produtos_array = []
        valor_total_pedido = 0
        valor_total_pedido_com_desconto = 0
        quantidade_produtos = 0
        produtos_descartados = 0

        for produto in produtos:
            produto_id = produto['produto_id']
            
            try:
                # Processar produto com monitoramento detalhado
                produto_processado = process_product_with_monitoring(
                    produto, politica, produtos_datas, rules, id_produto_bling_cache, penultimo_map
                )
                
                if produto_processado is None:
                    produtos_descartados += 1
                    continue
                
                # Atualizar totais
                valor_total_pedido += produto_processado['valor_total_produto']
                valor_total_pedido_com_desconto += produto_processado['valor_total_produto_com_desconto']
                quantidade_produtos += 1
                
                produtos_array.append(produto_processado)
                
            except Exception as e:
                rules.add_error(f"Erro ao processar produto {produto_id}: {str(e)}", produto_id)
                continue

        # Log do resultado da política
        rules.add_rule("POLICY_RESULT", f"Política {politica_id} processada", data={
            "produtos_incluidos": quantidade_produtos,
            "produtos_descartados": produtos_descartados,
            "valor_total": valor_total_pedido,
            "valor_com_desconto": valor_total_pedido_com_desconto
        })

        # Verificar valor mínimo
        valor_minimo = politica.get('valor_minimo') or 0
        if valor_total_pedido >= valor_minimo:
            politica_compra = montar_politica_compra(
                politica,
                valor_total_pedido,
                valor_total_pedido_com_desconto,
                quantidade_produtos,
                False,  # melhor_politica será definido depois
                produtos_array
            )
            resultado.append(politica_compra)
            rules.add_rule("POLICY_INCLUDED", f"Política {politica_id} incluída no resultado")
        else:
            rules.add_rule("POLICY_EXCLUDED", f"Política {politica_id} excluída - valor mínimo não atingido", data={
                "valor_total": valor_total_pedido,
                "valor_minimo": valor_minimo
            })

    # Determinar melhor política entre as que atingiram valor mínimo
    if resultado:
        melhor_politica_id = find_best_policy_among_results(resultado, rules)
        for politica_compra in resultado:
            if politica_compra['politica_id'] == melhor_politica_id:
                politica_compra['melhor_politica'] = True

    return resultado

def process_product_with_monitoring(produto: Dict, politica: Dict, produtos_datas: Dict,
                                   rules: CalculationRules, cache: Dict, penultimo_map: Dict = None) -> Optional[Dict]:
    produto_id = produto['produto_id']
    penultimo_map = penultimo_map or {}
    
    try:
        # Obter dados do histórico
        data_info = produtos_datas.get(produto_id, {})
        data_ultima_venda_str = data_info.get('data_ultima_venda')
        data_ultima_compra_str = data_info.get('data_ultima_compra')
        estoque_atual = produto.get('estoque_atual') or 0
        
        rules.add_rule("PRODUCT_DATA_COLLECTED", "Dados iniciais coletados", produto_id, data={
            "estoque_atual": estoque_atual,
            "data_ultima_venda_original": data_ultima_venda_str,
            "data_ultima_compra_original": data_ultima_compra_str
        })

        # REGRA 1: Aplicar regra de estoque com produto disponível
        if estoque_atual > 0:
            data_ultima_venda_str = datetime.now().date().isoformat()
            rules.add_rule("STOCK_AVAILABLE_RULE", "Produto com estoque - data de venda ajustada para hoje",
                          produto_id, data={"nova_data_venda": data_ultima_venda_str})
        elif not data_ultima_venda_str:
            rules.add_rule("NO_SALES_HISTORY_RULE", "Produto sem histórico de vendas e sem estoque - descartado", produto_id)
            return None

        # REGRA 2: Ajustar datas
        data_ultima_venda = ajustar_data_futura_with_monitoring(
            parser.isoparse(data_ultima_venda_str).date(), rules, produto_id
        )
        data_ultima_compra = ajustar_data_compra_with_monitoring(
            data_ultima_compra_str, data_ultima_venda, politica, rules, produto_id
        )

        # Calcular período de venda
        periodo_venda = max((data_ultima_venda - data_ultima_compra).days, 1)
        rules.add_rule("SALES_PERIOD_CALCULATED", f"Período de venda calculado: {periodo_venda} dias", 
                      produto_id, data={
                          "data_ultima_venda": data_ultima_venda.isoformat(),
                          "data_ultima_compra": data_ultima_compra.isoformat(),
                          "periodo_dias": periodo_venda
                      })

        # Buscar quantidade vendida
        quantidade_vendida = fetch_quantidade_vendida_with_monitoring(
            produto_id, data_ultima_compra.isoformat(), data_ultima_venda.isoformat(), rules
        )

        # Calcular média de vendas diárias
        media_venda_dia = quantidade_vendida / periodo_venda

        # DETECÇÃO DE ANOMALIA: usar penúltimo pedido para validar média
        penultimo_data = penultimo_map.get(produto_id)
        anomalia_detectada = False
        if penultimo_data:
            intervalo = penultimo_data.get('intervalo')
            qtd_penultima = penultimo_data.get('qtd_penultima')

            # Validar intervalo (entre 7 e 180 dias)
            if intervalo and qtd_penultima and 7 <= intervalo <= 180:
                media_esperada = qtd_penultima / intervalo

                # Se média atual > 3.0x média esperada, corrigir
                if media_venda_dia > media_esperada * 3.0:
                    media_original = media_venda_dia
                    media_venda_dia = media_esperada
                    anomalia_detectada = True

                    rules.add_warning(
                        f"Anomalia detectada: média {media_original:.2f}/dia corrigida para {media_esperada:.2f}/dia "
                        f"(baseado em penúltimo pedido: {qtd_penultima} unid em {intervalo} dias)",
                        produto_id
                    )

        rules.add_rule("DAILY_AVERAGE_CALCULATED", f"Média diária calculada: {media_venda_dia:.2f}" + (" (corrigida)" if anomalia_detectada else ""),
                      produto_id, data={
                          "quantidade_vendida": quantidade_vendida,
                          "periodo_venda": periodo_venda,
                          "media_venda_dia": media_venda_dia,
                          "anomalia_corrigida": anomalia_detectada
                      })

        # Calcular sugestão de quantidade
        sugestao_quantidade, multiplicacao = calcular_sugestao_with_monitoring(
            produto, politica, media_venda_dia, quantidade_vendida, rules
        )

        # Verificar se produto deve ser descartado
        if sugestao_quantidade <= 0:
            rules.add_rule("PRODUCT_DISCARDED", "Produto descartado - estoque suficiente", produto_id, data={
                "sugestao_quantidade": sugestao_quantidade,
                "estoque_atual": estoque_atual
            })
            return None

        # Calcular valores
        valor_total_produto, valor_total_produto_com_desconto = calcular_valores_with_monitoring(
            produto, politica, sugestao_quantidade, rules, produto_id
        )

        # Buscar id_produto_bling com cache
        if produto_id in cache:
            id_produto_bling = cache[produto_id]
        else:
            id_produto_bling = fetch_id_produto_bling(produto_id)
            cache[produto_id] = id_produto_bling

        # Montar resultado final do produto
        produto_resultado = montar_detalhes_produto(
            produto,
            quantidade_vendida,
            periodo_venda,
            sugestao_quantidade,
            valor_total_produto,
            valor_total_produto_com_desconto,
            multiplicacao,
            id_produto_bling,
            data_ultima_venda_str,
            data_ultima_compra_str
        )
        
        rules.add_rule("PRODUCT_PROCESSED", "Produto processado com sucesso", produto_id, data={
            "sugestao_quantidade": sugestao_quantidade,
            "valor_total": valor_total_produto,
            "valor_com_desconto": valor_total_produto_com_desconto
        })
        
        return produto_resultado
        
    except Exception as e:
        rules.add_error(f"Erro ao processar produto {produto_id}: {str(e)}", produto_id)
        return None

def calcular_sugestao_with_monitoring(produto: Dict, politica: Dict, media_venda_dia: float, 
                                    quantidade_vendida: float, rules: CalculationRules) -> tuple:
    produto_id = produto.get('produto_id')
    itens_por_caixa = produto.get('itens_por_caixa') or 1
    estoque_atual = produto.get('estoque_atual') or 0
    prazo_estoque = politica.get('prazo_estoque') or 0

    # Fórmula base
    sugestao_inicial = media_venda_dia * prazo_estoque - estoque_atual

    # Margem de segurança - aplicar APENAS quando estoque zerado
    aplicou_margem_seguranca = False

    if estoque_atual == 0:
        margem_seguranca = 1.25  # 25% a mais
        sugestao_inicial = sugestao_inicial * margem_seguranca
        aplicou_margem_seguranca = True

        rules.add_rule("SAFETY_MARGIN_APPLIED", "Margem de segurança aplicada - estoque zerado",
                      produto_id, data={
                          "estoque_atual": estoque_atual,
                          "margem_percentual": "25%"
                      })

    rules.add_rule("INITIAL_SUGGESTION", "Sugestão inicial calculada", produto_id, data={
        "media_venda_dia": media_venda_dia,
        "prazo_estoque": prazo_estoque,
        "estoque_atual": estoque_atual,
        "sugestao_inicial": sugestao_inicial,
        "margem_seguranca_aplicada": aplicou_margem_seguranca
    })

    if sugestao_inicial <= 0:
        return 0, False

    # Ajustar por embalagem - SEMPRE ARREDONDAR PARA CIMA para o próximo múltiplo de itens_por_caixa
    if itens_por_caixa > 1:
        sugestao_quantidade = round_up_to_multiple(sugestao_inicial, itens_por_caixa)
        rules.add_rule("PACKAGE_ROUND_UP", "Arredondado para múltiplo da caixa", produto_id, data={
            "sugestao_inicial": sugestao_inicial,
            "itens_por_caixa": itens_por_caixa,
            "sugestao_final": sugestao_quantidade
        })
    else:
        sugestao_quantidade = round(sugestao_inicial)
        rules.add_rule("SIMPLE_ROUND", "Arredondamento simples", produto_id, data={
            "sugestao_inicial": sugestao_inicial,
            "sugestao_final": sugestao_quantidade
        })

    multiplicacao = False

    # REGRA 8a: produto zerado com demanda no periodo → garante pelo menos 1 caixa
    if estoque_atual == 0 and quantidade_vendida > 0 and sugestao_quantidade < itens_por_caixa:
        sugestao_quantidade = itens_por_caixa
        multiplicacao = True
        rules.add_rule("ZERO_STOCK_DEMAND_PROTECTION",
                      "Produto zerado com demanda no periodo - garantida 1 caixa minima",
                      produto_id, data={
                          "estoque_atual": estoque_atual,
                          "quantidade_vendida_periodo": quantidade_vendida,
                          "itens_por_caixa": itens_por_caixa,
                          "quantidade_garantida": sugestao_quantidade
                      })

    # Regra existente: produto vendido em caixa garante 1 caixa
    unidade_produto = produto.get('unidade', 'UN').upper()
    eh_produto_caixa = unidade_produto not in ['UN', 'UNT']

    if eh_produto_caixa and sugestao_quantidade > 0:
        sugestao_quantidade = max(sugestao_quantidade, itens_por_caixa)
        multiplicacao = True
        rules.add_rule("BOX_MINIMUM_ENFORCED", "Garantida 1 caixa mínima - produto vendido em caixa",
                      produto_id, data={
                          "unidade": unidade_produto,
                          "itens_por_caixa": itens_por_caixa,
                          "quantidade_garantida": sugestao_quantidade
                      })

    return sugestao_quantidade, multiplicacao

def calcular_valores_with_monitoring(produto: Dict, politica: Dict, sugestao_quantidade: float,
                                   rules: CalculationRules, produto_id: int) -> tuple:
    preco_custo = produto.get('precocusto')
    valor_de_compra = preco_custo if preco_custo is not None else (produto.get('valor_de_compra') or 0)
    itens_por_caixa = produto.get('itens_por_caixa') or 1

    # CORREÇÃO: Se o produto é vendido em caixa (itens_por_caixa > 1), o valor_de_compra é o preço da CAIXA.
    # A sugestao_quantidade está em UNIDADES.
    # Portanto, precisamos converter unidades para caixas antes de multiplicar pelo preço.
    quantidade_compras = sugestao_quantidade / itens_por_caixa
    
    valor_total_produto = quantidade_compras * valor_de_compra
    desconto = politica.get('desconto') or 0
    valor_total_produto_com_desconto = valor_total_produto * (1 - desconto / 100)

    rules.add_rule("VALUES_CALCULATED", "Valores calculados (Corrigido por Caixa)", produto_id, data={
        "valor_unitario_ou_caixa": valor_de_compra,
        "preco_custo": preco_custo,
        "valor_de_compra_original": produto.get('valor_de_compra'),
        "itens_por_caixa": itens_por_caixa,
        "quantidade_unidades": sugestao_quantidade,
        "quantidade_compras_considerada": quantidade_compras,
        "valor_total": valor_total_produto,
        "desconto": desconto,
        "valor_com_desconto": valor_total_produto_com_desconto
    })
    
    return valor_total_produto, valor_total_produto_com_desconto

def ajustar_data_futura_with_monitoring(data: datetime, rules: CalculationRules, produto_id: int) -> datetime:
    data_hoje = datetime.now().date()
    if data > data_hoje:
        rules.add_rule("FUTURE_DATE_ADJUSTED", "Data futura ajustada para hoje", produto_id, data={
            "data_original": data.isoformat(),
            "data_ajustada": data_hoje.isoformat()
        })
        return data_hoje
    return data

def ajustar_data_compra_with_monitoring(data_ultima_compra_str: str, data_ultima_venda: datetime,
                                      politica: Dict, rules: CalculationRules, produto_id: int) -> datetime:
    prazo_estoque = politica.get('prazo_estoque') or 30

    if not data_ultima_compra_str:
        data_ajustada = data_ultima_venda - timedelta(days=prazo_estoque)
        rules.add_rule("NULL_PURCHASE_DATE", "Data de compra nula - usando prazo da política",
                      produto_id, data={
                          "data_venda": data_ultima_venda.isoformat(),
                          "prazo_usado": prazo_estoque,
                          "data_compra_assumida": data_ajustada.isoformat()
                      })
        return data_ajustada

    data_ultima_compra = parser.isoparse(data_ultima_compra_str).date()

    if data_ultima_compra >= data_ultima_venda:
        data_ajustada = data_ultima_venda - timedelta(days=prazo_estoque)
        rules.add_rule("PURCHASE_DATE_ADJUSTED", "Data de compra ajustada usando prazo da política",
                      produto_id, data={
                          "data_compra_original": data_ultima_compra.isoformat(),
                          "prazo_usado": prazo_estoque,
                          "data_compra_ajustada": data_ajustada.isoformat(),
                          "data_venda": data_ultima_venda.isoformat()
                      })
        return data_ajustada

    return data_ultima_compra

def fetch_quantidade_vendida_with_monitoring(produto_id: int, data_inicio: str, data_fim: str, 
                                           rules: CalculationRules) -> float:
    try:
        url = f"{API_URL_BASE}/rest/v1/rpc/get_quantidade_vendida"
        payload = {
            "p_produto_id": produto_id,
            "data_inicio": data_inicio,
            "data_fim": data_fim
        }
        response = requests.post(url, headers=HEADERS, json=payload)

        if response.status_code != 200:
            error_msg = f"Erro ao buscar quantidade vendida: {response.text}"
            rules.add_error(error_msg, produto_id)
            raise HTTPException(status_code=response.status_code, detail=f"Erro ao buscar quantidade vendida")

        result = response.json()
        quantidade = float(result[0]['quantidade_vendida']) if result else 0
        
        rules.add_rule("QUANTITY_SOLD_FETCHED", f"Quantidade vendida obtida: {quantidade}", produto_id, data={
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "quantidade_vendida": quantidade
        })
        
        return quantidade
    except Exception as e:
        rules.add_error(f"Exceção ao buscar quantidade vendida: {str(e)}", produto_id)
        raise

def find_best_policy_among_results(resultado: List[Dict], rules: CalculationRules) -> Optional[int]:
    """Encontra a melhor política entre as que foram incluídas no resultado (atingiram valor mínimo)"""
    if not resultado:
        return None

    melhor_desconto = 0
    menor_prazo = float('inf')
    melhor_politica_id = None

    rules.add_rule("BEST_POLICY_SELECTION", f"Selecionando melhor entre {len(resultado)} políticas que atingiram valor mínimo")

    for item in resultado:
        # Usar 'or' para tratar None como 0 (desconto pode existir mas ser None)
        desconto = item.get('desconto') or 0
        prazo_estoque = item.get('prazo_estoque') or float('inf')
        politica_id = item.get('politica_id')

        if desconto > melhor_desconto or (desconto == melhor_desconto and prazo_estoque < menor_prazo):
            melhor_desconto = desconto
            menor_prazo = prazo_estoque
            melhor_politica_id = politica_id

    rules.add_rule("BEST_POLICY_DETERMINED", f"Melhor política determinada: {melhor_politica_id}", data={
        "melhor_desconto": melhor_desconto,
        "menor_prazo": menor_prazo
    })

    return melhor_politica_id

def find_best_policy_with_monitoring(politicas: List[Dict], rules: CalculationRules) -> int:
    melhor_desconto = 0
    menor_prazo = float('inf')
    melhor_politica_id = None

    rules.add_rule("BEST_POLICY_SEARCH", f"Analisando {len(politicas)} políticas para encontrar a melhor")

    for politica in politicas:
        desconto = politica.get('desconto') or 0
        prazo_estoque = politica.get('prazo_estoque') or float('inf')
        politica_id = politica.get('id')

        rules.add_rule("POLICY_EVALUATION", "Avaliando política", data={
            "politica_id": politica_id,
            "desconto": desconto,
            "prazo_estoque": prazo_estoque,
            "eh_melhor_desconto": desconto > melhor_desconto,
            "eh_menor_prazo": prazo_estoque < menor_prazo
        })

        if desconto > melhor_desconto or (desconto == melhor_desconto and prazo_estoque < menor_prazo):
            melhor_desconto = desconto
            menor_prazo = prazo_estoque
            melhor_politica_id = politica_id
            
            rules.add_rule("NEW_BEST_POLICY", f"Nova melhor política encontrada: {politica_id}", data={
                "desconto": desconto,
                "prazo_estoque": prazo_estoque
            })

    rules.add_rule("BEST_POLICY_FINAL", f"Melhor política determinada: {melhor_politica_id}", data={
        "melhor_desconto": melhor_desconto,
        "menor_prazo": menor_prazo
    })

    return melhor_politica_id

# === FUNÇÕES ORIGINAIS MANTIDAS PARA COMPATIBILIDADE ===

def fetch_policies(fornecedor_id: int) -> List[Dict]:
    url = f"{API_URL_BASE}/rest/v1/rpc/flowb2b_fetch_politica_compra"
    payload = {"f_id": fornecedor_id}
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar políticas: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Erro ao buscar políticas")

    return response.json()

def fetch_products(fornecedor_id: int) -> List[Dict]:
    url = f"{API_URL_BASE}/rest/v1/rpc/flowb2b_get_produtos_detalhados"
    payload = {"f_id": fornecedor_id}
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar produtos: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Erro ao buscar produtos")

    return response.json()

def fetch_produto_datas(produto_ids: list) -> Dict:
    try:
        vendas_data = fetch_max_data_saida(produto_ids)
        compras_data = fetch_max_data_compra(produto_ids)

        produtos_datas = {
            produto_id: {
                "data_ultima_venda": vendas_data.get(produto_id),
                "data_ultima_compra": compras_data.get(produto_id)
            }
            for produto_id in produto_ids
        }
        return produtos_datas
    except Exception as e:
        logger.exception("Erro ao buscar datas dos produtos.")
        raise

def fetch_max_data_saida(produto_ids: list) -> Dict:
    url = f"{API_URL_BASE}/rest/v1/rpc/get_max_data_saida"
    payload = {"produto_ids": produto_ids}
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar datas de última venda: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Erro ao buscar datas de última venda")

    vendas_data = response.json()
    return {item['produto_id']: item['max_data_saida'] for item in vendas_data}

def fetch_max_data_compra(produto_ids: list) -> Dict:
    # data de ENTRADA real da NF (data_operacao), nao dh_sai_ent — evita defasagem de 1 dia
    url = f"{API_URL_BASE}/rest/v1/rpc/get_max_data_entrada_op"
    payload = {"produto_ids": produto_ids}
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar datas de última compra: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Erro ao buscar datas de última compra")

    compras_data = response.json()
    return {item['produto_id']: item['max_data_compra'] for item in compras_data}

def fetch_penultimo_pedido(produto_ids: list, empresa_id: int) -> Dict:
    """Busca a penúltima NF de entrada (não pedido) para recuar a janela em período curto"""
    url = f"{API_URL_BASE}/rest/v1/rpc/get_penultimo_nf_entrada"
    payload = {"p_produto_ids": produto_ids, "p_empresa_id": empresa_id}
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.warning(f"Erro ao buscar penúltimo pedido: {response.text}")
        return {}

    penultimo_data = response.json()
    result = {}
    for item in penultimo_data:
        produto_id = item.get('produto_id')
        if produto_id:
            result[produto_id] = {
                'qtd_penultima': item.get('qtd_penultima_compra'),
                'intervalo': item.get('intervalo_dias'),
                'data_penultima': item.get('data_penultima_compra')
            }
    return result

def fetch_id_produto_bling(produto_id: int) -> int:
    url = f"{API_URL_BASE}/rest/v1/produtos?id=eq.{produto_id}"
    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar id_produto_bling: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Erro ao buscar id_produto_bling")

    result = response.json()
    return result[0]['id_produto_bling'] if result else None

def process_calculation(politicas: List[Dict], produtos: List[Dict], produtos_datas: Dict, penultimo_map: Dict = None, empresa_id: int = None, fornecedor_id: int = None) -> list:
    resultado = []
    penultimo_map = penultimo_map or {}

    id_produto_bling_cache = {}

    # ETAPA 1 da regra: fornecedor de MENOR preco por produto. Um produto so
    # entra no pedido deste fornecedor se este for o mais barato dele.
    produto_ids_calc = [p['produto_id'] for p in produtos]
    fornecedor_mais_barato = fetch_fornecedor_mais_barato(produto_ids_calc, empresa_id)

    for politica in politicas:
        produtos_array = []
        valor_total_pedido = 0
        valor_total_pedido_com_desconto = 0
        quantidade_produtos = 0

        for produto in produtos:
            produto_id = produto['produto_id']

            # ETAPA 1: comprar do fornecedor mais barato. Se outro fornecedor
            # for mais barato para este produto, ele nao entra no pedido deste.
            forn_barato = fornecedor_mais_barato.get(produto_id)
            if forn_barato is not None and fornecedor_id is not None and forn_barato != fornecedor_id:
                continue

            data_info = produtos_datas.get(produto_id, {})
            data_ultima_venda_str = data_info.get('data_ultima_venda')
            data_ultima_compra_str = data_info.get('data_ultima_compra')
            estoque_atual = produto.get('estoque_atual') or 0

            # ETAPA 2: janela de medicao da venda REAL do sistema.
            data_hoje = datetime.now().date()

            # Inicio da janela = ultima compra (NF de entrada). Sem registro: 90 dias atras.
            if data_ultima_compra_str:
                data_ultima_compra = parser.isoparse(data_ultima_compra_str).date()
            else:
                data_ultima_compra = data_hoje - timedelta(days=90)

            # Fim da janela (referencia): com estoque -> hoje; em RUPTURA (estoque 0) ->
            # ultima venda (os dias sem estoque nao contam, nao dava pra vender sem produto).
            if estoque_atual > 0:
                data_ref = data_hoje
            elif data_ultima_venda_str:
                data_ref = parser.isoparse(data_ultima_venda_str).date()
                if data_ref > data_hoje:
                    data_ref = data_hoje
            else:
                data_ref = data_hoje

            # Anti-pico: se a janela desde a ultima compra for muito curta (< gatilho),
            # recua o inicio para a PENULTIMA NF de entrada, diluindo o pico de periodo curto.
            data_inicio = data_ultima_compra
            dias_janela = (data_ref - data_ultima_compra).days
            penu = penultimo_map.get(produto_id) or {}
            data_penultima_str = penu.get('data_penultima')
            if dias_janela < GATILHO_PENULTIMA_DIAS and data_penultima_str:
                data_penultima = parser.isoparse(str(data_penultima_str)).date()
                if data_penultima < data_inicio:
                    data_inicio = data_penultima

            # Janela MINIMA: recua o inicio ate completar JANELA_MINIMA_DIAS.
            # Vem DEPOIS do anti-pico da penultima NF (que pode nao ser suficiente
            # quando as entregas sao frequentes). Recuar o INICIO, e nao empurrar o
            # fim, preserva a regra de ruptura: o fim continua sendo a ultima venda,
            # entao dias sem estoque seguem fora da conta.
            if (data_ref - data_inicio).days < JANELA_MINIMA_DIAS:
                data_inicio = data_ref - timedelta(days=JANELA_MINIMA_DIAS)

            quantidade_vendida = fetch_quantidade_vendida(
                produto_id,
                data_inicio.isoformat(),
                data_ref.isoformat()
            )

            calc = calcular_sugestao_produto(
                item_por_caixa=produto.get('itens_por_caixa') or 1,
                data_ultima_compra=data_inicio,
                data_hoje=data_ref,
                estoque_atual=estoque_atual,
                qtd_vendida_periodo=quantidade_vendida,
                prazo_entrega=politica.get('prazo_entrega'),
                prazo_estoque=politica.get('prazo_estoque'),
            )
            sugestao_quantidade = calc['sugestao_quantidade']
            periodo_venda = calc['periodo_venda']
            multiplicacao = calc['multiplicacao_aplicada']

            if sugestao_quantidade <= 0:
                continue

            valor_total_produto, valor_total_produto_com_desconto = calcular_valores(produto, politica, sugestao_quantidade)

            valor_total_pedido += valor_total_produto
            valor_total_pedido_com_desconto += valor_total_produto_com_desconto
            quantidade_produtos += 1

            if produto_id in id_produto_bling_cache:
                id_produto_bling = id_produto_bling_cache[produto_id]
            else:
                id_produto_bling = fetch_id_produto_bling(produto_id)
                id_produto_bling_cache[produto_id] = id_produto_bling

            produtos_array.append(montar_detalhes_produto(
                produto,
                quantidade_vendida,
                periodo_venda,
                sugestao_quantidade,
                valor_total_produto,
                valor_total_produto_com_desconto,
                multiplicacao,
                id_produto_bling,
                data_ultima_venda_str,
                data_ultima_compra_str
            ))

        if valor_total_pedido >= (politica.get('valor_minimo') or 0):
            politica_compra = montar_politica_compra(
                politica,
                valor_total_pedido,
                valor_total_pedido_com_desconto,
                quantidade_produtos,
                False,  # melhor_politica será definido depois
                produtos_array
            )
            resultado.append(politica_compra)

    # Determinar melhor política entre as que atingiram valor mínimo
    if resultado:
        melhor_politica_id = find_best_policy_among_results_simple(resultado)
        for politica_compra in resultado:
            if politica_compra['politica_id'] == melhor_politica_id:
                politica_compra['melhor_politica'] = True

    return resultado

def ajustar_data_futura(data: datetime) -> datetime:
    return min(data, datetime.now().date())

def ajustar_data_compra(data_ultima_compra_str: str, data_ultima_venda: datetime, politica: Dict) -> datetime:
    prazo_estoque = politica.get('prazo_estoque') or 30

    if not data_ultima_compra_str:
        return data_ultima_venda - timedelta(days=prazo_estoque)

    data_ultima_compra = parser.isoparse(data_ultima_compra_str).date()

    if data_ultima_compra >= data_ultima_venda:
        return data_ultima_venda - timedelta(days=prazo_estoque)

    return data_ultima_compra

def fetch_quantidade_vendida(produto_id: int, data_inicio: str, data_fim: str) -> float:
    url = f"{API_URL_BASE}/rest/v1/rpc/get_quantidade_vendida"
    payload = {
        "p_produto_id": produto_id,
        "data_inicio": data_inicio,
        "data_fim": data_fim
    }
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code != 200:
        logger.error(f"Erro ao buscar quantidade vendida: {response.text}")
        raise HTTPException(status_code=response.status_code, detail=f"Erro ao buscar quantidade vendida")

    result = response.json()
    return float(result[0]['quantidade_vendida']) if result else 0

def calcular_sugestao(produto: Dict, politica: Dict, media_venda_dia: float, quantidade_vendida: float) -> tuple:
    itens_por_caixa = produto.get('itens_por_caixa') or 1
    estoque_atual = produto.get('estoque_atual') or 0
    prazo_estoque = politica.get('prazo_estoque') or 0

    sugestao_quantidade = media_venda_dia * prazo_estoque - estoque_atual

    # Margem de segurança - aplicar APENAS quando estoque zerado
    if estoque_atual == 0:
        margem_seguranca = 1.25  # 25% a mais
        sugestao_quantidade = sugestao_quantidade * margem_seguranca

    if sugestao_quantidade <= 0:
        return 0, False

    # Ajustar por embalagem - SEMPRE ARREDONDAR PARA CIMA para o próximo múltiplo de itens_por_caixa
    if itens_por_caixa > 1:
        sugestao_quantidade = round_up_to_multiple(sugestao_quantidade, itens_por_caixa)
    else:
        sugestao_quantidade = round(sugestao_quantidade)

    multiplicacao = False

    # REGRA 8a: produto zerado com demanda no periodo → garante pelo menos 1 caixa
    # Resolve casos de baixa rotacao onde round() zerava a sugestao mesmo tendo
    # havido vendas (ex: 3 vendas em 170 dias com prazo 22d). Para produtos
    # unitarios (itens_por_caixa=1) isso garante 1 unidade.
    if estoque_atual == 0 and quantidade_vendida > 0 and sugestao_quantidade < itens_por_caixa:
        sugestao_quantidade = itens_por_caixa
        multiplicacao = True

    # Regra existente: produto vendido em caixa garante 1 caixa
    unidade_produto = produto.get('unidade', 'UN').upper()
    eh_produto_caixa = unidade_produto not in ['UN', 'UNT']

    if eh_produto_caixa and sugestao_quantidade > 0:
        sugestao_quantidade = max(sugestao_quantidade, itens_por_caixa)
        multiplicacao = True

    return sugestao_quantidade, multiplicacao

def calcular_valores(produto: Dict, politica: Dict, sugestao_quantidade: float) -> tuple:
    preco_custo = produto.get('precocusto')
    valor_de_compra = preco_custo if preco_custo is not None else (produto.get('valor_de_compra') or 0)
    valor_total_produto = sugestao_quantidade * valor_de_compra
    desconto = politica.get('desconto') or 0
    valor_total_produto_com_desconto = valor_total_produto * (1 - desconto / 100)
    return valor_total_produto, valor_total_produto_com_desconto

def montar_detalhes_produto(produto: Dict, quantidade_vendida: float, periodo_venda: int,
                          sugestao_quantidade: float, valor_total_produto: float,
                          valor_total_produto_com_desconto: float, multiplicacao: bool,
                          id_produto_bling: int, data_ultima_venda_str: str,
                          data_ultima_compra_str: str) -> Dict:
    return {
        'produto_id': produto['produto_id'],
        'codigo_do_produto': produto.get('codigo_produto'),
        'nome_produto': produto.get('nome_produto'),
        'quantidade_vendida': quantidade_vendida,
        'periodo_venda': periodo_venda,
        'sugestao_quantidade': sugestao_quantidade,
        'valor_total_produto': valor_total_produto,
        'valor_total_produto_com_desconto': valor_total_produto_com_desconto,
        'valor_de_compra': produto.get('valor_de_compra') or produto.get('precocusto') or 0,
        'estoque_atual': produto.get('estoque_atual') or 0,
        'data_ultima_venda': data_ultima_venda_str,
        'data_ultima_compra': data_ultima_compra_str,
        'itens_por_caixa': produto.get('itens_por_caixa') or 1,
        'multiplicacao_aplicada': multiplicacao,
        'id_produto_bling': id_produto_bling
    }

def montar_politica_compra(politica: Dict, valor_total_pedido: float, 
                          valor_total_pedido_com_desconto: float, quantidade_produtos: int, 
                          melhor_politica: bool, produtos_array: list) -> Dict:
    return {
        'politica_id': politica.get('id'),
        'desconto': politica.get('desconto'),
        'bonificacao': politica.get('bonificacao'),
        'valor_minimo': politica.get('valor_minimo'),
        'prazo_estoque': politica.get('prazo_estoque'),
        'melhor_politica': melhor_politica,
        'quantidade_produtos': quantidade_produtos,
        'valor_total_pedido_sem_desconto': valor_total_pedido,
        'valor_total_pedido_com_desconto': valor_total_pedido_com_desconto,
        'produtos': produtos_array
    }

def find_best_policy_among_results_simple(resultado: List[Dict]) -> Optional[int]:
    """Versão simples: encontra melhor política entre as incluídas no resultado"""
    if not resultado:
        return None

    melhor_desconto = 0
    menor_prazo = float('inf')
    melhor_politica_id = None

    for item in resultado:
        # Usar 'or' para tratar None como 0 (desconto pode existir mas ser None)
        desconto = item.get('desconto') or 0
        prazo_estoque = item.get('prazo_estoque') or float('inf')

        if desconto > melhor_desconto or (desconto == melhor_desconto and prazo_estoque < menor_prazo):
            melhor_desconto = desconto
            menor_prazo = prazo_estoque
            melhor_politica_id = item['politica_id']

    return melhor_politica_id

def find_best_policy(politicas: List[Dict]) -> int:
    melhor_desconto = 0
    menor_prazo = float('inf')
    melhor_politica_id = None

    for politica in politicas:
        desconto = politica.get('desconto') or 0
        prazo_estoque = politica.get('prazo_estoque') or float('inf')

        if desconto > melhor_desconto or (desconto == melhor_desconto and prazo_estoque < menor_prazo):
            melhor_desconto = desconto
            menor_prazo = prazo_estoque
            melhor_politica_id = politica.get('id')

    return melhor_politica_id
