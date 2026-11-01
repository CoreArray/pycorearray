// ===========================================================
// gdscloud: Cloud Storage Access for GDS Files
//
// openssl_compat.h: Portable SHA-256 and HMAC-SHA256 for request signing,
//     using the crypto library of the platform:
//       macOS:   CommonCrypto (part of the system)
//       Windows: BCrypt, Windows CNG (part of the system)
//       others:  OpenSSL libcrypto, 1.x (legacy API) or 3.x (EVP API)
//     Define GDSCLOUD_USE_OPENSSL to use OpenSSL on all platforms.
//
// Copyright (C) 2026    Xiuwen Zheng
//
// This file is part of gdscloud.
// LGPL-3 License
// ===========================================================

#ifndef GDSCLOUD_OPENSSL_COMPAT_H
#define GDSCLOUD_OPENSSL_COMPAT_H

#if defined(GDSCLOUD_USE_OPENSSL)
#   define GDSCLOUD_CRYPTO_OPENSSL
#elif defined(__APPLE__)
#   define GDSCLOUD_CRYPTO_COMMONCRYPTO
#elif defined(_WIN32)
#   define GDSCLOUD_CRYPTO_BCRYPT
#else
#   define GDSCLOUD_CRYPTO_OPENSSL
#endif

#include <string.h>

// the largest block passed at once to CommonCrypto or BCrypt, whose data
// lengths are 32-bit
#ifndef GDSCLOUD_HASH_CHUNK
#   define GDSCLOUD_HASH_CHUNK    ((size_t)1 << 30)
#endif


#if defined(GDSCLOUD_CRYPTO_COMMONCRYPTO)

// =====================================================================
// macOS: CommonCrypto
// =====================================================================

#include <CommonCrypto/CommonDigest.h>
#include <CommonCrypto/CommonHMAC.h>

static inline void sha256_hash(const unsigned char *data, size_t len,
	unsigned char *out)
{
	CC_SHA256_CTX ctx;
	CC_SHA256_Init(&ctx);
	while (len > 0)
	{
		size_t n = (len < GDSCLOUD_HASH_CHUNK) ? len : GDSCLOUD_HASH_CHUNK;
		CC_SHA256_Update(&ctx, data, (CC_LONG)n);
		data += n; len -= n;
	}
	CC_SHA256_Final(out, &ctx);
}

static inline void hmac_sha256(const unsigned char *key, size_t key_len,
	const unsigned char *data, size_t data_len, unsigned char *out)
{
	CCHmac(kCCHmacAlgSHA256, key, key_len, data, data_len, out);
}


#elif defined(GDSCLOUD_CRYPTO_BCRYPT)

// =====================================================================
// Windows: BCrypt (Windows CNG)
// =====================================================================

#include <windows.h>
#include <bcrypt.h>

// SHA-256 of 'data', or HMAC-SHA256 if 'key' is not NULL; the output is all
// zeros if a BCrypt call fails, so that the server rejects the request
static inline void gdscloud_bcrypt_sha256(const unsigned char *key,
	size_t key_len, const unsigned char *data, size_t len, unsigned char *out)
{
	BCRYPT_ALG_HANDLE alg = NULL;
	BCRYPT_HASH_HANDLE hash = NULL;
	int ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg,
		BCRYPT_SHA256_ALGORITHM, NULL, key ? BCRYPT_ALG_HANDLE_HMAC_FLAG : 0));
	if (ok)
	{
		ok = BCRYPT_SUCCESS(BCryptCreateHash(alg, &hash, NULL, 0,
			(PUCHAR)key, (ULONG)key_len, 0));
	}
	while (ok && len > 0)
	{
		size_t n = (len < GDSCLOUD_HASH_CHUNK) ? len : GDSCLOUD_HASH_CHUNK;
		ok = BCRYPT_SUCCESS(BCryptHashData(hash, (PUCHAR)data, (ULONG)n, 0));
		data += n; len -= n;
	}
	if (ok)
		ok = BCRYPT_SUCCESS(BCryptFinishHash(hash, out, 32, 0));
	if (hash) BCryptDestroyHash(hash);
	if (alg) BCryptCloseAlgorithmProvider(alg, 0);
	if (!ok) memset(out, 0, 32);
}

static inline void sha256_hash(const unsigned char *data, size_t len,
	unsigned char *out)
{
	gdscloud_bcrypt_sha256(NULL, 0, data, len, out);
}

static inline void hmac_sha256(const unsigned char *key, size_t key_len,
	const unsigned char *data, size_t data_len, unsigned char *out)
{
	// a non-NULL key selects HMAC, also for an empty key
	static const unsigned char empty = 0;
	gdscloud_bcrypt_sha256(key ? key : &empty, key_len, data, data_len, out);
}


#else

// =====================================================================
// OpenSSL libcrypto
// =====================================================================

#include <openssl/opensslv.h>
#include <openssl/evp.h>

#if OPENSSL_VERSION_NUMBER >= 0x30000000L
#include <openssl/params.h>
#include <openssl/core_names.h>
#else
#include <openssl/hmac.h>
#endif

// Portable SHA-256 hash (one-shot)
// EVP_Digest() is available since OpenSSL 0.9.7 and NOT deprecated in 3.0
static inline void sha256_hash(const unsigned char *data, size_t len,
	unsigned char *out)
{
	unsigned int md_len = 32;
	EVP_Digest(data, len, out, &md_len, EVP_sha256(), NULL);
}

// Portable HMAC-SHA256
// OpenSSL 3.0+: EVP_MAC API
// OpenSSL < 3.0: legacy HMAC() one-shot
static inline void hmac_sha256(const unsigned char *key, size_t key_len,
	const unsigned char *data, size_t data_len, unsigned char *out)
{
#if OPENSSL_VERSION_NUMBER >= 0x30000000L
	EVP_MAC *mac = EVP_MAC_fetch(NULL, "HMAC", NULL);
	EVP_MAC_CTX *ctx = EVP_MAC_CTX_new(mac);
	OSSL_PARAM params[2];
	params[0] = OSSL_PARAM_construct_utf8_string(
		OSSL_MAC_PARAM_DIGEST, "SHA256", 0);
	params[1] = OSSL_PARAM_construct_end();
	EVP_MAC_init(ctx, key, key_len, params);
	EVP_MAC_update(ctx, data, data_len);
	size_t out_len = 32;
	EVP_MAC_final(ctx, out, &out_len, 32);
	EVP_MAC_CTX_free(ctx);
	EVP_MAC_free(mac);
#else
	unsigned int len = 32;
	HMAC(EVP_sha256(), key, (int)key_len, data, data_len, out, &len);
#endif
}

#endif

#endif /* GDSCLOUD_OPENSSL_COMPAT_H */
