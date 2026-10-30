# AutoBot — AI Automation Suite for Discord

**একটা Discord বট, চারটা প্রোডাকশন-লেভেল অটোমেশন।**

| # | Feature | One-liner |
|---|---------|-----------|
| 🕵️ | **Leak Monitor** | ইমেইল/ডোমেইন/কিওয়ার্ড মনিটর করে, লিক পেলে Discord-এ অ্যালার্ট দেয় |
| 🎬 | **Cinematic Director** | এক লাইনের আইডিয়া → স্ক্রিপ্ট → ভয়েসওভার → স্টক ফুটেজ → রিল (mp4) |
| 🎧 | **AI DJ** | মুড বললেই প্লেলিস্ট বানিয়ে ভয়েস চ্যানেলে লাইভ বাজায় |
| 🚀 | **Market Sniper** | লাইভ ক্রিপ্টো ডেটা, Reddit হাইপ, AI অ্যানালাইসিস আর প্রাইস অ্যালার্ট |

English overview and setup instructions follow the Bangla quick start.

---

## ⚡ Quick Start (বাংলা)

### ১. যা যা লাগবে

- Python **3.11+**
- **ffmpeg** (ভিডিও রেন্ডার + ভয়েস প্লেব্যাকের জন্য) — `sudo apt install ffmpeg` / `brew install ffmpeg`
- একটা Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications) → Bot)
- *(ঐচ্ছিক)* Gemini API key — [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- বাংলা ক্যাপশন ঠিকভাবে দেখতে: `sudo apt install fonts-noto-bengali`

### ২. ইনস্টল

```bash
git clone https://github.com/m447588103-design/auto-bot
cd auto-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env                 # DISCORD_BOT_TOKEN অবশ্যই দিন
python scripts/check_setup.py --network   # সব ঠিক আছে কিনা দেখুন
python main.py
```

### ৩. Discord-এ ইনভাইট

Developer Portal → **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands` →
permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`,
`Connect`, `Speak`, `Use Slash Commands`.

### ৪. প্রথম কমান্ড

```
/leak add value:acme.com kind:domain
/leak channel #security-alerts
/leak scan

/movie create theme:A lonely astronaut on Mars finding a glowing flower

/dj play mood:bhai mon bhalo na, ektu lofi bajao

/sniper hype
/sniper watch symbol:BTC direction:above price:100000
```

> সব কমান্ড `/` (slash) দিয়ে চলে। পুরনো `!agent`, `!doctor`, `!ping`-ও কাজ করে।

---

## 🧩 Architecture

```
main.py                     # bot bootstrap, service wiring, command sync
├── core/
│   ├── config.py           # typed env config (every key optional & safe)
│   ├── db.py               # async SQLite wrapper (WAL, thread-safe)
│   ├── gemini.py           # Gemini client (JSON mode, retries, fallbacks)
│   ├── http.py             # aiohttp sessions, retry/backoff, 429 handling
│   ├── discord_ui.py       # embeds, progress reporter, disclaimers
│   ├── logging_setup.py    # console + rotating file logs
│   └── utils.py            # pure helpers (formatting, masking, splitting)
├── features/
│   ├── leak_monitor/       # models, matcher, sources, store, monitor, cog
│   ├── cinematic/          # scriptwriter, tts, stock, captions, renderer,
│   │                       # pipeline, publishers, cog
│   ├── dj/                 # mood, spotify, youtube, curator, player, cog
│   ├── sniper/             # market, sentiment, analyst, alerts, cog
│   └── assistant/          # !agent, /help, /doctor
├── tests/                  # 69 offline tests (no network, no ffmpeg needed)
├── scripts/                # setup checker + YouTube OAuth helper
├── assets/music/           # drop royalty-free background tracks here
└── data/                   # sqlite DB, rendered videos, logs (git-ignored)
```

Design rules used everywhere:

- **Graceful degradation** — missing API key disables one integration, never crashes the bot.
- **Background work never blocks Discord** — ffmpeg runs as a subprocess, CPU work in threads,
  SQLite behind `asyncio.to_thread`.
- **Everything is de-duplicated and rate-limited** — leak hits, alerts, API calls.
- **Untrusted data is never rendered raw** — snippets are HTML-stripped and truncated;
  credentials found in stealer logs are intentionally withheld.

---

## 🕵️ 1. Dark Web & Leak Monitor

**কী করে:** কোম্পানির ডোমেইন, ইমেইল, ফোন, ইউজারনেম বা কিওয়ার্ড ওয়াচলিস্টে রাখলে বট
ব্যাকগ্রাউন্ডে বিভিন্ন লিক সোর্স স্ক্যান করে, ম্যাচ পেলে নির্দিষ্ট চ্যানেলে severity-সহ
অ্যালার্ট পাঠায়।

| Source | Data | Key |
|--------|------|-----|
| Have I Been Pwned | Account/domain breach records | `HIBP_API_KEY` (paid) |
| Hudson Rock (Cavalier) | Infostealer infections | free |
| ransomwatch | Ransomware leak-site victim posts | free |
| psbdmp.ws | Paste-site dumps | free |
| GitHub code search | Secrets accidentally committed | `GITHUB_TOKEN` (optional) |

**Commands**

| Command | Description |
|---------|-------------|
| `/leak add value kind label` | ওয়াচ টার্গেট যোগ (kind অটো-ডিটেক্ট হয়) |
| `/leak list` | সব টার্গেট দেখা |
| `/leak remove target_id` | টার্গেট সরানো |
| `/leak scan` | সাথে সাথে স্ক্যান (2 min cooldown) |
| `/leak hits limit` | সাম্প্রতিক লিক হিট |
| `/leak channel #channel` | অ্যালার্ট চ্যানেল সেট |
| `/leak status` | সোর্স হেলথ, কাউন্টার, শেষ স্ক্যান |

**Matching logic** (`features/leak_monitor/matcher.py`)

- Domain: matches `acme.com`, `login.acme.com`, `https://acme.com/x` — but **not**
  `notacme.com` or `acme.com.evil.net`.
- Email: exact address, not the `.br` lookalike.
- Keyword: unicode-aware word boundaries (`Project Falcon` ≠ `Project Falconry`).
- Phone: matches across formatting (`+880 1712-345678`).
- Alerts mask emails/phones/usernames; the same hit is never sent twice
  (SHA-256 fingerprint in SQLite).

**Permissions:** watchlist admin (`add`, `remove`, `scan`, `channel`) needs **Manage Server**; `/leak list`, `/leak hits`, `/leak status` are open to members.

**Alerts setup:** set the channel (`/leak channel`) → add a target → wait for the next scan (up to `LEAK_SCAN_INTERVAL`) or force one with `/leak scan`.

**Ethics / legal:** শুধু নিজের বা অনুমোদিত প্রতিষ্ঠানের সম্পদ মনিটর করুন। বট কখনো
পাসওয়ার্ড বা স্টোলেন ক্রেডেনশিয়াল দেখায় না, শুধু "কোথায় পাওয়া গেছে" মেটাডেটা।

---

## 🎬 2. AI Cinematic Movie Director

**Flow**

```
আইডিয়া → Gemini script (JSON scenes) → edge-tts voiceover (+ word timings)
        → Pexels vertical clips / picsum fallback → per-scene 1080×1920 edit
        → concat → ASS captions burn-in → music mix → size ladder → Discord mp4
```

- **Script:** Gemini writes hook, 3-8 scenes, visual search queries, music mood, hashtags.
  Bangla/Banglish আইডিয়া দিলে ন্যারেশন বাংলায় লেখে এবং `bn-*` ভয়েস বেছে নেয়।
- **Voice:** `edge-tts` (free neural voices), fallback `gTTS`; per-scene audio is padded to the scene length.
- **Footage:** Pexels portrait HD videos (free key). Key না থাকলে বা রেজাল্ট না পেলে
  `picsum.photos` স্টিল + Ken Burns zoom ব্যবহার হয় — তাই রেন্ডার কখনো ফেল করে না।
- **Captions:** word-by-word ASS captions (Arial/DejaVu, 1080×1920), title card at the start.
- **Music:** `assets/music/`-এ রাখা যেকোনো ট্র্যাক random ভাবে ১০% ভলিউমে মিক্স হয়।
- **Size:** Discord লিমিট অনুযায়ী 720p → 540p → 480p ladder-এ রি-এনকোড হয়।
- **Queue:** একসাথে একটা রেন্ডার (CPU বাঁচাতে), প্রতি ইউজার ৫ মিনিট cooldown, `/movie cancel`।

**Commands**

| Command | Description |
|---------|-------------|
| `/movie create theme [publish]` | পুরো রিল বানিয়ে চ্যানেলে পাঠায় (5-15 min render) |
| `/movie preview theme` | শুধু স্ক্রিপ্ট দেখায় |
| `/movie status` | ffmpeg/TTS/disk হেলথ, চলমান জব, সাম্প্রতিক জব |
| `/movie cancel job_id` | জব বাতিল |

**YouTube Shorts:** `publish:true` দিলে (এবং OAuth সেট থাকলে) `#Shorts` হিসেবে আপলোড হয়।
একবার সেটআপ:

```bash
pip install google-api-python-client google-auth google-auth-oauthlib
python scripts/youtube_auth.py
# .env: YOUTUBE_CLIENT_SECRET=credentials/client_secret.json
#       YOUTUBE_TOKEN_FILE=credentials/youtube_token.json
```

**Instagram Reels:** Graph API শুধু পাবলিক HTTPS URL + Business account নেয়, তাই বট
mp4 Discord-এ দেয় — সেখান থেকে ডাউনলোড করে Instagram app-এ আপলোড করুন।

---

## 🎧 3. AI Spotify / YouTube DJ

**কী করে:** ভয়েস চ্যানেলে থাকলে `/dj play` দিয়ে মুড লিখলেই বট:

1. Gemini (না থাকলে bilingual keyword engine) দিয়ে মুড বোঝে — English, বাংলা, Banglish।
2. Spotify (যদি key থাকে) থেকে genre-accurate ট্র্যাক সিড নেয়।
3. প্রতিটা ট্র্যাক YouTube-এ resolve করে (`yt-dlp`)।
4. `ffmpeg` দিয়ে ভয়েস চ্যানেলে স্ট্রিম করে, queue/autoplay/loop/volume সব হ্যান্ডল করে।

> **কেন Spotify সরাসরি বাজে না?** Spotify-এর DRM অডিও Discord voice-এ Stream করা যায় না
> (Terms of Service-ও বাধা দেয়)। তাই Spotify curation-এর জন্য, playback YouTube-এ।

**Commands**

| Command | Description |
|---------|-------------|
| `/dj play mood` | মুড বুঝে প্লেলিস্ট বানিয়ে বাজায় |
| `/dj add query` | নির্দিষ্ট গান/সার্চ কিউতে যোগ |
| `/dj queue` | চলমান + পরের ট্র্যাক |
| `/dj skip` · `/dj stop` | ট্র্যাক স্কিপ · সব বন্ধ/কিউ ক্লিয়ার |
| `/dj pause` · `/dj resume` | পজ/রিজিউম |
| `/dj volume 0-200` | ভলিউম |
| `/dj loop off/one/all` | লুপ মোড |
| `/dj autoplay true/false` | কিউ শেষ হলে নিজে থেকে আরও গান |
| `/dj nowplaying` | এখন কী বাজছে |
| `/dj leave` | ভয়েস চ্যানেল থেকে বের হওয়া |

**Mood engine** Banglish/Bangla keywords বোঝে — যেমন `mon bhalo na`, `kosto`, `pora`,
`gym`, `bristi`, `party`, `ghoom`, `purano gaan`, `মন খারাপ`, `বৃষ্টি` — এবং explicit
genre (lofi, hyperpop, phonk, k-pop, bangla, hindi…) pin করে।

**Requirements:** `PyNaCl` (requirements-এ আছে), ffmpeg, `yt-dlp`। Datacenter IP-তে
YouTube sign-in চাইলে `YTDLP_COOKIES_FILE` সেট করুন।

---

## 🚀 4. AI Crypto & Meme Coin Sniper

**Data sources**

| Source | What | Key |
|--------|------|-----|
| CoinGecko | Prices, 1h/24h/7d changes, market cap, trending, global stats | optional demo key |
| Dexscreener | New token profiles, DEX pairs, liquidity/FDV | free |
| Reddit | r/CryptoCurrency + meme subs hot posts → mention/hype scoring | free |
| X (Twitter) | Recent tweet volume per cashtag | `X_BEARER_TOKEN` optional |
| Google News RSS | Headlines | free |
| Finnhub | US stock quotes (for `/sniper stock`) | `FINNHUB_API_KEY` optional |

**Commands**

| Command | Description |
|---------|-------------|
| `/sniper hype` | Reddit + CoinGecko trending হাইপ র্যাডার |
| `/sniper coin BTC` | লাইভ প্রাইস, changes, market cap + নিউজ |
| `/sniper analyze SOL` | AI verdict (bullish/neutral/bearish), signals, risks, বাংলা সারাংশ |
| `/sniper movers` | ২৪ ঘণ্টার top gainers/losers |
| `/sniper newpairs 5` | নতুন DEX লিস্টিং (liquidity/FDV/age) |
| `/sniper stock AAPL` | US stock quote |
| `/sniper watch symbol direction price` | প্রাইস অ্যালার্ট (above/below) |
| `/sniper alerts` · `/sniper unwatch id` | অ্যালার্ট লিস্ট/ডিলিট |
| `/sniper channel #channel` | ডিফল্ট অ্যালার্ট চ্যানেল |

**Hype scoring:** mentions × 2 + log10(upvotes) × 1.5 + trending bonus 4 + 24h momentum bonus.
`NEAR`, `LINK`-এর মতো ইংরেজি শব্দের সাথে মিলে যাওয়া ticker-গুলোতে শুধু cashtag/uppercase
mention গোনা হয়, যাতে false positive না হয়।

**AI analysis:** Gemini কে structured snapshot (price, momentum, social, news, global stats) দেওয়া হয়,
strict JSON back — invalid হলে rule-based fallback চলে এবং embed-এ সেটা স্পষ্ট লেখা থাকে।

> ⚠️ **Not financial advice.** সব এমবেডে ডিসক্লেইমার থাকে। meme coin/new pair অত্যন্ত ঝুঁকিপূর্ণ।

---

## 👤 Assistant commands

| Command | Description |
|---------|-------------|
| `/help` | সব কমান্ডের তালিকা |
| `/features` | ফিচার ওভারভিউ |
| `/doctor` | API keys, ffmpeg, TTS, DB, disk চেক (secrets কখনো দেখায় না) |
| `!agent <question>` | Gemini general assistant (ঐচ্ছিক Playwright browsing) |
| `!ping` | Gateway latency |

---

## ⚙️ Configuration highlights

পুরো লিস্ট `.env.example`-এ (comments সহ)। গুরুত্বপূর্ণগুলো:

| Variable | Default | Notes |
|----------|---------|-------|
| `DISCORD_BOT_TOKEN` | — | **required** |
| `DEV_GUILD_IDS` | — | slash commands সাথে সাথে sync করতে guild id |
| `GEMINI_API_KEY` | — | না থাকলে demo template / rule engine চলে |
| `PEXELS_API_KEY` | — | না থাকলে picsum fallback |
| `HIBP_API_KEY` | — | HIBP scan skip হয় |
| `SPOTIFY_CLIENT_ID/SECRET` | — | curation enrich করে |
| `FINNHUB_API_KEY` | — | `/sniper stock` চালু করে |
| `CINEMATIC_MAX_SECONDS` | 60 | reel-এর সর্বোচ্চ দৈর্ঘ্য |
| `MAX_UPLOAD_MB` | 24 | রিল আপলোডের fallback সীমা (boost না থাকলে ~10 MB কার্যকর) |
| `CAPTION_FONT` | DejaVu Sans | বাংলার জন্য `Noto Sans Bengali` ইনস্টল করুন |
| `LOG_LEVEL` | INFO | DEBUG-এ বেশি তথ্য |

`/doctor` বা `python scripts/check_setup.py --network` দিয়ে সব যাচাই করা যায়।

---

## 🐳 Docker

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

Image-এ ffmpeg, `fonts-dejavu`, `fonts-noto-bengali` আর Python deps আছে। `data/`,
`assets/music/`, `credentials/` ভলিউম হিসেবে মাউন্ট হয় (renders ও token persist করে)।

---

## 🧪 Development

```bash
pip install -r requirements-dev.txt
pytest              # 69 offline tests: matcher, store, mood, scripts, captions,
                    # pipeline orchestration (fakes), alerts, command registration
ruff check .
ruff check . --fix
```

Offline tests-এর জন্য কোনো Discord token, API key বা ffmpeg লাগে না — pipeline টেস্টে
TTS/stock/ffmpeg ফাংশন monkeypatch করা হয়।

---

## 🩺 Troubleshooting

| সমস্যা | সমাধান |
|--------|--------|
| Slash commands দেখাচ্ছে না | `DEV_GUILD_IDS=<guild id>` সেট করে রিস্টার্ট; global sync-এ ১ ঘণ্টা লাগতে পারে |
| `ffmpeg not found` | `apt install ffmpeg`; custom path হলে `FFMPEG_BIN` |
| রিল ফেইল: "no TTS engine" | `pip install edge-tts` (fallback `gTTS`) |
| বাংলা লেখা ▯▯▯ দেখাচ্ছে | `apt install fonts-noto-bengali`, `CAPTION_FONT=Noto Sans Bengali` |
| Voice-এ কিছু শোনা যাচ্ছে না | `PyNaCl` ইনস্টল? `Connect`+`Speak` permission? ffmpeg? |
| YouTube "sign in to confirm" | `YTDLP_COOKIES_FILE=credentials/ytdlp-cookies.txt` |
| CoinGecko 429 | `COINGECKO_API_KEY` (free demo key) সেট করুন |
| Reddit 403/429 | cloud IP block — অন্য host বা `X_BEARER_TOKEN` ব্যবহার করুন |
| HIBP 403 on domain | HIBP dashboard-এ domain verify করতে হয় |
| ভিডিও ১০ MB-র বেশি | boost ছাড়া লিমিট; `CINEMATIC_MAX_UPLOAD_MB=9` |
| `GEMINI_API_KEY` নেই | সব ফিচার fallback-এ চলে; আসল AI আউটপুটের জন্য key দিন |

---

## 🔐 Safety, privacy & limits

- **Authorized monitoring only.** leak monitor নিজের/অনুমোদিত সম্পদের জন্য ব্যবহার করুন।
- Watch targets আর hits শুধু আপনার `data/autobot.db`-তে থাকে; থার্ড পার্টি API-তে শুধু
  কোয়েরি যায় (HIBP/Hudson Rock)। কোনো ক্রেডেনশিয়াল সংরক্ষণ বা দেখানো হয় না।
- Generated reels-এ ব্যবহৃত stock clips-এর ক্রেডিট bot embed-এ দেওয়া হয়; Pexels licence মানুন।
- Spotify শুধু curation-এ ব্যবহৃত, DRM circumvention নেই।
- Market data informational; bot কখনো trade করে না বা wallet access করে না।
- Rate limits (CoinGecko/HIBP/Reddit/X) মেনে চলা হয়; বেশি ট্রাফিক হলে key বা interval বাড়ান।
- Discord ToS: self-bot নয়, normal bot token; privileged intents portal-এ enable করুন।

## 📄 License

MIT — আপনি নিজের দায়িত্বে deploy ও modify করুন।
