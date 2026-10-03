"""Testes sem rede: instruções, ferramentas (banco simulado) e transcrição. Rodar: python -m pytest -q test/"""
import asyncio, datetime as dt, json, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co"); os.environ.setdefault("SUPABASE_SERVICE_KEY", "k")
import banco, instrucoes, agente  # noqa: E402

CTX = {
    "agora": "sábado, 03/10/2026 10:59",
    "ligacao": {"id": "L1", "direcao": "recebida", "telefone": "559182004400", "tenant_id": "T", "cliente": False, "interno": True},
    "lead": {"id": "LD", "nome": "Leonardo Daher", "fase": "proposta", "possui_cnpj": False, "municipio_atuacao": "Belém/PA", "qtd_socios": 2},
    "mensagens": [{"quem": "medico", "texto": "Eu não posso abrir um MEI pra emitir nota?"}],
    "ligacoes_anteriores": [], "plantao": ["Eduarda", "Emilly", "Priscila"],
    "faq": [{"slug": "mei", "pergunta": "Posso ser MEI?", "resposta": "Não, Doutor(a)."}],
}

def test_instrucoes_tem_regras_e_contexto():
    s = instrucoes.montar(CTX)
    for trecho in ("assistente virtual", "gravada", "R$ 400", "R$ 810", "Nenhum percentual", "Leonardo Daher", "Belém/PA",
                   "[mei]", "MEI pra emitir", "Eduarda, Emilly, Priscila", "registrar_resultado", "encerrar_ligacao"):
        assert trecho in s, trecho
    assert "número é interno" not in s          # tem lead: trata como lead
    assert "14,33%\"" not in s.replace("(nem \"14,33%\"", "")

def test_instrucoes_sem_lead_e_cliente():
    c = {**CTX, "lead": None, "ligacao": {**CTX["ligacao"], "cliente": True, "interno": False}}
    s = instrucoes.montar(c)
    assert "CLIENTE ativo" in s and "Não há ficha de lead" in s

def test_abertura():
    a = instrucoes.abertura(CTX)
    assert "Doutor(a) Leonardo" in a and "gravada" in a and "assistente virtual" in a
    assert "pergunte com quem fala" in instrucoes.abertura({**CTX, "lead": {"nome": "5591999990000"}})

class FakeCtx:  # JobContext mínimo
    def __init__(self): self.room = types.SimpleNamespace(name="lig-L1"); self.desligou = False; self.motivo = None
    async def delete_room(self): self.desligou = True
    def shutdown(self, motivo=""): self.motivo = motivo

def test_ferramentas(monkeypatch):
    chamadas = []
    async def rpc(nome, args): chamadas.append((nome, args)); return {"ok": True, "tarefa_id": "TF"} if nome == "ligacao_registrar" else {"ok": True}
    async def atualizar(lid, campos): chamadas.append(("upd", campos))
    monkeypatch.setattr(banco, "rpc", rpc); monkeypatch.setattr(banco, "atualizar_ligacao", atualizar)
    monkeypatch.setattr(agente, "ESPERA_HUMANO_S", 0.2)

    async def corre():
        est = agente.Estado("L1", FakeCtx(), CTX)
        ag = agente.Assistente(est)
        ferr = {t.info.name if hasattr(t, "info") else t.__name__: t for t in ag.tools}
        assert {"registrar_resultado", "chamar_humano", "encerrar_ligacao"} <= set(ferr)
        # encerrar sem registrar é barrado
        r = await ag.encerrar_ligacao(None)
        assert "registrar_resultado" in r
        # data inválida
        r = await ag.registrar_resultado(None, "agendou", "Quer café", "café", "quinta às 18h")
        assert "Data inválida" in r and not est.registrado
        r = await ag.registrar_resultado(None, "agendou", "Quer café na quinta", "café no escritório", "2026-10-08T18:00:00-03:00",
                                         possui_cnpj=False, municipio_atuacao="Belém", caminho_provavel="compartilhada")
        assert est.registrado and "tarefa" in r
        nome, args = [c for c in chamadas if c[0] == "ligacao_registrar"][-1]
        assert args["p_triagem"] == {"possui_cnpj": False, "municipio_atuacao": "Belém", "caminho_provavel": "compartilhada"}
        assert args["p_proximo_em"].startswith("2026-10-08T18:00:00-03:00")
        # chamar humano: sem ninguém entrar, volta para em_andamento
        est.session = None
        r = await ag.chamar_humano(None, "quer negociar preço")
        assert "Pedido feito" in r and any(c[0] == "ligacao_pedir_humano" for c in chamadas)
        await asyncio.sleep(0.4)
        assert ("upd", {"status": "em_andamento"}) in chamadas
        # encerrar depois de registrar
        r = await ag.encerrar_ligacao(None)
        await asyncio.sleep(1.2)
        assert est.ctx.desligou and est.ctx.motivo == "agente encerrou"
    asyncio.run(corre())

def test_transcricao():
    from livekit.agents import llm
    h = llm.ChatContext()
    h.add_message(role="system", content="x"); h.add_message(role="assistant", content="Boa tarde, Conta Medical")
    h.add_message(role="user", content="Quero saber o preço"); h.add_message(role="user", content="   ")
    s = types.SimpleNamespace(history=h)
    t = agente.transcricao(s, "Eduarda")
    assert [x["quem"] for x in t] == ["agente", "medico", "sistema"] and "Eduarda" in t[-1]["texto"]
