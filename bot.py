#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI News Telegram Bot - полностью автономный агент.
Запускается из GitHub Actions. Ищет свежие AI-новости в RSS,
выбирает лучшую через LLM, пишет пост, генерирует картинку, публикует в Telegram.
"""

import os
import re
import io
import sys
import json
import time
import random
import base64
import hashlib
import datetime as dt
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse, quote
from zoneinfo import ZoneInfo

import requests
import feedparser
from bs4 import BeautifulSoup

# ============================== КОНФИГ ==============================

MSK = ZoneInfo("Europe/Moscow")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHANNEL_ID", "-1004319183379").strip()
TG_ADMIN = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()

# --- GitHub Models: основной LLM. Работает из России, ключ = токен GitHub ---
GH_TOKEN = (os.environ.get("GH_MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
GH_MODEL = os.environ.get("GH_MODEL", "openai/gpt-4.1-mini").strip()

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "auto").strip()

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_IMAGE_MODEL = os.environ.get("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip()

SEED_SALT = os.environ.get("SEED_SALT", "ai-news-bot")
FORCE = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "30"))
MIN_SCORE = float(os.environ.get("MIN_SCORE", "6"))
SLOT_MINUTES = 20

WINDOWS = {
    "morning": (8, 10),
    "day": (13, 15),
    "evening": (19, 21),
}

# tier 1 = официальные источники, tier 2 = крупные медиа, tier 3 = сообщества
FEEDS = [
    ("https://openai.com/news/rss.xml", "OpenAI Blog", 1),
    ("https://blog.google/technology/ai/rss/", "Google AI Blog", 1),
    ("https://deepmind.google/blog/rss.xml", "Google DeepMind", 1),
    ("https://blogs.microsoft.com/ai/feed/", "Microsoft AI", 1),
    ("https://huggingface.co/blog/feed.xml", "Hugging Face", 1),
    ("https://blog.cloudflare.com/tag/ai/rss/", "Cloudflare AI", 1),
    ("https://aws.amazon.com/blogs/machine-learning/feed/", "AWS ML Blog", 1),
    ("https://stability.ai/news?format=rss", "Stability AI", 1),
    ("https://news.google.com/rss/search?q=Anthropic+Claude+when:2d&hl=en-US&gl=US&ceid=US:en", "Anthropic (news)", 1),
    ("https://news.google.com/rss/search?q=%22Meta+AI%22+OR+%22Llama+model%22+when:2d&hl=en-US&gl=US&ceid=US:en", "Meta AI (news)", 1),
    ("https://news.google.com/rss/search?q=xAI+Grok+when:2d&hl=en-US&gl=US&ceid=US:en", "xAI (news)", 1),
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch", 2),
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "The Verge", 2),
    ("https://arstechnica.com/ai/feed/", "Ars Technica", 2),
    ("https://venturebeat.com/category/ai/feed/", "VentureBeat", 2),
    ("https://www.wired.com/feed/tag/ai/latest/rss", "Wired", 2),
    ("https://www.technologyreview.com/topic/artificial-intelligence/feed", "MIT Tech Review", 2),
    ("https://the-decoder.com/feed/", "The Decoder", 2),
    ("https://www.marktechpost.com/feed/", "MarkTechPost", 2),
    ("https://hnrss.org/newest?q=AI+OR+LLM+OR+OpenAI+OR+Anthropic&points=100", "Hacker News", 3),
    ("https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day", "r/LocalLLaMA", 3),
]

KW_STRONG = {
    "gpt": 5, "chatgpt": 5, "openai": 5, "claude": 5, "anthropic": 5, "gemini": 5,
    "grok": 4, "llama": 4, "mistral": 3, "deepseek": 4, "qwen": 3, "midjourney": 4,
    "sora": 4, "veo": 4, "runway": 3, "suno": 4, "stable diffusion": 3, "flux": 3,
    "ai agent": 5, "agents": 3, "copilot": 3, "cursor": 3, "model": 2, "launch": 2,
    "release": 2, "releases": 2, "announce": 3, "unveil": 3, "open-source": 3,
    "open source": 3, "benchmark": 2, "multimodal": 3, "reasoning": 2, "api": 2,
    "free": 2, "update": 2, "neural": 2, "llm": 4, "artificial intelligence": 3,
    "robot": 2, "image generation": 3, "video generation": 4, "voice": 2,
}
KW_BAD = {
    "lawsuit": -3, "stock": -2, "shares": -2, "hiring": -2, "op-ed": -3,
    "opinion": -3, "podcast": -3, "deals": -4, "discount": -4, "coupon": -5,
    "best laptops": -5, "how to watch": -5, "review:": -2, "sponsored": -5,
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-news-bot/1.0)"}


def log(*a):
    print(f"[{dt.datetime.now(MSK):%H:%M:%S}]", *a, flush=True)


# ============================== РАСПИСАНИЕ ==============================

def current_window(now):
    for name, (h1, h2) in WINDOWS.items():
        if h1 <= now.hour < h2:
            return name, h1, h2
    return None, None, None


def should_run_now(now):
    """Детерминированно-случайный слот на каждый день и каждое окно."""
    if FORCE:
        return True, "force"
    name, h1, h2 = current_window(now)
    if not name:
        return False, "вне окна публикации"
    slots = []
    t = now.replace(hour=h1, minute=0, second=0, microsecond=0)
    end = now.replace(hour=h2, minute=0, second=0, microsecond=0)
    while t < end:
        slots.append(t)
        t += dt.timedelta(minutes=SLOT_MINUTES)
    seed = int(hashlib.sha256(f"{now:%Y-%m-%d}:{name}:{SEED_SALT}".encode()).hexdigest()[:12], 16)
    chosen = random.Random(seed).choice(slots)
    cur = now.replace(minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES, second=0, microsecond=0)
    if cur == chosen:
        return True, name
    return False, f"слот {cur:%H:%M} != выбранного {chosen:%H:%M} ({name})"


# ============================== ИСТОРИЯ ==============================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(items):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).isoformat()
    items = [i for i in items if i.get("published_at", "") >= cutoff][-800:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def canon_url(url):
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc.replace("www.", ""), p.path.rstrip("/"), "", "", "")).lower()
    except Exception:
        return url.lower()


def norm_title(t):
    t = re.sub(r"[^a-zа-я0-9 ]+", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def news_id(title, url):
    return hashlib.sha256((norm_title(title) + "|" + canon_url(url)).encode()).hexdigest()[:16]


def is_duplicate(item, history):
    hid = news_id(item["title"], item["url"])
    urls = {h.get("url_canon") for h in history}
    if hid in {h.get("id") for h in history}:
        return True
    if canon_url(item["url"]) in urls:
        return True
    nt = norm_title(item["title"])
    for h in history[-150:]:
        if SequenceMatcher(None, nt, norm_title(h.get("title", ""))).ratio() > 0.72:
            return True
        a = {w for w in nt.split() if len(w) > 3}
        b = {w for w in norm_title(h.get("title", "")).split() if len(w) > 3}
        if a and b and len(a & b) / max(1, min(len(a), len(b))) > 0.75:
            return True
    return False


# ============================== СБОР НОВОСТЕЙ ==============================

def entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        v = e.get(k)
        if v:
            try:
                return dt.datetime(*v[:6], tzinfo=dt.timezone.utc)
            except Exception:
                pass
    return None


def clean_text(html):
    if not html:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ")).strip()


def score_item(item):
    text = (item["title"] + " " + item["summary"][:300]).lower()
    s = 0.0
    for kw, w in KW_STRONG.items():
        if kw in text:
            s += w
    for kw, w in KW_BAD.items():
        if kw in text:
            s += w
    s += {1: 6, 2: 3, 3: 1}.get(item["tier"], 0)
    age_h = (dt.datetime.now(dt.timezone.utc) - item["dt"]).total_seconds() / 3600
    s += max(0.0, 12 - age_h / 2)
    return s


def collect():
    items, seen = [], set()
    now = dt.datetime.now(dt.timezone.utc)
    for url, source, tier in FEEDS:
        try:
            raw = requests.get(url, headers=UA, timeout=25).content
            fp = feedparser.parse(raw)
        except Exception as e:
            log(f"  ! фид недоступен {source}: {e}")
            continue
        cnt = 0
        for e in fp.entries[:30]:
            t = entry_time(e)
            if not t or (now - t).total_seconds() / 3600 > MAX_AGE_HOURS:
                continue
            link = (e.get("link") or "").split("?utm")[0]
            title = clean_text(e.get("title", ""))
            if not link or not title:
                continue
            cu = canon_url(link)
            if cu in seen:
                continue
            seen.add(cu)
            items.append({
                "title": title,
                "url": link,
                "source": source,
                "tier": tier,
                "dt": t,
                "summary": clean_text(e.get("summary", ""))[:600],
            })
            cnt += 1
        log(f"  {source}: {cnt} свежих")
    for it in items:
        it["score"] = score_item(it)
    items.sort(key=lambda x: -x["score"])
    return items


def fetch_article(url, limit=7000):
    """Достаём текст статьи, чтобы LLM писал по фактам, а не выдумывал."""
    try:
        r = requests.get(url, headers=UA, timeout=25)
        soup = BeautifulSoup(r.content, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            t.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        if not node:
            return ""
        paras = [clean_text(str(p)) for p in node.find_all(["p", "h2", "h3", "li"])]
        text = "\n".join(p for p in paras if len(p) > 40)
        return text[:limit]
    except Exception as e:
        log(f"  ! не удалось скачать статью: {e}")
        return ""


# ============================== LLM ==============================

def _openai_style(url, key, model, prompt, temperature, max_tokens, extra_headers=None):
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    h.update(extra_headers or {})
    r = requests.post(url, headers=h, timeout=120, json={
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": min(max_tokens, 4000),
    })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def openrouter_models():
    """Сам подбирает рабочие бесплатные модели OpenRouter."""
    models = []
    if OPENROUTER_MODEL and OPENROUTER_MODEL != "auto":
        models.append(OPENROUTER_MODEL)
    try:
        data = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
        free = [m["id"] for m in data
                if str(m.get("pricing", {}).get("prompt", "1")) in ("0", "0.0", "0.00")
                and int(m.get("context_length") or 0) >= 16000]
        pref = ("deepseek", "llama", "qwen", "mistral", "gemma", "glm", "gpt")
        free.sort(key=lambda i: min([k for k, p in enumerate(pref) if p in i.lower()] or [99]))
        models += [m for m in free if m not in models][:6]
        log(f"  OpenRouter: бесплатных моделей {len(free)}")
    except Exception as e:
        log(f"  ! список моделей OpenRouter недоступен: {e}")
    return models or ["meta-llama/llama-3.3-70b-instruct:free"]


def llm(prompt, temperature=0.7, max_tokens=2000):
    """GitHub Models (основной) -> Gemini -> OpenRouter (резерв)."""
    if GH_TOKEN:
        for url, model in (
            ("https://models.github.ai/inference/chat/completions", GH_MODEL),
            ("https://models.inference.ai.azure.com/chat/completions", GH_MODEL.split("/")[-1]),
        ):
            try:
                return _openai_style(url, GH_TOKEN, model, prompt, temperature, max_tokens,
                                     {"X-GitHub-Api-Version": "2026-03-10", "Accept": "application/vnd.github+json"})
            except Exception as e:
                log(f"  ! GitHub Models недоступен: {e}")
    if GEMINI_KEY:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
                },
                timeout=120,
            )
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception as e:
            log(f"  ! Gemini недоступен: {e}")
    if OPENROUTER_KEY:
        for model in openrouter_models():
            try:
                return _openai_style("https://openrouter.ai/api/v1/chat/completions", OPENROUTER_KEY,
                                     model, prompt, temperature, max_tokens)
            except Exception as e:
                log(f"  ! OpenRouter {model}: {e}")
    raise RuntimeError("Нет доступного LLM-провайдера")


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM вернул не JSON: " + text[:300])
    s = m.group(0)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", s))


def pick_best(candidates, history):
    recent_titles = [h.get("title", "") for h in history[-40:]]
    lines = []
    for i, c in enumerate(candidates):
        age = int((dt.datetime.now(dt.timezone.utc) - c["dt"]).total_seconds() / 3600)
        lines.append(f'{i}. [{c["source"]}, {age}ч назад] {c["title"]} — {c["summary"][:180]}')
    prompt = f"""Ты — редактор популярного русскоязычного Telegram-канала про ИИ и нейросети.
Аудитория: технически любопытные люди, не обязательно разработчики.

Ниже список свежих новостей-кандидатов. Выбери ОДНУ самую интересную и ценную для канала.

Приоритет (по убыванию):
1) релизы новых AI-моделей; 2) новые функции популярных нейросетей (ChatGPT, Claude, Gemini, Grok);
3) новые полезные AI-инструменты, которые читатель может попробовать сам; 4) AI-агенты;
5) важные события у OpenAI, Anthropic, Google, Meta, xAI; 6) впечатляющие возможности моделей; 7) исследования.

Отклоняй: биржевые/юридические/кадровые новости, мнения и колонки, рекламу, подборки скидок,
слухи без подтверждения, воду, очевидные вещи, локальные бизнес-новости без продукта.

Уже опубликовано ранее (не выбирай похожее):
{chr(10).join('- ' + t for t in recent_titles) or '- (пусто)'}

Кандидаты:
{chr(10).join(lines)}

Ответь СТРОГО одним JSON-объектом:
{{"index": <номер лучшего кандидата>, "score": <оценка ценности 0-10>, "reason": "<1 предложение почему>"}}
Если ни одна новость не тянет на публикацию — верни score ниже 5."""
    data = parse_json(llm(prompt, temperature=0.3, max_tokens=500))
    idx = int(data.get("index", -1))
    if not (0 <= idx < len(candidates)):
        raise ValueError("LLM вернул некорректный индекс")
    return candidates[idx], float(data.get("score", 0)), data.get("reason", "")


def write_post(item, article_text):
    prompt = f"""Ты — автор топового русскоязычного Telegram-канала про ИИ и нейросети.
Напиши пост по новости НИЖЕ. Пиши ТОЛЬКО по фактам из предоставленного текста.
Категорически запрещено выдумывать цифры, даты, названия, цены и возможности.
Если какого-то факта в тексте нет — просто не упоминай его.

ЗАГОЛОВОК НОВОСТИ: {item['title']}
ИСТОЧНИК: {item['source']}
URL: {item['url']}

ТЕКСТ СТАТЬИ:
{article_text[:6000]}

ФОРМАТ ПОСТА:
🚀 короткий цепляющий заголовок

лид: 1-2 предложения, главная суть

2-4 коротких абзаца по сути

Если это новая модель — укажи название, компанию, ключевые возможности, отличие от прошлой версии, доступность и цену (только если есть в тексте).
Если это новый инструмент — что это, кто сделал, что умеет, кому полезно, бесплатно ли, где попробовать (только если есть в тексте).
Можно использовать список с • для 2-4 пунктов.

В конце — одна короткая вовлекающая фраза или вопрос.

СТИЛЬ: живой, современный, технологичный, понятный обычному человеку, короткие абзацы,
без канцелярита, без жаргона без нужды, без ощущения робота, эмодзи умеренно (3-6 на весь пост).
Не пересказывай пресс-релиз занудно. Не пиши штампы вроде "В мире технологий".

ОГРАНИЧЕНИЯ ФОРМАТА:
- длина текста поста: 550-900 символов, это жёсткое требование;
- разметка Telegram HTML: разрешены только <b>, <i>, <code>, <a href="">; никаких markdown-звёздочек;
- НЕ добавляй строку Источник — её добавит система;
- НЕ добавляй хэштеги.

Также придумай промпт для генерации изображения к посту НА АНГЛИЙСКОМ:
конкретный визуал по смыслу новости, современный, качественный, кинематографичный,
без текста и букв на картинке, без логотипов, горизонтальный кадр.

Ответь СТРОГО одним JSON-объектом:
{{"post": "<текст поста с переносами строк>", "image_prompt": "<english image prompt>"}}"""
    data = parse_json(llm(prompt, temperature=0.85, max_tokens=2500))
    post = (data.get("post") or "").strip()
    img = (data.get("image_prompt") or "").strip()
    if len(post) < 200:
        raise ValueError("Слишком короткий пост от LLM")
    return post, img


# ============================== КАРТИНКА ==============================

STYLE = ("cinematic digital illustration, futuristic tech aesthetic, dramatic volumetric lighting, "
         "deep blues and neon accents, ultra detailed, 16:9, high quality, no text, no letters, "
         "no watermark, no logo")


def gen_image(prompt):
    full = f"{prompt}. {STYLE}"
    if CF_ACCOUNT and CF_TOKEN:
        try:
            r = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CF_IMAGE_MODEL}",
                headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
                json={"prompt": full[:2000], "steps": 6},
                timeout=120,
            )
            r.raise_for_status()
            if "image" in r.headers.get("content-type", ""):
                return r.content
            b64 = r.json().get("result", {}).get("image")
            if b64:
                return base64.b64decode(b64)
        except Exception as e:
            log(f"  ! Cloudflare image failed: {e}")
    try:
        seed = random.randint(1, 10000000)
        url = (f"https://image.pollinations.ai/prompt/{quote(full[:1200])}"
               f"?width=1280&height=720&model=flux&nologo=true&seed={seed}")
        r = requests.get(url, headers=UA, timeout=180)
        r.raise_for_status()
        if len(r.content) > 20000:
            return r.content
    except Exception as e:
        log(f"  ! Pollinations failed: {e}")
    return None


# ============================== TELEGRAM ==============================

def tg(method, data=None, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      data=data, files=files, timeout=90)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j}")
    return j


def notify_admin(text):
    if TG_ADMIN and TG_TOKEN:
        try:
            tg("sendMessage", {"chat_id": TG_ADMIN, "text": text[:4000],
                               "disable_web_page_preview": True})
        except Exception:
            pass


def publish(post_html, image_bytes, item):
    src = f'\n\nИсточник: <a href="{item["url"]}">{item["source"]}</a>'
    caption = post_html + src
    if image_bytes and len(caption) <= 1024:
        tg("sendPhoto",
           {"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"},
           {"photo": ("news.jpg", io.BytesIO(image_bytes), "image/jpeg")})
    elif image_bytes:
        m = tg("sendPhoto", {"chat_id": TG_CHAT},
               {"photo": ("news.jpg", io.BytesIO(image_bytes), "image/jpeg")})
        tg("sendMessage", {"chat_id": TG_CHAT, "text": caption, "parse_mode": "HTML",
                           "disable_web_page_preview": True,
                           "reply_to_message_id": m["result"]["message_id"]})
    else:
        tg("sendMessage", {"chat_id": TG_CHAT, "text": caption, "parse_mode": "HTML",
                           "disable_web_page_preview": False})


def sanitize_html(text):
    """Оставляем только теги, которые понимает Telegram."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<i>\1</i>", text)
    allowed = re.compile(r"</?(b|i|u|s|code|pre|a)(\s[^<>]*)?>", re.I)
    out, pos = [], 0
    for m in re.finditer(r"<[^<>]+>", text):
        out.append(text[pos:m.start()].replace("<", "&lt;").replace(">", "&gt;"))
        out.append(m.group(0) if allowed.fullmatch(m.group(0)) else "")
        pos = m.end()
    out.append(text[pos:].replace("<", "&lt;").replace(">", "&gt;"))
    res = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", res).strip()


# ============================== ОСНОВНОЙ ЦИКЛ ==============================

def main():
    now = dt.datetime.now(MSK)
    ok, why = should_run_now(now)
    log(f"Время МСК {now:%Y-%m-%d %H:%M} | запуск: {ok} ({why})")
    if not ok:
        return 0

    if not FORCE:
        jitter = random.randint(0, SLOT_MINUTES * 60 - 60)
        log(f"Джиттер {jitter // 60} мин")
        time.sleep(jitter)

    if not (TG_TOKEN and TG_CHAT):
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID")

    history = load_history()
    log("Собираю новости из RSS...")
    items = collect()
    log(f"Всего свежих: {len(items)}")

    fresh = [i for i in items if not is_duplicate(i, history)]
    log(f"После дедупликации: {len(fresh)}")
    if not fresh:
        log("Нет новых новостей — пропускаю публикацию.")
        return 0

    best, score, reason = pick_best(fresh[:25], history)
    log(f"Выбрано: {best['title']} (score {score}) — {reason}")
    if score < MIN_SCORE:
        log("Ничего достойного публикации — пропускаю.")
        return 0

    article = fetch_article(best["url"])
    if len(article) < 400:
        article = best["summary"]
    if len(article) < 200:
        log("Слишком мало фактуры — пропускаю, чтобы не выдумывать.")
        return 0

    post, image_prompt = write_post(best, article)
    post = sanitize_html(post)
    log(f"Пост готов ({len(post)} символов)")

    img = gen_image(image_prompt or best["title"])
    log("Картинка: " + ("ок" if img else "не удалось, публикую текстом"))

    if DRY_RUN:
        print("\n----- DRY RUN -----\n" + post + f"\n\nИсточник: {best['source']} {best['url']}\n")
        return 0

    publish(post, img, best)
    log("Опубликовано")

    history.append({
        "id": news_id(best["title"], best["url"]),
        "title": best["title"],
        "url": best["url"],
        "url_canon": canon_url(best["url"]),
        "source": best["source"],
        "news_date": best["dt"].isoformat(),
        "summary": best["summary"][:300],
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window": why,
    })
    save_history(history)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"ОШИБКА: {exc}")
        notify_admin(f"Бот упал: {exc}")
        sys.exit(1)
