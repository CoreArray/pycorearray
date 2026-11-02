"""Tests of the cloud access (pycorearray.cloud), ported from the gdscloud R
package: settings and credentials, request signing checked against an
independent computation, and reads from a local HTTP server (in a child
process, see cloud_server.py) through the HTTP, S3 and Azure backends.
"""

import base64
import datetime
import hashlib
import hmac
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import numpy as np
import pytest

import pycorearray
from pycorearray import cloud


pytestmark = pytest.mark.skipif(not cloud.available(),
	reason='pycorearray was built without cloud access')

HERE = os.path.dirname(os.path.abspath(__file__))

AZURITE_KEY = ('Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6'
	'tq/K1SZFPTOtr/KBHBeksoGMGw==')
EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
T2013 = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)

ENV_VARS = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION',
	'AWS_SESSION_TOKEN', 'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_S3',
	'GDSCLOUD_S3_PATH_STYLE', 'AZURE_STORAGE_ACCOUNT', 'AZURE_STORAGE_KEY',
	'AZURE_STORAGE_SAS_TOKEN', 'AZURE_STORAGE_ACCESS_TOKEN',
	'AZURE_STORAGE_ENDPOINT_SUFFIX', 'AZURE_STORAGE_SERVICE_ENDPOINT',
	'AZURE_STORAGE_CONNECTION_STRING', 'GCS_ACCESS_TOKEN',
	'GDSCLOUD_HTTP_TOKEN']


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
	# each test starts with no settings and no credential environment variables
	for v in ENV_VARS:
		monkeypatch.delenv(v, raising=False)
	saved = cloud.export_credentials()
	cloud.import_credentials({'settings': {}, 'url_settings': {}})
	yield
	cloud.import_credentials(saved)


def sha(s):
	return hashlib.sha256(s.encode()).hexdigest()

def hmac_sha256(key, s):
	return hmac.new(key, s.encode(), hashlib.sha256).digest()

def sigv4(secret, date, region, string_to_sign):
	k = hmac_sha256(('AWS4' + secret).encode(), date)
	for s in (region, 's3', 'aws4_request'):
		k = hmac_sha256(k, s)
	return hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

def header(req, name):
	hs = [ h for h in req['headers'] if h.lower().startswith(name.lower() + ':') ]
	return hs[0] if hs else None


# ===========================================================================
# Settings

def test_schemes():
	assert set(cloud.schemes()) == {'http', 'https', 's3', 'gs', 'az'}
	assert all(cloud.schemes().values())


def test_open_rejects_unsupported_urls():
	for url in ('ftp://example.com/file.gds', 'ssh://example.com/file.gds'):
		with pytest.raises(ValueError, match='Unsupported URL scheme') as e:
			cloud.open(url)
		for s in cloud.schemes():
			assert s + '://' in str(e.value)
		with pytest.raises(ValueError, match='Unsupported URL scheme'):
			pycorearray.gdsfile().open(url)
	with pytest.raises(TypeError):
		cloud.open(123)
	with pytest.raises(ValueError, match='read-only'):
		pycorearray.gdsfile().open('s3://bucket/file.gds', readonly=False)


def test_valid_schemes_fail_on_access_not_scheme():
	cloud.options(connect_timeout=2, timeout=2, max_retries=0)
	for s in cloud.schemes():
		with pytest.raises(Exception) as e:
			cloud.open(s + '://invalid.test/nonexistent.gds')
		assert 'Unsupported URL scheme' not in str(e.value)


def test_options():
	opt = cloud.options()
	assert opt == {'connect_timeout': 30, 'timeout': 60, 'max_retries': 3,
		'cache_size': 64}
	cloud.options(connect_timeout=5, timeout=7)
	cur = cloud.options()
	assert cur['connect_timeout'] == 5 and cur['timeout'] == 7
	assert cur['cache_size'] == 64
	assert cloud.cache_size(32) == 32 and cloud.options()['cache_size'] == 32
	cloud.options(max_retries=0)
	assert cloud.options()['max_retries'] == 0
	for kw in ({'timeout': -1}, {'connect_timeout': 'a'}, {'max_retries': -1},
			{'cache_size': [1, 2]}, {'cache_size': True}):
		with pytest.raises(ValueError):
			cloud.options(**kw)
	for v in (-1, 0, 'big'):
		with pytest.raises(ValueError):
			cloud.cache_size(v)


def test_cache_info_and_clear():
	info = cloud.cache_info(verbose=False)
	assert list(info) == ['num_streams', 'hits', 'misses', 'retries']
	assert cloud.cache_clear() is None
	assert isinstance(cloud.list_streams(), list)


def test_http_credentials():
	cloud.config_http(bearer_token='test-token-123')
	assert cloud._get_http_credentials('https://example.com/f.gds') == \
		{'bearer_token': 'test-token-123'}
	cloud.config_http(bearer_token='specific', url='https://private.example.com')
	assert cloud._get_http_credentials(
		'https://private.example.com/data/f.gds')['bearer_token'] == 'specific'
	assert cloud._get_http_credentials(
		'https://other.example.com/f.gds')['bearer_token'] == 'test-token-123'
	with pytest.raises(ValueError, match="'http://' or 'https://'"):
		cloud.config_http(bearer_token='x', url='s3://bucket/')
	with pytest.raises(TypeError):
		cloud.config_http(bearer_token=1)


def test_url_prefixes():
	# the longest matching prefix wins; a trailing '*' is ignored
	cloud.config_s3(region='r-bucket', url='s3://bucket*')
	cloud.config_s3(region='r-dir', url='s3://bucket/dir/')
	assert cloud._get_s3_credentials('s3://bucket/dir/f.gds')['region'] == 'r-dir'
	assert cloud._get_s3_credentials('s3://bucket/f.gds')['region'] == 'r-bucket'
	assert cloud._get_s3_credentials('s3://other/f.gds')['region'] == 'us-east-1'
	# an entry with no setting is removed
	cloud.config_s3(url='s3://bucket/dir/')
	assert cloud._get_s3_credentials('s3://bucket/dir/f.gds')['region'] == 'r-bucket'
	with pytest.raises(ValueError, match='does not match'):
		cloud.config_s3(region='x', url='gs://bucket/')


def test_credential_priority(monkeypatch):
	# URL-specific > session > environment variable
	monkeypatch.setenv('GCS_ACCESS_TOKEN', 'env')
	assert cloud._get_gcs_credentials('gs://b/k')['access_token'] == 'env'
	cloud.config_gcs(access_token='session')
	assert cloud._get_gcs_credentials('gs://b/k')['access_token'] == 'session'
	cloud.config_gcs(access_token=lambda: 'url', url='gs://b/')
	assert cloud._get_gcs_credentials('gs://b/k')['access_token'] == 'url'
	# a provider returning None falls back to the next one
	cloud.config_gcs(access_token=lambda: None, url='gs://b/')
	assert cloud._get_gcs_credentials('gs://b/k')['access_token'] == 'session'
	cloud.config_gcs(access_token='')
	assert cloud._get_gcs_credentials('gs://c/k')['access_token'] == 'env'


def test_export_import_credentials():
	cloud.config_s3(aws_access_key_id='K', url='s3://b/')
	cloud.config_http(bearer_token='tok')
	cloud.options(timeout=9)
	saved = cloud.export_credentials()
	cloud.import_credentials({'settings': {}, 'url_settings': {}})
	assert cloud._get_http_credentials('https://x/y')['bearer_token'] == ''
	cloud.import_credentials(saved)
	assert cloud._get_http_credentials('https://x/y')['bearer_token'] == 'tok'
	assert cloud._get_s3_credentials('s3://b/k')['access_key'] == 'K'
	assert cloud.options()['timeout'] == 9


# ===========================================================================
# Request signing, checked without network access

def test_sigv4_s3_get_object_example():
	# https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
	# (example "GET Object"); gdscloud uses the regional host, so the
	# signature is recomputed independently for that host
	cloud.config_s3(aws_access_key_id='AKIAIOSFODNN7EXAMPLE',
		aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
		region='us-east-1', session_token='')
	r = cloud._prepare_request('s3://examplebucket/test.txt',
		range='bytes=0-9', time=T2013)
	assert r['url'] == 'https://examplebucket.s3.us-east-1.amazonaws.com/test.txt'
	auth = header(r, 'Authorization')
	assert auth.startswith('Authorization: AWS4-HMAC-SHA256 '
		'Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
		'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature=')
	assert 'x-amz-date: 20130524T000000Z' in r['headers']
	assert 'x-amz-content-sha256: ' + EMPTY_SHA256 in r['headers']
	assert header(r, 'x-amz-security-token') is None
	canonical = '\n'.join(['GET', '/test.txt', '',
		'host:examplebucket.s3.us-east-1.amazonaws.com', 'range:bytes=0-9',
		'x-amz-content-sha256:' + EMPTY_SHA256, 'x-amz-date:20130524T000000Z',
		'', 'host;range;x-amz-content-sha256;x-amz-date', EMPTY_SHA256])
	sts = '\n'.join(['AWS4-HMAC-SHA256', '20130524T000000Z',
		'20130524/us-east-1/s3/aws4_request', sha(canonical)])
	assert auth.split('Signature=')[1] == sigv4(
		'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', '20130524', 'us-east-1', sts)


def test_sigv4_session_token():
	cloud.config_s3(aws_access_key_id='AKIA', aws_secret_access_key='s',
		region='eu-west-1', session_token='TOKEN')
	r = cloud._prepare_request('s3://b/k.gds')
	assert 'x-amz-security-token: TOKEN' in r['headers']
	auth = header(r, 'Authorization')
	assert 'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date;' \
		'x-amz-security-token,' in auth
	assert '/eu-west-1/s3/aws4_request' in auth


def test_aws_credentials_function():
	cloud.config_s3(aws_access_key_id='', aws_secret_access_key='',
		session_token='', region='us-west-2',
		credentials=lambda: {'aws_access_key_id': 'AKIAPROV',
			'aws_secret_access_key': 's', 'aws_session_token': 'STSTOKEN'})
	r = cloud._prepare_request('s3://b/k.gds')
	auth = header(r, 'Authorization')
	assert 'Credential=AKIAPROV/' in auth and '/us-west-2/s3/aws4_request' in auth
	assert 'x-amz-security-token: STSTOKEN' in r['headers']
	# a key without a secret is an error, and None means "not available"
	cloud.config_s3(credentials=lambda: {'aws_access_key_id': 'K'})
	with pytest.raises(ValueError, match='aws_secret_access_key'):
		cloud._get_s3_credentials('s3://b/k')
	cloud.config_s3(credentials=lambda: None, aws_access_key_id='STATIC')
	assert cloud._get_s3_credentials('s3://b/k')['access_key'] == 'STATIC'


def test_object_keys_percent_encoded_once():
	cloud.config_s3(aws_access_key_id='', aws_secret_access_key='',
		region='us-east-1', session_token='')
	r = cloud._prepare_request('s3://my-bucket/dir with space/f+g%.gds')
	assert r['url'] == ('https://my-bucket.s3.us-east-1.amazonaws.com/'
		'dir%20with%20space/f%2Bg%25.gds')
	assert r['headers'] == []   # anonymous
	cloud.config_gcs(access_token='tok')
	r = cloud._prepare_request('gs://bkt/a b/c.gds')
	assert r['url'] == 'https://storage.googleapis.com/bkt/a%20b/c.gds'
	assert r['headers'] == ['Authorization: Bearer tok']
	cloud.config_azure(account_name='acct', account_key='',
		sas_token='?sv=2020&sig=xyz')
	r = cloud._prepare_request('az://cont/a b.gds')
	assert r['url'] == \
		'https://acct.blob.core.windows.net/cont/a%20b.gds?sv=2020&sig=xyz'
	assert r['headers'] == []


def test_azure_shared_key():
	cloud.config_azure(account_name='acct', account_key=AZURITE_KEY,
		sas_token='')
	r = cloud._prepare_request('az://cont/blob.gds', range='bytes=0-9',
		time=T2013)
	assert r['url'] == 'https://acct.blob.core.windows.net/cont/blob.gds'
	assert 'x-ms-date: Fri, 24 May 2013 00:00:00 GMT' in r['headers']
	assert 'x-ms-version: 2020-10-02' in r['headers']
	sts = ('GET\n\n\n\n\n\n\n\n\n\n\nbytes=0-9\n'
		'x-ms-date:Fri, 24 May 2013 00:00:00 GMT\nx-ms-version:2020-10-02\n'
		'/acct/cont/blob.gds')
	sig = base64.b64encode(hmac_sha256(base64.b64decode(AZURITE_KEY), sts))
	assert header(r, 'Authorization') == \
		'Authorization: SharedKey acct:' + sig.decode()


def test_http_bearer_only_when_configured():
	cloud.config_http(bearer_token='')
	r = cloud._prepare_request('https://example.org/x.gds')
	assert r == {'url': 'https://example.org/x.gds', 'headers': []}
	cloud.config_http(bearer_token='t0k')
	assert cloud._prepare_request('https://example.org/x.gds')['headers'] == \
		['Authorization: Bearer t0k']


def test_s3_custom_endpoints():
	def cfg(**kw):
		cloud.config_s3(aws_access_key_id='K', aws_secret_access_key='S',
			region='us-east-1', session_token='', **kw)
	# path style is the default for a custom endpoint; the port is kept
	cfg(endpoint='http://minio.local:9000', path_style='auto')
	assert cloud._prepare_request('s3://bkt/dir/f.gds')['url'] == \
		'http://minio.local:9000/bkt/dir/f.gds'
	# https by default; explicit virtual-hosted style
	cfg(endpoint='s3.wasabisys.com', path_style=False)
	assert cloud._prepare_request('s3://bkt/f.gds')['url'] == \
		'https://bkt.s3.wasabisys.com/f.gds'
	# a path prefix on the endpoint; trailing slash and query ignored
	cfg(endpoint='https://gw.example.org/s3/?x=1', path_style=True)
	assert cloud._prepare_request('s3://bkt/f.gds')['url'] == \
		'https://gw.example.org/s3/bkt/f.gds'
	# path style on Amazon S3 itself (bucket names with dots)
	cfg(endpoint='', path_style=True)
	assert cloud._prepare_request('s3://my.bucket/f.gds')['url'] == \
		'https://s3.us-east-1.amazonaws.com/my.bucket/f.gds'
	cfg(endpoint='https:///nohost', path_style='auto')
	with pytest.raises(OSError, match='invalid S3 endpoint'):
		cloud._prepare_request('s3://bkt/f.gds')
	with pytest.raises(TypeError, match='endpoint'):
		cloud.config_s3(endpoint=1)
	with pytest.raises(ValueError, match='path_style'):
		cloud.config_s3(path_style='yes')
	# the signature covers host:port and the path-style URI
	cfg(endpoint='http://minio.local:9000', path_style='auto')
	r = cloud._prepare_request('s3://bkt/dir/f.gds', range='bytes=0-9',
		time=T2013)
	canonical = '\n'.join(['GET', '/bkt/dir/f.gds', '',
		'host:minio.local:9000', 'range:bytes=0-9',
		'x-amz-content-sha256:' + EMPTY_SHA256, 'x-amz-date:20130524T000000Z',
		'', 'host;range;x-amz-content-sha256;x-amz-date', EMPTY_SHA256])
	sts = '\n'.join(['AWS4-HMAC-SHA256', '20130524T000000Z',
		'20130524/us-east-1/s3/aws4_request', sha(canonical)])
	assert header(r, 'Authorization').split('Signature=')[1] == \
		sigv4('S', '20130524', 'us-east-1', sts)


def test_s3_endpoint_from_env_and_url(monkeypatch):
	monkeypatch.setenv('AWS_ENDPOINT_URL', 'https://env.example.org')
	monkeypatch.setenv('GDSCLOUD_S3_PATH_STYLE', 'false')
	c = cloud._get_s3_credentials('s3://b/k')
	assert c['endpoint'] == 'https://env.example.org' and c['path_style'] is False
	monkeypatch.setenv('AWS_ENDPOINT_URL_S3', 'https://s3env.example.org')
	assert cloud._get_s3_credentials('s3://b/k')['endpoint'] == \
		'https://s3env.example.org'
	cloud.config_s3(endpoint='https://global.example.org', path_style=True)
	c = cloud._get_s3_credentials('s3://b/k')
	assert c['endpoint'] == 'https://global.example.org' and c['path_style'] is True
	cloud.config_s3(endpoint='https://r2.example.org', url='s3://r2bucket/')
	assert cloud._get_s3_credentials('s3://r2bucket/k')['endpoint'] == \
		'https://r2.example.org'
	assert cloud._get_s3_credentials('s3://other/k')['endpoint'] == \
		'https://global.example.org'
	# 'auto' unsets the session value, back to the environment variable
	cloud.config_s3(path_style='auto')
	assert cloud._get_s3_credentials('s3://b/k')['path_style'] is False


def test_azure_bearer_suffix_and_endpoint():
	def cfg(**kw):
		cloud.config_azure(account_name='acct', account_key='', sas_token='',
			access_token='', endpoint_suffix='', endpoint='')
		cloud.config_azure(**kw)
	cfg(access_token='eyJ.tok')
	r = cloud._prepare_request('az://cont/b.gds')
	assert r['url'] == 'https://acct.blob.core.windows.net/cont/b.gds'
	assert set(r['headers']) == {'Authorization: Bearer eyJ.tok',
		'x-ms-version: 2020-10-02'}
	# a SAS token takes precedence over the bearer token
	cfg(access_token='eyJ.tok', sas_token='sv=1&sig=s')
	r = cloud._prepare_request('az://cont/b.gds')
	assert r['url'] == 'https://acct.blob.core.windows.net/cont/b.gds?sv=1&sig=s'
	assert r['headers'] == []
	cfg(endpoint_suffix='blob.core.chinacloudapi.cn')
	assert cloud._prepare_request('az://cont/b.gds')['url'] == \
		'https://acct.blob.core.chinacloudapi.cn/cont/b.gds'
	cfg(endpoint='http://127.0.0.1:10000/devstoreaccount1/')
	assert cloud._prepare_request('az://cont/b.gds')['url'] == \
		'http://127.0.0.1:10000/devstoreaccount1/cont/b.gds'
	cfg(endpoint='https:///nohost')
	with pytest.raises(OSError, match='invalid Azure endpoint'):
		cloud._prepare_request('az://cont/b.gds')
	# Shared Key against Azurite: the account appears twice in the
	# canonicalized resource
	cloud.config_azure(account_name='devstoreaccount1', account_key=AZURITE_KEY,
		sas_token='', access_token='',
		endpoint='http://127.0.0.1:10000/devstoreaccount1')
	r = cloud._prepare_request('az://cont/blob.gds', range='bytes=0-9',
		time=T2013)
	sts = ('GET\n\n\n\n\n\n\n\n\n\n\nbytes=0-9\n'
		'x-ms-date:Fri, 24 May 2013 00:00:00 GMT\nx-ms-version:2020-10-02\n'
		'/devstoreaccount1/devstoreaccount1/cont/blob.gds')
	sig = base64.b64encode(hmac_sha256(base64.b64decode(AZURITE_KEY), sts))
	assert header(r, 'Authorization') == \
		'Authorization: SharedKey devstoreaccount1:' + sig.decode()


def test_azure_env_and_connection_string(monkeypatch):
	monkeypatch.setenv('AZURE_STORAGE_ACCESS_TOKEN', 'envtok')
	monkeypatch.setenv('AZURE_STORAGE_ENDPOINT_SUFFIX',
		'blob.core.usgovcloudapi.net')
	monkeypatch.setenv('AZURE_STORAGE_SERVICE_ENDPOINT', 'https://svc.example.org')
	c = cloud._get_azure_credentials('az://c/b')
	assert c['access_token'] == 'envtok'
	assert c['endpoint_suffix'] == 'blob.core.usgovcloudapi.net'
	assert c['endpoint'] == 'https://svc.example.org'
	for v in ('AZURE_STORAGE_ACCESS_TOKEN', 'AZURE_STORAGE_ENDPOINT_SUFFIX',
			'AZURE_STORAGE_SERVICE_ENDPOINT'):
		monkeypatch.delenv(v)
	# the connection string as the last resort
	monkeypatch.setenv('AZURE_STORAGE_CONNECTION_STRING',
		'DefaultEndpointsProtocol=https;AccountName=csacct;'
		'AccountKey=Y3NrZXk=;EndpointSuffix=core.chinacloudapi.cn')
	c = cloud._get_azure_credentials('az://c/b')
	assert (c['account_name'], c['account_key'], c['endpoint_suffix'],
		c['endpoint']) == ('csacct', 'Y3NrZXk=', 'blob.core.chinacloudapi.cn', '')
	monkeypatch.setenv('AZURE_STORAGE_CONNECTION_STRING',
		'UseDevelopmentStorage=true')
	c = cloud._get_azure_credentials('az://c/b')
	assert c['account_name'] == 'devstoreaccount1'
	assert c['endpoint'] == 'http://127.0.0.1:10000/devstoreaccount1'
	assert c['account_key'].startswith('Eby8vdM02x')
	# explicit settings win over the connection string
	cloud.config_azure(account_name='explicit')
	assert cloud._get_azure_credentials('az://c/b')['account_name'] == 'explicit'
	assert cloud._azure_connection_string('')['account_name'] == ''


def test_azure_invalid_keys():
	cloud.options(connect_timeout=2, timeout=2, max_retries=0)
	cloud.config_azure(account_name='acct', account_key='not base64!!',
		sas_token='')
	with pytest.raises(OSError, match='base64'):
		cloud.open('az://container/file.gds')
	# 1000 valid base64 characters decode to more than any real key
	cloud.config_azure(account_key='QUJD' * 250)
	with pytest.raises(OSError, match='base64'):
		cloud.open('az://container/file.gds')


# ===========================================================================
# Reads from a local HTTP server

def start_server(*args):
	p = subprocess.Popen([sys.executable, os.path.join(HERE, 'cloud_server.py')]
		+ list(args), stdout=subprocess.PIPE, text=True)
	port = int(p.stdout.readline())
	return p, 'http://127.0.0.1:%d' % port


@pytest.fixture(scope='module')
def server():
	d = tempfile.mkdtemp()
	fn = os.path.join(d, 'test.gds')
	rng = np.random.default_rng(1000)
	f = pycorearray.gdsfile(); f.create(fn)
	r = f.root()
	r.add('sample.id', ['S%d' % i for i in range(1, 51)])
	r.add('position', rng.integers(1, 10**8, 5000).astype(np.int32),
		compress='LZMA_RA', closezip=True)
	# about 6 MB of incompressible data, so that reads cross 1 MB cache blocks
	r.add('geno', rng.integers(1, 201, (3000, 500)).astype(np.int32))
	r.add('text', ['alpha', 'beta', 'gamma'] * 300, compress='ZIP_RA',
		closezip=True)
	f.close()
	f = pycorearray.gdsfile(); f.open(fn)
	ref = { nm: f.root().index(nm).read()
		for nm in ('sample.id', 'position', 'geno', 'text') }
	f.close()
	p, url = start_server(d)
	yield { 'url': url, 'ref': ref, 'size': os.path.getsize(fn) }
	p.kill()
	p.wait()


def read_all(f, ref, names=None):
	for nm in names or ref:
		v = f.root().index(nm).read()
		assert np.array_equal(v, ref[nm]), nm


def test_range_reads(server):
	u = server['url'] + '/data/test.gds'
	f = pycorearray.gdsfile(); f.open(u)
	try:
		assert f.filename == u and f.filesize() == server['size']
		read_all(f, server['ref'])
		# random access into the big matrix crosses cache blocks
		g = f.root().index('geno').read(start=[1000, 100], count=[20, 10])
		assert np.array_equal(g, server['ref']['geno'][1000:1020, 100:110])
		lst = cloud.list_streams()
		assert [ s['url'] for s in lst ] == [u]
		assert lst[0]['file_size'] == server['size']
		assert cloud.cache_info(verbose=False)['num_streams'] == 1
	finally:
		f.close()
	assert cloud.list_streams() == []
	# pycorearray.cloud.open() is the same
	f = cloud.open(u)
	read_all(f, server['ref'], ['text'])
	f.close()


def test_server_ignoring_range_is_rejected(server):
	with pytest.raises(Exception, match='does not support HTTP Range'):
		cloud.open(server['url'] + '/norange/test.gds')
	with pytest.raises(Exception, match='instead of the requested offset'):
		cloud.open(server['url'] + '/badrange/test.gds')


def test_html_page_is_reported(server):
	before = cloud.cache_info(verbose=False)['retries']
	with pytest.raises(OSError) as e:
		cloud.open(server['url'] + '/login/test.gds')
	assert 'returned an HTML page' in str(e.value)
	assert "after a redirect to '%s/signin/page'" % server['url'] in str(e.value)
	with pytest.raises(OSError) as e:
		cloud.open(server['url'] + '/html/test.gds')
	assert 'returned an HTML page' in str(e.value)
	assert 'redirect' not in str(e.value)
	# an aborted transfer is not a transient failure
	assert cloud.cache_info(verbose=False)['retries'] == before


def test_transient_503_retried(server):
	cloud.options(max_retries=3)
	before = cloud.cache_info(verbose=False)['retries']
	f = cloud.open(server['url'] + '/flaky/test.gds')
	read_all(f, server['ref'], ['geno'])
	f.close()
	assert cloud.cache_info(verbose=False)['retries'] > before
	# without retries, the flaky endpoint fails on two out of three requests
	cloud.options(max_retries=0)
	errs = []
	for i in range(3):
		try:
			cloud.open(server['url'] + '/flaky/test.gds').close()
			errs.append('ok')
		except OSError as e:
			errs.append(str(e))
	assert any('HTTP 503' in e for e in errs)
	assert any('SlowDown' in e for e in errs)


def test_s3_compatible_endpoint(server):
	# with a custom endpoint and path-style addressing, s3://data/test.gds
	# maps to <endpoint>/data/test.gds through the S3 backend
	cloud.config_s3(aws_access_key_id='AKIAEXAMPLE',
		aws_secret_access_key='secret', endpoint=server['url'])
	f = cloud.open('s3://data/test.gds')
	read_all(f, server['ref'], ['geno'])
	assert cloud.list_streams()[0]['url'] == 's3://data/test.gds'
	f.close()
	# anonymous
	cloud.config_s3(aws_access_key_id='', aws_secret_access_key='')
	f = cloud.open('s3://data/test.gds')
	read_all(f, server['ref'], ['text'])
	f.close()
	with pytest.raises(OSError, match='S3: HTTP 404'):
		cloud.open('s3://data/missing.gds')


def test_azure_custom_endpoint(server):
	cloud.config_azure(account_name='devstoreaccount1', account_key=AZURITE_KEY,
		sas_token='', access_token='', endpoint=server['url'])
	f = cloud.open('az://data/test.gds')
	read_all(f, server['ref'], ['position'])
	f.close()
	cloud.config_azure(account_key='', access_token='tok')
	f = cloud.open('az://data/test.gds')
	read_all(f, server['ref'], ['text'])
	f.close()
	with pytest.raises(OSError, match='Azure: HTTP 404'):
		cloud.open('az://data/missing.gds')


def test_bearer_token_function(server):
	u = server['url'] + '/auth/test.gds'
	with pytest.raises(OSError, match='HTTP 401'):
		cloud.open(u)
	calls = []
	def token():
		calls.append(1)
		return 'secret-token'
	cloud.config_http(bearer_token=token)
	f = cloud.open(u)
	read_all(f, server['ref'], ['sample.id'])
	f.close()
	cloud.open(u).close()
	assert len(calls) == 2   # once per open, not per request
	cloud.config_http(bearer_token=lambda: 'expired')
	with pytest.raises(OSError, match='unauthorized'):
		cloud.open(u)


def test_aws_credentials_function_called_once(server):
	calls = []
	def creds():
		calls.append(1)
		return {'aws_access_key_id': 'AKIAEXAMPLE',
			'aws_secret_access_key': 's', 'session_token': 'tok'}
	cloud.config_s3(endpoint=server['url'], credentials=creds)
	f = cloud.open('s3://data/test.gds')
	read_all(f, server['ref'], ['sample.id', 'geno'])
	f.close()
	assert len(calls) == 1


def _read_in_child(url):
	f = pycorearray.gdsfile(); f.open(url)
	v = f.root().index('sample.id').read()
	f.close()
	return list(v)


def test_spawned_processes_get_the_settings(server):
	# the settings of the session are installed in spawned processes
	cloud.config_http(bearer_token='secret-token')
	ctx = multiprocessing.get_context('spawn')
	with ctx.Pool(2, initializer=cloud.import_credentials,
			initargs=(cloud.export_credentials(),)) as pool:
		res = pool.map(_read_in_child, [server['url'] + '/auth/test.gds'] * 2)
	assert res == [ list(server['ref']['sample.id']) ] * 2


# ===========================================================================
# Timeouts and user interrupts, with a server that never answers

def test_low_speed_timeout():
	p, url = start_server('--silent')
	try:
		cloud.options(timeout=1, max_retries=0)
		t0 = time.time()
		with pytest.raises(OSError, match='curl error'):
			cloud.open(url + '/file.gds')
		assert time.time() - t0 < 15
	finally:
		p.kill()
		p.wait()


@pytest.mark.skipif(sys.platform == 'win32', reason='SIGINT')
def test_keyboard_interrupt():
	p, url = start_server('--silent')
	code = textwrap.dedent('''
		import sys, pycorearray
		pycorearray.cloud.options(timeout=60)
		print('started', flush=True)
		try:
			pycorearray.cloud.open(sys.argv[1])
		except KeyboardInterrupt:
			print('KeyboardInterrupt', flush=True)
	''')
	# 'python -c' puts the working directory first on sys.path, so run the
	# child next to the pycorearray being tested (not the source tree without
	# the built extension)
	child = subprocess.Popen([sys.executable, '-c', code, url + '/file.gds'],
		stdout=subprocess.PIPE, text=True,
		cwd=os.path.dirname(os.path.dirname(pycorearray.__file__)))
	try:
		assert child.stdout.readline().strip() == 'started'
		time.sleep(2)   # let the child get into the transfer
		child.send_signal(signal.SIGINT)
		out, _ = child.communicate(timeout=15)
		assert out.strip() == 'KeyboardInterrupt'
	finally:
		child.kill()
		p.kill()
		p.wait()
