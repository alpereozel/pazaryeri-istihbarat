from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
import sqlite3, json, re, requests
from bs4 import BeautifulSoup

DB = "marketintel.db"
app = FastAPI(title="Pazaryeri İstihbarat MVP", version="0.1.0")

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
        category TEXT,
        price REAL,
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
        raw_json TEXT
    )""")
    c.commit()
    c.close()

init_db()

class AnalyzeRequest(BaseModel):
    url: HttpUrl

class SnapshotRequest(BaseModel):
    product_id: str
    price: float | None = None
    stock: int | None = None
    review_count: int | None = None

def marketplace(url):
    host = urlparse(str(url)).netloc.lower()
    if "trendyol.com" in host:
        return "trendyol"
    if "hepsiburada.com" in host:
        return "hepsiburada"
    return "unknown"

def product_id_from_url(url):
    m = re.search(r"-p-(\d+)", str(url))
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]", "", str(url))[-48:]

def extract_meta(soup):
    def meta(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
        return tag.get("content") if tag else None

    title = meta(prop="og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    desc = meta(prop="og:description")
    return title, desc

def fetch_public_page(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"
    }
    r = requests.get(str(url), headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title, desc = extract_meta(soup)
    return {"title": title, "description": desc, "html_bytes": len(r.content)}

def estimate_sales(snapshots):
    # Basit MVP tahmini:
    # 1) stok azalışı varsa bunu ana sinyal kabul et
    # 2) yorum artışını ikincil sinyal kabul et
    # 3) veri yoksa güven skorunu düşür
    if len(snapshots) < 2:
        return {"daily": None, "confidence": 0, "method": "insufficient_data"}

    rows = sorted(snapshots, key=lambda x: x["captured_at"])
    total_days = (datetime.fromisoformat(rows[-1]["captured_at"]) -
                  datetime.fromisoformat(rows[0]["captured_at"])).total_seconds() / 86400

    if total_days <= 0:
        return {"daily": None, "confidence": 0, "method": "invalid_interval"}

    stock_signal = 0
    review_signal = 0

    first, last = rows[0], rows[-1]
    if first["stock"] is not None and last["stock"] is not None:
        stock_signal = max(0, first["stock"] - last["stock"]) / total_days

    if first["review_count"] is not None and last["review_count"] is not None:
        review_delta = max(0, last["review_count"] - first["review_count"])
        # Yorum yazma oranı bilinmediği için konservatif bir katsayı.
        review_signal = (review_delta * 8) / total_days

    if stock_signal > 0 and review_signal > 0:
        daily = 0.7 * stock_signal + 0.3 * review_signal
    else:
        daily = max(stock_signal, review_signal)

    days = min(90, max(1, total_days))
    confidence = min(95, int(30 + days * 1.2 + (20 if stock_signal else 0) + (10 if review_signal else 0)))
    return {
        "daily": round(daily, 1),
        "confidence": confidence,
        "method": "stock_review_time_series"
    }

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(open("app/static/index.html", encoding="utf-8").read())

@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = str(req.url)
    mp = marketplace(url)
    if mp == "unknown":
        raise HTTPException(400, "Şimdilik sadece Trendyol ve Hepsiburada URL'leri destekleniyor.")

    pid = f"{mp}:{product_id_from_url(url)}"

    try:
        page = fetch_public_page(url)
    except Exception as e:
        page = {"title": None, "description": None, "fetch_error": str(e)}

    c = db()
    existing = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not existing:
        c.execute("""INSERT INTO products
        (id, marketplace, url, title, created_at)
        VALUES(?,?,?,?,?)""",
        (pid, mp, url, page.get("title"), datetime.now(timezone.utc).isoformat()))
        c.commit()

    snaps = [dict(x) for x in c.execute(
        "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at", (pid,)
    ).fetchall()]

    est = estimate_sales(snaps)
    latest_price = next((s["price"] for s in reversed(snaps) if s["price"] is not None), None)
    periods = {}
    for label, days in [("daily",1),("3_days",3),("7_days",7),("monthly",30)]:
        sales = round(est["daily"] * days, 1) if est["daily"] is not None else None
        revenue = round(sales * latest_price, 2) if sales is not None and latest_price else None
        periods[label] = {"sales": sales, "revenue": revenue}
    return {
        "product_id": pid,
        "marketplace": mp,
        "url": url,
        "page": page,
        "snapshots": len(snaps),
        "estimate": est,
        "periods": periods,
        "message": "İlk analiz için zaman serisi gerekir. Ürün takip edildikçe tahmin güçlenir."
    }

@app.post("/api/snapshots")
def add_snapshot(req: SnapshotRequest):
    c = db()
    if not c.execute("SELECT 1 FROM products WHERE id=?", (req.product_id,)).fetchone():
        raise HTTPException(404, "Ürün bulunamadı. Önce /api/analyze çağırın.")

    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots
        (product_id,captured_at,price,stock,review_count,raw_json)
        VALUES(?,?,?,?,?,?)""",
        (req.product_id, now, req.price, req.stock, req.review_count,
         json.dumps(req.model_dump(), ensure_ascii=False)))
    c.commit()
    return {"ok": True, "captured_at": now}

@app.get("/api/products")
def products():
    c = db()
    rows = [dict(x) for x in c.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()]
    return rows

@app.get("/api/products/{product_id}")
def product_detail(product_id: str):
    c = db()
    p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Ürün bulunamadı.")
    snaps = [dict(x) for x in c.execute(
        "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at", (product_id,)
    ).fetchall()]
    est = estimate_sales(snaps)
    price = next((s["price"] for s in reversed(snaps) if s["price"] is not None), p["price"])

    daily_sales = est["daily"]
    periods = {}
    for label, days in [
        ("daily", 1),
        ("3_days", 3),
        ("7_days", 7),
        ("monthly", 30),
    ]:
        sales = round(daily_sales * days, 1) if daily_sales is not None else None
        revenue = round(sales * price, 2) if sales is not None and price else None
        commission = round(revenue * (p["commission_rate"] or 0) / 100, 2) if revenue else None
        periods[label] = {
            "sales": sales,
            "revenue": revenue,
            "commission": commission,
        }

    return {
        "product": dict(p),
        "snapshots": snaps,
        "estimate": est,
        "periods": periods
    }
