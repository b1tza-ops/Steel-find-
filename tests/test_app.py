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
