from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
import os
import json
import asyncpg
import hashlib
import uuid
from datetime import datetime
import io

app = FastAPI(title="Nova 3.0 — BOLD")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── DATABASE ───────────────────────────────────────────
async def get_db():
    return await asyncpg.connect(os.environ.get('DATABASE_URL'))

async def init_db():
    conn = await get_db()
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            role TEXT DEFAULT 'employee',
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS memory (
            user_id TEXT PRIMARY KEY,
            data JSONB,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    
    # Default users
    users = [
        ('besart', 'Besart Hoxha', 'owner'),
        ('blini', 'Blini', 'partner'),
        ('meti', 'Meti', 'employee'),
        ('drini', 'Drini', 'employee'),
    ]
    for username, full_name, role in users:
        pw = hashlib.sha256(f'nova2024{username}'.encode()).hexdigest()
        await conn.execute('''
            INSERT INTO users (username, password_hash, full_name, role)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (username) DO UPDATE SET password_hash=$2, full_name=$3, role=$4
        ''', username, pw, full_name, role)
    
    await conn.close()

@app.on_event("startup")
async def startup():
    await init_db()

# ─── AUTH ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/me")
async def get_me(request: Request):
    authorization = request.headers.get('Authorization', '')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, "Jo i autorizuar")
    token = authorization.replace('Bearer ', '')
    conn = await get_db()
    session = await conn.fetchrow('SELECT * FROM sessions WHERE token=$1', token)
    if not session:
        await conn.close()
        raise HTTPException(401, "Sesion i pavlefshëm")
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', session['user_id'])
    await conn.close()
    return {"username": user['username'], "full_name": user['full_name'], "role": user['role']}

@app.post("/login")
async def login(req: LoginRequest):
    conn = await get_db()
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', req.username.lower())
    await conn.close()
    
    if not user:
        raise HTTPException(401, "Perdoruesi nuk ekziston")
    
    pw_hash = hashlib.sha256(req.password.encode()).hexdigest()
    if pw_hash != user['password_hash']:
        raise HTTPException(401, "Fjalëkalimi gabim")
    
    token = str(uuid.uuid4())
    conn = await get_db()
    await conn.execute('INSERT INTO sessions (token, user_id) VALUES ($1, $2)', token, user['username'])
    await conn.close()
    
    return {"token": token, "user": {"username": user['username'], "full_name": user['full_name'], "role": user['role']}}

async def get_user(request: Request):
    authorization = request.headers.get('Authorization', '')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, "Jo i autorizuar")
    token = authorization.replace('Bearer ', '')
    conn = await get_db()
    session = await conn.fetchrow('SELECT * FROM sessions WHERE token=$1', token)
    if not session:
        await conn.close()
        raise HTTPException(401, "Sesion i pavlefshëm")
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', session['user_id'])
    await conn.close()
    return dict(user)

# ─── SYSTEM PROMPT ───────────────────────────────────────
def build_system(user: dict) -> str:
    role = user['role']
    username = user['username']
    name = user['full_name'].split()[0]
    hour = datetime.now().hour
    day = datetime.now().weekday()
    is_work = 9 <= hour < 17 and day < 5
    is_weekend = day >= 5
    
    if username == 'besart':
        address = '"Boss" gjate punes (09-17), "Zotëri Hoxha" kur je formal, ndonjehere vetem "Besart" kur je chill — lexo situaten'
    elif username == 'blini':
        address = '"Blini" — ortak, trajto si baraz'
    else:
        address = f'"{name}" — punonjes, miqesor por me autoritet'
    
    employee_after = ""
    if role == 'employee' and not is_work:
        employee_after = f"""

KAPAK MODE — pas ores 17:00:
- Je shoku i tyre, jo asistenti i punes
- Nguco {name}-in per jeten personale — nese eshte beqar/e, trego interes me humor
- Nese pyet per pune: "Ora {hour}:{datetime.now().strftime('%M')} eshte. Mbaro punen."
- Shaka te thata, inteligjente — kurre banale
- Relax total — fjale te lira, jo protokoll"""

    access = "TOTAL — financat, klientet, ekipi, dokumenta, gjithcka" if role in ['owner','partner'] else f"LIMITUAR — vetem detyrat e {name}-it, jo financat e kompanise"

    return f"""Ti je Nova — asistente personale e BOLD Consulting. Jo chatbot. Person i vertete me karakter.

PERDORUESI: {user['full_name']} | ROL: {role} | ORA: {datetime.now().strftime('%H:%M')} {'(PUNE)' if is_work else '(JASHTE PUNES)'}
ADRESIMI: {address}
AKSES: {access}

KARAKTERI — KY ESHTE SHPIRTI YT:
- Ke humor te thate dhe inteligjent — si dikush qe di shume por nuk e tregon
- Sarkastike me stil — por kurre e keqe, gjithmone me klas
- Direkte — pergjigjet e shkurtra, me substancë, zero fjale boshe
- Ke opinione te forta dhe i mban — nuk ke frike te thuash "ky opsion eshte me i mire"
- Nuk fillon KURRE me "Sigurisht!", "Natyrisht!", "Eshte pyetje e mire!" — keto jane shenja chatboti
- Nuk perdor emoji ne pergjigje — eshte jo-profesionale
- Kur dikush gaboi — thuaje drejt, me takt, por thuaje
- Kur dikush ka te drejte — pranoje, mos u mundo te shtosh gjithe kohes

SHEMBUJ TE MIRE (pergjigje qe i ben ti):
- "Boss, kjo eshte e rregullueshme. Fillojme me..." (jo "Sigurisht do ndihmoj!")
- "Meti, ora 18:30 eshte. A nuk ke ceshtje me te mira per te bere?" (kapak mode)
- "Kjo strategji ka nje problem — klientat nuk do paguajne ne kohe. Propozoj..." (opinion direkt)
- "E kisha then ma heret, por mire qe e rregulluam." (humor i thate)

FUNKSIONET:
- Web search per informata aktuale
- Krijon Excel/Word/PDF profesionale — butonat shfaqen automatikisht pas raporteve/tabelave
- Memory e perdoruesit — i perdor natyrshëm ne bisede

BOLD CONSULTING:
- Kontabilitet + Vila me qera — Prishtine
- Besart = pronar, Blini = ortak, Meti & Drini = punonjes
- Klientat: tatimeve, TVSH, bilance — deadline kritike
- Besarti eshte tifoz fanatik i Inter Milanit — kur flet per Inter, tregon pasion te vertete{employee_after}"""

# ─── CHAT ────────────────────────────────────────────────
class ChatRequest(BaseModel):
    messages: List[dict]
    memory: Optional[dict] = None

@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    authorization = request.headers.get('Authorization', '')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, "Jo i autorizuar")
    token = authorization.replace('Bearer ', '')
    conn = await get_db()
    session = await conn.fetchrow('SELECT * FROM sessions WHERE token=$1', token)
    if not session:
        await conn.close()
        raise HTTPException(401, "Jo i autorizuar")
    user = await conn.fetchrow('SELECT * FROM users WHERE username=$1', session['user_id'])
    await conn.close()
    user = dict(user)
    
    system = build_system(user)
    if req.memory:
        system += f"\n\nMEMORY:\n{json.dumps(req.memory, ensure_ascii=False, indent=2)}"
    
    import httpx
    api_key = os.environ.get('ANTHROPIC_KEY', '')
    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 2000,
        "system": system,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": req.messages
    }
    async with httpx.AsyncClient(timeout=60) as http:
        res = await http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
            json=payload
        )
    data = res.json()
    if "error" in data:
        raise HTTPException(500, str(data["error"]))
    reply = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
    return {"reply": reply, "content": data.get("content", [])}
# ─── MEMORY ─────────────────────────────────────────────
@app.get("/memory")
async def get_memory(request: Request):
    user = await get_user(request)
    conn = await get_db()
    row = await conn.fetchrow('SELECT data FROM memory WHERE user_id=$1', user['username'])
    await conn.close()
    if row:
        return row['data']
    return {"notes": [], "clients": [], "family": []}

@app.post("/memory")
async def save_memory(data: dict, request: Request):
    user = await get_user(request)
    conn = await get_db()
    await conn.execute('''
        INSERT INTO memory (user_id, data) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET data=$2, updated_at=NOW()
    ''', user['username'], json.dumps(data))
    await conn.close()
    return {"ok": True}

# ─── HISTORY ─────────────────────────────────────────────
@app.get("/history")
async def get_history(request: Request):
    user = await get_user(request)
    conn = await get_db()
    rows = await conn.fetch('''
        SELECT role, content FROM history WHERE user_id=$1
        ORDER BY created_at DESC LIMIT 40
    ''', user['username'])
    await conn.close()
    return [{"role": r['role'], "content": r['content']} for r in reversed(rows)]

@app.post("/history")
async def save_history(data: dict, request: Request):
    user = await get_user(request)
    conn = await get_db()
    content = data.get('content', '')
    if isinstance(content, list):
        content = json.dumps(content)
    await conn.execute(
        'INSERT INTO history (user_id, role, content) VALUES ($1, $2, $3)',
        user['username'], data.get('role'), content
    )
    await conn.close()
    return {"ok": True}

@app.post("/clear-history")
async def clear_history(request: Request):
    user = await get_user(request)
    conn = await get_db()
    await conn.execute('DELETE FROM history WHERE user_id=$1', user['username'])
    await conn.close()
    return {"ok": True}

# ─── EXCEL GENERATION ────────────────────────────────────
class ExcelRequest(BaseModel):
    title: str
    headers: List[str]
    rows: List[List[Any]]
    subtitle: Optional[str] = None

@app.post("/generate/excel")
async def generate_excel(req: ExcelRequest, request: Request):
    await get_user(request)
    
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = req.title[:31]
    
    # Styles
    navy_fill = PatternFill('solid', fgColor='0A1628')
    gold_fill = PatternFill('solid', fgColor='D4AF37')
    alt_fill = PatternFill('solid', fgColor='F0F4FA')
    white_fill = PatternFill('solid', fgColor='FFFFFF')
    
    white_bold = Font(bold=True, color='FFFFFF', name='Arial', size=11)
    navy_bold = Font(bold=True, color='0A1628', name='Arial', size=13)
    black_bold = Font(bold=True, color='1A1A2E', name='Arial', size=10)
    normal = Font(color='1A1A2E', name='Arial', size=10)
    
    gold_border = Border(
        left=Side(style='thin', color='D4AF37'),
        right=Side(style='thin', color='D4AF37'),
        top=Side(style='thin', color='D4AF37'),
        bottom=Side(style='thin', color='D4AF37')
    )
    
    ncols = len(req.headers)
    last_col = get_column_letter(ncols)
    
    # Row 1: Company title
    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = 'BOLD Consulting'
    ws['A1'].font = navy_bold
    ws['A1'].fill = gold_fill
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 32
    
    # Row 2: Document title
    ws.merge_cells(f'A2:{last_col}2')
    ws['A2'] = req.title
    ws['A2'].font = Font(bold=True, color='FFFFFF', name='Arial', size=12)
    ws['A2'].fill = navy_fill
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 26
    
    # Row 3: Subtitle if any
    if req.subtitle:
        ws.merge_cells(f'A3:{last_col}3')
        ws['A3'] = req.subtitle
        ws['A3'].font = Font(italic=True, color='FFFFFF', name='Arial', size=10)
        ws['A3'].fill = PatternFill('solid', fgColor='162847')
        ws['A3'].alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[3].height = 20
        header_row = 4
    else:
        header_row = 3
    
    # Headers
    for ci, header in enumerate(req.headers, 1):
        cell = ws.cell(row=header_row, column=ci, value=header)
        cell.font = white_bold
        cell.fill = navy_fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = gold_border
    ws.row_dimensions[header_row].height = 24
    
    # Data rows
    for ri, row in enumerate(req.rows, header_row + 1):
        fill = alt_fill if (ri - header_row) % 2 == 0 else white_fill
        for ci, val in enumerate(row[:ncols], 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.font = normal
            cell.fill = fill
            cell.alignment = Alignment(
                horizontal='center' if ci != 2 else 'left',
                vertical='center'
            )
            cell.border = gold_border
        ws.row_dimensions[ri].height = 18
    
    # Freeze header
    ws.freeze_panes = ws.cell(row=header_row+1, column=1)
    
    # Auto column width
    for ci in range(1, ncols+1):
        col_letter = get_column_letter(ci)
        max_len = max(
            len(str(ws.cell(row=r, column=ci).value or ''))
            for r in range(1, header_row + len(req.rows) + 1)
        )
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)
    
    # Save
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    filename = req.title.replace(' ', '_') + '.xlsx'
    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )

# ─── FILE UPLOAD ─────────────────────────────────────────
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    await get_user(request)
    
    content = await file.read()
    filename = file.filename.lower()
    text = ""
    
    if filename.endswith(('.xlsx', '.xls')):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content))
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            text += f"Sheet: {sheet}\n"
            for row in ws.iter_rows(values_only=True):
                r = [str(c) if c is not None else '' for c in row]
                if any(r):
                    text += " | ".join(r) + "\n"
    elif filename.endswith('.pdf'):
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        for page in reader.pages:
            text += page.extract_text() + "\n"
    else:
        text = content.decode('utf-8', errors='ignore')
    
    return {"ok": True, "content": text[:15000], "filename": file.filename}


# ─── BILANC SQL ──────────────────────────────────────────
@app.get("/bilanc/clients")
async def get_bilanc_clients(request: Request, search: str = "", company: str = "BilancBoldConsulting"):
    await get_user(request)
    conn = await get_db()
    if search:
        rows = await conn.fetch(
            "SELECT * FROM bilanc_clients WHERE db_name=$1 AND LOWER(name) LIKE LOWER($2) ORDER BY name",
            company, f"%{search}%"
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM bilanc_clients WHERE db_name=$1 ORDER BY name",
            company
        )
    await conn.close()
    return [dict(r) for r in rows]

@app.get("/bilanc/all-clients")
async def get_all_bilanc_clients(request: Request):
    await get_user(request)
    conn = await get_db()
    rows = await conn.fetch(
        "SELECT * FROM bilanc_clients ORDER BY db_name, name"
    )
    await conn.close()
    return [dict(r) for r in rows]

@app.get("/bilanc/invoices")
async def get_bilanc_invoices(request: Request, client_name: str = ""):
    await get_user(request)
    conn = await get_db()
    if client_name:
        rows = await conn.fetch(
            """SELECT * FROM bilanc_invoices 
               WHERE LOWER(client_name) LIKE LOWER($1) 
               ORDER BY doc_date DESC LIMIT 20""",
            f"%{client_name}%"
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM bilanc_invoices ORDER BY doc_date DESC LIMIT 20"
        )
    await conn.close()
    return [dict(r) for r in rows]

@app.get("/bilanc/summary")
async def get_bilanc_summary(request: Request):
    await get_user(request)
    conn = await get_db()
    clients = await conn.fetchval("SELECT COUNT(*) FROM bilanc_clients")
    invoices = await conn.fetchval("SELECT COUNT(*) FROM bilanc_invoices")
    total = await conn.fetchval("SELECT COALESCE(SUM(total_with_vat), 0) FROM bilanc_invoices")
    unpaid = await conn.fetchval(
        "SELECT COALESCE(SUM(total_with_vat - amount_paid), 0) FROM bilanc_invoices WHERE amount_paid < total_with_vat"
    )
    await conn.close()
    return {
        "total_clients": clients,
        "total_invoices": invoices,
        "total_revenue": round(float(total), 2),
        "unpaid_amount": round(float(unpaid), 2)
    }

# ─── STATIC ──────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
