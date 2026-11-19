# kol-ai-guard (runs on sanmini, not on Railway)

Reads every post KOL Watch scans from Exclusive KOLs and decides with a local LLM
(Ollama `gemma4:12b`) whether it promotes a competitor — including brands that are
not on the keyword watchlist — or teaches foreign-stock trading outside Webull.

- Poll `GET /api/ai/pending` → read caption → `POST /api/ai/review`
- `urgent` → KOL Watch adds a hit `AI: <brand>` + broadcasts a red LINE Flex alert
  (skipped if the keyword matcher already alerted the same post)
- `watch` → stored only, visible in the dashboard tab 🤖 AI ตรวจโพสต์
- Policy text can be changed without touching sanmini: dashboard Settings → "กติกาให้ AI"

On sanmini: `~/kol-ai-guard/` (`.env` holds KOLWATCH_KEY), launchd label `com.so.kol-ai-guard`,
log `~/kol-ai-guard/logs/agent.log`. This folder is a copy for reference; deploy with scp.
