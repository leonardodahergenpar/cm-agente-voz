# cm-agente-voz

Agente de voz da Conta Medical para as ligações do WhatsApp comercial (+55 91 93618-2107).
Usa LiveKit Cloud (conector WhatsApp) e Gemini Live (fala → fala). v0.1.0, de 03/10/2026.

## Como funciona

1. O médico liga pelo WhatsApp. A Meta manda o evento `calls` para o `wa-gateway` (Edge Function, v2.1).
2. O gateway registra a ligação (`crm.ligacao_entrada`: acha ou cria o contato e o lead) e confere se este agente
   está no ar, pelo sinal de vida em `crm.voz_agentes`, renovado a cada 30 s.
   - Se estiver: entrega a ligação ao LiveKit (`AcceptWhatsAppCall`), na sala `lig-<id>`, e despacha o agente `cm-voz`.
   - Se não estiver: recusa a ligação na Meta e marca como `perdida`. Assim o médico não fica no silêncio.
3. O agente começa a ligação:
   - lê o contexto (`crm.ligacao_contexto`): a ficha do lead, as últimas mensagens, as ligações anteriores e o FAQ aprovado;
   - atende, diz que é assistente virtual e avisa que a ligação é gravada.

   Depois conduz pelo roteiro v2 (`src/instrucoes.py`), com três ferramentas:
   - `registrar_resultado`: grava na ficha do lead e abre uma tarefa para o Comercial;
   - `chamar_humano`: põe a ligação em `aguardando_humano`. A consultora entra pelo Inbox (rota `/voz/token` do gateway)
     e o agente se despede e sai. Se ninguém entrar em 45 s, o agente combina o retorno;
   - `encerrar_ligacao`.
4. Na saída, o agente grava a transcrição e uma estimativa de custo (`crm.ligacao_transcricao`).
   Se ninguém registrou o resultado, fica `caiu`.

O áudio só é gravado com as variáveis `S3_*` preenchidas. Ele vai para o bucket privado `crm-ligacoes` do Supabase
(guarda de 6 meses, decisão de 03/10). Nada é gravado no LiveKit Cloud (`record=False`).

## Publicar

Push na `main` → GitHub Actions (roda os testes) → `ghcr.io/leonardodahergenpar/cm-agente-voz:latest` → Hostinger
Docker Manager, projeto `cm-agente-voz` (YAML em `docker-compose.yml`, com os campos `COLE_AQUI` preenchidos lá).

## Testes

```
pip install -r requirements.txt pytest
python -m pytest -q test/
```

## Variáveis

| Variável | O que é |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | projeto `cm-comercial` no LiveKit Cloud |
| `GOOGLE_API_KEY` | chave do Gemini (Google AI Studio) |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | banco da plataforma |
| `GEMINI_LIVE_MODEL`, `GEMINI_VOZ` | modelo e voz; padrão `gemini-3.1-flash-live-preview` com a voz `Aoede` |
| `VOZ_ESPERA_HUMANO_S` | quanto tempo esperar a consultora entrar (padrão 45) |
| `VOZ_MAX_MIN` | duração máxima da ligação (padrão 15) |
| `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` | gravação no Storage do Supabase (opcional) |
