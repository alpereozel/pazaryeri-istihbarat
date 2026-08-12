from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse
from datetime import datetime, timezone
import sqlite3, json, re, requests, html as html_lib, math
from bs4 import BeautifulSoup

DB = "marketintel.db"
app = FastAPI(title="Pazaryeri İstihbarat", version="0.5.0")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id TEXT PRIMARY KEY,
        marketplace TEXT,
        url TEXT,
        title TEXT,
        seller TEXT,
        brand TEXT,
        category TEXT,
        price REAL,
        list_price REAL,
        rating REAL,
        review_count INTEGER,
        commission_rate REAL DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT,
        captured_at TEXT,
        price REAL,
        stock INTEGER,
        review_count INTEGER,
        rating REAL,
        raw_json TEXT
    )""")
    c.commit(); c.close()


init_db()


class AnalyzeRequest(BaseModel):
    url: HttpUrl


class SnapshotRequest(BaseModel):
    product_id: str
    price: float | None = None
    stock: int | None = None
    review_count: int | None = None
    rating: float | None = None


def marketplace(url):
    host = urlparse(str(url)).netloc.lower()
    if "trendyol.com" in host:
        return "trendyol"
    if "hepsiburada.com" in host:
        return "hepsiburada"
    return "unknown"


def product_id_from_url(url):
    m = re.search(r"-p-(\d+)", str(url))
    return m.group(1) if m else re.sub(r"[^a-zA-Z0-9]", "", str(url))[-48:]


def clean_text(v):
    if v is None: return None
    return re.sub(r"\s+", " ", html_lib.unescape(str(v))).strip()


def to_float(v):
    if v is None: return None
    s = re.sub(r"[^\d,\.\-]", "", str(v).strip())
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try: return float(s)
    except: return None


def to_int(v):
    if v is None: return None
    m = re.search(r"\d[\d\.\s,]*", str(v))
    if not m: return None
    s = m.group(0).replace(".", "").replace(" ", "").replace(",", "")
    try: return int(s)
    except: return None


def first_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw: continue
        try: obj = json.loads(raw)
        except: continue
        items = obj if isinstance(obj, list) else obj.get("@graph", []) if isinstance(obj, dict) else []
        if isinstance(obj, dict) and not items: items = [obj]
        for item in items:
            if isinstance(item, dict) and (item.get("@type") in ("Product", ["Product"]) or "offers" in item or "aggregateRating" in item):
                return item
    return {}


def find_json_value(text, keys):
    for key in keys:
        for pat in [
            rf'"{re.escape(key)}"\s*:\s*"([^"]+)"',
            rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
            rf"'{re.escape(key)}'\s*:\s*'([^']+)'",
        ]:
            m = re.search(pat, text, re.I)
            if m: return clean_text(m.group(1))
    return None


def find_label_number(text, labels):
    for label in labels:
        m = re.search(re.escape(label) + r".{0,120}?(\d[\d\.,]*)", text, re.I | re.S)
        if m: return m.group(1)
    return None


def detect_purchase_signal(text):
    patterns = [
        r'(?:son\s*24\s*saatte|son\s*24\s*saat içinde)\s*(\d+)\s*(?:kişi|adet)\s*(?:aldı|satın aldı)',
        r'(\d+)\s*(?:kişi|adet)\s*(?:bu ürünü\s*)?(?:aldı|satın aldı)\s*(?:son\s*24\s*saatte)?',
        r'(?:last\s*24\s*hours)\s*(\d+)\s*(?:people|orders|units)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return int(m.group(1))
    return None


def detect_badge(text):
    patterns = [
        r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',
        r'En\s*çok\s*satan\s*(\d+)?\.?\s*ürün',
        r'Best\s*Seller\s*(\d+)?',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return "En Çok Satan", int(m.group(1)) if m.group(1) else None
    return None, None


def detect_sold_signal(text):
    patterns = [
        r'(\d[\d\.,]*)\s*\+?\s*(?:adet|ürün)\s*(?:satıldı|satıldı\b)',
        r'(\d[\d\.,]*)\s*\+?\s*(?:satış|satıldı)',
        r'(?:sold|orders)\s*[:]?\s*(\d[\d\.,]*)\+?',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return to_int(m.group(1))
    return None


def parse_public_product(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.5",
        "Cache-Control": "no-cache"
    }
    r = requests.get(str(url), headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True)) or ""
    ld = first_jsonld(soup)

    title = clean_text(ld.get("name"))
    og = soup.find("meta", property="og:title")
    if not title and og: title = clean_text(og.get("content"))
    if not title and soup.title: title = clean_text(soup.title.get_text())

    brand = ld.get("brand")
    brand = clean_text(brand.get("name")) if isinstance(brand, dict) else clean_text(brand)
    brand = brand or find_json_value(r.text, ["brandName", "brandTitle"])

    offers = ld.get("offers") or {}
    if isinstance(offers, list): offers = offers[0] if offers else {}
    price = to_float(offers.get("price")) if isinstance(offers, dict) else None
    list_price = None
    for pat in [r'"salePrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"discountedPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"price"\s*:\s*([0-9]+(?:[.,][0-9]+)?)']:
        if price is None:
            m = re.search(pat, r.text, re.I)
            if m: price = to_float(m.group(1))
    for pat in [r'"listPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"originalPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"struckPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)']:
        m = re.search(pat, r.text, re.I)
        if m: list_price = to_float(m.group(1)); break

    ar = ld.get("aggregateRating") or {}
    rating = to_float(ar.get("ratingValue")) if isinstance(ar, dict) else None
    review_count = to_int(ar.get("reviewCount") or ar.get("ratingCount")) if isinstance(ar, dict) else None
    if rating is None: rating = to_float(find_label_number(text, ["Değerlendirme", "Puan", "rating"]))
    if review_count is None: review_count = to_int(find_label_number(text, ["Yorum", "Değerlendirme", "reviews"]))

    seller = None
    if isinstance(offers, dict):
        s = offers.get("seller")
        seller = clean_text(s.get("name")) if isinstance(s, dict) else clean_text(s)
    seller = seller or find_json_value(r.text, ["merchantName", "sellerName", "merchantTitle", "sellerTitle", "merchant_name"])

    category = clean_text(ld.get("category"))
    category = category or find_json_value(r.text, ["categoryName", "categoryTitle", "webCategory", "categoryPath", "category_name"])
    if not category:
        names = []
        for x in soup.find_all(attrs={"itemprop": "name"}):
            t = clean_text(x.get_text(" ", strip=True))
            if t and t not in names: names.append(t)
        if names: category = " > ".join(names[-4:])

    purchase_24h = detect_purchase_signal(text)
    badge, badge_rank = detect_badge(text)
    sold_total = detect_sold_signal(text)
    stock = None
    for pat in [r'"(?:stock|quantity|availableQuantity)"\s*:\s*(\d+)', r'"(?:stockCount|inventory)"\s*:\s*(\d+)']:
        m = re.search(pat, r.text, re.I)
        if m: stock = int(m.group(1)); break

    return {
        "title": title, "brand": brand, "seller": seller, "category": category,
        "price": price, "list_price": list_price, "rating": rating,
        "review_count": review_count, "purchase_signal_24h": purchase_24h,
        "sales_badge": badge, "sales_badge_rank": badge_rank,
        "sold_total_signal": sold_total, "stock": stock,
        "http_status": r.status_code, "bytes": len(r.content)
    }


def get_history(product_id):
    c = db()
    return [dict(x) for x in c.execute(
        "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at", (product_id,)
    ).fetchall()]


def time_series_signal(history):
    if len(history) < 2: return None
    first, last = history[0], history[-1]
    try:
        t0 = datetime.fromisoformat(first["captured_at"])
        t1 = datetime.fromisoformat(last["captured_at"])
        days = (t1 - t0).total_seconds() / 86400
    except: return None
    if days < 0.25: return None

    review_delta = None
    if first.get("review_count") is not None and last.get("review_count") is not None:
        review_delta = max(0, last["review_count"] - first["review_count"])

    stock_delta = None
    if first.get("stock") is not None and last.get("stock") is not None:
        stock_delta = first["stock"] - last["stock"]

    return {
        "days": days,
        "review_delta": review_delta,
        "review_velocity": (review_delta / days) if review_delta is not None else None,
        "observed_stock_decrease": max(0, stock_delta) if stock_delta is not None else None,
        "stock_velocity": (max(0, stock_delta) / days) if stock_delta is not None else None,
    }


def estimate_from_current(data, history):
    # 1) Explicit public 24h purchase count: strongest signal.
    p24 = data.get("purchase_signal_24h")
    if p24 is not None:
        return {
            "daily_low": p24, "daily_high": p24, "confidence": 92,
            "basis": "Açık 24 saatlik satış sinyali",
            "score": min(100, 75 + min(25, p24 // 5)),
            "reasons": ["Sayfada açık 24 saatlik satın alma sinyali bulundu."]
        }

    ts = time_series_signal(history)
    if ts and ts.get("review_velocity") and ts["review_velocity"] > 0:
        # Review-rate heuristic: assume roughly 2%–10% of orders produce a review.
        # This is intentionally wide and low-confidence.
        rv = ts["review_velocity"]
        lo = max(1, math.ceil(rv / 0.10))
        hi = max(lo, math.ceil(rv / 0.02))
        # If stock also fell, use it as a conservative observed floor, not as proof of sales.
        if ts.get("stock_velocity") and ts["stock_velocity"] > 0:
            lo = max(lo, math.floor(ts["stock_velocity"] * 0.7))
        return {
            "daily_low": lo, "daily_high": hi, "confidence": 55,
            "basis": "Yorum hızı + zaman serisi",
            "score": min(100, 45 + int(min(35, rv * 10))),
            "reasons": [f"İzleme süresinde günde yaklaşık {rv:.1f} yeni yorum gözlendi.", "Yorum→sipariş oranı için geniş (%2–10) varsayım kullanıldı."]
        }

    rank = data.get("sales_badge_rank")
    reviews = data.get("review_count") or 0
    if rank is not None and reviews:
        # A broad prior. Rank changes the range, but never pretends to be actual orders.
        base_lo = max(1, math.ceil(reviews / 180))
        base_hi = max(base_lo, math.ceil(reviews / 30))
        if rank <= 10: mult_lo, mult_hi = 1.8, 3.0
        elif rank <= 50: mult_lo, mult_hi = 1.3, 2.0
        elif rank <= 100: mult_lo, mult_hi = 1.1, 1.5
        else: mult_lo, mult_hi = 1.0, 1.25
        lo = max(1, math.floor(base_lo * mult_lo))
        hi = max(lo, math.ceil(base_hi * mult_hi))
        return {
            "daily_low": lo, "daily_high": hi, "confidence": 48 if rank <= 50 else 38,
            "basis": "Kategori satış etiketi + yorum hacmi",
            "score": max(35, 90 - min(60, rank)),
            "reasons": [f"Açık satış etiketi: En Çok Satan #{rank}.", f"Toplam yorum: {reviews}.", "Bu, kategori sıralaması ile yorum hacminden üretilen heuristik bir aralıktır."]
        }

    if reviews:
        lo = max(1, math.ceil(reviews / 180))
        hi = max(lo, math.ceil(reviews / 30))
        return {
            "daily_low": lo, "daily_high": hi, "confidence": 25,
            "basis": "Yorum hacmi heuristiği",
            "score": 30,
            "reasons": [f"Toplam yorum: {reviews}.", "Kategori satış sırası veya açık satış sinyali bulunamadı; geniş tahmin aralığı kullanıldı."]
        }

    return {
        "daily_low": None, "daily_high": None, "confidence": 5,
        "basis": "Yeterli açık veri yok", "score": 10,
        "reasons": ["Günlük satış için kullanılabilir açık sinyal bulunamadı."]
    }


def make_periods(est, price):
    out = {}
    for key, days in (("daily", 1), ("3_days", 3), ("7_days", 7), ("monthly", 30)):
        if est.get("daily_low") is None or not price:
            out[key] = {"sales_low": None, "sales_high": None, "revenue_low": None, "revenue_high": None}
            continue
        lo = est["daily_low"] * days; hi = est["daily_high"] * days
        out[key] = {
            "sales_low": lo, "sales_high": hi,
            "revenue_low": round(lo * price, 2), "revenue_high": round(hi * price, 2)
        }
    return out


@app.get("/", response_class=HTMLResponse)
def home():
    with open("app/static/index.html", encoding="utf-8") as f: return HTMLResponse(f.read())


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = str(req.url); mp = marketplace(url)
    if mp == "unknown": raise HTTPException(400, "Şimdilik sadece Trendyol ve Hepsiburada URL'leri destekleniyor.")
    pid = f"{mp}:{product_id_from_url(url)}"
    try: data = parse_public_product(url)
    except Exception as e: raise HTTPException(502, f"Ürün sayfası okunamadı: {e}")

    c = db()
    c.execute("""INSERT INTO products(id,marketplace,url,title,seller,brand,category,price,list_price,rating,review_count,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title,seller=excluded.seller,brand=excluded.brand,category=excluded.category,
                 price=excluded.price,list_price=excluded.list_price,rating=excluded.rating,review_count=excluded.review_count""",
              (pid, mp, url, data["title"], data["seller"], data["brand"], data["category"], data["price"],
               data["list_price"], data["rating"], data["review_count"], datetime.now(timezone.utc).isoformat()))
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (pid, now, data["price"], data.get("stock"), data["review_count"], data["rating"], json.dumps(data, ensure_ascii=False)))
    c.commit(); c.close()

    history = get_history(pid)
    est = estimate_from_current(data, history)
    return {
        "product_id": pid, "marketplace": mp, "product": data,
        "estimate": est, "periods": make_periods(est, data.get("price")),
        "history": {"snapshots": len(history), "time_series": time_series_signal(history)},
        "commission_rate": None,
        "commission_note": "Kategori komisyonu henüz otomatik doğrulanmadı; yanlış oran göstermemek için boş bırakıldı.",
        "message": "Satış rakamları tahmindir. Açık satış sinyali varsa önceliklidir; aksi halde kategori etiketi, yorum hacmi ve zaman serisi kullanılır."
    }


@app.post("/api/snapshots")
def add_snapshot(req: SnapshotRequest):
    c = db()
    if not c.execute("SELECT 1 FROM products WHERE id=?", (req.product_id,)).fetchone():
        raise HTTPException(404, "Ürün bulunamadı. Önce /api/analyze çağırın.")
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (req.product_id, now, req.price, req.stock, req.review_count, req.rating, json.dumps(req.model_dump(), ensure_ascii=False)))
    c.commit(); c.close(); return {"ok": True, "captured_at": now}


@app.get("/api/products")
def products():
    c = db(); rows = [dict(x) for x in c.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()]; c.close(); return rows
