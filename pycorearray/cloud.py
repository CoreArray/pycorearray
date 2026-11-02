"""Read-only access to GDS files on cloud storage

GDS files can be opened from HTTP/HTTPS URLs, Amazon S3 (``s3://bucket/key``,
also S3-compatible services such as MinIO, Ceph, Cloudflare R2, Wasabi or
Backblaze B2 via a custom endpoint), Google Cloud Storage (``gs://bucket/key``)
and Azure Blob Storage (``az://container/blob``), without downloading the
whole file first: only the blocks that are actually read are fetched, using
HTTP Range requests via libcurl, with an in-memory least-recently-used block
cache.

`gdsfile.open()` opens such a URL directly, e.g., a public file on Amazon S3
(the examples assume ``import pycorearray as gds``):

>>> f = gds.gdsfile()
>>> f.open('s3://gds-stat/download/hapmap/hapmap_r23a.gds')   # doctest: +SKIP
>>> f.root().index('position').read(start=[1000], count=[5])  # doctest: +SKIP
array([2409958, 2410723, 2410773, 2412474, 2413426], dtype=int32)
>>> f.close()                                                 # doctest: +SKIP

Credentials are resolved from, in this order, the entry configured for the
longest matching URL prefix (``config_*(..., url=...)``), the settings of the
session (``config_*()``) and the usual environment variables of each service.
A token can be given as a function returning it, which is called every time a
file is opened, so that expiring tokens are refreshed.

This module is the Python interface of the gdscloud R package, and uses the
same environment variables.
"""

import copy
import datetime
import os
import re
import sys

import pycorearray.ccall as cc


_SCHEMES = {
	'http':  'HTTP',
	'https': 'HTTPS',
	's3':    'Amazon S3 (and S3-compatible services)',
	'gs':    'Google Cloud Storage',
	'az':    'Azure Blob Storage',
}

# the settings of the session (config_*()), None if unset
_settings = {}
# URL-specific settings: URL prefix -> dict of the settings and 'scheme'
_url_settings = {}

# the key of the local Azure storage emulator (Azurite), which is public
_AZURITE_KEY = ('Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6'
	'tq/K1SZFPTOtr/KBHBeksoGMGw==')


# ===========================================================================
# Opening

def available():
	"""Whether pycorearray was built with cloud access (libcurl)

	Examples
	--------
	>>> gds.cloud.available()
	True
	"""
	return cc.cloud_available()


def schemes():
	"""The supported URL schemes

	Returns
	-------
	dict : URL scheme -> description

	Examples
	--------
	>>> gds.cloud.schemes()['gs']
	'Google Cloud Storage'
	>>> sorted(gds.cloud.schemes())
	['az', 'gs', 'http', 'https', 's3']
	"""
	return dict(_SCHEMES)


def _scheme(url):
	i = url.find('://')
	return url[:i] if i >= 0 else url


def _is_url(filename):
	# a URL scheme (RFC 3986) followed by '://'
	return re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', filename) is not None


def _open_into(f, url):
	"""Open 'url' into the gdsfile object 'f'"""
	scheme = _scheme(url)
	if scheme not in _SCHEMES:
		raise ValueError("Unsupported URL scheme: '%s'. Supported schemes: %s" %
			(scheme, ', '.join(s + '://' for s in _SCHEMES)))
	if not cc.cloud_available():
		raise RuntimeError('pycorearray was built without cloud access: '
			'libcurl (and, except on macOS, OpenSSL) was not found when it '
			'was compiled')
	_apply_options()
	cache_mb = float(_options['cache_size'])
	if scheme in ('http', 'https'):
		cred = _get_http_credentials(url)
		auth = 'Bearer ' + cred['bearer_token'] if cred['bearer_token'] else ''
		fid = cc.cloud_open_http(url, auth, cache_mb)
	elif scheme == 's3':
		c = _get_s3_credentials(url)
		ps = {True: 1, False: 0}.get(c['path_style'], -1)
		fid = cc.cloud_open_s3(url, c['access_key'], c['secret_key'],
			c['region'], c['session_token'], c['endpoint'], ps, cache_mb)
	elif scheme == 'gs':
		c = _get_gcs_credentials(url)
		fid = cc.cloud_open_gcs(url, c['access_token'], cache_mb)
	else:
		c = _get_azure_credentials(url)
		fid = cc.cloud_open_azure(url, c['account_name'], c['account_key'],
			c['sas_token'], c['access_token'], c['endpoint_suffix'],
			c['endpoint'], cache_mb)
	f.fileid = fid
	f.filename = url
	return f


def open(url):
	"""Open a GDS file on cloud storage

	The same as `gdsfile().open(url)`; the file is read-only.

	Parameters
	----------
	url : str
		a URL starting with 'http://', 'https://', 's3://', 'gs://' or 'az://'

	Returns
	-------
	gdsfile

	Examples
	--------
	>>> url = 's3://gds-stat/download/hapmap/hapmap_r23a.gds'   # a public file
	>>> f = gds.cloud.open(url)                                  # doctest: +SKIP
	>>> f.root().index('position').read(start=[1000], count=[5]) # doctest: +SKIP
	array([2409958, 2410723, 2410773, 2412474, 2413426], dtype=int32)
	>>> f.close()                                                # doctest: +SKIP
	"""
	if not isinstance(url, str):
		raise TypeError("'url' should be a string.")
	from pycorearray import gdsfile
	return _open_into(gdsfile(), url)


# ===========================================================================
# Options

def _env_num(name, default, minimum=1):
	try:
		v = float(os.environ.get('GDSCLOUD_' + name.upper(), ''))
	except ValueError:
		return default
	return v if v >= minimum else default


# the CA bundles of Linux distributions, in the order Go's crypto/x509 looks
# for them
_CA_BUNDLES = (
	'/etc/ssl/certs/ca-certificates.crt',                # Debian, Ubuntu, Arch
	'/etc/pki/tls/certs/ca-bundle.crt',                  # Fedora, RHEL
	'/etc/ssl/ca-bundle.pem',                            # openSUSE
	'/etc/pki/tls/cacert.pem',                           # OpenELEC
	'/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem', # CentOS, RHEL 7
	'/etc/ssl/cert.pem',                                 # Alpine
)

def _default_ca_bundle():
	# the environment variables of the curl command line tool
	for name in ('CURL_CA_BUNDLE', 'SSL_CERT_FILE'):
		v = os.environ.get(name, '')
		if v:
			return v
	# a libcurl bundled with a Linux wheel has no CA bundle, or the path of the
	# build system, which may not exist here
	if sys.platform.startswith('linux'):
		fn = cc.cloud_curl_ca_bundle()
		if not (fn and os.path.isfile(fn)):
			for fn in _CA_BUNDLES:
				if os.path.isfile(fn):
					return fn
	return ''   # the libcurl default


def _init_options():
	return {
		'connect_timeout': _env_num('connect_timeout', 30),
		'timeout': _env_num('timeout', 60),
		'max_retries': int(_env_num('max_retries', 3, minimum=0)),
		'cache_size': _env_num('cache_size_mb', 64),
		'ca_bundle': _default_ca_bundle(),
	}

# the defaults can be set by the environment variables GDSCLOUD_CONNECT_TIMEOUT,
# GDSCLOUD_TIMEOUT, GDSCLOUD_MAX_RETRIES and GDSCLOUD_CACHE_SIZE_MB
_options = _init_options()


def _check_num(v, name, minimum):
	if isinstance(v, bool) or not isinstance(v, (int, float)) or \
			not v >= minimum:
		raise ValueError("'%s' must be a single number >= %g." % (name, minimum))
	return float(v)


def options(connect_timeout=None, timeout=None, max_retries=None,
		cache_size=None, ca_bundle=None):
	"""Get or set the options of cloud access

	Parameters
	----------
	connect_timeout : float
		the timeout (in seconds) of establishing a connection
	timeout : float
		abort a transfer when it is slower than 1 byte per second for this
		number of seconds
	max_retries : int
		the maximum number of retries of a request after a transient failure
		(connection reset, timeout, HTTP 408/429/5xx), with exponential back-off
	cache_size : float
		the block cache size (in MB) of a newly opened file
	ca_bundle : str
		the CA certificate bundle (a PEM file) to verify HTTPS servers, ''
		for the default of libcurl

	None keeps the current value. The defaults (30, 60, 3, 64) can be set by
	the environment variables GDSCLOUD_CONNECT_TIMEOUT, GDSCLOUD_TIMEOUT,
	GDSCLOUD_MAX_RETRIES and GDSCLOUD_CACHE_SIZE_MB. The default CA bundle is
	the file in CURL_CA_BUNDLE or SSL_CERT_FILE; otherwise, on Linux, if the
	CA bundle built into libcurl does not exist (as with the libcurl bundled
	with a wheel), the CA bundle of the Linux distribution.

	Returns
	-------
	dict : the options

	Examples
	--------
	>>> opt = gds.cloud.options(timeout=120, max_retries=5)
	>>> opt['timeout'], opt['max_retries']
	(120.0, 5)
	"""
	if connect_timeout is not None:
		_options['connect_timeout'] = _check_num(connect_timeout,
			'connect_timeout', 1)
	if timeout is not None:
		_options['timeout'] = _check_num(timeout, 'timeout', 1)
	if max_retries is not None:
		_options['max_retries'] = int(_check_num(max_retries, 'max_retries', 0))
	if cache_size is not None:
		_options['cache_size'] = _check_num(cache_size, 'cache_size', 1)
	if ca_bundle is not None:
		if not isinstance(ca_bundle, str):
			raise TypeError("'ca_bundle' should be a string.")
		if ca_bundle and not os.path.isfile(ca_bundle):
			raise ValueError("'ca_bundle': no such file '%s'." % ca_bundle)
		_options['ca_bundle'] = ca_bundle
	return dict(_options)


def _apply_options():
	cc.cloud_set_options(float(_options['connect_timeout']),
		float(_options['timeout']), float(_options['max_retries']),
		_options['ca_bundle'])


# ===========================================================================
# Cache

def cache_size(size_mb=64):
	"""Set the block cache size (in MB) of newly opened files"""
	options(cache_size=size_mb)
	return size_mb


def cache_clear():
	"""Clear the block caches of all open cloud files"""
	cc.cloud_cache_clear()


def cache_info(verbose=True):
	"""Cache settings and statistics

	Parameters
	----------
	verbose : bool
		if True, print them

	Returns
	-------
	dict : the number of open cloud files ('num_streams'), the cache hits and
	misses, and the number of retried requests
	"""
	info = cc.cloud_cache_info()
	if verbose:
		print('pycorearray cloud cache settings:')
		print('  Default cache size: %g MB' % _options['cache_size'])
		print('  Block size: 1 MB')
		print('  Open cloud streams: %d' % info['num_streams'])
		print('  Global cache hits: %d' % info['hits'])
		print('  Global cache misses: %d' % info['misses'])
		print('  Retried requests: %d' % info['retries'])
	return info


def list_streams():
	"""The open cloud files

	Returns
	-------
	list : a dict for each file, with 'url', 'file_size', 'cache_blocks',
	'cache_hits' and 'cache_misses'
	"""
	return cc.cloud_list_streams()


# ===========================================================================
# Credentials

def _resolve(v):
	# a string, or a function returning one ("token provider"); None if unset
	if callable(v):
		v = v()
	if v is None:
		return None
	if not isinstance(v, str):
		raise TypeError('A credential must be a string, or a function '
			'returning one.')
	return v


def _first_nonempty(*vals):
	for v in vals:
		v = _resolve(v)
		if v:
			return v
	return ''


def _first_set(*vals):
	# the first True or False, None if none
	for v in vals:
		if v is not None:
			return bool(v)
	return None


def _env_logical(name):
	v = os.environ.get(name, '').lower()
	if v in ('true', 't', '1', 'yes'):
		return True
	if v in ('false', 'f', '0', 'no'):
		return False
	return None


def _check_token(v, name):
	if v is not None and not callable(v) and not isinstance(v, str):
		raise TypeError("'%s' must be None, a string, or a function returning "
			"a string." % name)


def _check_str(v, name):
	if v is not None and not isinstance(v, str):
		raise TypeError("'%s' must be None or a string." % name)


def _url_prefix(url, scheme):
	# normalize a URL prefix: strip a trailing wildcard, end with '/'
	if not isinstance(url, str) or not url:
		raise ValueError("'url' must be a non-empty string.")
	s = _scheme(url)
	if s != scheme:
		raise ValueError("URL scheme '%s://' does not match expected '%s://'." %
			(s, scheme))
	url = url.rstrip('*')
	return url if url.endswith('/') else url + '/'


def _set(url, scheme, fields):
	# the settings of the session, or the entry of a URL prefix
	if url is None:
		for k, v in fields.items():
			if v is not None:
				_settings[k] = v
		return
	key = _url_prefix(url, scheme)
	fields = { k: v for k, v in fields.items() if v is not None }
	if fields:
		fields['scheme'] = scheme
		_url_settings[key] = fields
	else:
		_url_settings.pop(key, None)


def _match(url, scheme):
	# the entry with the longest matching URL prefix within 'scheme', or {}
	keys = [ k for k, e in _url_settings.items()
		if e.get('scheme') == scheme and url.startswith(k) ]
	return _url_settings[max(keys, key=len)] if keys else {}


def config_http(bearer_token=None, url=None):
	"""Configure HTTP/HTTPS access

	Parameters
	----------
	bearer_token : str or function, optional
		a Bearer token sent in the Authorization header (environment variable
		GDSCLOUD_HTTP_TOKEN), or a function returning it
	url : str, optional
		if given, the setting applies to the URLs starting with this prefix
		(only http:// and https://)

	None keeps the current value, and '' unsets it.

	Examples
	--------
	>>> gds.cloud.config_http(bearer_token='my-token',
	...     url='https://data.example.org/private/')
	"""
	_check_token(bearer_token, 'bearer_token')
	if url is not None and _scheme(url) not in ('http', 'https'):
		raise ValueError("'url' must start with 'http://' or 'https://'.")
	_set(url, _scheme(url) if url else None,
		{ 'http_bearer_token': bearer_token })


def config_s3(aws_access_key_id=None, aws_secret_access_key=None, region=None,
		session_token=None, credentials=None, endpoint=None, path_style=None,
		url=None):
	"""Configure Amazon S3 access

	Parameters
	----------
	aws_access_key_id, aws_secret_access_key : str, optional
		the access key (environment variables AWS_ACCESS_KEY_ID and
		AWS_SECRET_ACCESS_KEY); without them, requests are anonymous
	region : str, optional
		the region (AWS_DEFAULT_REGION, default 'us-east-1')
	session_token : str, optional
		the session token of temporary credentials (AWS_SESSION_TOKEN)
	credentials : function, optional
		a function returning a dict with 'aws_access_key_id',
		'aws_secret_access_key' and optionally 'session_token' (or
		'aws_session_token') and 'region', or None; called every time a file
		is opened, it supplies the keys as one set and takes priority over the
		static keys
	endpoint : str, optional
		the endpoint of an S3-compatible service, e.g.,
		'https://minio.example.org:9000' (AWS_ENDPOINT_URL_S3 or
		AWS_ENDPOINT_URL)
	path_style : bool or 'auto', optional
		True for path-style URLs (https://endpoint/bucket/key), False for
		virtual-hosted style (https://bucket.endpoint/key), 'auto' to decide
		automatically (GDSCLOUD_S3_PATH_STYLE)
	url : str, optional
		if given, the settings apply to the URLs starting with this prefix,
		e.g., 's3://my-bucket/'

	None keeps the current value, and '' unsets a string.

	Examples
	--------
	A public bucket needs no settings. The keys of a private bucket, only
	used for the URLs under 's3://my-bucket/':

	>>> gds.cloud.config_s3(aws_access_key_id='AKIA...',
	...     aws_secret_access_key='...', region='us-west-2',
	...     url='s3://my-bucket/')

	A bucket on an S3-compatible service, e.g., MinIO:

	>>> gds.cloud.config_s3(endpoint='https://minio.example.org:9000',
	...     path_style=True, url='s3://data/')

	All settings None removes the entry of a URL prefix:

	>>> gds.cloud.config_s3(url='s3://data/')
	"""
	for v, nm in ((aws_access_key_id, 'aws_access_key_id'),
			(aws_secret_access_key, 'aws_secret_access_key'),
			(region, 'region'), (session_token, 'session_token'),
			(endpoint, 'endpoint')):
		_check_str(v, nm)
	if credentials is not None and not callable(credentials):
		raise TypeError("'credentials' must be None or a function returning a "
			"dict with 'aws_access_key_id', 'aws_secret_access_key' and "
			"optionally 'session_token'.")
	if path_style is not None and path_style not in (True, False, 'auto'):
		raise ValueError("'path_style' must be True, False or 'auto'.")
	if url is not None:
		_url_prefix(url, 's3')
	fields = {
		'aws_access_key_id': aws_access_key_id,
		'aws_secret_access_key': aws_secret_access_key,
		'aws_region': region,
		'aws_session_token': session_token,
		'aws_credentials': credentials,
		'aws_endpoint': endpoint,
	}
	if path_style is not None:
		# 'auto' unsets the value
		ps = None if path_style == 'auto' else path_style
		if url is None:
			_settings['aws_path_style'] = ps
		else:
			fields['aws_path_style'] = ps
	_set(url, 's3', fields)


def config_gcs(access_token=None, url=None):
	"""Configure Google Cloud Storage access

	Parameters
	----------
	access_token : str or function, optional
		an OAuth2 access token (environment variable GCS_ACCESS_TOKEN), or a
		function returning it, e.g., running 'gcloud auth print-access-token';
		without it, requests are anonymous
	url : str, optional
		if given, the setting applies to the URLs starting with this prefix,
		e.g., 'gs://my-bucket/'

	None keeps the current value, and '' unsets it.

	Examples
	--------
	A token function, called every time a file is opened, so that the
	token is refreshed:

	>>> import subprocess
	>>> def gcloud_token():
	...     return subprocess.run(['gcloud', 'auth', 'print-access-token'],
	...         capture_output=True, text=True, check=True).stdout.strip()
	>>> gds.cloud.config_gcs(access_token=gcloud_token)
	"""
	_check_token(access_token, 'access_token')
	if url is not None:
		_url_prefix(url, 'gs')
	_set(url, 'gs', { 'gcs_access_token': access_token })


def config_azure(account_name=None, account_key=None, sas_token=None,
		access_token=None, endpoint_suffix=None, endpoint=None, url=None):
	"""Configure Azure Blob Storage access

	Parameters
	----------
	account_name : str, optional
		the storage account (environment variable AZURE_STORAGE_ACCOUNT)
	account_key : str, optional
		the Shared Key of the account (AZURE_STORAGE_KEY)
	sas_token : str, optional
		a shared access signature (AZURE_STORAGE_SAS_TOKEN), which takes
		priority over the account key and the access token
	access_token : str or function, optional
		an OAuth2 (Entra ID) access token (AZURE_STORAGE_ACCESS_TOKEN), or a
		function returning it
	endpoint_suffix : str, optional
		the endpoint suffix of a sovereign cloud, e.g.,
		'blob.core.chinacloudapi.cn' (AZURE_STORAGE_ENDPOINT_SUFFIX)
	endpoint : str, optional
		a custom endpoint, e.g., the local emulator
		'http://127.0.0.1:10000/devstoreaccount1'
		(AZURE_STORAGE_SERVICE_ENDPOINT)
	url : str, optional
		if given, the settings apply to the URLs starting with this prefix,
		e.g., 'az://my-container/'

	The environment variable AZURE_STORAGE_CONNECTION_STRING is used as the
	last resort. None keeps the current value, and '' unsets a string.
	"""
	for v, nm in ((account_name, 'account_name'), (account_key, 'account_key'),
			(sas_token, 'sas_token'), (endpoint_suffix, 'endpoint_suffix'),
			(endpoint, 'endpoint')):
		_check_str(v, nm)
	_check_token(access_token, 'access_token')
	if url is not None:
		_url_prefix(url, 'az')
	_set(url, 'az', {
		'azure_account_name': account_name,
		'azure_account_key': account_key,
		'azure_sas_token': sas_token,
		'azure_access_token': access_token,
		'azure_endpoint_suffix': endpoint_suffix,
		'azure_endpoint': endpoint,
	})


def _get_http_credentials(url):
	m = _match(url, _scheme(url))
	return {
		'bearer_token': _first_nonempty(m.get('http_bearer_token'),
			_settings.get('http_bearer_token'),
			os.environ.get('GDSCLOUD_HTTP_TOKEN', '')),
	}


def _call_aws_provider(f, what):
	# None if there is no provider, or it returns None / no access key
	if f is None:
		return None
	v = f()
	if v is None:
		return None
	if not isinstance(v, dict):
		raise TypeError('The %s AWS \'credentials\' function must return a '
			'dict (aws_access_key_id, aws_secret_access_key, session_token) '
			'or None.' % what)
	def get(nm, alt=None):
		x = v.get(nm)
		if x is None and alt is not None:
			x = v.get(alt)
		if x is None:
			return ''
		if not isinstance(x, str):
			raise TypeError('The %s AWS \'credentials\' function returned a '
				'non-string value for \'%s\'.' % (what, nm))
		return x
	key = get('aws_access_key_id')
	if not key:
		return None
	secret = get('aws_secret_access_key')
	if not secret:
		raise ValueError('The %s AWS \'credentials\' function returned an '
			'access key without \'aws_secret_access_key\'.' % what)
	return { 'aws_access_key_id': key, 'aws_secret_access_key': secret,
		'session_token': get('session_token', 'aws_session_token'),
		'region': get('region') }


def _get_s3_credentials(url):
	m = _match(url, 's3')
	# a provider function (URL-specific first) supplies the keys as one set,
	# replacing the static chain, since mixing e.g. a fresh key with a stale
	# token would be wrong
	prov = _call_aws_provider(m.get('aws_credentials'), 'URL-specific')
	if prov is None:
		prov = _call_aws_provider(_settings.get('aws_credentials'), 'global')
	env = os.environ.get
	if prov is not None:
		access_key = prov['aws_access_key_id']
		secret_key = prov['aws_secret_access_key']
		session_token = prov['session_token']
	else:
		access_key = _first_nonempty(m.get('aws_access_key_id'),
			_settings.get('aws_access_key_id'), env('AWS_ACCESS_KEY_ID', ''))
		secret_key = _first_nonempty(m.get('aws_secret_access_key'),
			_settings.get('aws_secret_access_key'),
			env('AWS_SECRET_ACCESS_KEY', ''))
		session_token = _first_nonempty(m.get('aws_session_token'),
			_settings.get('aws_session_token'), env('AWS_SESSION_TOKEN', ''))
	return {
		'access_key': access_key,
		'secret_key': secret_key,
		'region': _first_nonempty(prov['region'] if prov else None,
			m.get('aws_region'), _settings.get('aws_region'),
			env('AWS_DEFAULT_REGION', ''), 'us-east-1'),
		'session_token': session_token,
		# S3-compatible services: a custom endpoint and the addressing style
		'endpoint': _first_nonempty(m.get('aws_endpoint'),
			_settings.get('aws_endpoint'), env('AWS_ENDPOINT_URL_S3', ''),
			env('AWS_ENDPOINT_URL', '')),
		'path_style': _first_set(m.get('aws_path_style'),
			_settings.get('aws_path_style'),
			_env_logical('GDSCLOUD_S3_PATH_STYLE')),
	}


def _get_gcs_credentials(url):
	m = _match(url, 'gs')
	return {
		'access_token': _first_nonempty(m.get('gcs_access_token'),
			_settings.get('gcs_access_token'),
			os.environ.get('GCS_ACCESS_TOKEN', '')),
	}


def _azure_connection_string(s=None):
	# parse e.g. "DefaultEndpointsProtocol=https;AccountName=..;AccountKey=..;
	# EndpointSuffix=core.windows.net" or "UseDevelopmentStorage=true"
	if s is None:
		s = os.environ.get('AZURE_STORAGE_CONNECTION_STRING', '')
	ans = { 'account_name': '', 'account_key': '', 'sas_token': '',
		'endpoint_suffix': '', 'endpoint': '' }
	if not s:
		return ans
	v = dict(p.split('=', 1) for p in s.split(';') if '=' in p)
	ans['account_name'] = v.get('AccountName', '')
	ans['account_key'] = v.get('AccountKey', '')
	ans['sas_token'] = v.get('SharedAccessSignature', '')
	ans['endpoint'] = v.get('BlobEndpoint', '')
	# "EndpointSuffix" refers to all services; the blob service is under
	# "blob." of it
	if v.get('EndpointSuffix'):
		ans['endpoint_suffix'] = 'blob.' + v['EndpointSuffix']
	# the shorthand for the local emulator (Azurite)
	if v.get('UseDevelopmentStorage', '').lower() == 'true':
		ans['account_name'] = ans['account_name'] or 'devstoreaccount1'
		ans['account_key'] = ans['account_key'] or _AZURITE_KEY
		ans['endpoint'] = ans['endpoint'] or \
			'http://127.0.0.1:10000/devstoreaccount1'
	return ans


def _get_azure_credentials(url):
	m = _match(url, 'az')
	cs = _azure_connection_string()
	env = os.environ.get
	def get(name, envvar, cs_name):
		return _first_nonempty(m.get('azure_' + name),
			_settings.get('azure_' + name), env(envvar, '') if envvar else '',
			cs.get(cs_name, '') if cs_name else '')
	return {
		'account_name': get('account_name', 'AZURE_STORAGE_ACCOUNT',
			'account_name'),
		'account_key': get('account_key', 'AZURE_STORAGE_KEY', 'account_key'),
		'sas_token': get('sas_token', 'AZURE_STORAGE_SAS_TOKEN', 'sas_token'),
		'access_token': get('access_token', 'AZURE_STORAGE_ACCESS_TOKEN',
			None),
		'endpoint_suffix': get('endpoint_suffix',
			'AZURE_STORAGE_ENDPOINT_SUFFIX', 'endpoint_suffix'),
		'endpoint': get('endpoint', 'AZURE_STORAGE_SERVICE_ENDPOINT',
			'endpoint'),
	}


def _prepare_request(url, range='bytes=0-0', time=None):
	# the request (URL and headers) that would be sent for 'url', without
	# sending it; used by the tests to check signatures
	if time is None:
		t = datetime.datetime.now(datetime.timezone.utc).timestamp()
	elif isinstance(time, datetime.datetime):
		if time.tzinfo is None:
			time = time.replace(tzinfo=datetime.timezone.utc)
		t = time.timestamp()
	else:
		t = float(time)
	scheme = _scheme(url)
	if scheme in ('http', 'https'):
		tok = _get_http_credentials(url)['bearer_token']
		params = { 'auth': 'Bearer ' + tok if tok else '' }
	elif scheme == 's3':
		params = _get_s3_credentials(url)
		params['path_style'] = { True: 'TRUE', False: 'FALSE' }.get(
			params['path_style'], '')
	elif scheme == 'gs':
		params = _get_gcs_credentials(url)
	elif scheme == 'az':
		params = _get_azure_credentials(url)
	else:
		raise ValueError("Unsupported URL scheme: '%s'" % scheme)
	u, h = cc.cloud_prepare_request(url, params, range, t)
	return { 'url': u, 'headers': h }


# ===========================================================================
# Child processes

def export_credentials():
	"""The cloud settings of this session, for child processes

	The credentials, URL-specific settings and options, to be installed by
	`import_credentials()` in a child process, which is needed when the
	processes are started by 'spawn' (the default on macOS and Windows),
	e.g., ::

		pool = multiprocessing.Pool(4,
			initializer=pycorearray.cloud.import_credentials,
			initargs=(pycorearray.cloud.export_credentials(),))

	A token function has to be picklable (a module-level function, not a
	lambda). Forked processes inherit the settings. Environment variables are
	inherited by child processes in either case.

	Returns
	-------
	dict
	"""
	return { 'settings': copy.deepcopy(_settings),
		'url_settings': copy.deepcopy(_url_settings),
		'options': dict(_options) }


def import_credentials(creds):
	"""Install the cloud settings from `export_credentials()`"""
	_settings.clear()
	_settings.update(creds.get('settings', {}))
	_url_settings.clear()
	_url_settings.update(creds.get('url_settings', {}))
	_options.update(creds.get('options', {}))
