"""Testes sem rede: instruções, ferramentas (banco simulado) e transcrição. Rodar: python -m pytest -q test/"""
import asyncio, os, sys, types
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

def test_instrucoes_onsaude_tem_valores_e_os_demais_nao():
    s = instrucoes.montar({**CTX, "onsaude": True})
    assert "quatrocentos reais" in s and "oitocentos e dez reais" in s and "LISTAGEM DA ONSAÚDE (mudança" in s
    g = instrucoes.montar({**CTX, "onsaude": False})
    assert "quatrocentos" not in g and "oitocentos" not in g and "NÃO fale de valores" in g
    for x in (s, g):
        assert "português do Brasil, do começo ao fim" in x and "[SISTEMA]" in x and "APRESENTAÇÃO DA CONTA MEDICAL" in x

def test_instrucoes_tem_regras_e_contexto():
    s = instrucoes.montar(CTX)
    for trecho in ("assistente virtual", "gravada", "Nenhum percentual", "Leonardo Daher", "Belém/PA",
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
    assert "NÃO faça a apresentação" in a                       # CTX tem mensagens e fase "proposta": já nos conhece
    novo = instrucoes.abertura({**CTX, "mensagens": [], "lead": {"nome": "Ana Souza", "fase": "novo"}})
    assert "apresente a Conta Medical" in novo and "espere a resposta" in novo
    comp = [{"modalidade": "video", "texto": "sexta-feira, 9 de outubro, às 18h", "consultora": "Priscila"}]
    r = instrucoes.abertura({**CTX, "compromissos": comp})
    assert "videochamada marcada conosco para sexta-feira, 9 de outubro, às 18h, com a Priscila" in r and "NÃO faça a apresentação" in r

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
    h.add_message(role="user", content="[SISTEMA] (aviso interno) O médico está em silêncio.")
    s = types.SimpleNamespace(history=h)
    t = agente.transcricao(s, "Eduarda")
    assert [x["quem"] for x in t] == ["agente", "medico", "sistema"] and "Eduarda" in t[-1]["texto"]

def test_cabecalho_chave_nova_e_antiga(monkeypatch):
    monkeypatch.setattr(banco, "CHAVE", "sb_secret_abc")
    h = banco._cab()
    assert h["apikey"] == "sb_secret_abc" and "Authorization" not in h and h["Content-Profile"] == "crm"
    monkeypatch.setattr(banco, "CHAVE", "eyJhbGciOi.x.y")
    assert banco._cab()["Authorization"] == "Bearer eyJhbGciOi.x.y"


def test_instruir_vai_como_aviso_do_sistema():
    class S:
        def generate_reply(self, **kw): self.kw = kw; return "h"
    s = S(); agente.instruir(s, "Pergunte se ele ainda está na linha.")
    assert "instructions" not in s.kw and s.kw["user_input"].startswith("[SISTEMA]") and "não leia" in s.kw["user_input"]


def test_calendario_e_agenda_nas_instrucoes():
    import datetime as dt
    c = instrucoes.calendario(dt.date(2026, 10, 3), 7)
    assert "sábado 03/10/2026 (2026-10-03)" in c and "quinta-feira 08/10/2026 (2026-10-08)" in c
    s = instrucoes.montar(CTX)
    assert "horarios_livres" in s and "marcar_reuniao" in s and "dia da semana, o dia, o mês e a hora" in s


def test_ferramentas_de_agenda(monkeypatch):
    chamadas = []
    async def rpc(nome, args):
        chamadas.append((nome, args))
        if nome == "agenda_livres":
            return [{"inicio": "2026-10-08T20:30:00+00:00", "texto": "quinta-feira, 8 de outubro, às 17h30"},
                    {"inicio": "2026-10-08T21:30:00+00:00", "texto": "quinta-feira, 8 de outubro, às 18h30"}]
        if nome == "agenda_marcar":
            return {"ok": True, "texto": "quinta-feira, 8 de outubro, às 17h30", "consultora": "Priscila"}
    monkeypatch.setattr(banco, "rpc", rpc)
    ctx = {**CTX, "ligacao": {**CTX["ligacao"], "tenant_id": "T"}}

    async def corre():
        est = agente.Estado("L1", FakeCtx(), ctx)
        ag = agente.Assistente(est)
        r = await ag.horarios_livres(None, "video", "2026-10-08T18:00:00-03:00", True)
        assert "NÃO está livre" in r and "17h30" in r and chamadas[-1][1]["p_so_no_dia"] is True
        r = await ag.horarios_livres(None, "video", "2026-10-08T17:30:00-03:00")
        assert "É O HORÁRIO PEDIDO" in r and "NÃO está livre" not in r
        r = await ag.marcar_reuniao(None, "video", "2026-10-08T20:30:00+00:00")
        assert "quinta-feira, 8 de outubro, às 17h30" in r and "Priscila" in r and est.marcado
        assert chamadas[-1][1]["p_lead"] == "LD" and chamadas[-1][1]["p_ligacao_id"] == "L1"
        est2 = agente.Estado("L2", FakeCtx(), {**ctx, "lead": None})
        assert "não tem ficha" in await agente.Assistente(est2).marcar_reuniao(None, "video", "x")
    asyncio.run(corre())


def test_encerrar_espera_registro_e_registra_sozinho(monkeypatch):
    chamadas = []
    async def rpc(nome, args): chamadas.append((nome, args)); return {"ok": True}
    monkeypatch.setattr(banco, "rpc", rpc)

    async def corre():
        est = agente.Estado("L1", FakeCtx(), CTX)
        est.marcado = {"ok": True, "modalidade": "video", "texto": "sexta-feira, 9 de outubro, às 18h", "consultora": "Priscila",
                       "inicio": "2026-10-09T21:00:00+00:00"}
        ag = agente.Assistente(est)
        r = await ag.encerrar_ligacao(None)          # sem registro: registra 'agendou' sozinho e encerra
        assert "encerrada" in r and est.registrado
        assert [c for c in chamadas if c[0] == "ligacao_registrar"][-1][1]["p_resultado"] == "agendou"
        await asyncio.sleep(1.2)
        assert est.ctx.desligou
    asyncio.run(corre())


def test_reuniao_ja_marcada_remarcar_e_cancelar(monkeypatch):
    comp = [{"participante_id": "P1", "modalidade": "video", "texto": "sexta-feira, 9 de outubro, às 18h", "consultora": "Priscila"}]
    s = instrucoes.montar({**CTX, "compromissos": comp})
    assert "REUNIÃO JÁ MARCADA" in s and "sexta-feira, 9 de outubro, às 18h, vídeo, com a Priscila" in s
    chamadas = []
    async def rpc(nome, args): chamadas.append((nome, args)); return {"ok": True}
    monkeypatch.setattr(banco, "rpc", rpc)

    async def corre():
        est = agente.Estado("L1", FakeCtx(), {**CTX, "compromissos": list(comp)})
        r = await agente.Assistente(est).cancelar_reuniao(None, "vai viajar")
        assert "Cancelada" in r and chamadas[-1] == ("agenda_cancelar", {"p_participante": "P1", "p_motivo": "pedido do médico por ligação: vai viajar"})
        assert "Não há reunião" in await agente.Assistente(est).cancelar_reuniao(None, "x")
    asyncio.run(corre())
