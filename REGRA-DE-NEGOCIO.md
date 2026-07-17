# Regra de Negócio

> Serviço que centraliza as **regras de negócio** do FlowB2B.
> (Antes chamado internamente de `validacao_ean` — nome que não refletia o que ele faz.)

Este serviço (Python/FastAPI, deploy no Render) reúne três funções de negócio:

## 1. Valida EAN (código de barras)
Valida o código de barras EAN-13 dos produtos (dígito verificador e formato), garantindo
que o cadastro do produto está com um código válido antes de usá-lo nas integrações.

## 2. Scraping de preço de concorrente (Cobasi / Petz)
Busca o preço do mesmo produto nos concorrentes (Cobasi via BeautifulSoup, Petz via
Selenium) a partir do EAN/nome, para comparação de preço e apoio à precificação.

## 3. Sugestão de pedido de compra — **a regra de compras**
Calcula, por produto, **quanto comprar** de cada fornecedor. É o coração do serviço.
Arquivo: `calculo_pedido_auto_otimizado.py` → função `calcular_sugestao_produto`.

Como decide (regra fechada com a Emilly):
- **Venda real** do sistema (nunca "compras − estoque", que distorce quando havia estoque).
- **Última compra** = data de **entrada real da NF** (`data_operacao`), não pedido de compra.
- **Janela de medição:** com estoque → até hoje; em **ruptura (estoque 0) → última venda**
  (os dias sem estoque não contam).
- **Anti-pico:** se a janela desde a última compra for **< 7 dias**, recua para a
  **penúltima NF de entrada** (dilui pico de período curto). Sem penúltima → sinaliza "revisar".
- **Cobertura** = prazo de entrega + prazo de estoque (do cadastro/`politica_compra`).
- Sugestão = média/dia × cobertura − estoque; **×1,25 se estoque = 0** (ruptura);
  **pedido mínimo 2**; arredonda para cima pelo múltiplo da **caixa**.
- Escolhe o **fornecedor mais barato** (por preço efetivo) para cada produto.

Funções de apoio no banco (Supabase): `get_max_data_entrada_op` (última entrada por
data de operação da NF), `get_penultimo_nf_entrada` (penúltima entrada, para o anti-pico),
`get_max_data_saida` (última venda), `fetch_quantidade_vendida` (venda real do período).
