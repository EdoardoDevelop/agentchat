# Idee per Agent Chat

Cose non ancora fatte. Quando una diventa lavoro, toglila da qui.

## Jeeves con altri fornitori oltre a OpenRouter
Oggi Jeeves (`jeeves.py`) usa solo OpenRouter. Deve poter usare anche, direttamente:
- **OpenAI** (i modelli di ChatGPT, con una chiave API di platform.openai.com);
- **Anthropic** (Claude, con una chiave API di console.anthropic.com);
- **altri**: Google Gemini, Mistral, DeepSeek, e un server locale compatibile con OpenAI (Ollama, LM Studio) indicato con il suo indirizzo.

Come:
- Nella pagina, menu → Jeeves: un **fornitore** da scegliere, poi la sua chiave (con «Prova») e il motore dall'elenco di quel fornitore (`/models`), come ora per OpenRouter. Una chiave per fornitore, salvata sul server e mai rimandata alla pagina.
- OpenAI, Gemini, Mistral, DeepSeek e i server locali parlano il formato di OpenAI (`/chat/completions` con `tools`): stesso codice di oggi, cambiano indirizzo e chiave. Anthropic ha il suo formato (`/v1/messages`, strumenti come `tool_use`/`tool_result`): serve un adattatore.
- La riserva può essere di un altro fornitore (es. Claude diretto, riserva OpenRouter).
- Da dire nella pagina: gli abbonamenti (ChatGPT Plus, Claude Pro/Max) non si possono usare così, servono le chiavi API, pagate a consumo.

## Rimandate
- Pulizia o archivio dei messaggi e degli allegati vecchi (il disco cresce senza fine).
- Ricerca nella pagina per testo, stanza e agente.
- Notifiche sul telefono (la pagina è già una PWA) quando un agente nomina l'owner o chiede un sì.
- Mappa ad argomenti e riassunti calcolati sul server, uguali per tutti i PC.
- Revisione incrociata: un «FATTO» con effetti reali controllato da un altro motore prima di presentarlo all'owner.
