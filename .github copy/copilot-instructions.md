# Project Guidelines

## Always Read This File First

Before starting any work, **always read this file** (`.github/copilot-instructions.md`). It contains project-wide rules for AI agents and must be followed.

- **Auto-Commit Changes:** When the session is over and modifications are complete, automatically commit and push all recent changes to the Git repository. Do not leave the workspace dirty. Write a concise and clear commit message.
- **No Local Debugging / Docker (on dev machine):** The bot runs on a remote **Intel N150 Mini PC** (12GB RAM + 8GB swap), not on this local development machine. Do not attempt to run, build, or test Docker or Node on the local dev machine — it is entirely pointless.
- **Remote Host Access (GRANTED):** You MAY SSH into the N150 host (`192.168.0.99`) and the Ugreen NAS (`192.168.0.100`) as `sneakyjoe` to deploy updates, rebuild Docker images, restart containers, run tests inside containers, and inspect logs. This is the expected deployment workflow — use it after committing+pushing code changes. SSH key: `.ssh/id_ed25519_joe_agent` (symlink to shared `/distr-fun/.ssh/`, gitignored). MANDATORY SSH options: `ssh -i .ssh/id_ed25519_joe_agent -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o PreferredAuthentications=publickey -o NumberOfPasswordPrompts=0 sneakyjoe@192.168.0.99 '...'`. Host repo path: `/home/sneakyjoe/bots/robot-joe-dev/discord-joe/`. Deploy: `rebuild-run.sh` rebuilds the Docker image and restarts the container. Do NOT run Docker/Node on the local dev machine; run them on the remote host via SSH.
- **Do Not Change Existing Functionality:** Never change, refactor, or delete any existing functionality, logic, or behavior unless explicitly requested by the user. Keeping existing functions intact is crucial for stability and consistency. If a change is needed, ask user if you are to add new functions or modules instead of modifying existing ones.
- **Git Info Pre-baking:** The bot relies on `git-info.json` since the deployment directory is not a Git repository. After any Git changes, run `generate-git-info.sh` to update `git-info.json`.
- **Stray Bracket Check Before Push (MANDATORY):** Before committing/pushing ANY fix or feature, you MUST run a syntax/bracket-balance verification on every file you modified — `node --check <file>` when Node is available, or at minimum a careful manual brace/paren/bracket balance inspection of the edited regions. This rule exists because stray/extra closing braces from incomplete edits have repeatedly shipped and crashed the bot at startup (e.g. `SyntaxError: Unexpected token '}'`). Never push JS that hasn't been syntax-checked. If a check fails, fix it BEFORE pushing — do not push known-broken syntax.
- **Task Accumulation (MANDATORY):** When the user sends a new message while previous tasks are still in progress or not yet completed, the new message ADDS to the task list — it does NOT replace or cancel previous tasks. You must complete ALL pending tasks from ALL user messages before considering the session done. Never silently drop or abandon a previous task when a new request arrives. Use a todo list to track all outstanding tasks across multiple user messages.
- **Branch Discipline (MANDATORY):** Always work on a feature branch. Before making any code changes, create a branch from `main` with a descriptive name (e.g. `feature/yt-edit-last`, `fix/duplicate-twm`). Never commit directly to `main`. Multiple LLM agents may work on this repo simultaneously — direct commits to `main` cause merge conflicts, accidental inclusions of other agents' uncommitted code (as happened with commit `ae7dde7` — labeled "docs" but included another agent's YouTube regex, which then caused a duplicate `const twM` crash), and divergent history. Merge your feature branch into `main` only after all changes are verified, syntax-checked, and complete. If you have uncommitted changes that aren't yours, `git stash` them before switching branches — do NOT commit another agent's uncommitted changes. Commit hygiene: do not include unrelated code changes in "docs" or "chore" commits. If a commit message says "docs:", the diff must contain ONLY documentation changes.
- **Docker Image Names:** The main container image tag must be `discord-joe`. The intermediate build image must be `rebuilding_discord`. After a successful build, retag as `discord-joe` before stopping the old container and starting the new one.
- **Daily Crypto Analysis:** The bot posts a daily BTC/ETH/ADA/SOL Fear & Greed + news + buy/hold/sell analysis to channel `845958500566695946` ("Криптовая баня") at 15:00 MSK. Implementation lives in `src/services/cryptoService.js` (data fetchers) and `src/services/cryptoScheduler.js` (cron + posting). The same logic is exposed on-demand via the `/cryptoanalysis` slash command and the `/cryptoanalysis` text command. Justification text is strictly limited to 800 characters per coin (excluding coin name, FNG index, and advice). The post is split into a date header + one separate message per coin.

## Architecture

- **Discord bot** ("Робот Джо / Robot Joe") for the sneakyjoe community
- **DeepSeek-primary LLM pipeline**: DeepSeek API answers are accepted directly (no quality estimation); a single local Ollama fallback (`qwen2.5:3b`) is used when a user's cloud-API quota is exhausted or DeepSeek is down. Per-user quota preserved across Discord (discord-joe) + Twitch/YouTube (streamer-joe).
- **Twitch/YouTube IRC migration (2026-07-04):** The Twitch IRC chat watcher (`src/services/twitchChat.js`) is now **disabled** in discord-joe via `TWITCH_IRC_DISABLED=1` env var. All Twitch chat handling (IRC connection, trigger-word detection, LLM replies, chat history recording, first-viewer pings) has been moved to the **streamer-joe** container (`stream_dashboard/app/services/twitch_chat.py`). Discord-Joe keeps Discord only. The `rebuild-run.sh` passes `-e TWITCH_IRC_DISABLED=1` to the container. See the streamer-joe repo for the new Twitch/YouTube architecture.
- **Restream history:** Discord-Joe's `src/services/restreamHistory.js` still monitors the Discord Restream bot channel (`TWITCH_HISTORY_CHANNEL_ID`) and forwards parsed chat messages to streamer-joe's `/api/profiles/bulk?channel=<ch>` endpoint with `X-Service-Key: <SHARE_PASS>` header. This is the only remaining Twitch-related responsibility in discord-joe.
- **Host:** Intel N150 Mini PC, 12GB RAM + 8GB swap (512GB SSD), Intel iGPU (QuickSync/VAAPI). All local services (Ollama, Whisper, SearXNG, transcoding) run on this one host (`localhost`). The only external dependency is the DeepSeek cloud API. Earlier the bot ran on an **Orange Pi Zero 2W** (aarch64/ARM); that host was removed starting with commit `6f5ca45` (2026-06-22, merge of `N150-12GBram`). The old remote hosts (192.168.0.100 / 192.168.0.101) are gone. See `N150_SETUP.md` for the host install steps.
- **LLM routing** (`classifyQueryComplexity`) is still done with the local LLM but now only decides context size — it no longer gates which backend is chosen.
- **Context compilation** (`compileContext`): Dynamically assembles tech knowledge base + user data + personas
- **Secret sanitization** (`sanitizeOutput`): Strips Discord tokens, DeepSeek key, SHARE_PASS from all outputs
- **In-progress status indicator** (`⏳`): All working placeholder messages end with `⏳` to indicate work in progress. Automatically removed from final messages. Cleaned up on bot restart for messages from the last 2 hours.

## Model Assignments

### Hardware
- **Host**: Intel N150 Mini PC, 12GB RAM + 8GB swap (512GB SSD), Intel iGPU (QuickSync/VAAPI). Runs Ollama, Whisper, SearXNG, and the bot container — all on `localhost`. The old remote hosts (Ugreen NAS `192.168.0.100`, Minisforum AI 255 / RTX 2080 `192.168.0.101`) have been removed.

### Available Models (from `data/LLMs.json`)

**Local Models (N150 host, `192.168.0.99:11434`)**:
- `qwen2.5:3b` - Shared general model (fallback + routing + translation) for BOTH bots
- `qwen2.5vl:latest` - Vision/OCR (primary)
- `gemma4:e4b` - Vision/OCR (fallback)
- `nomic-embed-text:latest` - Embeddings
- Small/last-resort TXT models (only loaded via `N150_LAST_RESORT_MODELS` when RAM is tight / primary local model unavailable): `llama3.2:3b`, `llama3.2:1b`, `qwen2.5-coder:1.5b`, `phi3.5:3.8b` — each ~1-3GB q4, coexist with Whisper (~3GB) on the 12GB box without OOMing.

(Remote/cloud `*-cloud` models were dropped — DeepSeek is the primary cloud model and the local `qwen2.5:3b` is the only fallback. `gemma4:12b` and `qwen3.5:9b` were dropped because they don't fit in 12GB RAM alongside Whisper.)

### Current Model Configuration (from `src/config.js`)
- **OLLAMA_MODEL** = `qwen2.5:3b` (local fallback + router — routing is always local)
- **OLLAMA_LARGE_LOCAL_MODEL** = `qwen2.5:3b` (local fallback in `queryLLM`, matches OLLAMA_MODEL; num_ctx 16K)
- **OLLAMA_TRANSLATION_MODEL** = `qwen2.5:3b` (shared with fallback/router)
- **OLLAMA_IMAGE_MODEL** = `qwen2.5vl` (primary vision model)
- **OLLAMA_VISION_MODELS** = `[qwen2.5vl, gemma4:e4b]` (cascade: fast primary → slower fallback)
- **DEEPSEEK_MODEL** = `deepseek-v4-flash` (PRIMARY response model)
- **WHISPER_MODEL** = `Systran/faster-whisper-medium` (used when `AUDIO_TRANSCRIPTION_URL` points to OpenAI-compatible faster-whisper endpoint)
- **AUDIO_TRANSCRIPTION_URL** default = `http://192.168.0.99:8000/v1/audio/transcriptions`; optional accelerated override = `http://192.168.0.99:8001/inference` (whisper.cpp Vulkan/iGPU). Local services are reached at the N150 host LAN IP (`192.168.0.99`) via published ports — not by container name (the `ollama`/`whisper`/`searxng` hostnames fail to resolve with `EAI_AGAIN` when their containers/network are down).

### LLM Pipeline Order (DeepSeek-primary)
1. **DeepSeek API** (`deepseek-v4-flash`) — primary; answer accepted directly (no quality check)
2. **Local Ollama** (`qwen2.5:3b`, 16K ctx) — single direct fallback when quota exhausted / DeepSeek down

### Model Assignment Strategy
- **Routing/Classification**: `qwen2.5:3b` (local) — fast, always local; decides context size only
- **LLM Pipeline**: `deepseek-v4-flash` (DeepSeek API) → `qwen2.5:3b` (local fallback)
- **Translation**: `qwen2.5:3b` (local) → DeepSeek (cloud fallback)
- **Image/OCR**: `qwen2.5vl` (local) → `gemma4:e4b` (local fallback)
- **Embeddings**: `nomic-embed-text:latest` (local)

## Timeout Configuration

All timeouts have been optimized to prevent stuck processes:
- **RAG_OLLAMA_TIMEOUT**: 30 seconds (reduced from 180s) - Main Ollama query timeout
- **RAG_OLLAMA_TIMEOUT_SHORT**: 10 seconds - Quick operations (quality checks)
- **RAG_OLLAMA_TIMEOUT_MEDIUM**: 20 seconds - Medium operations
- **Git commands**: 10 seconds
- **Quality check operations**: 8-10 seconds with `withTimeout` wrapper
- **queryOllamaWithRetry**: 25 seconds overall timeout, max 2 attempts (reduced from 5)

## Key Source Files for LLM Instructions

LLM behavior rules are embedded in source code, not standalone config. Before modifying prompts or orchestration, read:

- `src/services/prompts.js` — All system prompts (identity, language rules, formatting constraints)
- `src/services/llmOrchestrator.js` — LLM routing, quality pipeline, context compilation, timeout handling
- `src/services/personaManager.js` — Community member identification and transliteration
- `src/config.js` — Model names, API endpoints, timeouts, role thresholds
- `personas.md` — Community member knowledge base
- `data/LLMs.json` — Available remote and local models
- `data/model-assignments.md` — Model assignment strategy and priority order

## Bot Commands

### Admin-Only Commands
- **`/delete [count]`**: Delete bot messages (count: 1-100, admin only)
- **`/restart`**: Trigger container rebuild via Docker socket (admin only). Available as both slash command and text command.
- **`/gitlog [count] [filter]`**: View detailed git commit history (admin only, max 50 commits)

### General Access Commands
- **`/gitlog`**: View recent changes summary in Russian (last week, all users)
- **`/delete`**: Delete last bot/webhook message (limited for non-admins)
- **`/process`**: Re-process the link that created the current thread (must be used inside a thread created by a bot repost — Instagram/Twitter/Facebook/article). Allowed for the thread's parent-post author OR a server admin. Re-runs the matching handler with the current code so older posts can be fixed (e.g. cropped-image fix) without re-posting the link in the main channel. Available as both a slash command and a text command.
- **`/edit-last [text]`**: Edit the last bot-replaced message in the channel with new text. The text after the command becomes the new base text and the matching handler (Twitter/Instagram/Facebook/article) is re-run on the existing bot message. If no link is found in the text or the target message, just edits the message text directly. Works in threads (edits the thread starter) and in channels (finds the last bot/webhook message tied to the caller). Allowed for the original post author or a server admin. Available as both a slash command and a text command.
- **`/cryptoanalysis [symbol]`**: On-demand crypto Fear & Greed + news + buy/hold/sell analysis, posted to the "Криптовая баня" channel as a SINGLE message. When `symbol` is omitted the default 4 coins (BTC, ETH, ADA, SOL) are analyzed and combined into one post. When a ticker (e.g. `SOL`) or comma-separated list (e.g. `BTC,ETH,SOL`) is provided only those coins are analyzed. Single-coin posts can be up to ~1800 chars; the default 4-coin combined post is capped at ~1900 chars so it fits in one Discord message. Anyone with channel access can trigger it.

### Text Commands
- **`/gitlog [count] [filter]`**: Same as slash command, text-based alternative
- **`/restart`**: Same as slash command
- **`/delete`**: Same as slash command
- **`/process`**: Same as slash command, text-based alternative; re-processes the link that created the current thread. Must be used inside a bot-repost thread; allowed for the post author or a server admin.
- **`/edit-last [text]`**: Same as slash command, text-based alternative; edits the last bot-replaced message in the channel with new text. Trailing text after the command is the new base text.
- **`/cryptoanalysis [symbol]`**: Same as slash command, text-based alternative; trailing text is treated as a coin symbol or comma-separated list (e.g. `/cryptoanalysis SOL` or `/cryptoanalysis BTC,ETH,SOL`). No channel argument is accepted.
- **`/lookup [user] <phrase>`**: Keyword search across Streamer Joe chat history. Optional leading username scopes the search (e.g. `/lookup godyalis переезд`); without it, searches all users (e.g. `/lookup кот`). Calls the Streamer Joe dashboard `GET /api/chat-search` endpoint. Returns up to 15 matching chat messages (newest first) with timestamp, username, platform, and text. Text command only.

## Subtitle Extraction Workflow

The bot's subtitle extraction follows a strict priority order to prevent unnecessary frame grabbing:

1. **Check for soft subtitle streams** (SRT files) - always first
2. **Audio transcription** (Whisper) - primary method
   - **Whisper hallucination guard** (`isLikelyWhisperHallucination` in `src/services/ocrService.js`): before a Whisper transcript (>50 chars) is accepted as meaningful speech, it is screened for classic hallucination markers on silent/music-only audio — any CJK character (考, 嗯, …; also forbidden by the CJK rule) OR a short (≤400 chars) transcript where the same sentence repeats 2+ times. When detected, the transcript is discarded and the flow falls through to baked-in subtitle OCR (step 3), the correct path for videos whose message is on-screen text. This catches the case where the `isMusicOnlyTranscript` Ollama classifier times out (8s) and defaults to SPEECH, letting a hallucinated looped transcript get translated and posted as garbage.
3. **Baked-in subtitle extraction** (OCR on frames) - ONLY when:
   - No soft subtitles found AND
   - Audio transcription fails OR returns insufficient content (<50 chars) OR is discarded as a Whisper hallucination
   - Audio processing encounters errors

### Timeout Protection
- Maximum 2 minutes per frame extraction
- Maximum 5 minutes total extraction time per video
- Automatic abortion when time limits exceeded
- Graceful degradation when models timeout or fail

## Conventions

- **Language**: Bot responds exclusively in Russian (Cyrillic). CJK characters forbidden. Ollama responses with Chinese characters trigger retry with lower temperature, strip as fallback.
- **Grammar**: Borrowed tech terms masculine; proper Russian declension required.
- **Formatting**: No greetings, no self-introductions, no follow-up questions.
- **Security**: Never expose API keys, tokens, or secrets in any output.
- **No local execution**: Bot runs on remote N150 — do not attempt Docker/Node locally.
- **Git info**: Run `generate-git-info.sh` after Git changes to update `git-info.json`.
- **Docker tags**: Main image → `discord-joe`; build intermediate → `rebuilding_discord`.
- **In-progress status**: All working placeholders end with `⏳`, removed from final messages.

## Daily Crypto Analysis

The bot produces a daily BTC/ETH/ADA/SOL analysis post in the "Криптовая баня" Discord channel (`845958500566695946`) at 15:00 MSK. The same logic is also exposed on-demand via the `/cryptoanalysis` slash command and the `/cryptoanalysis` text command.

### Post Layout (single message)
- The entire post is ONE message in Discord (no separators, no multiple blocks).
- The default 4-coin post fits in one Discord message (≤1900 chars). Single-coin on-demand posts can be up to ~1800 chars.
- The post layout for a combined 4-coin post is:
  - Line 0: `📅 Крипто-анализ на DD.MM.YYYY, HH:MM (MSK)` (date header)
  - Line 1: `**Fear & Greed:** <value> (<classification>)` (one FNG line for the whole post)
  - Lines 2..N: per-coin OP line — single line: `🪙 **<ТИКЕР>**: Краткосрок: <ПОКУПАТЬ|ДЕРЖАТЬ|ПРОДАВАТЬ> (NN%) | Долгосрок: <ПОКУПАТЬ|ДЕРЖАТЬ|ПРОДАВАТЬ> (NN%)`. The LLM writes the words; `emojifyCryptoAdvice` converts the final posted text to emojis: `🪙 **<ТИКЕР>**: ⚡ 🟢 (NN%) | 🎯 🟡 (NN%)` (⚡=Краткосрок/short-term, 🎯=Долгосрок/long-term, 🟢=ПОКУПАТЬ/buy, 🟡=ДЕРЖАТЬ/hold, 🔴=ПРОДАВАТЬ/sell; the certainty `(NN%)` stays attached).
  - Blank line, then the Coin of the Day OP line — single line: `🔍 **Монета дня: <Имя> (<ТИКЕР>)**: ⚡ <advice-emoji> (NN%) | 🎯 <advice-emoji> (NN%)` (a blank line separates it from the per-coin lines above; it carries its own short/long recommendation on the same line).
  - Thread details (Раздел 2, behind `---THREAD_START---`): per-coin block with header `🪙 **<Имя> (<ТИКЕР>)**`, price line (bold `Цена:`), `Заголовки:` block (no `📰` emoji — uses bold label to avoid Discord rendering issues), emojified short/long advice lines `⚡ **(1-7д):** 🟢 (NN%) — ...` and `🎯 **(1-3м):** 🟡 (NN%) — ...` (the LLM writes `📊 **Краткосрок (1-7д):** ПОКУПАТЬ (NN%) — ...`; emojify converts 📊→⚡, 📈→🎯, drops the horizon word, and the advice word→emoji), and a justification block.
- Justification text per coin: ≤400 chars for the default 4-coin combined post (to fit everything in one message) and ≤800 chars for single-coin on-demand posts.
- Each recommendation carries a certainty `(NN%)` (0..100) reflecting signal strength and data quality; the LLM calibrates it (80-95% strong signal, 60-79% moderate, 40-59% mixed, 20-39% dominated by a price anomaly).
- Price anomalies: large 7d/30d moves (|change| >= ~30%, e.g. a single-day pump/dump) are treated as anomalies — the advice weights the underlying multi-day trend over the spike, and the certainty is lowered. The deterministic fallback (`computeFallbackAdvice`) mirrors this with `FALLBACK_ANOMALY_PCT=30`.
- Advice logic is FNG-zone-aware so the bot no longer defaults to ДЕРЖАТЬ when fear is high: Extreme Fear (FNG ≤24) and Fear (FNG 25-49) zones lean toward ПОКУПАТЬ (fear = discount entry) unless the coin is actively crashing (7d ≤ -10%); Neutral (FNG 50-74) defaults to ДЕРЖАТЬ with momentum overrides; Extreme Greed (FNG ≥75) leans toward ПРОДАВАТЬ/profit-taking. The deterministic fallback (`computeFallbackAdvice`) mirrors the same zone rules, returns separate short- (7d-driven) and long-term (30d-driven) recommendations with per-horizon certainty, and applies anomaly handling.
- Emojification is the FINAL step in `runCryptoAnalysisPost` (after all word-based consistency checks via `verifyAndFixAdviceConsistency`), so the verifier still sees the canonical Russian words; only the structured advice lines are emojified (justification prose mentioning ПОКУПАТЬ/ДЕРЖАТЬ/ПРОДАВАТЬ is left untouched).

### On-Demand Variants
- `/cryptoanalysis` (or text `/cryptoanalysis`) — default 4 coins (BTC, ETH, ADA, SOL).
- `/cryptoanalysis SOL` (or text `/cryptoanalysis SOL`) — single coin.
- `/cryptoanalysis BTC,ETH,SOL` (or text `/cryptoanalysis BTC,ETH,SOL`) — explicit list.
- Unknown tickers are resolved against CoinGecko's `/coins/list` (cached for 1h).
- The post always lands in `CRYPTO_CHANNEL_ID` (no channel override option).

### Data Sources
- **Fear & Greed**: `https://api.alternative.me/fng/?limit=1` (market-wide index, applied to each coin).
- **Prices**: CoinGecko `/coins/markets` with dynamic `ids=` (built from the requested coin list).
- **CoinGecko lookup**: `https://api.coingecko.com/api/v3/coins/list?include_platform=false` (cached in-memory for 1h) for resolving custom tickers to CoinGecko ids.
- **News**: `https://cryptopanic.com/news/rss/` (public RSS feed) with `https://api.coinpaprika.com/v1/news` as a fallback. News is keyword-matched to each coin by ticker, name, and a built-in synonym table.

### Lifecycle & Pattern Guidance
The LLM prompt embeds a small "lifecycle cheat sheet" for each coin and a few pattern-recognition hints:
- **Bitcoin (BTC)**: digital-gold narrative, halving cycles, ETF flows, regulatory news.
- **Ethereum (ETH)**: smart-contract platform, DeFi/NFT activity, network upgrades, L2 rollup adoption, gas-fee dynamics.
- **Cardano (ADA)**: long roadmap, developer activity, Voltaire-era milestones, partnerships, DEX liquidity.
- **Other coins (SOL, XRP, DOGE, etc.)**: the LLM is told to use the coin's own narrative and current market dynamics from the data.
- **Patterns**: price/FNG divergence, FNG extremes, volatility compression, 30d ranges.

### Files
- `src/services/cryptoService.js` — FNG/prices/news fetchers, CoinGecko coin-list cache, RSS parser, per-coin keyword filter, prompt-context serializer, symbol-resolution helpers (`resolveCoinBySymbol`, `parseCoinsArgument`).
- `src/services/cryptoScheduler.js` — node-cron job (`0 12 * * *` UTC = 15:00 MSK), `runCryptoAnalysisPost` (shared by the cron and the on-demand commands), rule-based fallback that combines all coins into one message.
- `src/services/prompts.js` — `CRYPTO_ANALYSIS` prompt (Russian, dynamic per-coin justification limit, single-message output).
- `src/config.js` — `CRYPTO_CHANNEL_ID`, `CRYPTO_POST_CRON`, `CRYPTO_COINS`, `CRYPTO_JUSTIFICATION_MAX_CHARS`, public API URLs.
- `index.js` — slash-command registration, scheduler startup, `/cryptoanalysis` interaction handler (parses `symbol` option).
- `src/handlers/messageCreate.js` — text-command support for `/cryptoanalysis` (parses trailing symbol text).

## Git Log Access Feature

The bot can access git log information through `/gitlog` command:

### For All Users
- Shows recent changes from the last week in Russian
- Format: `• YYYY-MM-DD: [translated commit message]`
- No hashes or technical details
- Automatically translated common terms (fix → исправление, feat → новая функция, etc.)

### For Admins (sneakyjoe)
- Detailed view with hashes and commit information
- Can specify count (1-50) and search filter
- Full git log access with technical details

## Startup Cleanup

On bot restart, the following cleanup operations run automatically:

1. **In-progress message cleanup**: Scans all channels for messages from the last 2 hours with `⏳` status
   - Finalizes messages with content (removes status indicator)
   - Deletes empty messages (just status)
2. **Orphaned task recovery**: Recovers from `taskPersistence.js`
3. **Catch-up scan**: Scans channels for missed requests
4. **Service verification**: Verifies all external services (Ollama, DeepSeek, Whisper)

## Build and Deploy

- **Docker**: See `Dockerfile` and `rebuild-run.sh` — deployed on the remote N150 host (`192.168.0.99`) via SSH, not on the local dev machine.
- **Container image tags**: `discord-joe` (main), `rebuilding_discord` (build intermediate)
- **Configuration**: `src/config.js` for all runtime settings
- **Model assignments**: `data/model-assignments.md` for strategy documentation
- **Available models**: `data/LLMs.json` for inventory