"""HTTP-level acceptance: real handler/service with fixture upstream only."""
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from urllib.error import HTTPError
from urllib.parse import quote, unquote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app'))
import server
from subtitle_providers import SubtitleService
from test_subtitle_providers import RELEASE, SRT, assrt_payload, assrt_row

class SubtitleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=SubtitleService(pathlib.Path(self.temp.name)/'settings.json')
        self.responses=[]
        def http(*args, **kwargs):
            value=self.responses.pop(0)
            return value if isinstance(value,tuple) else (200,{},json.dumps(value).encode())
        self.service._http=http
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.http.is_gateway=False;self.http.api_token='test-only-api-credential'
        self.http.subtitle_service=self.service;self.http.subtitle_csrf='test-only-csrf'
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.temp.cleanup()
    def request(self,path,body=None,auth=True,csrf=True):
        headers={'Authorization':'Bearer '+self.http.api_token} if auth else {}
        if body is not None:
            headers['Content-Type']='application/json'
            if csrf:headers['X-Subtitle-CSRF']=self.http.subtitle_csrf
        return urllib.request.urlopen(urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers))
    def test_auth_csrf_and_invalid_settings(self):
        for path,body,auth,csrf in [('/api/subtitles/settings',None,False,False),('/api/subtitles/settings',{},True,False),('/api/subtitles/download',{'file_id':'x'},False,True)]:
            with self.subTest(path=path,auth=auth,csrf=csrf):
                with self.assertRaises(HTTPError) as error:self.request(path,body,auth,csrf)
                self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:self.request('/api/subtitles/settings',[])
        self.assertEqual(error.exception.code,400)
        self.assertFalse(self.service.settings_path.exists())
    def test_settings_search_file_download_end_to_end(self):
        with self.request('/api/subtitles/settings') as response:settings=json.load(response)
        self.assertFalse(settings['assrt_configured']);self.assertTrue(settings['csrf'])
        with self.request('/api/subtitles/settings',{'assrt_token':'fixture-token'}) as response:
            raw=response.read();settings=json.loads(raw)
        self.assertTrue(settings['assrt_configured']);self.assertNotIn(b'fixture-token',raw)
        self.responses.append(assrt_payload([assrt_row()]))
        with self.request('/api/subtitles?title='+quote(RELEASE)) as response:candidate=json.load(response)['items'][0]
        detail=assrt_payload([dict(assrt_row(),filelist=[{'f':RELEASE+'.chs.srt','url':'https://file0.assrt.net/test.srt'}])])
        self.responses.append(detail)
        with self.request('/api/subtitles/files?id='+candidate['id']) as response:file=json.load(response)['files'][0]
        self.responses.extend([detail,(200,{'content-type':'application/octet-stream'},SRT)])
        with self.request('/api/subtitles/download',{'file_id':file['id'],'video_name':'实机示例.S01E02.mkv','language':'zh-CN'}) as response:
            self.assertEqual(response.read(),SRT)
            name=unquote(response.headers['Content-Disposition'].split("UTF-8''")[1])
            self.assertEqual(name,'实机示例.S01E02.zh-CN.srt')
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.service.settings_path.stat().st_mode & 0o777,0o600)
    def test_stale_selection_and_no_arbitrary_download_url(self):
        with self.assertRaises(HTTPError) as error:self.request('/api/subtitles/download',{'file_id':'https://127.0.0.1/private'})
        self.assertEqual(error.exception.code,410)
        self.assertEqual(self.responses,[])
    def test_gateway_root_opens_latest_tv(self):
        self.http.is_gateway=True;self.http.local_test=True
        with urllib.request.urlopen(self.base+'/app/media_link_helper/') as response:body=response.read().decode()
        self.assertIn('<h1>美剧追新</h1>',body)
        self.assertIn('discover-list',body)
        self.assertIn('settings.html',body)

if __name__=='__main__':unittest.main()
