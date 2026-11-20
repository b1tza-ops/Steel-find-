import io
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import fitz
from flask import Flask, abort, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024
DATA = Path(os.environ.get('STEELFIND_DATA', 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
MARK = re.compile(r'\b(?:BRK|AN|TR|SP|SC|B|C|P)\d{3,8}\b', re.I)
STATUSES = ['Not started', 'Delivered', 'Installed', 'Issue']
Image.MAX_IMAGE_PIXELS = 30_000_000


@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'index.sqlite', timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


with db() as c:
    c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS drawings(id TEXT PRIMARY KEY,name TEXT,project TEXT,pages INTEGER);
        CREATE TABLE IF NOT EXISTS marks(drawing TEXT,page INTEGER,mark TEXT,x REAL,y REAL,w REAL,h REAL);
        CREATE TABLE IF NOT EXISTS notes(project TEXT,mark TEXT,status TEXT,note TEXT,PRIMARY KEY(project,mark));
        CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,project TEXT,mark TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS marks_drawing ON marks(drawing);
        CREATE INDEX IF NOT EXISTS marks_mark ON marks(mark);
        CREATE INDEX IF NOT EXISTS photos_piece ON photos(project,mark);
    ''')
    columns = {row['name'] for row in c.execute('PRAGMA table_info(drawings)')}
    for column in ['number', 'revision', 'floor', 'lot']:
        if column not in columns:
            c.execute(f"ALTER TABLE drawings ADD COLUMN {column} TEXT DEFAULT ''")
    c.execute('INSERT OR IGNORE INTO projects SELECT DISTINCT project FROM drawings')


def field(data, name, limit=120):
    return str(data.get(name, '') or '').strip()[:limit]


def require_piece(c, project, mark):
    if not MARK.fullmatch(mark):
        abort(400, 'Enter a valid piece mark.')
    if not c.execute('''SELECT 1 FROM marks m JOIN drawings d ON d.id=m.drawing
                        WHERE d.project=? AND m.mark=?''', (project, mark)).fetchone():
        abort(404, 'Piece not found in this project.')


@app.get('/')
def home():
    return app.send_static_file('index.html')


@app.get('/api/projects')
def projects():
    with db() as c:
        rows = c.execute('''
            WITH pieces AS (
                SELECT DISTINCT d.project,m.mark FROM marks m JOIN drawings d ON d.id=m.drawing
            ), counts AS (
                SELECT p.project,COUNT(*) pieces,
                  SUM(CASE WHEN n.status='Installed' THEN 1 ELSE 0 END) installed,
                  SUM(CASE WHEN n.status='Issue' THEN 1 ELSE 0 END) issues
                FROM pieces p LEFT JOIN notes n ON n.project=p.project AND n.mark=p.mark GROUP BY p.project
            )
            SELECT p.name,(SELECT COUNT(*) FROM drawings d WHERE d.project=p.name) drawings,
              COALESCE(c.pieces,0) pieces,COALESCE(c.installed,0) installed,COALESCE(c.issues,0) issues
            FROM projects p LEFT JOIN counts c ON c.project=p.name ORDER BY p.name
        ''')
        return jsonify([dict(row) for row in rows])


@app.post('/api/projects')
def create_project():
    name = field(request.get_json() or {}, 'name')
    if not name:
        abort(400, 'Give your project a name.')
    with db() as c:
        if c.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            abort(409, 'A project with this name already exists.')
        c.execute('INSERT INTO projects VALUES(?)', (name,))
    return jsonify(name=name), 201


@app.get('/api/drawings')
def drawings():
    with db() as c:
        return jsonify([dict(row) for row in c.execute('''SELECT d.*,
            (SELECT COUNT(DISTINCT mark) FROM marks m WHERE m.drawing=d.id) piece_count
            FROM drawings d ORDER BY d.name''')])


@app.post('/api/drawings')
def upload():
    f = request.files.get('file')
    if not f:
        abort(400, 'Choose a PDF file.')
    project = field(request.form, 'project') or 'My project'
    raw = f.read()
    try:
        with fitz.open(stream=raw, filetype='pdf') as doc:
            if doc.needs_pass or not len(doc) or len(doc) > 100:
                abort(400, 'Use an unlocked PDF with 1 to 100 pages.')
            pages = len(doc)
            rows = []
            for n, p in enumerate(doc):
                for word in p.get_text('words'):
                    for match in MARK.finditer(word[4]):
                        rect = fitz.Rect(word[:4]) * p.rotation_matrix
                        rows.append((n + 1, match.group().upper(), rect.x0 / p.rect.width,
                                     rect.y0 / p.rect.height, rect.width / p.rect.width, rect.height / p.rect.height))
    except (RuntimeError, ValueError):
        abort(400, 'This file could not be read as a PDF.')
    ident = uuid.uuid4().hex
    path = DATA / (ident + '.pdf')
    path.write_bytes(raw)
    try:
        with db() as c:
            c.execute('INSERT OR IGNORE INTO projects VALUES(?)', (project,))
            c.execute('''INSERT INTO drawings(id,name,project,pages,number,revision,floor,lot)
                         VALUES(?,?,?,?,?,?,?,?)''',
                      (ident, Path(f.filename or 'drawing.pdf').name, project, pages,
                       *[field(request.form, k) for k in ['number', 'revision', 'floor', 'lot']]))
            c.executemany('INSERT INTO marks VALUES(?,?,?,?,?,?,?)', [(ident, *row) for row in rows])
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return jsonify(id=ident, matches=len(rows), message='Indexed' if rows else
                   'No searchable piece marks found. This PDF may need OCR.'), 201


@app.patch('/api/drawings/<ident>')
def update_drawing(ident):
    data = request.get_json() or {}
    with db() as c:
        if not c.execute('SELECT 1 FROM drawings WHERE id=?', (ident,)).fetchone():
            abort(404)
        c.execute('UPDATE drawings SET number=?,revision=?,floor=?,lot=? WHERE id=?',
                  (*[field(data, k) for k in ['number', 'revision', 'floor', 'lot']], ident))
    return jsonify(ok=True)


@app.get('/api/search')
def search():
    q = request.args.get('q', '').strip().upper()
    project = request.args.get('project', '')
    if not q:
        return jsonify([])
    with db() as c:
        rows = c.execute('''SELECT m.*,d.name,d.project,d.number,d.revision,d.floor,d.lot,
            COALESCE(n.status,'Not started') status,COALESCE(n.note,'') note
            FROM marks m JOIN drawings d ON d.id=m.drawing
            LEFT JOIN notes n ON n.project=d.project AND n.mark=m.mark
            WHERE instr(m.mark,?)>0 AND (?='' OR d.project=?)
            ORDER BY m.mark,d.name,m.page LIMIT 1000''', (q, project, project))
        return jsonify([dict(row) for row in rows])


@app.get('/api/pieces')
def pieces():
    project = request.args.get('project', '')
    q = request.args.get('q', '').strip().upper()
    status = request.args.get('status', '')
    try:
        offset = max(0, int(request.args.get('offset', '0')))
    except ValueError:
        abort(400)
    query = '''FROM (SELECT DISTINCT d.project,m.mark FROM marks m JOIN drawings d ON d.id=m.drawing) p
               LEFT JOIN notes n ON n.project=p.project AND n.mark=p.mark
               WHERE (?='' OR p.project=?) AND instr(p.mark,?)>0 AND (?='' OR COALESCE(n.status,'Not started')=?)'''
    params = (project, project, q, status, status)
    with db() as c:
        total = c.execute('SELECT COUNT(*) ' + query, params).fetchone()[0]
        rows = c.execute('''SELECT p.*,COALESCE(n.status,'Not started') status,COALESCE(n.note,'') note,
            (SELECT COUNT(*) FROM photos ph WHERE ph.project=p.project AND ph.mark=p.mark) photos ''' + query +
            ' ORDER BY p.project,p.mark LIMIT 100 OFFSET ?', (*params, offset))
        return jsonify(total=total, items=[dict(row) for row in rows])


def drawing_path(ident):
    if not re.fullmatch('[a-f0-9]{32}', ident):
        abort(404)
    path = DATA / (ident + '.pdf')
    if not path.exists():
        abort(404)
    return path


@app.get('/api/drawings/<ident>/pdf')
def pdf(ident):
    return send_file(drawing_path(ident), mimetype='application/pdf')


@app.get('/api/drawings/<ident>/pages/<int:page>')
def image(ident, page):
    with fitz.open(drawing_path(ident)) as doc:
        if page < 1 or page > len(doc):
            abort(404)
        p = doc[page - 1]
        max_size = 650 if request.args.get('thumbnail') == '1' else 3500
        scale = min(2.5, max_size / max(p.rect.width, p.rect.height))
        data = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
    response = app.response_class(data, mimetype='image/png')
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response


@app.put('/api/notes')
def note():
    data = request.get_json() or {}
    if data.get('status') not in STATUSES:
        abort(400, 'Choose a valid installation status.')
    project, mark = field(data, 'project'), field(data, 'mark').upper()
    with db() as c:
        require_piece(c, project, mark)
        c.execute('INSERT OR REPLACE INTO notes VALUES(?,?,?,?)',
                  (project, mark, data['status'], field(data, 'note', 10000)))
    return jsonify(ok=True)


@app.get('/api/photos')
def photos():
    with db() as c:
        rows = c.execute('SELECT * FROM photos WHERE project=? AND mark=? ORDER BY created,id',
                         (request.args.get('project', ''), request.args.get('mark', '').upper()))
        return jsonify([dict(row) for row in rows])


@app.post('/api/photos')
def upload_photo():
    project, mark = field(request.form, 'project'), field(request.form, 'mark').upper()
    with db() as c:
        require_piece(c, project, mark)
    f = request.files.get('file')
    if not f:
        abort(400, 'Choose a site photo.')
    raw = f.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        abort(400, 'Use a photo smaller than 12 MB.')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 30_000_000:
                abort(400, 'Use a photo with at most 30 megapixels.')
            if source.format not in {'JPEG', 'PNG', 'WEBP'}:
                abort(400, 'Use a JPEG, PNG or WebP photo.')
            photo = ImageOps.exif_transpose(source)
            photo.thumbnail((2400, 2400))
            output = io.BytesIO()
            photo.convert('RGB').save(output, format='JPEG', quality=85)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        abort(400, 'Use a JPEG, PNG or WebP photo. HEIC is not supported yet.')
    ident = uuid.uuid4().hex
    path = DATA / (ident + '.jpg')
    path.write_bytes(output.getvalue())
    try:
        with db() as c:
            c.execute('INSERT INTO photos(id,project,mark) VALUES(?,?,?)', (ident, project, mark))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return jsonify(id=ident), 201


@app.get('/api/photos/<ident>')
def photo(ident):
    if not re.fullmatch('[a-f0-9]{32}', ident):
        abort(404)
    path = DATA / (ident + '.jpg')
    if not path.exists():
        abort(404)
    response = send_file(path, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def bad_request(error):
    return jsonify(error=error.description), error.code


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8000')))
