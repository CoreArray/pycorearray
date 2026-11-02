#!/bin/sh
# Build a static liblzma for the macOS wheels (cibuildwheel before-all), for
# the deployment target of the wheels: the liblzma of Homebrew is built for
# the macOS version of the build machine. libcurl and CommonCrypto come with
# macOS.
#
# Usage: build-xz-macos.sh PREFIX

set -e

XZ_VERSION=5.8.4
XZ_SHA256=0014c7886930454fe8bd4228665b51af55eeae560ea135c9c4cd33f55b2591d9

PREFIX=$1
export MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-11.0}

cd "${TMPDIR:-/tmp}"
curl -fsSLO https://github.com/tukaani-project/xz/releases/download/v$XZ_VERSION/xz-$XZ_VERSION.tar.gz
echo "$XZ_SHA256  xz-$XZ_VERSION.tar.gz" | shasum -a 256 -c -
tar xzf xz-$XZ_VERSION.tar.gz
cd xz-$XZ_VERSION
./configure --prefix="$PREFIX" --disable-shared --enable-static --with-pic \
	--disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo \
	--disable-lzma-links --disable-scripts --disable-doc --disable-nls
make -j$(sysctl -n hw.ncpu)
make install
