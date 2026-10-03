"""cm-agente-voz — agente de voz da Conta Medical nas ligações do WhatsApp comercial (LiveKit + Gemini Live).

O wa-gateway aceita a ligação no LiveKit (AcceptWhatsAppCall) e despacha este agente ("cm-voz") para a sala lig-<id>,
com metadata {ligacao_id, tenant_id, lead_id}. O agente:
  1. carrega o contexto (crm.ligacao_contexto): ficha do lead, últimas mensagens, ligações anteriores, FAQ aprovado;
  2. atende, avisa que a ligação é gravada e conduz pelo roteiro (instrucoes.py);
  3. registra o resultado na ficha (crm.ligacao_registrar, que abre tarefa para o Comercial);
  4. chama uma consultora quando preciso (crm.ligacao_pedir_humano → painel do Inbox); se ela entrar, sai da ligação;
  5. ao sair, grava a transcrição (crm.ligacao_transcricao).
Gravação do áudio no Storage (bucket crm-ligacoes) só se as variáveis S3_* estiverem definidas.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Literal

from google.genai import types as gtypes
from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool, room_io
from livekit.plugins import google

try:  # supressão de ruído da LiveKit Cloud (modelo para áudio de telefone); sem ela, segue sem filtro
    from livekit.plugins import noise_cancellation
except Exception:  # noqa: BLE001
    noise_cancellation = None

import banco
import instrucoes

VERSAO = "0.3.3"
AGENTE = os.environ.get("VOZ_AGENTE_NOME", "cm-voz")
MODELO = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
VOZ = os.environ.get("GEMINI_VOZ", "Aoede")
ESPERA_HUMANO_S = int(os.environ.get("VOZ_ESPERA_HUMANO_S", "45"))
MAX_MIN = float(os.environ.get("VOZ_MAX_MIN", "15"))
# preço de referência do Gemini Live (US$/min de áudio) para a estimativa de custo gravada na ligação
PRECO_ENTRADA_MIN = float(os.environ.get("VOZ_PRECO_ENTRADA_USD_MIN", "0.005"))
PRECO_SAIDA_MIN = float(os.environ.get("VOZ_PRECO_SAIDA_USD_MIN", "0.018"))

log = logging.getLogger("cm-voz")
agora_iso = lambda: dt.datetime.now(dt.timezone.utc).isoformat()  # noqa: E731

Modalidade = Literal["cafe", "video", "retorno"]
SILENCIO_MS = int(os.environ.get("VOZ_SILENCIO_MS", "500"))   # pausa do médico que encerra a vez dele (menor = responde mais rápido)
Resultado = Literal["interessado", "agendou", "sem_interesse", "retornar", "duvida_humano", "nao_e_lead"]
Caminho = Literal["compartilhada", "individual", "migracao", "indefinido"]


class Estado:
    """Estado de uma ligação (um por job)."""

    def __init__(self, ligacao_id: str, ctx: JobContext, contexto: dict[str, Any]):
        self.ligacao_id = ligacao_id
        self.ctx = ctx
        self.contexto = contexto
        self.registrado = False
        self.humano_entrou = asyncio.Event()
        self.humano_nome: str | None = None
        self.espera: asyncio.Task | None = None
        self.session: AgentSession | None = None
        self.inicio = time.monotonic()
        self.encerrando = False
        self.aguardando = False   # esperando a consultora entrar (não pergunta "ainda está na linha?" nesse tempo)
        self.marcado: dict[str, Any] | None = None


class Assistente(Agent):
    def __init__(self, est: Estado):
        super().__init__(instructions=instrucoes.montar(est.contexto))
        self.est = est

    @function_tool
    async def registrar_resultado(
        self,
        context: RunContext,
        resultado: Resultado,
        resumo: str,
        proximo_passo: str | None = None,
        proximo_em: str | None = None,
        possui_cnpj: bool | None = None,
        municipio_atuacao: str | None = None,
        municipio_residencia: str | None = None,
        especialidade: str | None = None,
        caminho_provavel: Caminho | None = None,
    ) -> str:
        """Registra o resultado da ligação na ficha do lead. Use antes de se despedir (e de novo se algo mudar).

        Args:
            resultado: agendou (café/vídeo combinado), interessado (quer proposta/resumo), retornar (combinar retorno),
                duvida_humano (precisa de uma pessoa), sem_interesse (recusou), nao_e_lead (cliente, equipe ou engano).
            resumo: 2 a 4 frases com o caso e as palavras do médico (inclua o motivo, se recusou).
            proximo_passo: o que foi combinado, ex.: "café no escritório", "vídeo com o consultor", "proposta pelo WhatsApp".
            proximo_em: data e hora combinadas em ISO 8601 com fuso -03:00, ex.: 2026-10-08T18:00:00-03:00.
            possui_cnpj: se o médico já tem empresa/CNPJ.
            municipio_atuacao: onde ele atende.
            municipio_residencia: onde ele mora.
            especialidade: especialidade médica, se disse.
            caminho_provavel: compartilhada (sociedade médica), individual (empresa própria), migracao (já tem empresa), indefinido.
        """
        triagem = {k: v for k, v in {
            "possui_cnpj": possui_cnpj, "municipio_atuacao": municipio_atuacao, "municipio_residencia": municipio_residencia,
            "especialidade": especialidade, "caminho_provavel": caminho_provavel}.items() if v is not None}
        quando = None
        if proximo_em:
            try:
                quando = dt.datetime.fromisoformat(proximo_em).isoformat()
            except ValueError:
                return "Data inválida: use ISO 8601 com fuso, por exemplo 2026-10-08T18:00:00-03:00. Tente de novo."
        r = await banco.rpc("ligacao_registrar", {
            "p_ligacao_id": self.est.ligacao_id, "p_resultado": resultado, "p_resumo": resumo,
            "p_proximo_passo": proximo_passo, "p_proximo_em": quando, "p_triagem": triagem})
        self.est.registrado = True
        log.info("resultado registrado %s %s", self.est.ligacao_id, r)
        return "Registrado na ficha." + (" A consultora recebeu a tarefa de dar sequência." if (r or {}).get("tarefa_id") else "")

    @function_tool
    async def chamar_humano(self, context: RunContext, motivo: str) -> str:
        """Chama uma consultora para entrar na ligação (ela entra pela plataforma).

        Args:
            motivo: por que precisa de uma pessoa, em uma frase (aparece para a consultora).
        """
        if self.est.humano_entrou.is_set():
            return "A consultora já está na ligação."
        await banco.rpc("ligacao_pedir_humano", {"p_ligacao_id": self.est.ligacao_id, "p_motivo": motivo})
        if not self.est.espera or self.est.espera.done():
            self.est.aguardando = True
            self.est.espera = asyncio.create_task(esperar_humano(self.est))
        return (f"Pedido feito. Diga ao médico que vai chamar uma consultora e peça um instante, sem prometer tempo. "
                f"Enquanto ninguém entra, continue ajudando no que puder. Se em {ESPERA_HUMANO_S} segundos ninguém entrar, eu aviso.")

    @function_tool
    async def horarios_livres(self, context: RunContext, modalidade: Modalidade, preferencia: str | None = None,
                              so_no_dia: bool = False) -> str:
        """Consulta a agenda das consultoras. Use SEMPRE antes de propor ou aceitar um horário.

        Args:
            modalidade: cafe (presencial no escritório, Belém), video (Google Meet) ou retorno (a consultora liga para ele).
            preferencia: dia e hora que o médico pediu, ISO 8601 com fuso -03:00 (ex.: 2026-10-08T18:00:00-03:00).
                Se ele só disse o dia, use 09:00 desse dia. Sem preferência, deixe vazio (vêm os próximos horários livres).
            so_no_dia: true para listar só horários do mesmo dia da preferência.
        """
        tenant = (self.est.contexto.get("ligacao") or {}).get("tenant_id")
        quando = None
        if preferencia:
            try:
                quando = dt.datetime.fromisoformat(preferencia).isoformat()
            except ValueError:
                return "Preferência inválida: use ISO 8601 com fuso, ex.: 2026-10-08T18:00:00-03:00."
        opc = await banco.rpc("agenda_livres", {"p_tenant": tenant, "p_modalidade": modalidade, "p_preferido": quando,
                                                "p_qtd": 4, "p_so_no_dia": so_no_dia}) or []
        if not opc:
            return "Não há horário livre nos próximos dias para essa modalidade. Ofereça outra modalidade ou um retorno."
        linhas = []
        for o in opc:
            exato = bool(quando) and o["inicio"][:16] == dt.datetime.fromisoformat(quando).astimezone(dt.timezone.utc).isoformat()[:16]
            linhas.append(f"- {o['texto']} (inicio={o['inicio']}{'; É O HORÁRIO PEDIDO' if exato else ''})")
        aviso = ("" if not quando or "É O HORÁRIO PEDIDO" in linhas[0] else
                 "O horário pedido NÃO está livre. Diga isso em meia frase e ofereça as duas primeiras opções abaixo.\n")
        return aviso + "Horários livres (fale exatamente como no texto; para marcar, passe o inicio):\n" + "\n".join(linhas)

    @function_tool
    async def marcar_reuniao(self, context: RunContext, modalidade: Modalidade, inicio: str) -> str:
        """Marca o compromisso na agenda, num horário devolvido por horarios_livres e aceito pelo médico.

        Args:
            modalidade: cafe, video ou retorno.
            inicio: o valor "inicio" exatamente como veio de horarios_livres.
        """
        lead = (self.est.contexto.get("lead") or {}).get("id")
        if not lead:
            return "Este número não tem ficha de lead: não marque; use chamar_humano ou registre 'retornar'."
        tenant = (self.est.contexto.get("ligacao") or {}).get("tenant_id")
        r = await banco.rpc("agenda_marcar", {"p_tenant": tenant, "p_lead": lead, "p_modalidade": modalidade,
                                              "p_inicio": inicio, "p_origem": "agente", "p_ligacao_id": self.est.ligacao_id}) or {}
        if not r.get("ok"):
            alt = "; ".join(f"{a['texto']} (inicio={a['inicio']})" for a in (r.get("alternativas") or [])[:3])
            return f"Esse horário acabou de ser ocupado. Ofereça: {alt or 'outro dia'}."
        self.est.marcado = r
        local = {"cafe": f"café no escritório ({r.get('endereco') or 'Quadra Corporate, na Doca'})",
                 "video": "videochamada pelo Google Meet (o link vai pelo WhatsApp)",
                 "retorno": "a consultora liga para ele"}[modalidade]
        return (f"Marcado: {r['texto']}, {local}, com a {r['consultora']}. Confirme ao médico dizendo o dia da semana, o dia, o mês "
                f"e a hora exatamente assim: '{r['texto']}'. Depois use registrar_resultado com resultado 'agendou'.")

    @function_tool
    async def cancelar_reuniao(self, context: RunContext, motivo: str) -> str:
        """Cancela a reunião já marcada do médico (listada em REUNIÃO JÁ MARCADA). Só se ele pedir para cancelar
        sem remarcar; para trocar de horário, use horarios_livres + marcar_reuniao (o horário antigo é liberado sozinho).

        Args:
            motivo: o motivo, com as palavras do médico.
        """
        comp = self.est.contexto.get("compromissos") or []
        if not comp:
            return "Não há reunião marcada para este médico."
        for c in comp:
            await banco.rpc("agenda_cancelar", {"p_participante": c["participante_id"], "p_motivo": f"pedido do médico por ligação: {motivo}"})
        self.est.contexto["compromissos"] = []
        return (f"Cancelada: {comp[0]['texto']}. Confirme ao médico, pergunte se quer deixar um retorno marcado e registre o "
                f"resultado ('retornar' ou 'sem_interesse').")

    @function_tool
    async def encerrar_ligacao(self, context: RunContext) -> str:
        """Desliga a ligação. Use logo depois de se despedir (o resultado já deve ter sido registrado)."""
        # v0.3.2: no Gemini 3.8 as ferramentas rodam em paralelo; o encerrar podia chegar antes do registrar terminar,
        # voltava "registre antes" e a ligação ficava aberta depois da despedida (3º teste). Agora espera o registro.
        for _ in range(16):
            if self.est.registrado:
                break
            await asyncio.sleep(0.5)
        if not self.est.registrado and self.est.marcado:
            await registrar_automatico(self.est)
        if not self.est.registrado:
            return "Antes de encerrar, use registrar_resultado."
        asyncio.create_task(desligar(self.est, "agente encerrou"))
        return "Ligação será encerrada."


async def registrar_automatico(est: "Estado") -> None:
    """Reunião marcada mas o modelo não registrou: registra 'agendou' com o que a agenda devolveu."""
    m = est.marcado or {}
    try:
        await banco.rpc("ligacao_registrar", {
            "p_ligacao_id": est.ligacao_id, "p_resultado": "agendou",
            "p_resumo": f"Reunião marcada pela assistente: {m.get('modalidade', '')} {m.get('texto', '')} com {m.get('consultora', '')}.",
            "p_proximo_passo": {"cafe": "café no escritório", "video": "vídeo com a consultora", "retorno": "retorno por ligação"}.get(m.get("modalidade", ""), "reunião"),
            "p_proximo_em": m.get("inicio")})
        est.registrado = True
    except Exception as e:  # noqa: BLE001
        log.error("registro automático falhou: %s", e)


AVISO = "[SISTEMA]"


def instruir(session: AgentSession, texto: str):
    """Pede uma fala ao modelo sem que ele leia a instrução em voz alta.

    v0.2: generate_reply(instructions=...) manda o texto como turno do PRÓPRIO modelo; o Gemini 3.8 Live continuava
    a frase e lia a instrução para o médico (teste de 03/10). Agora vai como aviso do sistema num turno de usuário,
    marcado com [SISTEMA] — as instruções dizem que esses avisos nunca são lidos, e a transcrição os descarta.
    """
    return session.generate_reply(user_input=f"{AVISO} (aviso interno da plataforma, não é fala do médico; não leia nem mencione) {texto}")


async def esperar_humano(est: Estado) -> None:
    try:
        await asyncio.wait_for(est.humano_entrou.wait(), timeout=ESPERA_HUMANO_S)
    except asyncio.TimeoutError:
        await banco.atualizar_ligacao(est.ligacao_id, {"status": "em_andamento"})
        est.aguardando = False
        if est.session and not est.encerrando:
            instruir(est.session, "Nenhuma consultora conseguiu entrar agora. Em português, diga isso com naturalidade, peça "
                                  "desculpas pela espera e pergunte o melhor dia e horário para a consultora retornar.")


async def desligar(est: Estado, motivo: str) -> None:
    if est.encerrando:
        return
    est.encerrando = True
    try:
        if est.session and est.session.current_speech:
            await asyncio.wait_for(asyncio.shield(est.session.current_speech.wait_for_playout()), timeout=15)
        await asyncio.sleep(0.8)
    except Exception:  # noqa: BLE001
        pass
    log.info("encerrando %s: %s", est.ligacao_id, motivo)
    try:
        await est.ctx.delete_room()   # derruba a ligação no WhatsApp (o conector sai da sala)
    except Exception as e:  # noqa: BLE001
        log.warning("delete_room: %s", e)
    est.ctx.shutdown(motivo)


async def iniciar_gravacao(est: Estado) -> None:
    """Grava o áudio da sala no Storage do Supabase (S3), se configurado."""
    chave, segredo, ponto = os.environ.get("S3_ACCESS_KEY"), os.environ.get("S3_SECRET_KEY"), os.environ.get("S3_ENDPOINT")
    faltam = [n for n, v in (("S3_ENDPOINT", ponto), ("S3_ACCESS_KEY", chave), ("S3_SECRET_KEY", segredo),
                             ("S3_REGION", os.environ.get("S3_REGION"))) if not v or v == "COLE_AQUI"]
    if faltam:   # v0.3.1: deixa o motivo na ligação (nos dois primeiros testes a gravação não ligou e não havia rastro)
        await banco.atualizar_ligacao(est.ligacao_id, {"erro": {"gravacao": f"desligada: faltam {', '.join(faltam)}"}})
        return
    t = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))
    caminho = f"{est.contexto['ligacao']['tenant_id']}/{t:%Y}/{t:%m}/{est.ligacao_id}.ogg"
    req = api.RoomCompositeEgressRequest(
        room_name=est.ctx.room.name, audio_only=True,
        file_outputs=[api.EncodedFileOutput(
            file_type=api.EncodedFileType.OGG, filepath=caminho,
            s3=api.S3Upload(access_key=chave, secret=segredo, endpoint=ponto, region=os.environ.get("S3_REGION", "us-east-1"),
                            bucket=os.environ.get("S3_BUCKET", "crm-ligacoes"), force_path_style=True))])
    try:
        info = await est.ctx.api.egress.start_room_composite_egress(req)
        await banco.atualizar_ligacao(est.ligacao_id, {"audio_path": caminho})
        log.info("gravação iniciada %s egress=%s", caminho, info.egress_id)
    except Exception as e:  # noqa: BLE001
        log.error("gravação não iniciou: %s", e)
        await banco.atualizar_ligacao(est.ligacao_id, {"erro": {"gravacao": str(e)[:300]}})


def transcricao(session: AgentSession | None, humano: str | None) -> list[dict[str, Any]]:
    turnos: list[dict[str, Any]] = []
    if not session:
        return turnos
    for item in session.history.items:
        if getattr(item, "type", "") != "message" or item.role not in ("user", "assistant"):
            continue
        texto = (item.text_content or "").strip()
        if not texto or texto.startswith(AVISO):
            continue
        turnos.append({"quem": "medico" if item.role == "user" else "agente", "texto": texto,
                       "em": dt.datetime.fromtimestamp(item.created_at, dt.timezone.utc).isoformat()})
    if humano:
        turnos.append({"quem": "sistema", "texto": f"{humano} entrou na ligação; daqui em diante a conversa não foi transcrita."})
    return turnos


server = AgentServer()


@server.rtc_session(agent_name=AGENTE)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    ligacao_id = meta.get("ligacao_id")
    if not ligacao_id:
        log.error("job sem ligacao_id: %s", ctx.job.metadata)
        return
    contexto = await banco.rpc("ligacao_contexto", {"p_ligacao_id": ligacao_id}) or {}
    est = Estado(ligacao_id, ctx, contexto)
    await ctx.connect()

    # quem é o médico na sala (participante do conector do WhatsApp)
    medico = await ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_CONNECTOR)

    def ao_entrar(p: rtc.RemoteParticipant) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD and (p.attributes or {}).get("papel") == "humano":
            est.humano_nome = (p.name or "a consultora").split()[0]
            est.humano_entrou.set()
            asyncio.create_task(passar_para_humano(est))

    ctx.room.on("participant_connected", ao_entrar)

    llm = google.realtime.RealtimeModel(
        model=MODELO, voice=VOZ, language="pt-BR", temperature=0.6,
        input_audio_transcription=gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=gtypes.AudioTranscriptionConfig(),
        # v0.3: fim da vez do médico mais rápido (pausas longas no 2º teste)
        realtime_input_config=gtypes.RealtimeInputConfig(automatic_activity_detection=gtypes.AutomaticActivityDetection(
            end_of_speech_sensitivity=gtypes.EndSensitivity.END_SENSITIVITY_HIGH, silence_duration_ms=SILENCIO_MS)),
    )
    session = AgentSession(llm=llm, user_away_timeout=20.0)
    est.session = session

    async def ao_sair(motivo: str = "") -> None:
        dur_min = (time.monotonic() - est.inicio) / 60
        try:
            uso = session.usage.model_dump(mode="json") if hasattr(session.usage, "model_dump") else str(session.usage)
        except Exception:  # noqa: BLE001
            uso = None
        try:
            await banco.rpc("ligacao_transcricao", {
                "p_ligacao_id": ligacao_id, "p_turnos": transcricao(session, est.humano_nome),
                "p_custo_usd": round(dur_min * (PRECO_ENTRADA_MIN + PRECO_SAIDA_MIN), 4),
                "p_meta": {"versao": VERSAO, "roteiro": instrucoes.VERSAO_ROTEIRO, "modelo": MODELO, "voz": VOZ,
                           "minutos_agente": round(dur_min, 2), "uso": uso, "saida": motivo,
                           "custo": "estimado pelo tempo do agente na ligação"}})
            if not est.registrado and not est.humano_nome:
                await banco.rpc("ligacao_registrar", {
                    "p_ligacao_id": ligacao_id, "p_resultado": "caiu",
                    "p_resumo": "Ligação terminou sem o agente registrar resultado (o médico desligou antes ou a ligação caiu)."})
        except Exception as e:  # noqa: BLE001
            log.error("falha ao gravar o fim da ligação %s: %s", ligacao_id, e)

    ctx.add_shutdown_callback(ao_sair)
    # sessão fechada (o médico desligou, erro do modelo…) → encerra o job; a gravação do fim roda no ao_sair
    session.on("close", lambda ev: ctx.shutdown(f"sessão fechada: {getattr(ev, 'reason', '')}"))

    await session.start(
        room=ctx.room, agent=Assistente(est), record=False,   # nada fica gravado no LiveKit Cloud
        room_options=room_io.RoomOptions(
            participant_identity=medico.identity,
            participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_CONNECTOR],
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())
            if noise_cancellation and os.environ.get("VOZ_FILTRO_RUIDO", "1") == "1" else True),
    )
    await banco.atualizar_ligacao(ligacao_id, {"status": "em_andamento", "atendida_em": agora_iso()})
    fala = instruir(session, instrucoes.abertura(contexto))
    await fala
    await banco.atualizar_ligacao(ligacao_id, {"aviso_gravacao_em": agora_iso()})
    await iniciar_gravacao(est)

    # médico em silêncio: pergunta uma vez se ainda está na linha; na segunda, encerra
    ausencias = {"n": 0}

    def ao_mudar_usuario(ev) -> None:  # noqa: ANN001
        if getattr(ev, "new_state", "") != "away" or est.humano_entrou.is_set() or est.encerrando or est.aguardando:
            return
        ausencias["n"] += 1
        if est.registrado:   # já se despediu e registrou: silêncio depois disso é fim de ligação
            asyncio.create_task(desligar(est, "silêncio depois da despedida"))
        elif ausencias["n"] == 1:
            instruir(session, "O médico está em silêncio há uns 20 segundos. Pergunte, numa frase curta e em português, se ele ainda está na linha.")
        else:
            asyncio.create_task(desligar(est, "médico em silêncio"))

    session.on("user_state_changed", ao_mudar_usuario)

    # limite de duração
    async def relogio() -> None:
        await asyncio.sleep(max(60.0, (MAX_MIN - 2) * 60))
        if not est.encerrando and not est.humano_entrou.is_set():
            instruir(session, "A ligação está longa. Proponha o próximo passo, registre o resultado e despeça-se em até um minuto.")
        await asyncio.sleep(120)
        if not est.encerrando and not est.humano_entrou.is_set():
            await desligar(est, "tempo máximo")

    asyncio.create_task(relogio())


async def passar_para_humano(est: Estado) -> None:
    """A consultora entrou pela plataforma: o agente se despede e sai; a ligação continua entre os dois."""
    est.aguardando = False
    if est.espera and not est.espera.done():
        est.espera.cancel()
    s = est.session
    if not s:
        return
    try:
        s.interrupt()
        # o Gemini Live não tem say(): pede a frase ao modelo
        h = instruir(s, f"A consultora {est.humano_nome} acabou de entrar na ligação. Diga só, numa frase curta: "
                        f"'Pronto, a {est.humano_nome} entrou na ligação. Vou deixar vocês conversarem.' Não diga mais nada.")
        await asyncio.wait_for(h.wait_for_playout(), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("despedida na passagem: %s", e)
    log.info("passagem para humano %s (%s)", est.ligacao_id, est.humano_nome)
    est.encerrando = True
    est.ctx.shutdown("passou para a consultora")   # o agente sai; a sala e a ligação continuam


def batidas() -> None:
    """Sinal de vida a cada 30 s: sem ele, o wa-gateway recusa a ligação em vez de deixar o médico no silêncio."""
    ident = os.environ.get("VOZ_AGENTE_ID") or socket.gethostname()
    while True:
        try:
            banco.batida(ident, VERSAO, {"modelo": MODELO, "voz": VOZ, "agente": AGENTE})
        except Exception as e:  # noqa: BLE001
            log.warning("batida falhou: %s", e)
        time.sleep(30)


if __name__ == "__main__":
    faltando = [v for v in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")
                if not os.environ.get(v)]
    if faltando:
        raise SystemExit(f"variáveis ausentes: {', '.join(faltando)}")
    threading.Thread(target=batidas, daemon=True).start()
    cli.run_app(server)
