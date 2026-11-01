// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// gcs_backend.c: Google Cloud Storage provider (OAuth2 bearer token)
//     for the generic curl backend
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

typedef struct GCSProviderData {
	char access_token[CLOUD_MAX_CRED_LEN];
	char endpoint[CLOUD_MAX_ENDPOINT_LEN];
} GCSProviderData;


// =====================================================================
// Provider vtable implementation
// =====================================================================

static int gcs_prepare(void *pd, const char *range_value, time_t now,
	CurlRequest *req, char *err, size_t err_size)
{
	(void)range_value; (void)now; (void)err; (void)err_size;
	GCSProviderData *gcs = (GCSProviderData *)pd;
	snprintf(req->url, sizeof(req->url), "%s", gcs->endpoint);
	if (gcs->access_token[0])
	{
		char auth_hdr[CLOUD_MAX_CRED_LEN + 32];
		snprintf(auth_hdr, sizeof(auth_hdr), "Authorization: Bearer %s",
			gcs->access_token);
		curl_request_add_header(req, auth_hdr);
	}
	return 0;
}

static void gcs_error_hint(void *pd, const CurlResponseInfo *info,
	char *err, size_t err_size)
{
	GCSProviderData *gcs = (GCSProviderData *)pd;
	size_t cur = strlen(err);
	if (cur + 1 >= err_size) return;
	if (info->http_code == 401 && gcs->access_token[0])
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: OAuth2 access tokens expire after about an hour; "
			"obtain a fresh token (e.g. `gcloud auth print-access-token`) "
			"and call gdsCloudConfigGCS() again]");
	}
	else if ((info->http_code == 401 || info->http_code == 403) &&
		!gcs->access_token[0])
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: no credentials are configured, so the request was sent "
			"anonymously; see ?gdsCloudConfigGCS]");
	}
}

static const char *gcs_endpoint(void *pd)
{
	return ((GCSProviderData *)pd)->endpoint;
}

static void gcs_free(void *pd)
{
	GCSProviderData *gcs = (GCSProviderData *)pd;
	if (!gcs) return;
	memset(gcs->access_token, 0, sizeof(gcs->access_token));
	free(gcs);
}

const CurlProvider gcs_provider = {
	.name       = "GCS",
	.prepare    = gcs_prepare,
	.error_hint = gcs_error_hint,
	.endpoint   = gcs_endpoint,
	.free_data  = gcs_free
};


// =====================================================================
// Construction: gs://bucket/key -> https://storage.googleapis.com/bucket/key
// Returns NULL with a message in `err` on invalid input.
// =====================================================================

void *gcs_provider_create(const char *url, const char *access_token,
	char *err, size_t err_size)
{
	err[0] = '\0';
	if (strncmp(url, "gs://", 5) != 0)
	{
		snprintf(err, err_size, "must start with 'gs://'");
		return NULL;
	}
	const char *rest = url + 5;
	const char *slash = strchr(rest, '/');
	if (!slash || slash == rest || !slash[1])
	{
		snprintf(err, err_size, "missing object key (expected 'gs://bucket/key')");
		return NULL;
	}
	char bucket[256];
	size_t bucket_len = (size_t)(slash - rest);
	if (bucket_len >= sizeof(bucket))
	{
		snprintf(err, err_size, "bucket name is too long");
		return NULL;
	}
	memcpy(bucket, rest, bucket_len);
	bucket[bucket_len] = '\0';

	GCSProviderData *gcs = (GCSProviderData *)calloc(1, sizeof(GCSProviderData));
	if (!gcs) return NULL;

	if (access_token && access_token[0])
		snprintf(gcs->access_token, sizeof(gcs->access_token), "%s", access_token);

	char encoded_key[CLOUD_MAX_URL_LEN];
	cloud_url_encode_path(slash + 1, encoded_key, sizeof(encoded_key));
	snprintf(gcs->endpoint, sizeof(gcs->endpoint),
		"https://storage.googleapis.com/%s/%s", bucket, encoded_key);
	return gcs;
}
