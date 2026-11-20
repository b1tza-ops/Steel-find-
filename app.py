import json, os, re, sqlite3, uuid
from pathlib import Path
import fitz
from flask import Flask, request, jsonify, send_file, abort

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024
DATA = Path(os.environ.get('STEELFIND_DATA', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
MARK = re.compile(r'\b(?:BRK|AN|TR|SP|SC|B|C|P)\d{3,8}\b', re.I)

def db():
    c = sqlite3.connect(DATA / 'index.sqlite')
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.executescript('''CREATE TABLE IF NOT EXISTS drawings(id TEXT PRIMARY KEY,name TEXT,project TEXT,pages INTEGER);
    CREATE TABLE IF NOT EXISTS marks(drawing TEXT,page INTEGER,mark TEXT,x REAL,y REAL,w REAL,h REAL);
    CREATE TABLE IF NOT EXISTS notes(project TEXT,mark TEXT,status TEXT,note TEXT,PRIMARY KEY(project,mark));''')

@app.get('/')
def home(): return app.send_static_file('index.html')

@app.get('/api/drawings')
def drawings():
    with db() as c: return jsonify([dict(r) for r in c.execute('SELECT * FROM drawings ORDER BY name')])

@app.post('/api/drawings')
def upload():
    f = request.files.get('file')
    if not f: abort(400, 'Choose a PDF file.')
    project = request.form.get('project','My project').strip()[:120] or 'My project'
    raw = f.read()
    try:
        doc = fitz.open(stream=raw, filetype='pdf')
        if doc.needs_pass or len(doc) > 100: abort(400, 'Use an unlocked PDF with at most 100 pages.')
        rows=[]
        for n,p in enumerate(doc):
            # Coordinates are normalized to the visible page, including page rotation.
            for word in p.get_text('words'):
                for match in MARK.finditer(word[4]):
                    r=fitz.Rect(word[:4]) * p.rotation_matrix
                    rows.append((n+1,match.group().upper(),r.x0/p.rect.width,r.y0/p.rect.height,r.width/p.rect.width,r.height/p.rect.height))
    except (RuntimeError, ValueError): abort(400, 'This file could not be read as a PDF.')
    ident=uuid.uuid4().hex
    path=DATA / (ident+'.pdf')
    path.write_bytes(raw)
    try:
        with db() as c:
            c.execute('INSERT INTO drawings VALUES(?,?,?,?)',(ident,Path(f.filename or 'drawing.pdf').name,project,len(doc)))
            c.executemany('INSERT INTO marks VALUES(?,?,?,?,?,?,?)',[(ident,*row) for row in rows])
    except Exception:
        path.unlink(missing_ok=True)
        raise
    doc.close()
    return jsonify(id=ident,matches=len(rows),message='Indexed' if rows else 'No searchable piece marks found. This PDF may need OCR.'),201

@app.get('/api/search')
def search():
    q=request.args.get('q','').strip().upper()
    project=request.args.get('project','')
    if not q: return jsonify([])
    with db() as c:
        rows=c.execute('''SELECT m.*,d.name,d.project,COALESCE(n.status,'Not started') status,COALESCE(n.note,'') note
        FROM marks m JOIN drawings d ON d.id=m.drawing LEFT JOIN notes n ON n.project=d.project AND n.mark=m.mark
        WHERE instr(m.mark,?)>0 AND (?='' OR d.project=?) ORDER BY m.mark,d.name,m.page LIMIT 1000''',(q,project,project))
        return jsonify([dict(r) for r in rows])

def drawing_path(ident):
    if not re.fullmatch('[a-f0-9]{32}',ident): abort(404)
    p=DATA/(ident+'.pdf')
    if not p.exists(): abort(404)
    return p

@app.get('/api/drawings/<ident>/pdf')
def pdf(ident): return send_file(drawing_path(ident),mimetype='application/pdf')

@app.get('/api/drawings/<ident>/pages/<int:page>')
def image(ident,page):
    with fitz.open(drawing_path(ident)) as d:
        if page<1 or page>len(d): abort(404)
        p=d[page-1]
        scale=min(2.5, 3500/max(p.rect.width,p.rect.height))
        data=p.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes('png')
    return app.response_class(data,mimetype='image/png')

@app.put('/api/notes')
def note():
    data=request.get_json() or {}
    if data.get('status') not in ['Not started','Delivered','Installed','Issue']: abort(400)
    project=str(data.get('project',''))[:120]; mark=str(data.get('mark','')).upper()
    if not MARK.fullmatch(mark): abort(400)
    with db() as c:
        if not c.execute('SELECT 1 FROM marks m JOIN drawings d ON d.id=m.drawing WHERE d.project=? AND m.mark=?',(project,mark)).fetchone(): abort(404)
        c.execute('INSERT OR REPLACE INTO notes VALUES(?,?,?,?)',(project,mark,data['status'],str(data.get('note',''))[:10000]))
    return jsonify(ok=True)

@app.errorhandler(400)
@app.errorhandler(413)
def bad_request(e): return jsonify(error=e.description),e.code

if __name__=='__main__': app.run(host='127.0.0.1',port=int(os.environ.get('PORT','8000')))
