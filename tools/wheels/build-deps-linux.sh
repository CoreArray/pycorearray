#!/bin/sh
# Build the libraries of the Linux wheels in the manylinux container
# (cibuildwheel before-all); auditwheel then copies them into the wheels.
#
# liblzma comes from the system. libcurl (HTTP and HTTPS only) and OpenSSL are
# built from source: the system libcurl 7.61 depends on about 20 other
# libraries, and a current libcurl needs OpenSSL 3. The libcurl has no CA
# bundle built in: pycorearray.cloud uses the one of the Linux distribution
# where the wheel is installed.

set -e

OPENSSL_VERSION=3.5.9
OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
CURL_VERSION=8.23.0
CURL_SHA256=e3de77cfa6367083c776a26647fc9f633b538c06c3baa73d931a252ac8f72b49

PREFIX=/usr/local
NPROC=$(nproc)

# the CFLAGS of the wheels (pyproject.toml) would replace the default flags of
# OpenSSL, including -O3
unset CFLAGS CXXFLAGS

dnf install -y xz-devel perl-IPC-Cmd perl-Time-Piece

cd /tmp

# OpenSSL (an LTS version)
curl -fsSLO https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz
echo "$OPENSSL_SHA256  openssl-$OPENSSL_VERSION.tar.gz" | sha256sum -c -
tar xzf openssl-$OPENSSL_VERSION.tar.gz
cd openssl-$OPENSSL_VERSION
# the configuration directory does not exist, so that no openssl.cnf of the
# system is loaded
./Configure --prefix=$PREFIX --libdir=lib --openssldir=$PREFIX/ssl \
	shared no-tests no-docs
make -j$NPROC
make install_sw
cd ..

# libcurl
curl -fsSLO https://github.com/curl/curl/releases/download/curl-$(echo $CURL_VERSION | tr . _)/curl-$CURL_VERSION.tar.gz
echo "$CURL_SHA256  curl-$CURL_VERSION.tar.gz" | sha256sum -c -
tar xzf curl-$CURL_VERSION.tar.gz
cd curl-$CURL_VERSION
./configure --prefix=$PREFIX --enable-shared --disable-static \
	--with-openssl=$PREFIX --without-ca-bundle --without-ca-path \
	--with-ca-fallback \
	--without-zlib --without-brotli --without-zstd --without-nghttp2 \
	--without-libidn2 --without-libpsl --without-libssh2 \
	--disable-ldap --disable-ldaps --disable-rtsp --disable-dict \
	--disable-telnet --disable-tftp --disable-pop3 --disable-imap \
	--disable-smtp --disable-gopher --disable-mqtt --disable-ftp \
	--disable-file --disable-ipfs --disable-websockets --disable-docs \
	--disable-manual
make -j$NPROC
make install
cd ..

ldconfig
curl-config --version
curl-config --protocols
