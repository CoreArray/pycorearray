// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// curl_backend.h: Generic libcurl backend shared by all providers
//
// A provider (HTTP, S3, GCS, Azure, ...) only resolves the request URL
// and adds its authentication headers. Everything else -- performing the
// transfer, Range validation, timeouts, retries, user interrupts, size
// detection and error formatting -- lives in curl_backend.c.
//
// Copyright (C) 2026    Xiuwen Zheng
//
// This file is part of gdscloud.
// LGPL-3 License
// ===========================================================

#ifndef GDSCLOUD_CURL_BACKEND_H
#define GDSCLOUD_CURL_BACKEND_H

#include "cloud_stream.h"
#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif


// =====================================================================
// Limits
// =====================================================================

/// Maximum length of a request URL (endpoint + SAS token / query string)
#define CURL_MAX_REQ_URL_LEN   (CLOUD_MAX_ENDPOINT_LEN + CLOUD_MAX_CRED_LEN + 16)
/// Maximum length of a single header line
#define CURL_MAX_HEADER_LEN    (CLOUD_MAX_CRED_LEN + 256)


// =====================================================================
// Request being prepared by a provider
// =====================================================================

typedef struct CurlRequest {
	/// full URL to fetch (a provider may append a query string, e.g. SAS)
	char url[CURL_MAX_REQ_URL_LEN];
	/// provider-supplied headers (authentication, API version, ...);
	/// the generic backend adds Range and User-Agent itself
	struct curl_slist *headers;
} CurlRequest;

/// Append a header line to the request; returns 0 on success
int curl_request_add_header(CurlRequest *req, const char *line);


// =====================================================================
// Response metadata collected by the generic backend
// =====================================================================

typedef struct CurlResponseInfo {
	long http_code;                  // status of the (final) response
	long long content_range_start;   // -1 if absent
	long long content_range_total;   // -1 if absent / unknown ("*")
	long long content_length;        // -1 if absent
	long long blob_content_length;   // Azure x-ms-blob-content-length, -1 if absent
	char content_type[128];          // Content-Type, "" if absent
	char bucket_region[64];          // S3 x-amz-bucket-region
	char request_id[128];            // x-amz-request-id / x-ms-request-id
} CurlResponseInfo;


// =====================================================================
// Provider vtable: what each cloud service implements
// =====================================================================

typedef struct CurlProvider {
	/// short name used as the error-message prefix, e.g. "S3"
	const char *name;

	/// Prepare a GET request for the byte range described by
	/// `range_value` ("bytes=a-b"; passed because AWS SigV4 and Azure
	/// Shared Key sign the Range header). `now` is the request time, so
	/// that signatures can be reproduced in tests. Fill `req->url` and
	/// add auth headers. Return 0 on success, or -1 with a message in
	/// `err`.
	int (*prepare)(void *provider_data, const char *range_value,
		time_t now, CurlRequest *req, char *err, size_t err_size);

	/// Optional: append a provider-specific hint to an HTTP error
	/// message (e.g. S3 region mismatch). May be NULL.
	void (*error_hint)(void *provider_data, const CurlResponseInfo *info,
		char *err, size_t err_size);

	/// The resolved endpoint shown in error messages
	const char *(*endpoint)(void *provider_data);

	/// Free the provider state (zeroing any secrets first)
	void (*free_data)(void *provider_data);
} CurlProvider;


// =====================================================================
// Generic backend state
// =====================================================================

typedef struct CurlBackendData {
	const CurlProvider *provider;
	void *provider_data;
	CURL *curl;
	char last_error[CLOUD_MAX_ERROR_LEN];
} CurlBackendData;

/// Wrap a provider in the generic backend. Takes ownership of
/// `provider_data` (freed with provider->free_data on close, and also
/// when this function fails). Returns NULL on failure.
CurlBackendData *curl_backend_create(const CurlProvider *provider,
	void *provider_data);

/// The CloudBackend vtable driving a CurlBackendData
extern CloudBackend curl_backend_vtable;


// =====================================================================
// Helpers shared by the providers
// =====================================================================

/// Percent-encode a URL path, keeping '/' and RFC 3986 unreserved
/// characters. Applied once: a '%' in the input is itself encoded.
void cloud_url_encode_path(const char *src, char *dst, size_t dst_size);

/// Base64 encode; returns 0 on success (-1 if `out` is too small)
int cloud_base64_encode(const unsigned char *in, size_t in_len,
	char *out, size_t out_size);

/// Base64 decode; returns the number of decoded bytes, or -1 on invalid
/// input or if `out` is too small
long cloud_base64_decode(const char *in, unsigned char *out, size_t out_size);

/// Broken-down UTC time for `t` (thread-safe gmtime)
void cloud_utc_time(time_t t, struct tm *utc);

/// Split an endpoint such as "https://host:9000/prefix/" into its parts:
/// `scheme_out` gets "http" or "https" (https when absent), `host_out`
/// gets "host[:port]" (as needed for a Host header) and `path_out` the
/// path without query string or trailing slash ("" when none). Returns
/// 0 on success, -1 when there is no host or a buffer is too small.
int cloud_split_endpoint(const char *endpoint,
	char *scheme_out, size_t scheme_size, char *host_out, size_t host_size,
	char *path_out, size_t path_size);


#ifdef __cplusplus
}
#endif

#endif /* GDSCLOUD_CURL_BACKEND_H */
