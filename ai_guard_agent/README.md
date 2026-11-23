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

## Comments (24 Nov 2026)
When no post is waiting, the agent reads comments the KOL wrote under their own posts:
`GET /api/ai/comments_pending` → `review_comment()` (prompt `build_comment`, same grounding +
second-look rules as posts) → `POST /api/ai/comment_review`. Only KOL-written comments with a
competitor word, a link or sign-up wording are queued; the same text pasted again is read once.
A KOL comment hit waits for this verdict before LINE: urgent → LINE, watch/none → no LINE,
no verdict in 2 h → the keyword alert is sent anyway.
