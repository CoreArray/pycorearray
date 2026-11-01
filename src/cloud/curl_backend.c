// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// curl_backend.c: Generic libcurl backend shared by all providers
//
// Copyright (C) 2026    Xiuwen Zheng
//
// This file is part of gdscloud.
// LGPL-3 License
// ===========================================================

#include "curl_backend.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <stdarg.h>

#ifdef _WIN32
#ifndef strncasecmp
#define strncasecmp _strnicmp
#endif
#else
#include <strings.h>
#endif


// =====================================================================
// Request helpers
// =====================================================================

int curl_request_add_header(CurlRequest *req, const char *line)
{
	struct curl_slist *h = curl_slist_append(req->headers, line);
	if (!h) return -1;
	req->headers = h;
	return 0;
}

static void curl_request_free(CurlRequest *req)
{
	if (req->headers) curl_slist_free_all(req->headers);
	req->headers = NULL;
}


// =====================================================================
// Shared helpers
// =====================================================================

void cloud_url_encode_path(const char *src, char *dst, size_t dst_size)
{
	static const char *hex = "0123456789ABCDEF";
	size_t j = 0;
	if (dst_size == 0) return;
	for (size_t i = 0; src[i] && j + 3 < dst_size; i++)
	{
		unsigned char c = (unsigned char)src[i];
		if (c == '/' || isalnum(c) || c == '-' || c == '_' || c == '.' ||
			c == '~')
		{
			dst[j++] = (char)c;
		} else {
			dst[j++] = '%';
			dst[j++] = hex[(c >> 4) & 0x0f];
			dst[j++] = hex[c & 0x0f];
		}
	}
	dst[j] = '\0';
}

static const char B64_CHARS[] =
	"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int cloud_base64_encode(const unsigned char *in, size_t in_len,
	char *out, size_t out_size)
{
	size_t need = ((in_len + 2) / 3) * 4 + 1;
	if (out_size < need) return -1;
	size_t i = 0, j = 0;
	while (i + 2 < in_len)
	{
		unsigned v = ((unsigned)in[i] << 16) | ((unsigned)in[i+1] << 8) | in[i+2];
		out[j++] = B64_CHARS[(v >> 18) & 63];
		out[j++] = B64_CHARS[(v >> 12) & 63];
		out[j++] = B64_CHARS[(v >> 6) & 63];
		out[j++] = B64_CHARS[v & 63];
		i += 3;
	}
	if (i < in_len)
	{
		unsigned v = (unsigned)in[i] << 16;
		if (i + 1 < in_len) v |= (unsigned)in[i+1] << 8;
		out[j++] = B64_CHARS[(v >> 18) & 63];
		out[j++] = B64_CHARS[(v >> 12) & 63];
		out[j++] = (i + 1 < in_len) ? B64_CHARS[(v >> 6) & 63] : '=';
		out[j++] = '=';
	}
	out[j] = '\0';
	return 0;
}

static int b64_value(char c)
{
	if (c >= 'A' && c <= 'Z') return c - 'A';
	if (c >= 'a' && c <= 'z') return c - 'a' + 26;
	if (c >= '0' && c <= '9') return c - '0' + 52;
	if (c == '+') return 62;
	if (c == '/') return 63;
	return -1;
}

long cloud_base64_decode(const char *in, unsigned char *out, size_t out_size)
{
	unsigned acc = 0;
	int bits = 0;
	long n = 0;
	for (const char *p = in; *p; p++)
	{
		if (*p == '=' || *p == '\n' || *p == '\r' || *p == ' ') continue;
		int v = b64_value(*p);
		if (v < 0) return -1;
		acc = (acc << 6) | (unsigned)v;
		bits += 6;
		if (bits >= 8)
		{
			bits -= 8;
			if ((size_t)n >= out_size) return -1;
			out[n++] = (unsigned char)((acc >> bits) & 0xff);
		}
	}
	return n;
}

void cloud_utc_time(time_t t, struct tm *utc)
{
#ifdef _WIN32
	gmtime_s(utc, &t);
#else
	gmtime_r(&t, utc);
#endif
}


int cloud_split_endpoint(const char *endpoint,
	char *scheme_out, size_t scheme_size, char *host_out, size_t host_size,
	char *path_out, size_t path_size)
{
	const char *p = endpoint;
	const char *scheme = "https";
	if (strncasecmp(p, "https://", 8) == 0) { p += 8; scheme = "https"; }
	else if (strncasecmp(p, "http://", 7) == 0) { p += 7; scheme = "http"; }
	if (!*p || *p == '/' || *p == '?') return -1;

	size_t host_len = strcspn(p, "/?");
	if (host_len >= host_size || strlen(scheme) >= scheme_size) return -1;
	memcpy(host_out, p, host_len);
	host_out[host_len] = '\0';
	strcpy(scheme_out, scheme);

	path_out[0] = '\0';
	p += host_len;
	if (*p == '/')
	{
		size_t path_len = strcspn(p, "?");
		while (path_len > 0 && p[path_len - 1] == '/') path_len--;
		if (path_len >= path_size) return -1;
		memcpy(path_out, p, path_len);
		path_out[path_len] = '\0';
	}
	return 0;
}


// =====================================================================
// libcurl callbacks
// =====================================================================

typedef struct {
	unsigned char *buf;
	long long size;
	long long capacity;
	int full;   // set when data beyond the capacity was refused
} WriteBuffer;

static size_t write_cb(void *data, size_t size, size_t nmemb, void *userp)
{
	WriteBuffer *wb = (WriteBuffer *)userp;
	size_t realsize = size * nmemb;
	long long room = wb->capacity - wb->size;
	if ((long long)realsize > room)
	{
		// keep what fits, then abort the transfer: more than the requested
		// range is never needed, and error bodies are small anyway
		if (room > 0)
		{
			memcpy(wb->buf + wb->size, data, (size_t)room);
			wb->size += room;
		}
		wb->full = 1;
		return 0;
	}
	if (realsize > 0)
	{
		memcpy(wb->buf + wb->size, data, realsize);
		wb->size += (long long)realsize;
	}
	return realsize;
}

/// per-response header state: metadata plus the Range validation
typedef struct {
	CurlResponseInfo info;
	long long expected_offset;   // requested start offset
	int range_problem;           // 1: 200 for offset > 0, 2: wrong Content-Range
} HeaderState;

static void header_state_reset(HeaderState *hs, long long expected_offset)
{
	memset(&hs->info, 0, sizeof(hs->info));
	hs->info.content_range_start = -1;
	hs->info.content_range_total = -1;
	hs->info.content_length = -1;
	hs->info.blob_content_length = -1;
	hs->expected_offset = expected_offset;
	hs->range_problem = 0;
}

/// copy a header value (after "Name:"), trimming blanks and CR/LF
static void header_value(const char *line, size_t total, size_t name_len,
	char *out, size_t out_size)
{
	const char *p = line + name_len;
	size_t remaining = (total > name_len) ? (total - name_len) : 0;
	while (remaining > 0 && (*p == ' ' || *p == '\t')) { p++; remaining--; }
	while (remaining > 0 && (p[remaining-1] == '\r' || p[remaining-1] == '\n' ||
		p[remaining-1] == ' ' || p[remaining-1] == '\t'))
		remaining--;
	size_t n = (remaining < out_size - 1) ? remaining : out_size - 1;
	memcpy(out, p, n);
	out[n] = '\0';
}

static int header_is(const char *line, size_t total, const char *name)
{
	size_t n = strlen(name);
	return total > n && strncasecmp(line, name, n) == 0;
}

static size_t header_cb(char *buffer, size_t size, size_t nitems, void *userdata)
{
	HeaderState *hs = (HeaderState *)userdata;
	CurlResponseInfo *info = &hs->info;
	size_t total = size * nitems;
	char value[256];

	if (total >= 5 && strncmp(buffer, "HTTP/", 5) == 0)
	{
		// status line: a new response starts (also after a redirect), so
		// forget the headers of the previous one
		long long expected = hs->expected_offset;
		header_state_reset(hs, expected);
		const char *p = (const char *)memchr(buffer, ' ', total);
		info->http_code = p ? strtol(p + 1, NULL, 10) : 0;
		if (info->http_code == 200 && expected > 0)
		{
			// the whole file is coming instead of the requested range:
			// stop now rather than downloading all of it
			hs->range_problem = 1;
			return 0;
		}
	}
	else if (header_is(buffer, total, "content-range:"))
	{
		// "bytes START-END/TOTAL" or "bytes */TOTAL"
		header_value(buffer, total, 14, value, sizeof(value));
		const char *p = value;
		if (strncasecmp(p, "bytes", 5) == 0) p += 5;
		while (*p == ' ') p++;
		if (isdigit((unsigned char)*p))
		{
			info->content_range_start = strtoll(p, NULL, 10);
			if (info->http_code == 206 &&
				info->content_range_start != hs->expected_offset)
			{
				hs->range_problem = 2;
				return 0;
			}
		}
		const char *slash = strchr(p, '/');
		if (slash)
		{
			slash++;
			while (*slash == ' ') slash++;
			if (isdigit((unsigned char)*slash))
				info->content_range_total = strtoll(slash, NULL, 10);
		}
	}
	else if (header_is(buffer, total, "content-length:"))
	{
		header_value(buffer, total, 15, value, sizeof(value));
		info->content_length = strtoll(value, NULL, 10);
	}
	else if (header_is(buffer, total, "x-ms-blob-content-length:"))
	{
		header_value(buffer, total, 25, value, sizeof(value));
		info->blob_content_length = strtoll(value, NULL, 10);
	}
	else if (header_is(buffer, total, "content-type:"))
	{
		header_value(buffer, total, 13, info->content_type,
			sizeof(info->content_type));
	}
	else if (header_is(buffer, total, "x-amz-bucket-region:"))
	{
		header_value(buffer, total, 20, info->bucket_region,
			sizeof(info->bucket_region));
	}
	else if (header_is(buffer, total, "x-amz-request-id:"))
	{
		header_value(buffer, total, 17, info->request_id,
			sizeof(info->request_id));
	}
	else if (header_is(buffer, total, "x-ms-request-id:"))
	{
		header_value(buffer, total, 16, info->request_id,
			sizeof(info->request_id));
	}
	return total;
}


// =====================================================================
// The core transfer: GET a byte range, with retries
// =====================================================================

typedef struct {
	CURLcode res;
	HeaderState hdr;
	WriteBuffer body;
	CloudTransfer tr;
	long redirects;                    // number of redirects followed
	char final_url[CLOUD_MAX_URL_LEN]; // URL of the final response
} Transfer;

static void set_error(CurlBackendData *bd, const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(bd->last_error, sizeof(bd->last_error), fmt, ap);
	va_end(ap);
}

static void append_error(char *err, size_t err_size, const char *fmt, ...)
{
	size_t cur = strlen(err);
	if (cur + 1 >= err_size) return;
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(err + cur, err_size - cur, fmt, ap);
	va_end(ap);
}

/// GET `range_value` into t->body. Returns 0 when a final HTTP response
/// (of any status) was obtained, -1 on a hard failure (message set).
static int perform_range(CurlBackendData *bd, const char *range_value,
	long long offset, Transfer *t)
{
	CurlRequest req;
	req.url[0] = '\0';
	req.headers = NULL;

	char err[CLOUD_MAX_ERROR_LEN];
	err[0] = '\0';
	if (bd->provider->prepare(bd->provider_data, range_value, time(NULL),
		&req, err, sizeof(err)) != 0)
	{
		set_error(bd, "%s: %s", bd->provider->name,
			err[0] ? err : "failed to prepare the request");
		curl_request_free(&req);
		return -1;
	}
	char line[128];
	snprintf(line, sizeof(line), "Range: %s", range_value);
	curl_request_add_header(&req, line);

	CURL *curl = bd->curl;
	curl_easy_reset(curl);
	cloud_curl_setup(curl, &t->tr);
	curl_easy_setopt(curl, CURLOPT_USERAGENT, GDSCLOUD_USER_AGENT);
	curl_easy_setopt(curl, CURLOPT_URL, req.url);
	curl_easy_setopt(curl, CURLOPT_HTTPHEADER, req.headers);
	curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
	curl_easy_setopt(curl, CURLOPT_WRITEDATA, &t->body);
	curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_cb);
	curl_easy_setopt(curl, CURLOPT_HEADERDATA, &t->hdr);
	curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
	curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

	// perform the request, retrying transient failures with back-off
	int aborted;
	for (int attempt = 0; ; attempt++)
	{
		t->body.size = 0;
		t->body.full = 0;
		header_state_reset(&t->hdr, offset);
		t->res = curl_easy_perform(curl);
		// a transfer aborted by write_cb or header_cb is not a network
		// failure and must not be retried, whatever code libcurl reports
		// for it: CURLE_WRITE_ERROR over HTTP/1.1, CURLE_RECV_ERROR over
		// HTTP/2
		aborted = t->body.full || t->hdr.range_problem;
		if (aborted) break;
		if (!cloud_should_retry(&t->tr, t->res, curl, attempt)) break;
	}
	curl_request_free(&req);
	// aborting from write_cb once the buffer is full is not an error
	if (t->body.full) t->res = CURLE_OK;

	char *final_url = NULL;
	t->redirects = 0;
	curl_easy_getinfo(curl, CURLINFO_REDIRECT_COUNT, &t->redirects);
	curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &final_url);
	snprintf(t->final_url, sizeof(t->final_url), "%s",
		final_url ? final_url : "");

	if (aborted)
	{
		// libcurl (seen with 8.7.1) keeps an HTTP/2 connection whose stream
		// was aborted, and the next request on it stalls until the
		// low-speed timeout; discard the handle together with its
		// connections
		curl_easy_cleanup(bd->curl);
		bd->curl = curl_easy_init();
	}

	if (cloud_transfer_interrupted(&t->tr, t->res, bd->provider->name,
		bd->last_error, sizeof(bd->last_error)))
		return -1;

	// a transfer aborted by header_cb because of a Range problem
	const char *endpoint = bd->provider->endpoint(bd->provider_data);
	if (t->hdr.range_problem == 1 ||
		(t->res == CURLE_OK && t->hdr.info.http_code == 200 && offset > 0))
	{
		set_error(bd, "%s: the server does not support HTTP Range requests "
			"(it returned the whole file instead of bytes %lld-%lld of '%s')",
			bd->provider->name, offset, offset + t->body.capacity - 1, endpoint);
		return -1;
	}
	if (t->hdr.range_problem == 2)
	{
		set_error(bd, "%s: the server returned bytes starting at %lld "
			"instead of the requested offset %lld ('%s')",
			bd->provider->name, t->hdr.info.content_range_start, offset,
			endpoint);
		return -1;
	}
	if (t->res != CURLE_OK)
	{
		cloud_format_error(bd->last_error, sizeof(bd->last_error),
			bd->provider->name, endpoint, t->res, 0, NULL, 0);
		return -1;
	}
	return 0;
}

/// Format a non-2xx HTTP response into bd->last_error
static void format_http_error(CurlBackendData *bd, const Transfer *t)
{
	const CurlResponseInfo *info = &t->hdr.info;
	cloud_format_error(bd->last_error, sizeof(bd->last_error),
		bd->provider->name, bd->provider->endpoint(bd->provider_data),
		CURLE_OK, info->http_code, t->body.buf, t->body.size);
	if (info->request_id[0])
	{
		append_error(bd->last_error, sizeof(bd->last_error),
			" [request id: %s]", info->request_id);
	}
	if (bd->provider->error_hint)
	{
		bd->provider->error_hint(bd->provider_data, info,
			bd->last_error, sizeof(bd->last_error));
	}
}


// =====================================================================
// CloudBackend implementation
// =====================================================================

/// Make sure there is a usable handle: it is re-created after a fork or
/// an aborted transfer, which can fail. Returns 0 on success.
static int check_curl(CurlBackendData *bd)
{
	cloud_check_reinit_curl(&bd->curl);
	if (!bd->curl) bd->curl = curl_easy_init();
	if (bd->curl) return 0;
	set_error(bd, "%s: failed to initialize libcurl", bd->provider->name);
	return -1;
}

static long long curl_backend_read_range(void *backend_data, const char *url,
	long long offset, long long length, unsigned char *buffer)
{
	(void)url;
	CurlBackendData *bd = (CurlBackendData *)backend_data;
	if (check_curl(bd) != 0) return -1;

	char range_value[128];
	snprintf(range_value, sizeof(range_value), "bytes=%lld-%lld",
		offset, offset + length - 1);

	Transfer t;
	t.body.buf = buffer;
	t.body.capacity = length;
	if (perform_range(bd, range_value, offset, &t) != 0)
		return -1;

	long code = t.hdr.info.http_code;
	if (code == 206 || code == 200)
		return t.body.size;
	format_http_error(bd, &t);
	return -1;
}

// A 1-byte GET (Range: bytes=0-0) rather than HEAD, so that on error the
// service's XML/JSON error body is available for diagnostics. On success
// the size comes from "Content-Range: bytes 0-0/<TOTAL>"; servers that
// ignore the Range header answer 200 and are handled via Content-Length.
// An HTML page instead of the file, e.g. a sign-in page reached through a
// redirect, is reported here rather than as an invalid GDS header later.
static long long curl_backend_get_size(void *backend_data, const char *url)
{
	(void)url;
	CurlBackendData *bd = (CurlBackendData *)backend_data;
	if (check_curl(bd) != 0) return -1;

	unsigned char body_buf[4096];
	Transfer t;
	t.body.buf = body_buf;
	t.body.capacity = (long long)sizeof(body_buf);
	if (perform_range(bd, "bytes=0-0", 0, &t) != 0)
		return -1;

	const CurlResponseInfo *info = &t.hdr.info;
	if (info->http_code != 200 && info->http_code != 206)
	{
		format_http_error(bd, &t);
		return -1;
	}
	if (strncasecmp(info->content_type, "text/html", 9) == 0)
	{
		char redirect[CLOUD_MAX_URL_LEN + 32];
		redirect[0] = '\0';
		if (t.redirects > 0)
		{
			// without the query string, which may hold a signed token
			int n = (int)strcspn(t.final_url, "?");
			snprintf(redirect, sizeof(redirect), " after a redirect to '%.*s'",
				n, t.final_url);
		}
		set_error(bd, "%s: '%s' returned an HTML page (%s) instead of the "
			"file%s; it may require signing in, or the URL is not a direct "
			"link to the file", bd->provider->name,
			bd->provider->endpoint(bd->provider_data), info->content_type,
			redirect);
		return -1;
	}
	long long size = -1;
	if (info->http_code == 206) size = info->content_range_total;
	if (size < 0) size = info->blob_content_length;
	if (size < 0 && info->http_code == 200) size = info->content_length;
	if (size < 0)
	{
		set_error(bd, "%s: could not determine the size of '%s' (no usable "
			"Content-Range or Content-Length header)", bd->provider->name,
			bd->provider->endpoint(bd->provider_data));
		return -1;
	}
	return size;
}

static void curl_backend_close(void *backend_data)
{
	CurlBackendData *bd = (CurlBackendData *)backend_data;
	if (!bd) return;
	if (bd->curl) curl_easy_cleanup(bd->curl);
	if (bd->provider && bd->provider->free_data && bd->provider_data)
		bd->provider->free_data(bd->provider_data);
	free(bd);
}

static const char *curl_backend_get_last_error(void *backend_data)
{
	CurlBackendData *bd = (CurlBackendData *)backend_data;
	return bd ? bd->last_error : "";
}

CloudBackend curl_backend_vtable = {
	.read_range     = curl_backend_read_range,
	.get_size       = curl_backend_get_size,
	.close          = curl_backend_close,
	.get_last_error = curl_backend_get_last_error
};


// =====================================================================
// Construction
// =====================================================================

CurlBackendData *curl_backend_create(const CurlProvider *provider,
	void *provider_data)
{
	CurlBackendData *bd = (CurlBackendData *)calloc(1, sizeof(CurlBackendData));
	if (!bd)
	{
		if (provider->free_data) provider->free_data(provider_data);
		return NULL;
	}
	bd->provider = provider;
	bd->provider_data = provider_data;
	bd->curl = curl_easy_init();
	if (!bd->curl)
	{
		curl_backend_close(bd);
		return NULL;
	}
	return bd;
}
