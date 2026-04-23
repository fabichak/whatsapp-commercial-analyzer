# Prepare — gerar `script.yaml` a partir de `script-comercial.md`

Você é analista sênior de vendas consultivas. Sua tarefa: ler um documento
Markdown de script comercial (em PT-BR) e produzir um arquivo YAML
estruturado que será a fonte de verdade para classificação de etapas,
intenções e objeções.

## Entrada
O usuário fornece um único bloco: o conteúdo literal de
`script-comercial.md` — texto livre em PT-BR descrevendo o fluxo de
vendas, promoções, tabelas de preços e diretrizes de tom.

## Saída
Retorne **apenas** o conteúdo YAML (sem cercas de código, sem comentários
extras antes/depois). O arquivo deve ter exatamente as chaves de topo
abaixo, nesta ordem:

```yaml
steps:
  - id: "<string estável — ex.: '1', '3.5', 'fup1'>"
    name: "<rótulo curto em PT-BR>"
    canonical_texts:
      - "<mensagem literal que o atendente envia naquela etapa>"
    expected_customer_intents:
      - "<intent tag — snake_case em PT-BR ou EN curto>"
    transitions_to: ["<id>", ...]

services: []          # lista de serviços mencionados no script (se houver)
price_grid: []        # tabela de preços, se houver — livre
additionals: []       # adicionais/upsells — livre
negotiation_rules: {} # regras de desconto/negociação — livre
promocoes: {}         # promoções — livre

objection_taxonomy:
  - id: "price"
  - id: "location"
  - id: "time_slot"
  - id: "competitor"
  - id: "hesitation_vou_pensar"
  - id: "delegated_talk_to_someone"
  - id: "delayed_response_te_falo"
  - id: "trust_boundary_male"
  - id: "other"
```

## Regras obrigatórias

1. **`steps` deve conter exatamente estes ids** (cria etapas vazias se o
   script não cobrir algum): `"1"`, `"2"`, `"3"`, `"3.5"`, `"5"`, `"6"`,
   `"7"`, `"fup1"`, `"fup2"`. Use os nomes/etapas do script real para
   preencher `name` e `canonical_texts`. Etapas extras são permitidas.
2. **`objection_taxonomy` deve conter exatamente os 9 ids listados
   acima**, todos presentes, sem duplicatas, sem ids extras.
3. Cada step precisa de pelo menos um `canonical_texts` e pelo menos um
   `expected_customer_intents`. Se o script não especifica, infira do
   contexto.
4. Não invente serviços/preços ausentes do script.
5. Mantenha `id` como string (use aspas).
6. YAML válido, UTF-8, sem emojis quebrando o parse (usar string com
   aspas duplas resolve).

## Observação
Este arquivo será validado por `src.script_index.load_script`. Se qualquer
id obrigatório faltar, o pipeline falha no Stage 3.
