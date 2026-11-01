import os
import shutil
import subprocess
import sys
import sysconfig

from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext
import numpy


class build_ext_cppstd(build_ext):
	"""Apply the C++ standard flag to C++ translation units only.

	CoreArray ships one C source (dParallel_Ext.c); clang rejects -std=c++NN
	on C files, so the flag must not be passed globally.
	"""
	CXX_STD = '-std=c++11'

	def build_extension(self, ext):
		orig_compile = self.compiler._compile

		def _compile(obj, src, ext_, cc_args, extra_postargs, pp_opts):
			postargs = list(extra_postargs)
			if src.endswith(('.cpp', '.cxx', '.cc', '.C')):
				postargs = [self.CXX_STD] + postargs
			return orig_compile(obj, src, ext_, cc_args, postargs, pp_opts)

		self.compiler._compile = _compile
		try:
			super().build_extension(ext)
		finally:
			self.compiler._compile = orig_compile


corearray_fnlst = [ os.path.join('src', 'CoreArray', fn) for fn in [
	'CoreArray.cpp',
	'dAllocator.cpp',
	'dAny.cpp',
	'dBase.cpp',
	'dBitGDS.cpp',
	'dEndian.cpp',
	'dFile.cpp',
	'dParallel.cpp',
	'dParallel_Ext.c',
	'dPlatform.cpp',
	'dRealGDS.cpp',
	'dRleGDS.cpp',
	'dSerial.cpp',
	'dSparse.cpp',
	'dStrGDS.cpp',
	'dStream.cpp',
	'dStruct.cpp',
	'dVLIntGDS.cpp' ] ]

zlib_fnlst = [ os.path.join('src', 'ZLIB', fn) for fn in [
	'adler32.c',
	'compress.c',
	'crc32.c',
	'deflate.c',
	'infback.c',
	'inffast.c',
	'inflate.c',
	'inftrees.c',
	'trees.c',
	'uncompr.c',
	'zutil.c' ] ]

lz4_fnlst = [ os.path.join('src', 'LZ4', fn) for fn in [
	'lz4.c',
	'lz4hc.c',
	'lz4frame.c',
	'xxhash.c' ] ]

pycorearray_fnlst = [ os.path.join('src', fn) for fn in [
	'PyCoreArray.cpp',
	'PyCloud.cpp',
	'ccall.cpp' ] ]

# cloud access: src/cloud/ is copied unchanged from src/ of the gdscloud R
# package (the upstream), these files plus cloud_stream.h, curl_backend.h and
# openssl_compat.h
cloud_fnlst = [ os.path.join('src', 'cloud', fn) for fn in [
	'cloud_stream.c',
	'curl_backend.c',
	'http_backend.c',
	's3_backend.c',
	'gcs_backend.c',
	'azure_backend.c' ] ]


# ----------------------------------------------------------------------------
# Locate liblzma (xz). Honour an explicit prefix, otherwise probe the usual
# Homebrew / system locations so the extension links against a real liblzma.
# ----------------------------------------------------------------------------
def _lzma_dirs():
	inc, lib = [], []
	prefix = os.environ.get('LZMA_PREFIX') or os.environ.get('XZ_PREFIX')
	candidates = []
	if prefix:
		candidates.append(prefix)
	candidates += [
		'/opt/homebrew/opt/xz',   # Apple Silicon Homebrew
		'/usr/local/opt/xz',      # Intel Homebrew
		'/opt/homebrew',
		'/usr/local',
		'/usr',
	]
	for c in candidates:
		i, l = os.path.join(c, 'include'), os.path.join(c, 'lib')
		if os.path.exists(os.path.join(i, 'lzma.h')):
			inc.append(i)
			lib.append(l)
			break
	return inc, lib


lzma_inc, lzma_lib = _lzma_dirs()


# ----------------------------------------------------------------------------
# Cloud access needs libcurl, and OpenSSL libcrypto for request signing except
# on macOS (which uses CommonCrypto). Without them, or if the environment
# variable PYCOREARRAY_NO_CLOUD is set, the extension is built without cloud
# access.
# ----------------------------------------------------------------------------
def _flags(cmd):
	try:
		return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().split()
	except (OSError, subprocess.CalledProcessError):
		return None

def _cloud_flags():
	if os.environ.get('PYCOREARRAY_NO_CLOUD'):
		return None, 'PYCOREARRAY_NO_CLOUD is set'
	# libcurl: prefer the system curl-config on macOS
	if sys.platform == 'darwin' and os.path.exists('/usr/bin/curl-config'):
		curl_config = '/usr/bin/curl-config'
	else:
		curl_config = shutil.which('curl-config')
	cflags = libs = None
	if curl_config:
		cflags, libs = _flags([curl_config, '--cflags']), _flags([curl_config, '--libs'])
	if libs is None:
		cflags = _flags(['pkg-config', '--cflags', 'libcurl'])
		libs = _flags(['pkg-config', '--libs', 'libcurl'])
	if libs is None:
		return None, 'libcurl not found'
	cflags = cflags or []
	if sys.platform == 'darwin':
		# a non-system libcurl (e.g., Homebrew) must be found at runtime
		libs += [ '-Wl,-rpath,' + f[2:] for f in libs if f.startswith('-L') ]
	else:
		prefix = os.environ.get('OPENSSL_PREFIX')
		if prefix:
			cflags += [ '-I' + os.path.join(prefix, 'include') ]
			libs += [ '-L' + os.path.join(prefix, 'lib'), '-lcrypto' ]
		else:
			ssl_libs = _flags(['pkg-config', '--libs', 'libcrypto'])
			if ssl_libs is not None:
				cflags += _flags(['pkg-config', '--cflags', 'libcrypto']) or []
				libs += ssl_libs
			elif any(os.path.exists(os.path.join(d, 'openssl', 'evp.h'))
					for d in ('/usr/include', '/usr/local/include')):
				libs += [ '-lcrypto' ]
			else:
				return None, 'OpenSSL (libcrypto) not found'
	return (cflags, libs), None


cloud, why_no_cloud = _cloud_flags()
if cloud is None:
	sys.stderr.write('pycorearray: building without cloud access (%s)\n' % why_no_cloud)

extra_compile_args = ['-O2']
extra_link_args = []
# Make sure the dynamic linker can find a Homebrew liblzma at runtime.
for l in lzma_lib:
	extra_link_args += ['-Wl,-rpath,' + l]

src_fnlst = corearray_fnlst + zlib_fnlst + lz4_fnlst + pycorearray_fnlst
define_cloud = []
if cloud is not None:
	src_fnlst += cloud_fnlst
	define_cloud = [ ('PYCOREARRAY_WITH_CLOUD', None) ]
	extra_compile_args += cloud[0]
	extra_link_args += cloud[1]

ext = Extension(
	'pycorearray.ccall',
	src_fnlst,
	include_dirs = [ 'pycorearray/include', 'src/CoreArray', numpy.get_include() ] + lzma_inc,
	library_dirs = lzma_lib,
	define_macros = define_cloud + [
		('USING_PYTHON', None),
		('_FILE_OFFSET_BITS', 64),
		# XZ/LZMA via the system liblzma (external-lib mode); LZ4 bundled in src/LZ4
		('COREARRAY_USE_LZMA_EXT', None),
		('NPY_NO_DEPRECATED_API', 'NPY_1_7_API_VERSION'),
		# oldest NumPy the binary runs on; keep in sync with pyproject.toml
		('NPY_TARGET_VERSION', 'NPY_1_25_API_VERSION'),
	],
	libraries = [ 'lzma' ],
	extra_compile_args = extra_compile_args,
	extra_link_args = extra_link_args,
)

setup(ext_modules=[ext], cmdclass={'build_ext': build_ext_cppstd})
