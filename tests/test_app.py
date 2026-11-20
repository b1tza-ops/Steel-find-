import importlib,os,tempfile,unittest,io
import fitz

class DrawingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();os.environ['STEELFIND_DATA']=cls.tmp.name
        import app
        cls.module=importlib.reload(app);cls.client=cls.module.app.test_client()
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def pdf(self,mark='AN22020',rotation=0):
        d=fitz.open();p=d.new_page();p.insert_text((80,100),mark);p.set_rotation(rotation);b=d.tobytes();d.close();return b
    def upload(self,name,project='Test',rotation=0):
        return self.client.post('/api/drawings',data={'file':(io.BytesIO(self.pdf(rotation=rotation)),name),'project':project})
    def test_search_links_sheets_and_persists_notes(self):
        a=self.upload('erection.pdf');b=self.upload('angles.pdf')
        self.assertEqual(a.status_code,201);self.assertEqual(b.status_code,201)
        hits=self.client.get('/api/search?q=22020&project=Test').json
        self.assertEqual(len(hits),2)
        self.assertTrue(all(0<=h['x']<=1 and 0<=h['y']<=1 for h in hits))
        self.assertEqual(self.client.put('/api/notes',json={'project':'Test','mark':'AN22020','status':'Installed','note':'Done'}).status_code,200)
        self.assertTrue(all(h['status']=='Installed' for h in self.client.get('/api/search?q=an22020&project=Test').json))
        ident=a.json['id'];self.assertEqual(self.client.get(f'/api/drawings/{ident}/pages/1').mimetype,'image/png')
        self.assertEqual(self.client.get(f'/api/drawings/{ident}/pages/2').status_code,404)
    def test_invalid_pdf_rejected(self):
        r=self.client.post('/api/drawings',data={'file':(io.BytesIO(b'invalid'),'bad.pdf')});self.assertEqual(r.status_code,400)
    def test_projects_and_rotation(self):
        self.upload('rotated.pdf','Other',90)
        hits=self.client.get('/api/search?q=22020&project=Other').json
        self.assertEqual(len(hits),1);self.assertTrue(0<=hits[0]['x']<=1);self.assertTrue(0<=hits[0]['y']<=1)
        self.assertEqual(self.client.get('/api/search?q=22020&project=Missing').json,[])
    def test_invalid_status(self):
        self.assertEqual(self.client.put('/api/notes',json={'status':'anything'}).status_code,400)

    def test_project_creation_and_unique_progress(self):
        self.assertEqual(self.client.post('/api/projects',json={'name':'New job'}).status_code,201)
        self.assertEqual(self.client.post('/api/projects',json={'name':'New job'}).status_code,409)
        self.upload('first.pdf','Progress job');self.upload('second.pdf','Progress job')
        self.client.put('/api/notes',json={'project':'Progress job','mark':'AN22020','status':'Issue','note':'Missing bolts'})
        project=next(p for p in self.client.get('/api/projects').json if p['name']=='Progress job')
        self.assertEqual((project['drawings'],project['pieces'],project['issues']),(2,1,1))
        listing=self.client.get('/api/pieces?project=Progress+job&status=Issue').json
        self.assertEqual(listing['total'],1)
        self.assertEqual(listing['items'][0]['note'],'Missing bolts')
        self.assertEqual(self.client.get('/api/pieces?project=Progress+job&status=Installed').json['total'],0)

    def test_drawing_metadata_and_thumbnail(self):
        ident=self.upload('metadata.pdf','Metadata job').json['id']
        details={'number':'SW61','revision':'C01','floor':'Level 4','lot':'22 West'}
        self.assertEqual(self.client.patch('/api/drawings/'+ident,json=details).status_code,200)
        hit=self.client.get('/api/search?q=AN22020&project=Metadata+job').json[0]
        self.assertTrue(all(hit[k]==v for k,v in details.items()))
        response=self.client.get(f'/api/drawings/{ident}/pages/1?thumbnail=1')
        self.assertEqual(response.status_code,200)
        from PIL import Image
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertLessEqual(max(image.size),651)

    def test_photo_upload_and_project_isolation(self):
        from PIL import Image
        self.upload('photo.pdf','Photo job')
        photo=io.BytesIO();Image.new('RGB',(40,30),'green').save(photo,format='PNG');photo.seek(0)
        result=self.client.post('/api/photos',data={'project':'Photo job','mark':'AN22020','file':(photo,'photo.png')})
        self.assertEqual(result.status_code,201)
        with self.client.get('/api/photos/'+result.json['id']) as response:
            self.assertEqual(response.mimetype,'image/jpeg')
        self.assertEqual(len(self.client.get('/api/photos?project=Photo+job&mark=AN22020').json),1)
        self.assertEqual(self.client.get('/api/photos?project=Other&mark=AN22020').json,[])
        self.assertEqual(self.client.get('/api/pieces?project=Photo+job').json['items'][0]['photos'],1)
        bad=self.client.post('/api/photos',data={'project':'Photo job','mark':'AN22020','file':(io.BytesIO(b'not an image'),'fake.jpg')})
        self.assertEqual(bad.status_code,400)

    def test_progress_pagination(self):
        d=fitz.open();p=d.new_page()
        for n in range(125):
            p.insert_text((30+(n//45)*150,30+(n%45)*15),'B'+str(30000+n))
        self.client.post('/api/drawings',data={'file':(io.BytesIO(d.tobytes()),'many.pdf'),'project':'Many pieces'})
        d.close()
        first=self.client.get('/api/pieces?project=Many+pieces').json
        second=self.client.get('/api/pieces?project=Many+pieces&offset=100').json
        self.assertEqual(first['total'],125);self.assertEqual(len(first['items']),100);self.assertEqual(len(second['items']),25)
        self.assertFalse({x['mark'] for x in first['items']} & {x['mark'] for x in second['items']})

    def test_upgrade_preserves_existing_notes(self):
        import sqlite3, subprocess, sys
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            with sqlite3.connect(Path(folder)/'index.sqlite') as c:
                c.executescript("CREATE TABLE drawings(id TEXT PRIMARY KEY,name TEXT,project TEXT,pages INTEGER); CREATE TABLE notes(project TEXT,mark TEXT,status TEXT,note TEXT,PRIMARY KEY(project,mark)); INSERT INTO drawings VALUES('old','old.pdf','Legacy',1); INSERT INTO notes VALUES('Legacy','AN22020','Installed','Existing record');")
            env={**os.environ,'STEELFIND_DATA':folder}
            script="import app; c=app.app.test_client(); assert c.get('/api/drawings').json[0]['revision']==''; assert c.get('/api/projects').json[0]['name']=='Legacy'; connection=app.sqlite3.connect(app.DATA/'index.sqlite'); assert connection.execute('SELECT note FROM notes').fetchone()[0]=='Existing record'; connection.close()"
            subprocess.run([sys.executable,'-c',script],env=env,check=True)
