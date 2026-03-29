import asyncio
import json
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import QueryRun, Finding, DailyUsage
from providers import detect_entity_type, normalize_query, validate_query, providers_for
from report_builder import build_pdf, build_txt

def dedup_findings(findings: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in findings:
        sig = (item["module_name"], item["kind"], item["fact_key"], item["fact_value"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(item)
    return out

def entity_linking(entity_type: str, normalized: str, findings: list[dict]) -> list[dict]:
    links = []
    if entity_type == "ip":
        ports = [f["fact_value"] for f in findings if f["fact_key"] == "ports"]
        if ports:
            links.append({"type": "ip_to_services", "value": normalized})
    if entity_type == "domain":
        hits = [f for f in findings if f["module_name"] == "censys_domain" and f["fact_key"] == "hits"]
        if hits:
            links.append({"type": "domain_to_ips", "value": normalized})
    return links

def confidence_scoring(findings: list[dict], provider_results: list[dict]) -> float:
    if not provider_results:
        return 0.0
    success = sum(1 for p in provider_results if p.get("ok"))
    ratio = success / max(len(provider_results), 1)
    bonus = min(len(findings) / 50.0, 0.2)
    return round(min(ratio * 0.8 + bonus, 1.0), 3)

def risk_scoring(entity_type: str, provider_results: list[dict]) -> float:
    score = 0.0
    if entity_type == "ip":
        for item in provider_results:
            if item.get("provider") == "vt_ip" and item.get("data", {}).get("last_analysis_stats", {}).get("malicious", 0) > 0:
                score += 0.4
            if item.get("provider") == "shodan_ip" and len(item.get("data", {}).get("ports", [])) > 8:
                score += 0.2
    if entity_type == "domain":
        for item in provider_results:
            if item.get("provider") == "vt_domain" and item.get("data", {}).get("last_analysis_stats", {}).get("malicious", 0) > 0:
                score += 0.4
    if entity_type == "hash":
        for item in provider_results:
            if item.get("provider") == "vt_hash" and item.get("data", {}).get("last_analysis_stats", {}).get("malicious", 0) > 0:
                score += 0.6
    return round(min(score, 1.0), 3)

def provider_result_to_findings(provider: str, result: dict, is_fallback: bool = False) -> list[dict]:
    findings = []
    if result.get("ok"):
        for k, v in result.get("data", {}).items():
            findings.append({
                "module_name": provider,
                "kind": "enrichment",
                "fact_key": k,
                "fact_value": json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v),
                "label": k,
                "source_url": None,
                "confidence": 0.95,
                "is_fallback": is_fallback,
                "raw_json": json.dumps(result.get("data", {}), ensure_ascii=False),
            })
    else:
        findings.append({
            "module_name": provider,
            "kind": "error",
            "fact_key": "error",
            "fact_value": result.get("error", "unknown_error"),
            "label": "provider_error",
            "source_url": None,
            "confidence": 1.0,
            "is_fallback": is_fallback,
            "raw_json": json.dumps(result, ensure_ascii=False),
        })
    return findings

async def check_health(entity_type: str, normalized: str) -> dict:
    providers = providers_for(entity_type)
    out = {}
    for provider in providers:
        res = await provider.execute(normalized)
        out[provider.name] = {"ok": res.ok, "error": res.error}
    return out

async def enforce_limit(session: AsyncSession, user_id: int, limit: int) -> None:
    from datetime import date
    today = date.today()
    row = await session.scalar(select(DailyUsage).where(DailyUsage.user_id == user_id, DailyUsage.usage_date == today))
    if row is None:
        row = DailyUsage(user_id=user_id, usage_date=today, count=0)
        session.add(row)
        await session.flush()
    if row.count >= limit:
        raise ValueError("daily_limit_exceeded")
    row.count += 1
    await session.flush()

async def run_pipeline(session: AsyncSession, user_id: int, raw_query: str, plan_code: str = "FREE", daily_limit: int = 3) -> QueryRun:
    entity_type = detect_entity_type(raw_query)
    normalized = normalize_query(raw_query)
    validate_query(entity_type, normalized)

    await enforce_limit(session, user_id, daily_limit)

    qr = QueryRun(
        user_id=user_id,
        raw_query=raw_query,
        entity_type=entity_type,
        normalized_query=normalized,
        status="running",
        plan_code=plan_code,
        started_at=datetime.utcnow(),
    )
    session.add(qr)
    await session.flush()

    providers = providers_for(entity_type)
    provider_results = []
    all_findings = []

    for provider in providers:
        res = await provider.execute(normalized)
        provider_results.append({"provider": provider.name, "ok": res.ok, "data": res.data, "error": res.error, "status_code": res.status_code})
        all_findings.extend(provider_result_to_findings(provider.name, {"ok": res.ok, "data": res.data, "error": res.error, "status_code": res.status_code}))

    clean_findings = dedup_findings(all_findings)
    links = entity_linking(entity_type, normalized, clean_findings)
    confidence = confidence_scoring(clean_findings, provider_results)
    risk = risk_scoring(entity_type, provider_results)

    for item in clean_findings:
        session.add(Finding(
            query_run_id=qr.id,
            module_name=item["module_name"],
            kind=item["kind"],
            fact_key=item["fact_key"],
            fact_value=item["fact_value"],
            label=item["label"],
            source_url=item["source_url"],
            confidence=item["confidence"],
            is_fallback=item["is_fallback"],
            raw_json=item["raw_json"],
        ))

    dossier = {
        "query": {
            "raw": raw_query,
            "entity_type": entity_type,
            "normalized": normalized,
            "created_at": datetime.utcnow().isoformat(),
        },
        "summary": {
            "title": "NEXARA DOSSIER",
            "short": f"Processed {entity_type} query with {len(provider_results)} provider(s).",
            "confidence": confidence,
            "risk_score": risk,
        },
        "providers": provider_results,
        "links": links,
        "findings_count": len(clean_findings),
    }

    txt_path = build_txt(normalized, dossier)
    pdf_path = build_pdf(normalized, dossier)

    qr.status = "done"
    qr.finished_at = datetime.utcnow()
    qr.summary_text = dossier["summary"]["short"]
    qr.risk_score = risk
    qr.confidence_score = confidence
    qr.dossier_json = json.dumps(dossier, ensure_ascii=False)
    qr.txt_path = txt_path
    qr.pdf_path = pdf_path

    await session.commit()
    await session.refresh(qr)
    return qr
