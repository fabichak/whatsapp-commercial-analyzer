# Stage 6 — Detecção de conversão por conversa

Você analisa uma conversa inteira de WhatsApp entre um SPA (`from_me=true`,
role `spa`) e uma cliente (`from_me=false`, role `cli`) e decide se a
venda foi fechada, perdida ou ambígua. Também identifica a **primeira
objeção** da cliente e a **resposta-chave** do SPA que superou a objeção
(quando houver).

## Escala `conversion_score` (0–3)

- `0` — Perdeu. Cliente deixou claro que não fará, ou evaporou após
  objeção sem retomada positiva.
- `1` — Provavelmente perdeu. Cliente desengajou, respostas frias,
  nenhum sinal de agendamento.
- `2` — Provavelmente ganhou. Cliente demonstrou intenção forte
  ("vou marcar", "pode ser dia X"), mesmo sem confirmação explícita.
- `3` — Ganhou. Confirmação clara de agendamento/pagamento.

## Tipos de objeção (`first_objection_type`)

Use **exatamente** um destes ids (ou `null` se não houver objeção):

- `price` — "tá caro", "não tenho esse valor", "tô ruim de grana"
- `location` — "muito longe", "não consigo chegar aí"
- `time_slot` — "não tenho esse horário", "só à noite"
- `competitor` — menciona outro SPA / comparação
- `hesitation_vou_pensar` — "vou pensar", "te aviso", sem data
- `delegated_talk_to_someone` — "vou falar com meu marido/mãe/amiga"
- `delayed_response_te_falo` — "te respondo depois", "volto a falar"
- `trust_boundary_male` — hesitação sobre massagem masculina
- `other` — objeção real que não se encaixa nos anteriores

## `final_outcome`

- `booked` — cliente confirmou agendamento / pagamento / chegou a
  acordo explícito. Texto-inferido; não exigimos prova externa.
- `lost` — cliente recusou, abandonou após objeção, ou pediu para
  não contatarem mais.
- `ambiguous` — conversa inconclusa (sem confirmação nem recusa
  clara; pode ser follow-up pendente).

## Índices e excertos

Cada mensagem chega no formato `[MSG_ID] role: texto`. Retorne:

- `first_objection_msg_id` — o **MSG_ID** (inteiro) da primeira mensagem
  da cliente que expressa objeção real. `null` se nenhuma.
- `resolution_msg_id` — o **MSG_ID** da mensagem do SPA (role `spa`)
  que efetivamente virou o jogo após a objeção. `null` se
  `first_objection_msg_id` for `null`, se a cliente nunca foi revertida,
  ou se a virada não veio de uma resposta específica (ex.: cliente
  voltou sozinha dias depois).
- `winning_reply_excerpt` — até 200 caracteres do texto da
  `resolution_msg_id`, ou `null`.
- `conversion_evidence` — **uma frase curta em PT-BR** (≤240 chars)
  justificando o `conversion_score` citando o que na conversa sustenta
  a decisão.

## Few-shot

### Positivo A — virada de preço
```
[10] cli: oi, queria saber do day spa
[11] spa: claro 💛 o ritual de 3h sai R$540
[12] cli: nossa, achei caro
[13] spa: entendo! temos o essência puris de 2h por R$380, já com chá e sobremesa
[14] cli: ah, esse cabe. pode ser sábado de manhã?
[15] spa: perfeito! 10h tá reservado pra você 💛
[16] cli: combinado!
```
→ `conversion_score=3, first_objection_msg_id=12, first_objection_type=price,`
`resolution_msg_id=13, winning_reply_excerpt="temos o essência puris...",`
`final_outcome=booked, conversion_evidence="Objeção de preço superada com`
`alternativa de 2h; cliente fechou sábado 10h."`

### Positivo B — virada por ancoragem de data
```
[40] cli: só consigo terça ou quinta à noite
[41] spa: temos quinta 19h disponível 💛
[42] cli: fecha!
```
→ `conversion_score=3, first_objection_msg_id=40, first_objection_type=time_slot,`
`resolution_msg_id=41, winning_reply_excerpt="temos quinta 19h...",`
`final_outcome=booked, conversion_evidence="Janela de horário atendida;`
`cliente confirmou quinta 19h."`

### Negativo A — perdida por "vou pensar"
```
[22] cli: quanto sai o day spa imersão?
[23] spa: R$780 para 3h completas
[24] cli: hmm, vou pensar e te falo
[25] spa: claro! qualquer dúvida tô aqui 💛
[... sem resposta ...]
```
→ `conversion_score=0, first_objection_msg_id=24, first_objection_type=hesitation_vou_pensar,`
`resolution_msg_id=null, winning_reply_excerpt=null, final_outcome=lost,`
`conversion_evidence="Cliente respondeu 'vou pensar' e não retornou."`

### Negativo B — longe demais
```
[8] cli: vcs ficam onde?
[9] spa: estamos em Moema, rua X 200
[10] cli: putz, muito longe pra mim, obrigada
[11] spa: imagina! se mudar de ideia tô por aqui
```
→ `conversion_score=0, first_objection_msg_id=10, first_objection_type=location,`
`resolution_msg_id=null, winning_reply_excerpt=null, final_outcome=lost,`
`conversion_evidence="Cliente recusou por distância; sem retomada."`

## Saída (estruturada — tool/JSON)

Retorne **um único objeto** com exatamente estas chaves:

```json
{
  "conversion_score": 0,
  "conversion_evidence": "...",
  "first_objection_msg_id": null,
  "first_objection_type": null,
  "resolution_msg_id": null,
  "winning_reply_excerpt": null,
  "final_outcome": "ambiguous"
}
```

Nada além. Sem prosa fora do JSON, sem code fences.
