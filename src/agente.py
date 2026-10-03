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

import banco
import instrucoes

VERSAO = "0.1.0"
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
            self.est.espera = asyncio.create_task(esperar_humano(self.est))
        return (f"Pedido feito. Diga ao médico que vai chamar uma consultora e peça um instante, sem prometer tempo. "
                f"Enquanto ninguém entra, continue ajudando no que puder. Se em {ESPERA_HUMANO_S} segundos ninguém entrar, eu aviso.")

    @function_tool
    async def encerrar_ligacao(self, context: RunContext) -> str:
        """Desliga a ligação. Use só depois de registrar o resultado e de se despedir."""
        if not self.est.registrado:
            return "Antes de encerrar, use registrar_resultado."
        asyncio.create_task(desligar(self.est, "agente encerrou"))
        return "Ligação será encerrada."


async def esperar_humano(est: Estado) -> None:
    try:
        await asyncio.wait_for(est.humano_entrou.wait(), timeout=ESPERA_HUMANO_S)
    except asyncio.TimeoutError:
        await banco.atualizar_ligacao(est.ligacao_id, {"status": "em_andamento"})
        if est.session and not est.encerrando:
            est.session.generate_reply(instructions=(
                "Nenhuma consultora conseguiu entrar agora. Diga isso com naturalidade, peça desculpas pela espera, "
                "combine que a consultora retorna (pergunte o melhor horário), registre com resultado 'retornar' "
                "e siga normalmente."))


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
    if not (chave and segredo and ponto):
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
        if not texto:
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
        room_options=room_io.RoomOptions(participant_identity=medico.identity,
                                         participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_CONNECTOR]),
    )
    await banco.atualizar_ligacao(ligacao_id, {"status": "em_andamento", "atendida_em": agora_iso()})
    fala = session.generate_reply(instructions=instrucoes.abertura(contexto))
    await fala
    await banco.atualizar_ligacao(ligacao_id, {"aviso_gravacao_em": agora_iso()})
    await iniciar_gravacao(est)

    # médico em silêncio: pergunta uma vez se ainda está na linha; na segunda, encerra
    ausencias = {"n": 0}

    def ao_mudar_usuario(ev) -> None:  # noqa: ANN001
        if getattr(ev, "new_state", "") != "away" or est.humano_entrou.is_set() or est.encerrando:
            return
        ausencias["n"] += 1
        if ausencias["n"] == 1:
            session.generate_reply(instructions="O médico ficou em silêncio. Pergunte com gentileza se ele ainda está na linha.")
        else:
            asyncio.create_task(desligar(est, "médico em silêncio"))

    session.on("user_state_changed", ao_mudar_usuario)

    # limite de duração
    async def relogio() -> None:
        await asyncio.sleep(max(60.0, (MAX_MIN - 2) * 60))
        if not est.encerrando and not est.humano_entrou.is_set():
            session.generate_reply(instructions="A ligação está longa. Proponha o próximo passo, registre o resultado e despeça-se em até um minuto.")
        await asyncio.sleep(120)
        if not est.encerrando and not est.humano_entrou.is_set():
            await desligar(est, "tempo máximo")

    asyncio.create_task(relogio())


async def passar_para_humano(est: Estado) -> None:
    """A consultora entrou pela plataforma: o agente se despede e sai; a ligação continua entre os dois."""
    if est.espera and not est.espera.done():
        est.espera.cancel()
    s = est.session
    if not s:
        return
    try:
        s.interrupt()
        # o Gemini Live não tem say(): pede a frase ao modelo
        h = s.generate_reply(instructions=f"Diga apenas, numa frase curta: 'Pronto, a {est.humano_nome} entrou na ligação. "
                                          f"Vou deixar vocês conversarem.' Não diga mais nada.")
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
