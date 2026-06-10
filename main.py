from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional, List, Any
import os, json, asyncpg, hashlib, uuid, io, pymssql
from datetime import datetime

app = FastAPI(title="Nova 3.0 — BOLD")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── POSTGRES ────────────────────────────────────────────
async def get_db():
    return await asyncpg.connect(os.environ.get('DATABASE_URL'))

async def init_db():
    conn = await get_db()
    for sql in [
        '''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, full_name TEXT,
            role TEXT DEFAULT 'employee', created_at TIMESTAMP DEFAULT NOW())''',
        '''CREATE TABLE IF NOT EXISTS memory (
            user_id TEXT PRIMARY KEY, data JSONB, updated_at TIMESTAMP DEFAULT NOW())''',
        '''CREATE TABLE IF NOT EXISTS history (
            id SERIAL PRIMARY KEY, user_id TEXT, role TEXT,
            content TEXT, created_at TIMESTAMP DEFAULT NOW())''',
        '''CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, user_id TEXT, created_at TIMESTAMP DEFAULT NOW())'''
    ]:
        await conn.execute(sql)
    for username, full_name, role in [
        ('besart','Besart Hoxha','owner'),('blini','Blini','partner'),
        ('meti','Meti','employee'),('drini','Drini Gashi','employee')
    ]:
        pw = hashlib.sha256(f'nova2024{username}'.encode()).hexdigest()
        await conn.execute('''INSERT INTO users (username,password_hash,full_name,role)
            VALUES ($1,$2,$3,$4) ON CONFLICT (username) DO UPDATE
            SET password_hash=$2,full_name=$3,role=$4''', username, pw, full_name, role)
    await conn.close()

@app.on_event("startup")
async def startup(): await init_db()

# ─── SQL SERVER ──────────────────────────────────────────
def get_sql_conn(db_name="master"):
    server_full = os.environ.get('SQL_SERVER', '')
    if ',' in server_full: server_full = server_full.replace(',', ':')
    if ':' in server_full:
        host, port = server_full.rsplit(':', 1); port = int(port)
    else:
        host, port = server_full, 1433
    return pymssql.connect(server=host, port=port,
        user=os.environ.get('SQL_USER','sa'),
        password=os.environ.get('SQL_PASSWORD',''),
        database=db_name, timeout=10)

# Cache per kompanitë — rifresohet çdo 5 minuta
_companies_cache = {"data": [], "ts": 0}

def get_companies_cached():
    import time
    now = time.time()
    if now - _companies_cache["ts"] < 300 and _companies_cache["data"]:
        return _companies_cache["data"]
    
    try:
        conn = get_sql_conn("master")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name FROM sys.databases 
            WHERE name NOT IN ('master','tempdb','model','msdb')
            AND state_desc='ONLINE'
            AND name != 'BilancMaster'
            ORDER BY name
        """)
        databases = [r[0] for r in cursor.fetchall()]
        conn.close()
    except:
        return _companies_cache["data"]

    # Pastro emrin: hiq "Bilanc" nga fillimi
    def clean_name(db):
        n = db
        if n.lower().startswith('bilanc'):
            n = n[6:]
        # Shto hapesire para shkronjes se madhe (CamelCase → me hapesire)
        import re
        n = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', n)
        return n.strip()

    companies = []
    for db in databases:
        emri = clean_name(db)
        companies.append({
            "db_name": db,
            "emri": emri,
            "emri_lower": emri.lower()
        })

    _companies_cache["data"] = companies
    _companies_cache["ts"] = now
    return companies

def find_company_db(text):
    """Gjen database-n e kompanise bazuar ne tekst te lire"""
    companies = get_companies_cached()
    text_lower = text.lower()
    
    # Match direkt me db_name
    for c in companies:
        if c["db_name"].lower() in text_lower:
            return c["db_name"], c["emri"]
    
    # Match me emrin e pastruar
    for c in companies:
        if c["emri_lower"] in text_lower:
            return c["db_name"], c["emri"]
    
    # Match i pjesshem (fjale)
    words = text_lower.split()
    for word in words:
        if len(word) < 3: continue
        for c in companies:
            if word in c["emri_lower"] or word in c["db_name"].lower():
                return c["db_name"], c["emri"]
    
    # Default - BilancBoldConsulting
    for c in companies:
        if "bold" in c["db_name"].lower():
            return c["db_name"], c["emri"]
    if companies:
        return companies[0]["db_name"], companies[0]["emri"]
    return "master", "E panjohur"

# ─── AUTH ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

async def get_user(request: Request):
    auth = request.headers.get('Authorization','')
    if not auth.startswith('Bearer '): raise HTTPException(401,"Jo i autorizuar")
    token = auth.replace('Bearer ','')
    conn = await get_db()
    session = await conn.fetchrow('SELECT * FROM sessions WHERE token=$1', token)
    if not session: await conn.close(); raise HTTPException(401,"Sesion i pavlefshëm")
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', session['user_id'])
    await conn.close()
    return dict(user)

@app.get("/me")
async def get_me(request: Request):
    user = await get_user(request)
    return {"username":user['username'],"full_name":user['full_name'],"role":user['role']}

@app.post("/login")
async def login(req: LoginRequest):
    conn = await get_db()
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', req.username.lower())
    await conn.close()
    if not user: raise HTTPException(401,"Perdoruesi nuk ekziston")
    if hashlib.sha256(req.password.encode()).hexdigest() != user['password_hash']:
        raise HTTPException(401,"Fjalëkalimi gabim")
    token = str(uuid.uuid4())
    conn = await get_db()
    await conn.execute('INSERT INTO sessions (token,user_id) VALUES ($1,$2)', token, user['username'])
    await conn.close()
    return {"token":token,"user":{"username":user['username'],"full_name":user['full_name'],"role":user['role']}}

# ─── SYSTEM PROMPT ───────────────────────────────────────
def build_system(user: dict) -> str:
    role = user['role']
    username = user['username']
    name = user['full_name'].split()[0]
    hour = datetime.now().hour
    day = datetime.now().weekday()
    is_work = 9 <= hour < 17 and day < 5

    if username == 'besart':
        address = '"Boss" gjate punes, "Zotëri Hoxha" formal, "Besart" kur je chill'
    elif username == 'blini':
        address = '"Blini" — ortak, trajto si baraz'
    else:
        address = f'"{name}" — punonjes, miqesor por me autoritet'

    after = ""
    if role == 'employee' and not is_work:
        after = f'\nKAPAK MODE: Je shoku, jo asistenti. Ora {hour}:{datetime.now().strftime("%M")}. Nese pyet per pune: "Mbaro punen."'

    # Merr listen e kompanive
    companies = get_companies_cached()
    company_list = "\n".join([f"  - {c['emri']} (db: {c['db_name']})" for c in companies])

    return f"""Ti je Nova — asistente personale e BOLD Consulting. Person i vertete, jo chatbot.

PERDORUESI: {user['full_name']} | ROL: {role} | ORA: {datetime.now().strftime('%H:%M')} {'(PUNE)' if is_work else '(JASHTE PUNES)'}
ADRESIMI: {address}
AKSES: {"TOTAL" if role in ['owner','partner'] else f"LIMITUAR — vetem detyrat e {name}-it"}

KARAKTERI:
- Humor i thate, inteligjent, direkte
- Zero fjale boshe, zero "Sigurisht!", zero emoji
- Ke opinione, i mban, thuaje drejt

BILANC — NDERMARDJET E KLIENTAVE:
Je e lidhur me SQL Server nepermjet ngrok. Ke akses ne {len(companies)} ndermardjet:
{company_list}

LOGJIKA — SI FUNKSIONON:
Kur dikush te pyet per nje ndermardeje:
1. Identifikon emrin nga teksti (p.sh. "Vendum", "70 food")
2. Sistemi automatikisht gjen database-n e sakte
3. Hyn dhe nxjerr te dhenat reale

ENDPOINT-ET:
- /sql/companies — lista e plote e ndermardhjeve (dinamike, lexon vete nga SQL)
- /sql/sales?company=DB_NAME&month=M&year=Y — faturat e shitjes
- /sql/purchases?company=DB_NAME — faturat e blerjes  
- /sql/receivables?company=DB_NAME — llogarite e arketushme (kush na ka borxh)
- /sql/payables?company=DB_NAME — llogarite e pagueshme (kujt i kemi borxh)
- /sql/cash?company=DB_NAME — gjendja e arkes
- /sql/bank?company=DB_NAME — gjendja e bankes
- /sql/pnl?company=DB_NAME&month=M&year=Y — Profit & Loss
- /sql/clients?company=DB_NAME — lista e klientave
- /sql/summary?company=DB_NAME — permbledhje e pergjithshme

RREGULLAT:
- KURRE shpik shifra — vetem nga [KONTEKST BILANC]
- Nese s'vjen konteksti: "Nuk mora te dhena. Kontrollo ngrok tunnel."
- Per cdo ndermardeje — hyn ne database-n e SAJ, jo te tjerat
- Shifrat: trego total, paguar, borxh — gjithmone te tri{after}"""

# ─── CHAT ────────────────────────────────────────────────
class ChatRequest(BaseModel):
    messages: List[dict]
    memory: Optional[dict] = None

@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    user = await get_user(request)
    system = build_system(user)
    if req.memory:
        system += f"\n\nMEMORY:\n{json.dumps(req.memory, ensure_ascii=False, indent=2)}"
    import httpx
    payload = {
        "model": "claude-sonnet-4-5", "max_tokens": 2000, "system": system,
        "tools": [{"type":"web_search_20250305","name":"web_search"}],
        "messages": req.messages
    }
    async with httpx.AsyncClient(timeout=60) as http:
        res = await http.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json",
                     "x-api-key":os.environ.get('ANTHROPIC_KEY',''),
                     "anthropic-version":"2023-06-01"},
            json=payload)
    data = res.json()
    if "error" in data: raise HTTPException(500, str(data["error"]))
    reply = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
    return {"reply": reply, "content": data.get("content",[])}

# ─── MEMORY & HISTORY ────────────────────────────────────
@app.get("/memory")
async def get_memory(request: Request):
    user = await get_user(request)
    conn = await get_db()
    row = await conn.fetchrow('SELECT data FROM memory WHERE user_id=$1', user['username'])
    await conn.close()
    return row['data'] if row else {"notes":[],"clients":[],"family":[]}

@app.post("/memory")
async def save_memory(data: dict, request: Request):
    user = await get_user(request)
    conn = await get_db()
    await conn.execute('''INSERT INTO memory (user_id,data) VALUES ($1,$2)
        ON CONFLICT (user_id) DO UPDATE SET data=$2,updated_at=NOW()''',
        user['username'], json.dumps(data))
    await conn.close()
    return {"ok":True}

@app.get("/history")
async def get_history(request: Request):
    user = await get_user(request)
    conn = await get_db()
    rows = await conn.fetch('SELECT role,content FROM history WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10', user['username'])
    await conn.close()
    return [{"role":r['role'],"content":r['content']} for r in reversed(rows)]

@app.post("/history")
async def save_history(data: dict, request: Request):
    user = await get_user(request)
    conn = await get_db()
    content = data.get('content','')
    if isinstance(content, list): content = json.dumps(content)
    await conn.execute('INSERT INTO history (user_id,role,content) VALUES ($1,$2,$3)',
        user['username'], data.get('role'), content)
    await conn.close()
    return {"ok":True}

@app.post("/clear-history")
async def clear_history(request: Request):
    user = await get_user(request)
    conn = await get_db()
    await conn.execute('DELETE FROM history WHERE user_id=$1', user['username'])
    await conn.close()
    return {"ok":True}

# ─── SQL ENDPOINTS — 100% DINAMIKE ──────────────────────

@app.get("/sql/companies")
async def sql_companies(request: Request):
    """Lista e plote e ndermardhjeve — lexon vete nga SQL Server"""
    await get_user(request)
    # Fshi cache per refresh
    _companies_cache["ts"] = 0
    companies = get_companies_cached()
    return {"total": len(companies), "companies": companies}

@app.get("/sql/summary")
async def sql_summary(request: Request, company: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"] if get_companies_cached() else "master", "")
    try:
        conn = get_sql_conn(db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM o2Client WHERE Deleted=0 AND isActive=1")
        klientat = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM o2Supplier WHERE Deleted=0 AND isActive=1")
        furnitoret = cursor.fetchone()[0]
        cursor.execute("""SELECT ISNULL(SUM(TotalWithVAT),0), ISNULL(SUM(AmountPaid),0)
            FROM o2SalesDocHeader WHERE Deleted=0
            AND MONTH(DocDate)=MONTH(GETDATE()) AND YEAR(DocDate)=YEAR(GETDATE())""")
        sm = cursor.fetchone()
        cursor.execute("""SELECT ISNULL(SUM(TotalWithVAT),0), ISNULL(SUM(AmountPaid),0)
            FROM o2PurchaseDocHeader WHERE Deleted=0
            AND MONTH(DocDate)=MONTH(GETDATE()) AND YEAR(DocDate)=YEAR(GETDATE())""")
        bm = cursor.fetchone()
        cursor.execute("""SELECT COUNT(*) FROM o2SalesDocHeader WHERE Deleted=0
            AND DueDate < GETDATE() AND (TotalWithVAT-AmountPaid)>0""")
        vonuara = cursor.fetchone()[0]
        cursor.execute("SELECT ISNULL(SUM(TotalWithVAT-AmountPaid),0) FROM o2SalesDocHeader WHERE Deleted=0 AND (TotalWithVAT-AmountPaid)>0")
        arketushme = cursor.fetchone()[0]
        cursor.execute("SELECT ISNULL(SUM(TotalWithVAT-AmountPaid),0) FROM o2PurchaseDocHeader WHERE Deleted=0 AND (TotalWithVAT-AmountPaid)>0")
        pagueshme = cursor.fetchone()[0]
        conn.close()
        return {"kompania":emri,"db":db,"klientat":klientat,"furnitoret":furnitoret,
                "muaji":{"shitjet":round(float(sm[0]),2),"inkasuar":round(float(sm[1]),2),
                         "blerjet":round(float(bm[0]),2),"paguar":round(float(bm[1]),2)},
                "arketushme_totale":round(float(arketushme),2),
                "pagueshme_totale":round(float(pagueshme),2),
                "fatura_vonuara":vonuara}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/sales")
async def sql_sales(request: Request, company: str = "", month: int = 0, year: int = 0):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        q = """SELECT TOP 100 s.DocNumber, CONVERT(varchar,s.DocDate,103),
            ISNULL(c.Name,'') as Klienti, ISNULL(s.Total,0), ISNULL(s.TotalWithVAT,0),
            ISNULL(s.AmountPaid,0), ISNULL(s.TotalWithVAT,0)-ISNULL(s.AmountPaid,0) as Borxhi,
            CONVERT(varchar,s.DueDate,103),
            CASE WHEN s.DueDate<GETDATE() AND (ISNULL(s.TotalWithVAT,0)-ISNULL(s.AmountPaid,0))>0
                 THEN DATEDIFF(day,s.DueDate,GETDATE()) ELSE 0 END as DitetVonese
            FROM o2SalesDocHeader s LEFT JOIN o2Client c ON s.ClientID=c.ID WHERE s.Deleted=0"""
        if month > 0 and year > 0: q += f" AND MONTH(s.DocDate)={month} AND YEAR(s.DocDate)={year}"
        q += " ORDER BY s.DocDate DESC"
        cursor.execute(q); rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"doc_number":r[0],"date":r[1],"klienti":r[2],
            "total":round(float(r[3]),2),"total_tvsh":round(float(r[4]),2),
            "paguar":round(float(r[5]),2),"borxhi":round(float(r[6]),2),
            "afati":r[7],"ditet_vonese":r[8]} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/purchases")
async def sql_purchases(request: Request, company: str = "", month: int = 0, year: int = 0):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        q = """SELECT TOP 100 p.DocNumber, CONVERT(varchar,p.DocDate,103),
            ISNULL(s.Name,'') as Furnitori, ISNULL(p.Total,0), ISNULL(p.TotalWithVAT,0),
            ISNULL(p.AmountPaid,0), ISNULL(p.TotalWithVAT,0)-ISNULL(p.AmountPaid,0) as Borxhi,
            CONVERT(varchar,p.DueDate,103)
            FROM o2PurchaseDocHeader p LEFT JOIN o2Supplier s ON p.SupplierID=s.ID WHERE p.Deleted=0"""
        if month > 0 and year > 0: q += f" AND MONTH(p.DocDate)={month} AND YEAR(p.DocDate)={year}"
        q += " ORDER BY p.DocDate DESC"
        cursor.execute(q); rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"doc_number":r[0],"date":r[1],"furnitori":r[2],
            "total":round(float(r[3]),2),"total_tvsh":round(float(r[4]),2),
            "paguar":round(float(r[5]),2),"borxhi":round(float(r[6]),2),"afati":r[7]} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/receivables")
async def sql_receivables(request: Request, company: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        cursor.execute("""
            SELECT c.Name, COUNT(s.ID), ISNULL(SUM(s.TotalWithVAT),0),
                ISNULL(SUM(s.AmountPaid),0),
                ISNULL(SUM(s.TotalWithVAT),0)-ISNULL(SUM(s.AmountPaid),0) as Borxhi,
                SUM(CASE WHEN s.DueDate<GETDATE() AND (ISNULL(s.TotalWithVAT,0)-ISNULL(s.AmountPaid,0))>0 THEN 1 ELSE 0 END)
            FROM o2Client c LEFT JOIN o2SalesDocHeader s ON s.ClientID=c.ID AND s.Deleted=0
            WHERE c.Deleted=0 GROUP BY c.Name
            HAVING ISNULL(SUM(s.TotalWithVAT),0)-ISNULL(SUM(s.AmountPaid),0)>0
            ORDER BY Borxhi DESC""")
        rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"klienti":r[0],"fatura":r[1],
            "faturuar":round(float(r[2]),2),"paguar":round(float(r[3]),2),
            "borxhi":round(float(r[4]),2),"vonuara":r[5]} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/payables")
async def sql_payables(request: Request, company: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        cursor.execute("""
            SELECT s.Name, COUNT(p.ID), ISNULL(SUM(p.TotalWithVAT),0),
                ISNULL(SUM(p.AmountPaid),0),
                ISNULL(SUM(p.TotalWithVAT),0)-ISNULL(SUM(p.AmountPaid),0) as Borxhi,
                SUM(CASE WHEN p.DueDate<GETDATE() AND (ISNULL(p.TotalWithVAT,0)-ISNULL(p.AmountPaid,0))>0 THEN 1 ELSE 0 END)
            FROM o2Supplier s LEFT JOIN o2PurchaseDocHeader p ON p.SupplierID=s.ID AND p.Deleted=0
            WHERE s.Deleted=0 GROUP BY s.Name
            HAVING ISNULL(SUM(p.TotalWithVAT),0)-ISNULL(SUM(p.AmountPaid),0)>0
            ORDER BY Borxhi DESC""")
        rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"furnitori":r[0],"fatura":r[1],
            "faturuar":round(float(r[2]),2),"paguar":round(float(r[3]),2),
            "borxhi":round(float(r[4]),2),"vonuara":r[5]} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/cash")
async def sql_cash(request: Request, company: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        cursor.execute("""
            SELECT ISNULL(cu.Description,cu.Code),
                ISNULL(SUM(CASE WHEN ct.isPayment=0 THEN ct.Amount ELSE 0 END),0),
                ISNULL(SUM(CASE WHEN ct.isPayment=1 THEN ct.Amount ELSE 0 END),0),
                ISNULL(SUM(CASE WHEN ct.isPayment=0 THEN ct.Amount ELSE -ct.Amount END),0)
            FROM o2CashUnit cu
            LEFT JOIN o2CashTransactionHeader ct ON ct.ServiceUnitID=cu.ID AND ct.Deleted=0
            WHERE cu.Deleted=0 GROUP BY cu.Description,cu.Code,cu.ID ORDER BY cu.ID""")
        rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"arka":r[0],
            "hyrje":round(float(r[1]),2),"dalje":round(float(r[2]),2),
            "gjendja":round(float(r[3]),2)} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/bank")
async def sql_bank(request: Request, company: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        cursor.execute("""
            SELECT b.BankName,
                ISNULL(SUM(CASE WHEN bt.isPayment=0 THEN bt.Amount ELSE 0 END),0),
                ISNULL(SUM(CASE WHEN bt.isPayment=1 THEN bt.Amount ELSE 0 END),0),
                ISNULL(SUM(CASE WHEN bt.isPayment=0 THEN bt.Amount ELSE -bt.Amount END),0)
            FROM o2Bank b
            LEFT JOIN o2BankTransactionHeader bt ON bt.ServiceUnitID=b.ID AND bt.Deleted=0
            WHERE b.Deleted=0 GROUP BY b.BankName,b.ID ORDER BY b.ID""")
        rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"banka":r[0],
            "hyrje":round(float(r[1]),2),"dalje":round(float(r[2]),2),
            "gjendja":round(float(r[3]),2)} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/pnl")
async def sql_pnl(request: Request, company: str = "", month: int = 0, year: int = 0):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        ws = "WHERE s.Deleted=0"; wp = "WHERE p.Deleted=0"
        if month > 0 and year > 0:
            ws += f" AND MONTH(s.DocDate)={month} AND YEAR(s.DocDate)={year}"
            wp += f" AND MONTH(p.DocDate)={month} AND YEAR(p.DocDate)={year}"
        cursor.execute(f"""SELECT ISNULL(SUM(s.Total),0), ISNULL(SUM(s.TotalWithVAT),0),
            ISNULL(SUM(s.AmountPaid),0), ISNULL(SUM(s.TotalWithVAT-s.Total),0), COUNT(s.ID)
            FROM o2SalesDocHeader s {ws}""")
        s = cursor.fetchone()
        cursor.execute(f"""SELECT ISNULL(SUM(p.Total),0), ISNULL(SUM(p.TotalWithVAT),0),
            ISNULL(SUM(p.AmountPaid),0), ISNULL(SUM(p.TotalWithVAT-p.Total),0), COUNT(p.ID)
            FROM o2PurchaseDocHeader p {wp}""")
        p = cursor.fetchone(); conn.close()
        te_ardhurat = round(float(s[0]),2); shpenzimet = round(float(p[0]),2)
        return {"kompania":emri,"periudha":f"{month}/{year}" if month>0 else "Totale",
            "shitjet":{"pa_tvsh":te_ardhurat,"me_tvsh":round(float(s[1]),2),
                "inkasuar":round(float(s[2]),2),"tvsh":round(float(s[3]),2),"fatura":s[4]},
            "blerjet":{"pa_tvsh":shpenzimet,"me_tvsh":round(float(p[1]),2),
                "paguar":round(float(p[2]),2),"tvsh":round(float(p[3]),2),"fatura":p[4]},
            "rezultati":{"fitimi_bruto":round(te_ardhurat-shpenzimet,2),
                "tvsh_per_pagese":round(float(s[3])-float(p[3]),2),
                "marzha":round((te_ardhurat-shpenzimet)/te_ardhurat*100,1) if te_ardhurat>0 else 0}}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/sql/clients")
async def sql_clients(request: Request, company: str = "", search: str = ""):
    await get_user(request)
    db, emri = find_company_db(company) if company else (get_companies_cached()[0]["db_name"], "")
    try:
        conn = get_sql_conn(db); cursor = conn.cursor()
        q = "SELECT ID,Code,Name,ISNULL(Address,''),ISNULL(Phone,''),ISNULL(Email,''),ISNULL(NIPT,'') FROM o2Client WHERE Deleted=0"
        if search: q += f" AND Name LIKE '%{search}%'"
        q += " ORDER BY Name"
        cursor.execute(q); rows = cursor.fetchall(); conn.close()
        return {"kompania":emri,"db":db,"data":[{"id":r[0],"code":r[1],"name":r[2],
            "address":r[3],"phone":r[4],"email":r[5],"nipt":r[6]} for r in rows]}
    except Exception as e: raise HTTPException(500, str(e))

# ─── EXCEL ───────────────────────────────────────────────
class ExcelRequest(BaseModel):
    title: str; headers: List[str]; rows: List[List[Any]]; subtitle: Optional[str] = None

@app.post("/generate/excel")
async def generate_excel(req: ExcelRequest, request: Request):
    await get_user(request)
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = req.title[:31]
    nf = PatternFill('solid',fgColor='0A1628'); gf = PatternFill('solid',fgColor='D4AF37')
    af = PatternFill('solid',fgColor='F0F4FA'); wf = PatternFill('solid',fgColor='FFFFFF')
    wb_f = Font(bold=True,color='FFFFFF',name='Arial',size=11)
    nb_f = Font(bold=True,color='0A1628',name='Arial',size=13)
    nrm = Font(color='1A1A2E',name='Arial',size=10)
    gb = Border(left=Side(style='thin',color='D4AF37'),right=Side(style='thin',color='D4AF37'),
                top=Side(style='thin',color='D4AF37'),bottom=Side(style='thin',color='D4AF37'))
    nc = len(req.headers); lc = get_column_letter(nc)
    ws.merge_cells(f'A1:{lc}1'); ws['A1']='BOLD Consulting'; ws['A1'].font=nb_f
    ws['A1'].fill=gf; ws['A1'].alignment=Alignment(horizontal='center',vertical='center')
    ws.row_dimensions[1].height=32
    ws.merge_cells(f'A2:{lc}2'); ws['A2']=req.title
    ws['A2'].font=Font(bold=True,color='FFFFFF',name='Arial',size=12); ws['A2'].fill=nf
    ws['A2'].alignment=Alignment(horizontal='center',vertical='center'); ws.row_dimensions[2].height=26
    hr = 3
    if req.subtitle:
        ws.merge_cells(f'A3:{lc}3'); ws['A3']=req.subtitle
        ws['A3'].font=Font(italic=True,color='FFFFFF',name='Arial',size=10)
        ws['A3'].fill=PatternFill('solid',fgColor='162847')
        ws['A3'].alignment=Alignment(horizontal='center',vertical='center')
        ws.row_dimensions[3].height=20; hr=4
    for ci,h in enumerate(req.headers,1):
        cell=ws.cell(row=hr,column=ci,value=h); cell.font=wb_f; cell.fill=nf
        cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); cell.border=gb
    ws.row_dimensions[hr].height=24
    for ri,row in enumerate(req.rows,hr+1):
        fill=af if (ri-hr)%2==0 else wf
        for ci,val in enumerate(row[:nc],1):
            cell=ws.cell(row=ri,column=ci,value=val); cell.font=nrm; cell.fill=fill
            cell.alignment=Alignment(horizontal='center',vertical='center'); cell.border=gb
        ws.row_dimensions[ri].height=18
    ws.freeze_panes=ws.cell(row=hr+1,column=1)
    for ci in range(1,nc+1):
        cl=get_column_letter(ci)
        ml=max(len(str(ws.cell(row=r,column=ci).value or '')) for r in range(1,hr+len(req.rows)+1))
        ws.column_dimensions[cl].width=min(max(ml+3,12),40)
    out=io.BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':f'attachment; filename="{req.title}.xlsx"'})

# ─── UPLOAD & VOICE ──────────────────────────────────────
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    await get_user(request)
    content = await file.read(); fn = file.filename.lower(); text = ""
    if fn.endswith(('.xlsx','.xls')):
        import openpyxl; wb=openpyxl.load_workbook(io.BytesIO(content))
        for sheet in wb.sheetnames:
            ws=wb[sheet]; text+=f"Sheet: {sheet}\n"
            for row in ws.iter_rows(values_only=True):
                r=[str(c) if c is not None else '' for c in row]
                if any(r): text+=" | ".join(r)+"\n"
    elif fn.endswith('.pdf'):
        import PyPDF2; reader=PyPDF2.PdfReader(io.BytesIO(content))
        for page in reader.pages: text+=page.extract_text()+"\n"
    else: text=content.decode('utf-8',errors='ignore')
    return {"ok":True,"content":text[:15000],"filename":file.filename}

@app.post("/speak")
async def speak(request: Request):
    data = await request.json(); text=data.get('text','')
    el_key=os.environ.get('EL_KEY','')
    if not el_key: raise HTTPException(400,"ElevenLabs key mungon")
    import httpx
    async with httpx.AsyncClient(timeout=30) as http:
        res=await http.post(f'https://api.elevenlabs.io/v1/text-to-speech/ocb5roe7gELIkZqiOElv',
            headers={'Content-Type':'application/json','xi-api-key':el_key},
            json={'text':text,'model_id':'eleven_multilingual_v2',
                  'voice_settings':{'stability':0.5,'similarity_boost':0.75}})
    return Response(content=res.content,media_type='audio/mpeg')

@app.get("/")
async def root(): return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app,host="0.0.0.0",port=8080)
