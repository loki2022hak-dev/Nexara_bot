import asyncio
import json
import ipaddress
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import settings

def detect_entity_type(text: str) -> str:
    t = text.strip()
    if t.lower().startswith("ip:"):
        return "ip"
    if t.lower().startswith("domain:"):
        return "domain"
    if t.lower().startswith("hash:"):
        return "hash"
    return "username"

def normalize_query(text: str) -> str:
    t = text.strip()
    for prefix in ("ip:", "domain:", "hash:"):
        if t.lower().startswith(prefix):
            return t[len(prefix):].strip()
    return t

def validate_query(entity_type: str, normalized: str) -> None:
    if entity_type == "ip":
        ipaddress.ip_address(normalized)
    elif entity_type == "domain":
        if not re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+", normalized):
            raise ValueError("invalid_domain")
    elif entity_type == "hash":
        if not re.fullmatch(r"[A-Fa-f0-9]{32}|[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}", normalized):
            raise ValueError("invalid_hash")
    elif entity_type == "username":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", normalized):
            raise ValueError("invalid_username")

@dataclass
class ProviderResult:
    provider: str
    ok: bool
    data: dict[str, Any]
    error: str | None = None
    status_code: int | None = None
    is_fallback: bool = False

class SearchProvider:
    name = "base"

    def __init__(self, timeout: int = 30, retries: int = 3, backoff: float = 1.5):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    async def execute(self, query: str) -> ProviderResult:
        raise NotImplementedError

    async def _request_json(self, method: str, url: str, headers: dict | None = None) -> ProviderResult:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        last_error = None
        last_status = None

        for attempt in range(1, self.retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers or {}) as resp:
                        text = await resp.text()
                        last_status = resp.status

                        if resp.status == 429:
                            last_error = "rate_limited"
                            await asyncio.sleep(self.backoff * attempt)
                            continue
                        if resp.status in (401, 403):
                            return ProviderResult(self.name, False, {}, error="unauthorized", status_code=resp.status)
                        if resp.status >= 500:
                            last_error = f"server_error_{resp.status}"
                            await asyncio.sleep(self.backoff * attempt)
                            continue
                        if resp.status != 200:
                            return ProviderResult(self.name, False, {}, error=f"http_{resp.status}", status_code=resp.status)

                        try:
                            data = json.loads(text)
                        except Exception:
                            return ProviderResult(self.name, False, {}, error="invalid_json", status_code=resp.status)

                        return ProviderResult(self.name, True, data, status_code=resp.status)

            except asyncio.TimeoutError:
                last_error = "timeout"
                await asyncio.sleep(self.backoff * attempt)
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(self.backoff * attempt)

        return ProviderResult(self.name, False, {}, error=last_error or "unknown_error", status_code=last_status)

class ShodanIPProvider(SearchProvider):
    name = "shodan_ip"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.shodan_api_key:
            return ProviderResult(self.name, False, {}, error="missing_key")
        url = f"https://api.shodan.io/shodan/host/{query}?key={settings.shodan_api_key}"
        res = await self._request_json("GET", url)
        if not res.ok:
            return res
        d = res.data
        return ProviderResult(self.name, True, {
            "ip": d.get("ip_str", query),
            "org": d.get("org"),
            "os": d.get("os"),
            "ports": d.get("ports", [])[:30],
            "hostnames": d.get("hostnames", [])[:30],
            "tags": d.get("tags", [])[:30],
        })

class CensysHostProvider(SearchProvider):
    name = "censys_host"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.censys_bearer_token:
            return ProviderResult(self.name, False, {}, error="missing_key")
        headers = {"Authorization": f"Bearer {settings.censys_bearer_token}"}
        url = f"https://search.censys.io/api/v2/hosts/{query}"
        res = await self._request_json("GET", url, headers=headers)
        if not res.ok:
            return res
        r = res.data.get("result", {})
        return ProviderResult(self.name, True, {
            "ip": query,
            "country": (r.get("location") or {}).get("country"),
            "as_name": (r.get("autonomous_system") or {}).get("name"),
            "services": [
                {
                    "port": s.get("port"),
                    "service_name": s.get("service_name"),
                    "transport_protocol": s.get("transport_protocol"),
                }
                for s in (r.get("services") or [])[:25]
            ]
        })

class CensysDomainProvider(SearchProvider):
    name = "censys_domain"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.censys_bearer_token:
            return ProviderResult(self.name, False, {}, error="missing_key")
        headers = {"Authorization": f"Bearer {settings.censys_bearer_token}"}
        url = f"https://search.censys.io/api/v2/hosts/search?q=names%3A%20{query}&per_page=10"
        res = await self._request_json("GET", url, headers=headers)
        if not res.ok:
            return res
        r = res.data.get("result", {})
        return ProviderResult(self.name, True, {
            "domain": query,
            "total": r.get("total"),
            "hits": [
                {"ip": h.get("ip"), "name": h.get("name")}
                for h in (r.get("hits") or [])[:10]
            ]
        })

class VTIPProvider(SearchProvider):
    name = "vt_ip"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.vt_api_key:
            return ProviderResult(self.name, False, {}, error="missing_key")
        headers = {"x-apikey": settings.vt_api_key}
        url = f"https://www.virustotal.com/api/v3/ip_addresses/{query}"
        res = await self._request_json("GET", url, headers=headers)
        if not res.ok:
            return res
        a = (res.data.get("data") or {}).get("attributes", {})
        return ProviderResult(self.name, True, {
            "ip": query,
            "country": a.get("country"),
            "as_owner": a.get("as_owner"),
            "reputation": a.get("reputation"),
            "last_analysis_stats": a.get("last_analysis_stats", {}),
        })

class VTDomainProvider(SearchProvider):
    name = "vt_domain"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.vt_api_key:
            return ProviderResult(self.name, False, {}, error="missing_key")
        headers = {"x-apikey": settings.vt_api_key}
        url = f"https://www.virustotal.com/api/v3/domains/{query}"
        res = await self._request_json("GET", url, headers=headers)
        if not res.ok:
            return res
        a = (res.data.get("data") or {}).get("attributes", {})
        return ProviderResult(self.name, True, {
            "domain": query,
            "reputation": a.get("reputation"),
            "categories": a.get("categories"),
            "last_analysis_stats": a.get("last_analysis_stats", {}),
        })

class VTHashProvider(SearchProvider):
    name = "vt_hash"

    async def execute(self, query: str) -> ProviderResult:
        if not settings.vt_api_key:
            return ProviderResult(self.name, False, {}, error="missing_key")
        headers = {"x-apikey": settings.vt_api_key}
        url = f"https://www.virustotal.com/api/v3/files/{query}"
        res = await self._request_json("GET", url, headers=headers)
        if not res.ok:
            return res
        a = (res.data.get("data") or {}).get("attributes", {})
        return ProviderResult(self.name, True, {
            "sha256": a.get("sha256"),
            "type_description": a.get("type_description"),
            "size": a.get("size"),
            "names": (a.get("names") or [])[:20],
            "last_analysis_stats": a.get("last_analysis_stats", {}),
        })

def providers_for(entity_type: str) -> list[SearchProvider]:
    if entity_type == "ip":
        return [ShodanIPProvider(), CensysHostProvider(), VTIPProvider()]
    if entity_type == "domain":
        return [CensysDomainProvider(), VTDomainProvider()]
    if entity_type == "hash":
        return [VTHashProvider()]
    return []
