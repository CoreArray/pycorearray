// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// azure_backend.c: Azure Blob Storage provider for the generic curl
//     backend. Authentication: SAS token, OAuth2 bearer token (Microsoft
//     Entra ID / managed identity) or Shared Key; anonymous access for
//     public containers. Sovereign clouds via `endpoint_suffix`, the
//     Azurite emulator via a full `endpoint`.
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

// Portable OpenSSL helpers (HMAC-SHA256)
#include "openssl_compat.h"

#define AZURE_API_VERSION   "2020-10-02"


// =====================================================================
// Provider state
// =====================================================================

typedef struct AzureProviderData {
	char account_name[256];
	unsigned char account_key[512];          // decoded Shared Key bytes
	size_t account_key_len;                  // 0 when no key is configured
	char sas_token[CLOUD_MAX_CRED_LEN];
	char access_token[CLOUD_MAX_CRED_LEN];   // OAuth2 bearer token
	char canonical_resource[CLOUD_MAX_URL_LEN + 1024];  // "/account/[prefix/]container/blob"
	char endpoint[CLOUD_MAX_ENDPOINT_LEN];   // blob URL without the SAS token
} AzureProviderData;


// =====================================================================
// Shared Key signature (Blob service, version 2009-09-19 and later)
// =====================================================================

static int azure_sign_request(const AzureProviderData *az,
	const char *range_value, const char *date_str,
	char *auth_header, size_t auth_size)
{
	char string_to_sign[CLOUD_MAX_URL_LEN + 2048];
	snprintf(string_to_sign, sizeof(string_to_sign),
		"GET\n"   // method
		"\n"      // Content-Encoding
		"\n"      // Content-Language
		"\n"      // Content-Length
		"\n"      // Content-MD5
		"\n"      // Content-Type
		"\n"      // Date
		"\n"      // If-Modified-Since
		"\n"      // If-Match
		"\n"      // If-None-Match
		"\n"      // If-Unmodified-Since
		"%s\n"    // Range
		"x-ms-date:%s\n"
		"x-ms-version:" AZURE_API_VERSION "\n"
		"%s",     // canonicalized resource
		range_value, date_str, az->canonical_resource);

	unsigned char sig[32];
	hmac_sha256(az->account_key, az->account_key_len,
		(const unsigned char *)string_to_sign, strlen(string_to_sign), sig);

	char sig_b64[64];
	if (cloud_base64_encode(sig, 32, sig_b64, sizeof(sig_b64)) != 0)
		return -1;
	snprintf(auth_header, auth_size, "Authorization: SharedKey %s:%s",
		az->account_name, sig_b64);
	return 0;
}


// =====================================================================
// Provider vtable implementation
// =====================================================================

static int azure_prepare(void *pd, const char *range_value, time_t now,
	CurlRequest *req, char *err, size_t err_size)
{
	AzureProviderData *az = (AzureProviderData *)pd;

	if (az->sas_token[0])
	{
		// SAS: the token travels in the query string
		const char *tok = az->sas_token;
		if (*tok == '?') tok++;
		char sep = strchr(az->endpoint, '?') ? '&' : '?';
		snprintf(req->url, sizeof(req->url), "%s%c%s", az->endpoint, sep, tok);
		return 0;
	}

	snprintf(req->url, sizeof(req->url), "%s", az->endpoint);
	if (az->access_token[0])
	{
		// OAuth2 (Entra ID); x-ms-version is mandatory with bearer tokens
		char auth_hdr[CLOUD_MAX_CRED_LEN + 32];
		snprintf(auth_hdr, sizeof(auth_hdr), "Authorization: Bearer %s",
			az->access_token);
		curl_request_add_header(req, auth_hdr);
		curl_request_add_header(req, "x-ms-version: " AZURE_API_VERSION);
		return 0;
	}
	if (az->account_key_len > 0)
	{
		struct tm utc;
		cloud_utc_time(now, &utc);
		char date_str[64];
		strftime(date_str, sizeof(date_str), "%a, %d %b %Y %H:%M:%S GMT", &utc);

		char auth_hdr[1024], date_hdr[128];
		if (azure_sign_request(az, range_value, date_str,
			auth_hdr, sizeof(auth_hdr)) != 0)
		{
			snprintf(err, err_size, "failed to sign the request");
			return -1;
		}
		snprintf(date_hdr, sizeof(date_hdr), "x-ms-date: %s", date_str);
		curl_request_add_header(req, auth_hdr);
		curl_request_add_header(req, date_hdr);
		curl_request_add_header(req, "x-ms-version: " AZURE_API_VERSION);
	}
	// otherwise: anonymous access (public container)
	return 0;
}

static void azure_error_hint(void *pd, const CurlResponseInfo *info,
	char *err, size_t err_size)
{
	AzureProviderData *az = (AzureProviderData *)pd;
	size_t cur = strlen(err);
	if (cur + 1 >= err_size) return;
	if (info->http_code == 401 && az->access_token[0])
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: OAuth2 access tokens expire after about an hour; obtain "
			"a fresh token (e.g. `az account get-access-token --resource "
			"https://storage.azure.com/`) and call gdsCloudConfigAzure() again]");
	}
	else if ((info->http_code == 401 || info->http_code == 403 ||
		info->http_code == 404) &&
		!az->sas_token[0] && !az->access_token[0] && az->account_key_len == 0)
	{
		snprintf(err + cur, err_size - cur,
			" [Hint: no credentials are configured, so the request was sent "
			"anonymously; see ?gdsCloudConfigAzure]");
	}
}

static const char *azure_endpoint(void *pd)
{
	return ((AzureProviderData *)pd)->endpoint;
}

static void azure_free(void *pd)
{
	AzureProviderData *az = (AzureProviderData *)pd;
	if (!az) return;
	memset(az->account_key, 0, sizeof(az->account_key));
	memset(az->sas_token, 0, sizeof(az->sas_token));
	memset(az->access_token, 0, sizeof(az->access_token));
	free(az);
}

const CurlProvider azure_provider = {
	.name       = "Azure",
	.prepare    = azure_prepare,
	.error_hint = azure_error_hint,
	.endpoint   = azure_endpoint,
	.free_data  = azure_free
};


// =====================================================================
// Construction
//
//   url:             az://container/blob
//   endpoint_suffix: DNS suffix of the blob service, default
//                    "blob.core.windows.net" (e.g. "blob.core.chinacloudapi.cn")
//   endpoint:        optional full base URL such as
//                    "http://127.0.0.1:10000/devstoreaccount1" (Azurite);
//                    takes precedence over endpoint_suffix
//
// The blob URL is <base>/container/blob and the Shared Key canonicalized
// resource is "/" + account + <path of base> + "/container/blob".
// Returns NULL with a message in `err` on invalid input.
// =====================================================================

void *azure_provider_create(const char *url, const char *account_name,
	const char *account_key, const char *sas_token, const char *access_token,
	const char *endpoint_suffix, const char *endpoint,
	char *err, size_t err_size)
{
	err[0] = '\0';
	if (strncmp(url, "az://", 5) != 0)
	{
		snprintf(err, err_size, "must start with 'az://'");
		return NULL;
	}
	const char *rest = url + 5;
	const char *slash = strchr(rest, '/');
	if (!slash || slash == rest || !slash[1])
	{
		snprintf(err, err_size,
			"missing blob name (expected 'az://container/blob')");
		return NULL;
	}
	if (!account_name || !account_name[0])
	{
		snprintf(err, err_size, "the Azure storage account name is required "
			"(see ?gdsCloudConfigAzure or the AZURE_STORAGE_ACCOUNT "
			"environment variable)");
		return NULL;
	}
	char container[256];
	size_t container_len = (size_t)(slash - rest);
	if (container_len >= sizeof(container))
	{
		snprintf(err, err_size, "container name is too long");
		return NULL;
	}
	memcpy(container, rest, container_len);
	container[container_len] = '\0';

	AzureProviderData *az = (AzureProviderData *)calloc(1, sizeof(AzureProviderData));
	if (!az) return NULL;

	snprintf(az->account_name, sizeof(az->account_name), "%s", account_name);
	if (sas_token && sas_token[0])
		snprintf(az->sas_token, sizeof(az->sas_token), "%s", sas_token);
	if (access_token && access_token[0])
		snprintf(az->access_token, sizeof(az->access_token), "%s", access_token);
	if (account_key && account_key[0])
	{
		// decode the base64 Shared Key once; real keys are 64 bytes
		long n = cloud_base64_decode(account_key, az->account_key,
			sizeof(az->account_key));
		if (n <= 0)
		{
			snprintf(err, err_size, "the Azure storage account key is not "
				"valid base64 (or is too long)");
			azure_free(az);
			return NULL;
		}
		az->account_key_len = (size_t)n;
	}

	// where the blob service lives
	char base[CLOUD_MAX_ENDPOINT_LEN], base_path[CLOUD_MAX_URL_LEN];
	base_path[0] = '\0';
	if (endpoint && endpoint[0])
	{
		char scheme[8], host[1024];
		if (cloud_split_endpoint(endpoint, scheme, sizeof(scheme),
			host, sizeof(host), base_path, sizeof(base_path)) != 0)
		{
			snprintf(err, err_size, "invalid Azure endpoint '%s' (expected "
				"'https://host[:port][/prefix]')", endpoint);
			azure_free(az);
			return NULL;
		}
		snprintf(base, sizeof(base), "%s://%s%s", scheme, host, base_path);
	} else {
		snprintf(base, sizeof(base), "https://%s.%s", account_name,
			(endpoint_suffix && endpoint_suffix[0]) ? endpoint_suffix
				: "blob.core.windows.net");
	}

	char encoded_blob[CLOUD_MAX_URL_LEN];
	cloud_url_encode_path(slash + 1, encoded_blob, sizeof(encoded_blob));
	int n_url = snprintf(az->endpoint, sizeof(az->endpoint), "%s/%s/%s",
		base, container, encoded_blob);
	int n_res = snprintf(az->canonical_resource, sizeof(az->canonical_resource),
		"/%s%s/%s/%s", account_name, base_path, container, encoded_blob);
	if (n_url < 0 || (size_t)n_url >= sizeof(az->endpoint) ||
		n_res < 0 || (size_t)n_res >= sizeof(az->canonical_resource))
	{
		snprintf(err, err_size, "the resolved request URL is too long");
		azure_free(az);
		return NULL;
	}
	return az;
}
