"""Acesso ao Supabase (PostgREST) com a service key — só as rotinas crm.* da inbox_09 e a tabela crm.ligacoes."""
from __future__ import annotations

import os
from typing import Any

import httpx

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
CHAVE = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _cab(perfil: str = "crm") -> dict[str, str]:
    h = {"apikey": CHAVE, "Content-Type": "application/json", "Content-Profile": perfil, "Accept-Profile": perfil}
    if CHAVE.startswith("eyJ"):          # chave antiga (JWT) vai também no Authorization;
        h["Authorization"] = f"Bearer {CHAVE}"   # a nova (sb_secret_…) só no apikey (mesmo padrão do cm-ctb-leitura)
    return h


class ErroBanco(RuntimeError):
    pass


async def rpc(nome: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{URL}/rest/v1/rpc/{nome}", headers=_cab(), json=args)
    if r.status_code >= 300:
        raise ErroBanco(f"{nome}: HTTP {r.status_code} {r.text[:300]}")
    return r.json() if r.content else None


async def atualizar_ligacao(ligacao_id: str, campos: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.patch(f"{URL}/rest/v1/ligacoes", params={"id": f"eq.{ligacao_id}"},
                          headers={**_cab(), "Prefer": "return=minimal"}, json=campos)
    if r.status_code >= 300:
        raise ErroBanco(f"ligacoes: HTTP {r.status_code} {r.text[:300]}")


def batida(agente_id: str, versao: str, info: dict[str, Any]) -> None:
    """Sinal de vida (síncrono, roda numa thread do processo principal)."""
    r = httpx.post(f"{URL}/rest/v1/rpc/voz_batida", headers=_cab(), timeout=10,
                   json={"p_id": agente_id, "p_versao": versao, "p_info": info})
    if r.status_code >= 300:
        raise ErroBanco(f"voz_batida: HTTP {r.status_code} {r.text[:200]}")
