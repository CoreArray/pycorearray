// ===========================================================
//
// PyCloud.cpp: Read-only access to GDS files on cloud storage (HTTP/HTTPS,
//     Amazon S3, Google Cloud Storage, Azure Blob Storage), the Python glue
//     of the C code in src/cloud/ (from the gdscloud R package)
//
// Copyright (C) 2026    Xiuwen Zheng
//
// pycorearray is free software: you can redistribute it and/or modify it
// under the terms of the GNU General Public License Version 3 as
// published by the Free Software Foundation.
//
// pycorearray is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public
// License along with pycorearray.
// If not, see <http://www.gnu.org/licenses/>.


#define COREARRAY_PYCOREARRAY_PACKAGE

#include <PyGDS_CPP.h>

#ifdef PYCOREARRAY_WITH_CLOUD

#include "cloud/cloud_stream.h"
#include "cloud/curl_backend.h"
#include <string>
#include <vector>
#include <algorithm>


// Providers implemented in the C files
extern "C" {

extern const CurlProvider http_provider;
extern void *http_provider_create(const char *url, const char *auth,
	char *err, size_t err_size);

extern const CurlProvider s3_provider;
extern void *s3_provider_create(const char *url, const char *access_key,
	const char *secret_key, const char *region, const char *session_token,
	const char *endpoint, int path_style, char *err, size_t err_size);

extern const CurlProvider gcs_provider;
extern void *gcs_provider_create(const char *url, const char *access_token,
	char *err, size_t err_size);

extern const CurlProvider azure_provider;
extern void *azure_provider_create(const char *url, const char *account_name,
	const char *account_key, const char *sas_token, const char *access_token,
	const char *endpoint_suffix, const char *endpoint,
	char *err, size_t err_size);

// in PyCoreArray.cpp
extern PdGDSFile GDS_File_Open_Callback(void *user_data,
	TdCbStreamRead read_fn, TdCbStreamWrite write_fn,
	TdCbStreamSeek seek_fn, TdCbStreamGetSize getsize_fn,
	TdCbStreamSetSize setsize_fn, TdCbStreamClose close_fn,
	C_BOOL ReadOnly, C_BOOL AllowError);

} // extern "C"


namespace pycorearray
{
	extern int GetFileIndex(PdGDSFile file, bool throw_error=true);
}

using namespace std;
using namespace CoreArray;
using namespace pycorearray;


/// cloud errors (raised as OSError in Python)
class ErrGDSCloud: public ErrCoreArray
{
public:
	ErrGDSCloud(): ErrCoreArray()
		{ }
	ErrGDSCloud(const char *fmt, ...): ErrCoreArray()
		{ _COREARRAY_ERRMACRO_(fmt); }
	ErrGDSCloud(const string &msg): ErrCoreArray()
		{ fMessage = msg; }
};


/// the cloud streams of the open GDS files
static vector<CloudStream*> g_open_streams;

/// set when a callback fails, so that a failure while opening a file is
/// reported as a cloud error (OSError) rather than a GDS format error
static bool g_stream_failed = false;


// =====================================================================
// Callbacks of the GDS stream: user_data is a CloudStream*
// =====================================================================

static ssize_t cloud_cb_read(void *user_data, void *buffer, ssize_t count)
{
	CloudStream *cs = (CloudStream *)user_data;
	long long result = cloud_stream_read(cs, buffer, (long long)count);
	if (result < 0)
	{
		g_stream_failed = true;
		const char *err = cloud_stream_get_last_error(cs);
		if (err && err[0])
			throw ErrGDSCloud("Cloud stream read error: %s", err);
		else
			throw ErrGDSCloud("Cloud stream read error");
	}
	return (ssize_t)result;
}

/// origin matches TdSysSeekOrg (0=begin, 1=current, 2=end)
static C_Int64 cloud_cb_seek(void *user_data, C_Int64 offset, int origin)
{
	return cloud_stream_seek((CloudStream *)user_data, offset, origin);
}

static C_Int64 cloud_cb_getsize(void *user_data)
{
	CloudStream *cs = (CloudStream *)user_data;
	long long result = cloud_stream_getsize(cs);
	if (result < 0)
	{
		g_stream_failed = true;
		const char *err = cloud_stream_get_last_error(cs);
		if (err && err[0])
			throw ErrGDSCloud("Cloud stream get size error: %s", err);
		else
			throw ErrGDSCloud("Cloud stream get size error");
	}
	return result;
}

static void cloud_cb_close(void *user_data)
{
	CloudStream *cs = (CloudStream *)user_data;
	g_open_streams.erase(
		std::remove(g_open_streams.begin(), g_open_streams.end(), cs),
		g_open_streams.end());
	cloud_stream_close(cs);
}


/// the common part of every open: wrap the provider in the generic
/// backend, create the cached stream, pre-check access (for a detailed
/// error message) and open the GDS file; takes ownership of
/// 'provider_data', and returns the index of the GDS file
static int open_cloud_gds(const char *url, const CurlProvider *provider,
	void *provider_data, double cache_mb)
{
	CurlBackendData *bd = curl_backend_create(provider, provider_data);
	if (!bd)
		throw ErrGDSCloud("Failed to initialize libcurl for '%s'", url);

	long long max_cache = (long long)(cache_mb * 1024 * 1024);
	CloudStream *cs = cloud_stream_create(url, &curl_backend_vtable, bd,
		CLOUD_BLOCK_SIZE, max_cache);
	if (!cs)
	{
		curl_backend_vtable.close(bd);
		throw ErrGDSCloud("Failed to create cloud stream for '%s'", url);
	}

	if (cloud_stream_getsize(cs) < 0)
	{
		const char *err = cloud_stream_get_last_error(cs);
		string msg = (err && err[0]) ? string(err)
			: string("Failed to access '") + url + "'";
		cloud_stream_close(cs);
		throw ErrGDSCloud(msg);
	}

	// the stream is closed by cloud_cb_close() if the file fails to open
	PdGDSFile file;
	g_stream_failed = false;
	try {
		file = GDS_File_Open_Callback(cs, cloud_cb_read, NULL, cloud_cb_seek,
			cloud_cb_getsize, NULL, cloud_cb_close, true, false);
	}
	catch (ErrGDSFmt &E) {
		if (g_stream_failed) throw ErrGDSCloud(E.what());
		throw;
	}
	g_open_streams.push_back(cs);
	return GetFileIndex(file);
}


/// a value of a dict as a string, or ""
static const char *param(PyObject *dict, const char *name)
{
	if (!PyDict_Check(dict)) return "";
	PyObject *v = PyDict_GetItemString(dict, name);
	if (v && PYSTR_IS(v))
	{
		const char *s = PYSTR_CHAR(v);
		if (s) return s;
	}
	return "";
}


/// the user interrupt check of a transfer: runs the Python signal handlers
/// (the GIL is held while reading), and a KeyboardInterrupt is kept as the
/// pending exception
static int py_pending_interrupt(void)
{
	if (PyErr_Occurred()) return 1;
	return (PyErr_CheckSignals() != 0) ? 1 : 0;
}


// =====================================================================
// Python functions (C linkage, as declared in ccall.cpp)
// =====================================================================

extern "C"
{

/// initialize libcurl and the cloud streams, called by PyInit_ccall()
COREARRAY_DLL_LOCAL void pycorearray_cloud_init()
{
	curl_global_init(CURL_GLOBAL_ALL);
	// re-initialize the curl handles in forked processes
	cloud_init_fork_tracking();
	cloud_interrupt_hook = py_pending_interrupt;
}


/// whether the cloud access is available
COREARRAY_DLL_LOCAL PyObject* cloudAvailable(PyObject *self, PyObject *args)
{
	Py_RETURN_TRUE;
}


/// open a GDS file from HTTP/HTTPS, returns the file index
COREARRAY_DLL_LOCAL PyObject* cloudOpenHTTP(PyObject *self, PyObject *args)
{
	const char *url, *auth;
	double cache_mb;
	if (!PyArg_ParseTuple(args, "ssd", &url, &auth, &cache_mb))
		return NULL;

	int file_id = -1;
	COREARRAY_TRY
		if (!url[0])
			throw ErrGDSCloud("HTTP URL is empty or missing");
		char err[256];
		void *pd = http_provider_create(url, auth, err, sizeof(err));
		if (!pd)
			throw ErrGDSCloud("Invalid HTTP URL '%s': %s", url, err);
		file_id = open_cloud_gds(url, &http_provider, pd, cache_mb);
	COREARRAY_CATCH
	return PyInt_FromLong(file_id);
}


/// open a GDS file from S3; 'path_style': 1, 0, or -1 for automatic
COREARRAY_DLL_LOCAL PyObject* cloudOpenS3(PyObject *self, PyObject *args)
{
	const char *url, *access_key, *secret_key, *region, *session_token;
	const char *endpoint;
	int path_style;
	double cache_mb;
	if (!PyArg_ParseTuple(args, "ssssssid", &url, &access_key, &secret_key,
			&region, &session_token, &endpoint, &path_style, &cache_mb))
		return NULL;

	int file_id = -1;
	COREARRAY_TRY
		if (!url[0])
			throw ErrGDSCloud("S3 URL is empty or missing");
		char err[512];
		void *pd = s3_provider_create(url, access_key, secret_key, region,
			session_token, endpoint, path_style, err, sizeof(err));
		if (!pd)
			throw ErrGDSCloud("Invalid S3 URL '%s': %s", url, err);
		file_id = open_cloud_gds(url, &s3_provider, pd, cache_mb);
	COREARRAY_CATCH
	return PyInt_FromLong(file_id);
}


/// open a GDS file from Google Cloud Storage
COREARRAY_DLL_LOCAL PyObject* cloudOpenGCS(PyObject *self, PyObject *args)
{
	const char *url, *access_token;
	double cache_mb;
	if (!PyArg_ParseTuple(args, "ssd", &url, &access_token, &cache_mb))
		return NULL;

	int file_id = -1;
	COREARRAY_TRY
		if (!url[0])
			throw ErrGDSCloud("GCS URL is empty or missing");
		char err[256];
		void *pd = gcs_provider_create(url, access_token, err, sizeof(err));
		if (!pd)
			throw ErrGDSCloud("Invalid GCS URL '%s': %s", url, err);
		file_id = open_cloud_gds(url, &gcs_provider, pd, cache_mb);
	COREARRAY_CATCH
	return PyInt_FromLong(file_id);
}


/// open a GDS file from Azure Blob Storage
COREARRAY_DLL_LOCAL PyObject* cloudOpenAzure(PyObject *self, PyObject *args)
{
	const char *url, *account_name, *account_key, *sas_token, *access_token;
	const char *endpoint_suffix, *endpoint;
	double cache_mb;
	if (!PyArg_ParseTuple(args, "sssssssd", &url, &account_name, &account_key,
			&sas_token, &access_token, &endpoint_suffix, &endpoint, &cache_mb))
		return NULL;

	int file_id = -1;
	COREARRAY_TRY
		if (!url[0])
			throw ErrGDSCloud("Azure URL is empty or missing");
		char err[512];
		void *pd = azure_provider_create(url, account_name, account_key,
			sas_token, access_token, endpoint_suffix, endpoint,
			err, sizeof(err));
		if (!pd)
			throw ErrGDSCloud("Cannot open '%s': %s", url, err);
		file_id = open_cloud_gds(url, &azure_provider, pd, cache_mb);
	COREARRAY_CATCH
	return PyInt_FromLong(file_id);
}


/// the request a provider would send for 'url' (a tuple of the URL and a
/// list of headers), without sending it; 'params' is a dict of provider
/// credentials, and 'time' is in seconds since the epoch (UTC); used by the
/// tests to check signatures
COREARRAY_DLL_LOCAL PyObject* cloudPrepareRequest(PyObject *self,
	PyObject *args)
{
	const char *url, *range;
	PyObject *params;
	double tm;
	if (!PyArg_ParseTuple(args, "sOsd", &url, &params, &range, &tm))
		return NULL;

	PyObject *rv_ans = NULL;
	COREARRAY_TRY
		const CurlProvider *provider = NULL;
		void *pd = NULL;
		char err[512];
		if (strncmp(url, "http://", 7)==0 || strncmp(url, "https://", 8)==0)
		{
			provider = &http_provider;
			pd = http_provider_create(url, param(params, "auth"), err,
				sizeof(err));
		} else if (strncmp(url, "s3://", 5) == 0)
		{
			provider = &s3_provider;
			int ps = -1;
			if (strcmp(param(params, "path_style"), "TRUE") == 0) ps = 1;
			else if (strcmp(param(params, "path_style"), "FALSE") == 0) ps = 0;
			pd = s3_provider_create(url, param(params, "access_key"),
				param(params, "secret_key"), param(params, "region"),
				param(params, "session_token"), param(params, "endpoint"),
				ps, err, sizeof(err));
		} else if (strncmp(url, "gs://", 5) == 0)
		{
			provider = &gcs_provider;
			pd = gcs_provider_create(url, param(params, "access_token"),
				err, sizeof(err));
		} else if (strncmp(url, "az://", 5) == 0)
		{
			provider = &azure_provider;
			pd = azure_provider_create(url, param(params, "account_name"),
				param(params, "account_key"), param(params, "sas_token"),
				param(params, "access_token"),
				param(params, "endpoint_suffix"), param(params, "endpoint"),
				err, sizeof(err));
		} else
			throw ErrGDSCloud("Unsupported URL '%s'", url);
		if (!pd)
			throw ErrGDSCloud("Invalid URL '%s': %s", url, err);

		CurlRequest req;
		req.url[0] = '\0';
		req.headers = NULL;
		err[0] = '\0';
		int rc = provider->prepare(pd, range, (time_t)tm, &req, err,
			sizeof(err));
		vector<string> hdrs;
		for (struct curl_slist *p = req.headers; p; p = p->next)
			hdrs.push_back(p->data);
		string req_url(req.url);
		if (req.headers) curl_slist_free_all(req.headers);
		provider->free_data(pd);
		if (rc != 0)
			throw ErrGDSCloud("%s: %s", provider->name, err);

		PyObject *h = PyList_New(hdrs.size());
		for (size_t i=0; i < hdrs.size(); i++)
			PyList_SET_ITEM(h, i, PYSTR_SET(hdrs[i].c_str()));
		rv_ans = Py_BuildValue("(sN)", req_url.c_str(), h);
	COREARRAY_CATCH
	return rv_ans;
}


/// set the transfer options: timeouts in seconds and the maximum number of
/// retries (a value <= 0 or < 0 respectively keeps the current one), and the
/// CA bundle ("" for the libcurl default)
COREARRAY_DLL_LOCAL PyObject* cloudSetOptions(PyObject *self, PyObject *args)
{
	double ct, lt, mr;
	const char *ca;
	if (!PyArg_ParseTuple(args, "ddds", &ct, &lt, &mr, &ca))
		return NULL;
	cloud_set_timeouts(
		(IsFinite(ct) && ct > 0) ? (long)ct : 0,
		(IsFinite(lt) && lt > 0) ? (long)lt : 0);
	cloud_set_max_retries((IsFinite(mr) && mr >= 0) ? (int)mr : -1);
	if (cloud_set_ca_bundle(ca) != 0)
		return PyErr_NoMemory();
	Py_RETURN_NONE;
}


/// the CA bundle built into libcurl, None if none or unknown (libcurl < 7.84)
COREARRAY_DLL_LOCAL PyObject* cloudCurlCABundle(PyObject *self, PyObject *args)
{
	string s;
#if LIBCURL_VERSION_NUM >= 0x075400
	CURL *curl = curl_easy_init();
	if (curl)
	{
		char *p = NULL;
		if (curl_easy_getinfo(curl, CURLINFO_CAINFO, &p) == CURLE_OK && p)
			s = p;
		curl_easy_cleanup(curl);
	}
#endif
	if (s.empty()) Py_RETURN_NONE;
	return PYSTR_SET(s.c_str());
}


/// clear the block caches of all open cloud streams
COREARRAY_DLL_LOCAL PyObject* cloudCacheClear(PyObject *self, PyObject *args)
{
	for (size_t i=0; i < g_open_streams.size(); i++)
		cache_clear_all(&g_open_streams[i]->cache);
	Py_RETURN_NONE;
}


/// cache statistics of all open cloud streams
COREARRAY_DLL_LOCAL PyObject* cloudCacheInfo(PyObject *self, PyObject *args)
{
	long long total_hits = 0, total_misses = 0;
	for (size_t i=0; i < g_open_streams.size(); i++)
	{
		total_hits   += g_open_streams[i]->cache.total_hits;
		total_misses += g_open_streams[i]->cache.total_misses;
	}
	return Py_BuildValue("{s:i,s:L,s:L,s:L}",
		"num_streams", (int)g_open_streams.size(),
		"hits", total_hits, "misses", total_misses,
		"retries", cloud_total_retries());
}


/// a list of the open cloud streams, a dict for each
COREARRAY_DLL_LOCAL PyObject* cloudListStreams(PyObject *self, PyObject *args)
{
	PyObject *ans = PyList_New(g_open_streams.size());
	for (size_t i=0; i < g_open_streams.size(); i++)
	{
		CloudStream *cs = g_open_streams[i];
		PyList_SET_ITEM(ans, i, Py_BuildValue("{s:s,s:L,s:i,s:L,s:L}",
			"url", cs->url, "file_size", cs->file_size,
			"cache_blocks", cs->cache.num_blocks,
			"cache_hits", cs->cache.total_hits,
			"cache_misses", cs->cache.total_misses));
	}
	return ans;
}

} // extern "C"


#else  // PYCOREARRAY_WITH_CLOUD

// =====================================================================
// Built without cloud access (libcurl not found)
// =====================================================================

static PyObject *no_cloud()
{
	PyErr_SetString(PyExc_RuntimeError,
		"pycorearray was built without cloud access: libcurl (and, except "
		"on macOS, OpenSSL) was not found when it was compiled");
	return NULL;
}

extern "C"
{

COREARRAY_DLL_LOCAL void pycorearray_cloud_init() { }

COREARRAY_DLL_LOCAL PyObject* cloudAvailable(PyObject *self, PyObject *args)
	{ Py_RETURN_FALSE; }
COREARRAY_DLL_LOCAL PyObject* cloudOpenHTTP(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudOpenS3(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudOpenGCS(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudOpenAzure(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudPrepareRequest(PyObject *self,
	PyObject *args) { return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudSetOptions(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudCurlCABundle(PyObject *self, PyObject *args)
	{ Py_RETURN_NONE; }
COREARRAY_DLL_LOCAL PyObject* cloudCacheClear(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudCacheInfo(PyObject *self, PyObject *args)
	{ return no_cloud(); }
COREARRAY_DLL_LOCAL PyObject* cloudListStreams(PyObject *self, PyObject *args)
	{ return no_cloud(); }

} // extern "C"

#endif  // PYCOREARRAY_WITH_CLOUD
