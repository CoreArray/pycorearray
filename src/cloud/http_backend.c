// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// http_backend.c: plain HTTP/HTTPS provider (optional Bearer token) for
//     the generic curl backend
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


// =====================================================================
// Provider state
// =====================================================================

typedef struct HTTPProviderData {
	char url[CLOUD_MAX_URL_LEN];
	char auth_header[CLOUD_MAX_CRED_LEN + 32];   // "Authorization: Bearer <token>"
} HTTPProviderData;


// =====================================================================
// Provider vtable implementation
// =====================================================================

static int http_prepare(void *pd, const char *range_value, time_t now,
	CurlRequest *req, char *err, size_t err_size)
{
	(void)range_value; (void)now; (void)err; (void)err_size;
	HTTPProviderData *http = (HTTPProviderData *)pd;
	snprintf(req->url, sizeof(req->url), "%s", http->url);
	if (http->auth_header[0])
		curl_request_add_header(req, http->auth_header);
	return 0;
}

static const char *http_endpoint(void *pd)
{
	return ((HTTPProviderData *)pd)->url;
}

static void http_free(void *pd)
{
	HTTPProviderData *http = (HTTPProviderData *)pd;
	if (!http) return;
	memset(http->auth_header, 0, sizeof(http->auth_header));
	free(http);
}

const CurlProvider http_provider = {
	.name       = "HTTP",
	.prepare    = http_prepare,
	.error_hint = NULL,
	.endpoint   = http_endpoint,
	.free_data  = http_free
};


// =====================================================================
// Construction. `auth` is the Authorization header value ("Bearer <token>")
// or empty. Returns NULL with a message in `err` on invalid input.
// =====================================================================

void *http_provider_create(const char *url, const char *auth,
	char *err, size_t err_size)
{
	err[0] = '\0';
	if (!url || !url[0] ||
		(strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0))
	{
		snprintf(err, err_size, "must start with 'http://' or 'https://'");
		return NULL;
	}
	if (strlen(url) >= CLOUD_MAX_URL_LEN)
	{
		snprintf(err, err_size, "URL is too long");
		return NULL;
	}
	HTTPProviderData *http = (HTTPProviderData *)calloc(1, sizeof(HTTPProviderData));
	if (!http) return NULL;
	snprintf(http->url, sizeof(http->url), "%s", url);
	if (auth && auth[0])
		snprintf(http->auth_header, sizeof(http->auth_header),
			"Authorization: %s", auth);
	return http;
}
