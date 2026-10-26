"""Catalog gateway grant and authoritative-reply regression checks."""
import importlib
import os
import tempfile
import unittest
from test_notify import make_token, SECRET


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        os.environ['AUTH_SECRET']=SECRET
        os.environ['ARTIFACTS_DIR']=self.directory.name
        import app
        importlib.reload(app)
        self.calls=[]
        self.result={'app_id':'a'*32,'release_id':'b'*32,'version':'1.0.0'}
        def exchange(payload):
            self.calls.append(payload)
            if isinstance(self.result,Exception): raise self.result
            return self.result
        self.client=app.create_app(catalog_request=exchange).test_client()
        self.headers={'Authorization':'Bearer '+make_token(caps=['catalog:publish'],iid='a0000001')}
        self.body={'manifest':{'slug':'synthetic-demo'},'request_id':'retry-1'}

    def test_publish_waits_for_committed_result_and_attributes_instance(self):
        result=self.client.post('/v1/catalog/publications',json=self.body,headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json,self.result)
        self.assertEqual(self.calls,[dict(self.body,action='publish',instance_uuid='a0000001')])

    def test_status_uses_same_publisher_and_request(self):
        result=self.client.get('/v1/catalog/publications/retry-1',headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.calls,[{'action':'status','instance_uuid':'a0000001','request_id':'retry-1'}])

    def test_authentication_and_capability_required(self):
        for headers,code in [({},401),({'Authorization':'Bearer '+make_token()},403),
                ({'Authorization':'Bearer '+make_token(caps=['catalog:publish'],secret='wrong')},401)]:
            self.assertEqual(self.client.post('/v1/catalog/publications',json=self.body,headers=headers).status_code,code)
        self.assertEqual(self.calls,[])

    def test_cannot_supply_owner_or_instance(self):
        for key in ('user_id','instance_uuid','owner'):
            self.assertEqual(self.client.post('/v1/catalog/publications',json=dict(self.body,**{key:'foreign'}),headers=self.headers).status_code,400)
        self.assertEqual(self.calls,[])

    def test_timeout_or_transport_ack_is_not_publication_success(self):
        for result in (TimeoutError('private detail'),{'ok':True},{'error':'unknown'}):
            self.result=result
            response=self.client.post('/v1/catalog/publications',json=self.body,headers=self.headers)
            self.assertEqual(response.status_code,503)
            self.assertNotIn('private detail',response.get_data(as_text=True))

    def test_conflict_and_private_not_found(self):
        for error,status in [('conflict',409),('not_found',404)]:
            self.result={'error':error,'message':'private metadata must not leak'}
            response=self.client.post('/v1/catalog/publications',json=self.body,headers=self.headers)
            self.assertEqual(response.status_code,status)
            self.assertNotIn('private metadata',response.get_data(as_text=True))

    def test_bounded_publication(self):
        body=dict(self.body,manifest={'description':'x'*(64*1024)})
        self.assertEqual(self.client.post('/v1/catalog/publications',json=body,headers=self.headers).status_code,413)
        self.assertEqual(self.calls,[])

    def test_private_app_quota_is_non_retryable_for_publish_and_status(self):
        self.result={'error':'private_app_limit_reached', 'message':'untrusted backend detail'}
        for method,path,body in [('POST','/v1/catalog/publications',self.body),
                ('GET','/v1/catalog/publications/retry-1',None)]:
            response=self.client.open(path,method=method,json=body,headers=self.headers)
            self.assertEqual(response.status_code,409)
            self.assertEqual(response.json,{'error':'private_app_limit_reached',
                'message':'You can have up to 100 private apps per account.'})
