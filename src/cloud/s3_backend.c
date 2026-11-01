// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// s3_backend.c: Amazon S3 provider (AWS Signature Version 4) for the
//     generic curl backend. A custom endpoint serves any S3-compatible
//     service (MinIO, Ceph RGW, Cloudflare R2, Wasabi, Backblaze B2,
//     DigitalOcean Spaces, ...).
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
#include <time.h>

// Portable OpenSSL helpers (SHA-256 + HMAC-SHA256)
#include "openssl_compat.h"


// =====================================================================
// Provider state
// =====================================================================

typedef struct S3ProviderData {
	char access_key[CLOUD_MAX_CRED_LEN];
	char secret_key[CLOUD_MAX_CRED_LEN];
	char session_token[CLOUD_MAX_CRED_LEN];
	char region[128];
	char host[1024];                         // Host header value (host[:port])
	char canonical_uri[CLOUD_MAX_URL_LEN + 1024]; // percent-encoded request path
	char endpoint[CLOUD_MAX_ENDPOINT_LEN];   // full request URL
} S3ProviderData;


// =====================================================================
// Helpers
// =====================================================================

static void hex_encode(const unsigned char *in, size_t len, char *out)
{
	static const char hex[] = "0123456789abcdef";
	for (size_t i = 0; i < len; i++)
	{
		out[i*2]   = hex[(in[i] >> 4) & 0x0f];
		out[i*2+1] = hex[in[i] & 0x0f];
	}
	out[len*2] = '\0';
}

// SHA-256 of the empty string (payload hash of a GET request)
static const char *EMPTY_PAYLOAD_HASH =
	"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";


// =====================================================================
// AWS Signature Version 4 for a GET request with a Range header
// =====================================================================

static int aws_sigv4_sign(const S3ProviderData *s3, const char *range_value,
	time_t now, char *auth_header, size_t auth_size,
	char *date_header, size_t date_size,
	char *token_header, size_t token_size)
{
	struct tm utc;
	cloud_utc_time(now, &utc);
	char datestamp[16], amzdate[32];
	strftime(datestamp, sizeof(datestamp), "%Y%m%d", &utc);
	strftime(amzdate, sizeof(amzdate), "%Y%m%dT%H%M%SZ", &utc);

	snprintf(date_header, date_size, "x-amz-date: %s", amzdate);
	token_header[0] = '\0';
	if (s3->session_token[0])
	{
		snprintf(token_header, token_size, "x-amz-security-token: %s",
			s3->session_token);
	}

	// canonical headers (sorted by name) and the signed-header list
	char canonical_headers[4096];
	const char *signed_headers;
	if (s3->session_token[0])
	{
		snprintf(canonical_headers, sizeof(canonical_headers),
			"host:%s\nrange:%s\nx-amz-content-sha256:%s\n"
			"x-amz-date:%s\nx-amz-security-token:%s\n",
			s3->host, range_value, EMPTY_PAYLOAD_HASH, amzdate,
			s3->session_token);
		signed_headers =
			"host;range;x-amz-content-sha256;x-amz-date;x-amz-security-token";
	} else {
		snprintf(canonical_headers, sizeof(canonical_headers),
			"host:%s\nrange:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n",
			s3->host, range_value, EMPTY_PAYLOAD_HASH, amzdate);
		signed_headers = "host;range;x-amz-content-sha256;x-amz-date";
	}

	// canonical request: method, URI, query, headers, signed headers, payload
	size_t cr_size = strlen(s3->canonical_uri) + sizeof(canonical_headers) + 512;
	char *canonical_request = (char *)malloc(cr_size);
	if (!canonical_request) return -1;
	snprintf(canonical_request, cr_size, "GET\n%s\n\n%s\n%s\n%s",
		s3->canonical_uri, canonical_headers, signed_headers,
		EMPTY_PAYLOAD_HASH);
	unsigned char cr_hash[32];
	sha256_hash((const unsigned char *)canonical_request,
		strlen(canonical_request), cr_hash);
	free(canonical_request);
	char cr_hash_hex[65];
	hex_encode(cr_hash, 32, cr_hash_hex);

	char scope[256];
	snprintf(scope, sizeof(scope), "%s/%s/s3/aws4_request",
		datestamp, s3->region);
	char string_to_sign[512];
	snprintf(string_to_sign, sizeof(string_to_sign),
		"AWS4-HMAC-SHA256\n%s\n%s\n%s", amzdate, scope, cr_hash_hex);

	// signing key: HMAC chain over date, region, service, "aws4_request"
	char key_buf[CLOUD_MAX_CRED_LEN + 8];
	snprintf(key_buf, sizeof(key_buf), "AWS4%s", s3->secret_key);
	unsigned char k_date[32], k_region[32], k_service[32], k_signing[32];
	hmac_sha256((const unsigned char *)key_buf, strlen(key_buf),
		(const unsigned char *)datestamp, strlen(datestamp), k_date);
	hmac_sha256(k_date, 32, (const unsigned char *)s3->region,
		strlen(s3->region), k_region);
	hmac_sha256(k_region, 32, (const unsigned char *)"s3", 2, k_service);
	hmac_sha256(k_service, 32, (const unsigned char *)"aws4_request", 12,
		k_signing);
	memset(key_buf, 0, sizeof(key_buf));

	unsigned char sig[32];
	hmac_sha256(k_signing, 32, (const unsigned char *)string_to_sign,
		strlen(string_to_sign), sig);
	char sig_hex[65];
	hex_encode(sig, 32, sig_hex);

	snprintf(auth_header, auth_size,
		"Authorization: AWS4-HMAC-SHA256 Credential=%s/%s, "
		"SignedHeaders=%s, Signature=%s",
		s3->access_key, scope, signed_headers, sig_hex);
	return 0;
}


// =====================================================================
// Provider vtable implementation
// =====================================================================

static int s3_prepare(void *pd, const char *range_value, time_t now,
	CurlRequest *req, char *err, size_t err_size)
{
	S3ProviderData *s3 = (S3ProviderData *)pd;
	snprintf(req->url, sizeof(req->url), "%s", s3->endpoint);

	// unsigned (anonymous) request when no credentials are configured
	if (!s3->access_key[0] || !s3->secret_key[0]) return 0;

	char auth_hdr[2048], date_hdr[128], token_hdr[CLOUD_MAX_CRED_LEN + 64];
	if (aws_sigv4_sign(s3, range_value, now, auth_hdr, sizeof(auth_hdr),
		date_hdr, sizeof(date_hdr), token_hdr, sizeof(token_hdr)) != 0)
	{
		snprintf(err, err_size, "failed to sign the request");
		return -1;
	}
	char sha_hdr[128];
	snprintf(sha_hdr, sizeof(sha_hdr), "x-amz-content-sha256: %s",
		EMPTY_PAYLOAD_HASH);
	curl_request_add_header(req, auth_hdr);
	curl_request_add_header(req, date_hdr);
	curl_request_add_header(req, sha_hdr);
	if (token_hdr[0]) curl_request_add_header(req, token_hdr);
	return 0;
}

static void s3_error_hint(void *pd, const CurlResponseInfo *info,
	char *err, size_t err_size)
{
	S3ProviderData *s3 = (S3ProviderData *)pd;
	size_t cur = strlen(err);
	if (cur + 1 >= err_size) return;

	// the most common cause of opaque 403/301 responses: the bucket lives
	// in a different region than the one the request was signed for
	if (info->bucket_region[0] && strcmp(info->bucket_region, s3->region) != 0)
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: bucket region is '%s' but the request was signed for "
			"'%s'; set region='%s' in gdsCloudConfigS3()]",
			info->bucket_region, s3->region, info->bucket_region);
	}
	else if ((info->http_code == 403 || info->http_code == 401) &&
		(!s3->access_key[0] || !s3->secret_key[0]))
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: no credentials are configured, so the request was sent "
			"anonymously; see ?gdsCloudConfigS3]");
	}
}

static const char *s3_endpoint(void *pd)
{
	return ((S3ProviderData *)pd)->endpoint;
}

static void s3_free(void *pd)
{
	S3ProviderData *s3 = (S3ProviderData *)pd;
	if (!s3) return;
	memset(s3->access_key, 0, sizeof(s3->access_key));
	memset(s3->secret_key, 0, sizeof(s3->secret_key));
	memset(s3->session_token, 0, sizeof(s3->session_token));
	free(s3);
}

const CurlProvider s3_provider = {
	.name       = "S3",
	.prepare    = s3_prepare,
	.error_hint = s3_error_hint,
	.endpoint   = s3_endpoint,
	.free_data  = s3_free
};


// =====================================================================
// Construction
//
//   url:        s3://bucket/key
//   endpoint:   optional "https://host[:port][/prefix]" of an
//               S3-compatible service; empty -> the AWS regional endpoint
//               https://bucket.s3.<region>.amazonaws.com
//   path_style: 1 -> https://host/bucket/key, 0 -> https://bucket.host/key,
//               -1 -> path-style for a custom endpoint, virtual-hosted
//               style for AWS
//
// Returns NULL with a message in `err` on invalid input.
// =====================================================================

void *s3_provider_create(const char *url, const char *access_key,
	const char *secret_key, const char *region, const char *session_token,
	const char *endpoint, int path_style, char *err, size_t err_size)
{
	err[0] = '\0';
	if (strncmp(url, "s3://", 5) != 0)
	{
		snprintf(err, err_size, "must start with 's3://'");
		return NULL;
	}
	const char *rest = url + 5;
	const char *slash = strchr(rest, '/');
	if (!slash || slash == rest || !slash[1])
	{
		snprintf(err, err_size, "missing object key (expected 's3://bucket/key')");
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

	// where the service lives
	char scheme[8], host[1024], base_path[CLOUD_MAX_URL_LEN];
	int custom = (endpoint && endpoint[0]);
	if (custom)
	{
		if (cloud_split_endpoint(endpoint, scheme, sizeof(scheme),
			host, sizeof(host), base_path, sizeof(base_path)) != 0)
		{
			snprintf(err, err_size, "invalid S3 endpoint '%s' (expected "
				"'https://host[:port][/prefix]')", endpoint);
			return NULL;
		}
		if (path_style < 0) path_style = 1;
	} else {
		strcpy(scheme, "https");
		snprintf(host, sizeof(host), "s3.%s.amazonaws.com",
			(region && region[0]) ? region : "us-east-1");
		base_path[0] = '\0';
		if (path_style < 0) path_style = 0;
	}

	S3ProviderData *s3 = (S3ProviderData *)calloc(1, sizeof(S3ProviderData));
	if (!s3) return NULL;

	if (access_key && access_key[0])
		snprintf(s3->access_key, sizeof(s3->access_key), "%s", access_key);
	if (secret_key && secret_key[0])
		snprintf(s3->secret_key, sizeof(s3->secret_key), "%s", secret_key);
	if (session_token && session_token[0])
		snprintf(s3->session_token, sizeof(s3->session_token), "%s", session_token);
	snprintf(s3->region, sizeof(s3->region), "%s",
		(region && region[0]) ? region : "us-east-1");

	// the object key is percent-encoded once: this is both the request
	// path and the SigV4 canonical URI
	char encoded_key[CLOUD_MAX_URL_LEN];
	cloud_url_encode_path(slash + 1, encoded_key, sizeof(encoded_key));
	int n_host, n_uri;
	if (path_style)
	{
		n_host = snprintf(s3->host, sizeof(s3->host), "%s", host);
		n_uri = snprintf(s3->canonical_uri, sizeof(s3->canonical_uri),
			"%s/%s/%s", base_path, bucket, encoded_key);
	} else {
		n_host = snprintf(s3->host, sizeof(s3->host), "%s.%s", bucket, host);
		n_uri = snprintf(s3->canonical_uri, sizeof(s3->canonical_uri),
			"%s/%s", base_path, encoded_key);
	}
	int n_url = snprintf(s3->endpoint, sizeof(s3->endpoint), "%s://%s%s",
		scheme, s3->host, s3->canonical_uri);
	if (n_host < 0 || (size_t)n_host >= sizeof(s3->host) ||
		n_uri < 0 || (size_t)n_uri >= sizeof(s3->canonical_uri) ||
		n_url < 0 || (size_t)n_url >= sizeof(s3->endpoint))
	{
		snprintf(err, err_size, "the resolved request URL is too long");
		s3_free(s3);
		return NULL;
	}
	return s3;
}
