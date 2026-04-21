"""Stage 8: Sonnet-generated PT-BR report + CSV dumps.

See TECH_PLAN.md §M1-T6. Full-data version deepens in M2-S8-T1 but
the skeleton always runs — Sonnet produces all 7 sections, with
"(sem dados — stub)" placeholders where inputs are empty.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path
from typing import Any

from src.context import Context
from src.schemas import (
    Aggregation,
    ConversationConversion,
    SpaTemplate,
    TemplateSentiment,
    Turnaround,
)

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 6000

SECTION_HEADERS = [
    "## 1. Resumo executivo",
    "## 2. Análise por etapa do script",
    "## 3. O que dizemos que funciona (top 10 templates positivos)",
    "## 4. O que dizemos que pode melhorar (top 10 templates negativos)",
    "## 5. Viradas de jogo (top 20 turnarounds)",
    "## 6. Padrões de argumentação vencedora",
    "## 7. Lacunas no script",
]

CSV_FILES: dict[str, list[str]] = {
    "turnarounds.csv": [
        "telefone", "data", "tipo_objecao",
        "mensagem_cliente", "resposta_vencedora", "confirmacao",
    ],
    "lost_deals.csv": [
        "telefone", "data", "tipo_objecao",
        "mensagem_cliente", "resposta_vencedora", "confirmacao",
    ],
    "per_step.csv": [
        "step_id", "on_script_count", "off_script_count",
        "top_intents", "top_objections", "top_clusters",
    ],
    "spa_templates_scored.csv": [
        "template_id", "canonical_text", "instance_count",
        "warmth", "clarity", "script_adherence", "polarity", "critique",
    ],
    "off_script_clusters.csv": [
        "step_id", "medoid_text", "size", "example_msg_ids",
    ],
}


_HEADER_PREFIXES = [f"## {i}." for i in range(1, 8)]


def _normalize_headers(text: str) -> str:
    """Rewrite any `## N. ...` line to the canonical SECTION_HEADERS[N-1].

    Model output usually gets titles right but occasionally paraphrases or
    mis-spells one (observed: "Viadas" → "Viradas"). We trust the numeric
    prefix and overwrite the rest of the H2 line.
    """
    canonical = {p: SECTION_HEADERS[i] for i, p in enumerate(_HEADER_PREFIXES)}
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        matched = False
        for prefix, want in canonical.items():
            if stripped.startswith(prefix):
                out_lines.append(want)
                matched = True
                break
        if not matched:
            out_lines.append(line)
    # Ensure any missing header is appended at the end with a stub body.
    present = set()
    for line in out_lines:
        for i, h in enumerate(SECTION_HEADERS):
            if line == h:
                present.add(i)
    for i, h in enumerate(SECTION_HEADERS):
        if i not in present:
            out_lines.extend(["", h, "(sem dados — stub)"])
    return "\n".join(out_lines) + ("\n" if not text.endswith("\n") else "")


def _load_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def _load_jsonl(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _score_template(tpl: dict, sent: dict | None) -> float:
    if sent is None:
        return 0.0
    pol = {"pos": 1, "neu": 0, "neg": -1}.get(sent.get("polarity", "neu"), 0)
    return pol * tpl.get("instance_count", 0) * sent.get("warmth", 0)


def _build_prompt_payload(ctx: Context) -> dict:
    d = ctx.data_dir

    templates = _load_json(d / "spa_templates.json")
    tpl_by_id = {t["template_id"]: t for t in templates}
    sentiments = _load_json(d / "template_sentiment.json")
    sent_by_id = {s["template_id"]: s for s in sentiments}
    conversions = _load_jsonl(d / "conversions.jsonl")
    turnarounds = _load_json(d / "turnarounds.json")
    lost_deals = _load_json(d / "lost_deals.json")
    aggregations = _load_json(d / "aggregations.json")

    scored = []
    for tpl in templates:
        s = sent_by_id.get(tpl["template_id"])
        scored.append({
            "template_id": tpl["template_id"],
            "canonical_text": tpl["canonical_text"],
            "instance_count": tpl["instance_count"],
            "sentiment": s,
            "score": _score_template(tpl, s),
        })
    scored_pos = sorted(scored, key=lambda x: x["score"], reverse=True)[:10]
    scored_neg = sorted(scored, key=lambda x: x["score"])[:10]

    top_turnarounds = turnarounds[:20]

    return {
        "volumes": {
            "conversations": len(conversions),
            "templates": len(templates),
            "turnarounds": len(turnarounds),
            "lost_deals": len(lost_deals),
        },
        "per_step": aggregations.get("per_step", {}),
        "off_script_clusters_global": aggregations.get("off_script_clusters", []),
        "top_positive_templates": scored_pos,
        "top_negative_templates": scored_neg,
        "top_turnarounds": top_turnarounds,
        "lost_deals_sample": lost_deals[:20],
        "conversion_summary": {
            "booked": sum(1 for c in conversions if c.get("final_outcome") == "booked"),
            "lost": sum(1 for c in conversions if c.get("final_outcome") == "lost"),
            "ambiguous": sum(1 for c in conversions if c.get("final_outcome") == "ambiguous"),
        },
    }


def _write_csvs(ctx: Context,
                turnarounds: list[dict],
                lost_deals: list[dict],
                aggregations: dict,
                templates: list[dict],
                sent_by_id: dict) -> list[Path]:
    out_dir = ctx.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    def _write(name: str, header: list[str], rows: list[list]):
        p = out_dir / name
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        paths.append(p)

    def _turn_row(t: dict) -> list:
        return [
            t.get("phone", ""),
            t.get("date", ""),
            t.get("objection_type", ""),
            t.get("customer_message", ""),
            t.get("winning_reply", ""),
            t.get("confirmation", ""),
        ]

    _write("turnarounds.csv", CSV_FILES["turnarounds.csv"], [_turn_row(t) for t in turnarounds])
    _write("lost_deals.csv", CSV_FILES["lost_deals.csv"], [_turn_row(t) for t in lost_deals])

    per_step_rows = []
    for step_id, agg in sorted((aggregations.get("per_step") or {}).items()):
        per_step_rows.append([
            step_id,
            agg.get("on_script_count", 0),
            agg.get("off_script_count", 0),
            json.dumps(agg.get("top_intents", []), ensure_ascii=False),
            json.dumps(agg.get("top_objections", []), ensure_ascii=False),
            json.dumps(agg.get("top_clusters", []), ensure_ascii=False),
        ])
    _write("per_step.csv", CSV_FILES["per_step.csv"], per_step_rows)

    tpl_rows = []
    for tpl in templates:
        s = sent_by_id.get(tpl["template_id"], {})
        tpl_rows.append([
            tpl["template_id"],
            tpl["canonical_text"],
            tpl["instance_count"],
            s.get("warmth", ""),
            s.get("clarity", ""),
            s.get("script_adherence", ""),
            s.get("polarity", ""),
            s.get("critique", ""),
        ])
    _write("spa_templates_scored.csv", CSV_FILES["spa_templates_scored.csv"], tpl_rows)

    off_rows = []
    for c in aggregations.get("off_script_clusters", []) or []:
        off_rows.append([
            c.get("step_id", ""),
            c.get("medoid_text", ""),
            c.get("size", 0),
            json.dumps(c.get("example_msg_ids", []), ensure_ascii=False),
        ])
    _write("off_script_clusters.csv", CSV_FILES["off_script_clusters.csv"], off_rows)

    return paths


def _prompt_text(ctx: Context) -> str:
    p = ctx.prompts_dir / "stage8_report.md"
    if not p.exists():
        raise FileNotFoundError(f"missing prompt file: {p}")
    return p.read_text(encoding="utf-8")


def run(ctx: Context) -> dict:
    t0 = time.time()

    payload = _build_prompt_payload(ctx)
    system = _prompt_text(ctx)

    user_msg = (
        "Dados agregados da análise (JSON). Se um bloco vier vazio, marque a seção correspondente como `(sem dados — stub)`.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    cost_before = 0.0
    if ctx.client is not None:
        cost_before = ctx.client.get_usage_report()["api"]["cost_usd"]

    text = ctx.client.complete(
        model=MODEL,
        messages=[{"role": "user", "content": user_msg}],
        system=system,
        max_tokens=MAX_OUTPUT_TOKENS,
    )

    if not isinstance(text, str):
        raise RuntimeError(f"expected text response, got {type(text).__name__}")

    # Python-side guard: model occasionally paraphrases or mis-spells H2 titles
    # (e.g. "Viadas" vs "Viradas"). Section bodies are all the model's content —
    # we only rewrite the header line itself to the canonical spec.
    text = _normalize_headers(text)

    report_path = ctx.output_dir / "report.md"
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")

    d = ctx.data_dir
    turnarounds = _load_json(d / "turnarounds.json")
    lost_deals = _load_json(d / "lost_deals.json")
    aggregations = _load_json(d / "aggregations.json")
    templates = _load_json(d / "spa_templates.json")
    sentiments = _load_json(d / "template_sentiment.json")
    sent_by_id = {s["template_id"]: s for s in sentiments}
    csv_paths = _write_csvs(ctx, turnarounds, lost_deals, aggregations, templates, sent_by_id)

    cost_after = 0.0
    if ctx.client is not None:
        cost_after = ctx.client.get_usage_report()["api"]["cost_usd"]
    api_delta = max(0.0, cost_after - cost_before)

    log.info("stage8: report → %s (%d bytes) + %d CSVs", report_path, len(text), len(csv_paths))
    return {
        "stage": 8,
        "outputs": [report_path, *csv_paths],
        "llm_usd_max": 0.0,
        "llm_usd_api": api_delta,
        "elapsed_s": time.time() - t0,
    }
