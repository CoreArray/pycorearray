pycorearray: Python Interface to CoreArray Genomic Data Structure (GDS) Files
===

[![CI](https://github.com/CoreArray/pycorearray/actions/workflows/ci.yml/badge.svg)](https://github.com/CoreArray/pycorearray/actions/workflows/ci.yml)

![GPLv3](http://www.gnu.org/graphics/gplv3-88x31.png)
[GNU General Public License, GPLv3](http://www.gnu.org/copyleft/gpl.html) (2017)

pre-release version: v0.2

> **Renamed from `pygds`:** the name `pygds` on PyPI belongs to an unrelated
> project, so both the package name and the import name are now `pycorearray`.
> Existing code only needs `import pygds` replaced with `import pycorearray`.


## Features

This package provides a high-level Python interface to CoreArray Genomic Data Structure (GDS) data files, which are portable across platforms with hierarchical structure to store multiple scalable array-oriented data sets with metadata information. It is suited for large-scale datasets, especially for data which are much larger than the available random-access memory. The pycorearray package offers the efficient operations specifically designed for integers of less than 8 bits, since a diploid genotype, like single-nucleotide polymorphism (SNP), usually occupies fewer bits than a byte. Data compression and decompression are available with relatively efficient random access. GDS files on cloud storage (HTTP/HTTPS, Amazon S3, Google Cloud Storage, Azure Blob Storage) can be opened directly, read-only, without downloading the whole file.


## Package Maintainer

Dr. Xiuwen Zheng ([zhengxwen@gmail.com](zhengxwen@gmail.com))


## Prerequisites

Python 3.10 or later

NumPy 1.25 or later

liblzma in [xz](https://tukaani.org/xz/) utilities (for XZ/LZMA compression).
The bundled CoreArray sources also build in ZLIB and LZ4, so the supported
compression methods are: `ZIP`/`ZIP_RA` (zlib), `LZMA`/`LZMA_RA` (xz), and
`LZ4`/`LZ4_RA`.

On macOS with Homebrew, install xz with `brew install xz`; the build probes
`/opt/homebrew/opt/xz` and `/usr/local/opt/xz` automatically, or set the
`LZMA_PREFIX` environment variable to point at a custom prefix.

For [cloud storage access](#cloud-storage): libcurl, and on Linux also
OpenSSL (libcrypto) to sign S3 and Azure requests; macOS uses its system
libcurl and CommonCrypto. On Debian/Ubuntu: `sudo apt-get install
libcurl4-openssl-dev libssl-dev`. Without them, or with the environment
variable `PYCOREARRAY_NO_CLOUD=1`, pycorearray is built without cloud access.


## Installation

```sh
pip install git+https://github.com/CoreArray/pycorearray.git
```

Or, from a checkout, build the extension in place:

```sh
python3 setup.py build_ext --inplace
python3 -m pytest tests/   # run the tests and the examples in the docstrings
```


## Citation

#### Original papers (implemented in R/Bioconductor):

[gdsfmt](http://bioconductor.org/packages/gdsfmt), [SeqArray](http://bioconductor.org/packages/SeqArray)

Zheng X, Levine D, Shen J, Gogarten SM, Laurie C, Weir BS (2012). A High-performance Computing Toolset for Relatedness and Principal Component Analysis of SNP Data. *Bioinformatics*. [DOI: 10.1093/bioinformatics/bts606](http://dx.doi.org/10.1093/bioinformatics/bts606).

Zheng X, Gogarten S, Lawrence M, Stilp A, Conomos M, Weir BS, Laurie C, Levine D (2017). SeqArray -- A storage-efficient high-performance data format for WGS variant calls. *Bioinformatics*. [DOI: 10.1093/bioinformatics/btx145](http://dx.doi.org/10.1093/bioinformatics/btx145).



## Examples

```python
import pycorearray as gds

fn = gds.get_example_path('ceu_exon.gds')
f = gds.gdsfile()
f.open(fn)
f.show()
f.close()
```

```
File: pycorearray/data/ceu_exon.gds (32.5K)
+    [  ] *
|--+ description   [  ] *
|--+ sample.id   { Str8 90 LZMA_ra(35.8%), 258B } *
|--+ variant.id   { Int32 1348 LZMA_ra(16.8%), 906B } *
|--+ position   { Int32 1348 LZMA_ra(64.6%), 3.4K } *
|--+ chromosome   { Str8 1348 LZMA_ra(4.63%), 158B } *
|--+ allele   { Str8 1348 LZMA_ra(16.7%), 902B } *
|--+ genotype   [  ] *
|  |--+ data   { Bit2 1348x90x2 LZMA_ra(26.3%), 15.6K } *
|  |--+ extra.index   { Int32 0x3 LZMA_ra, 19B } *
|  \--+ extra   { Int16 0 LZMA_ra, 19B }
|--+ phase   [  ]
|  |--+ data   { Bit1 1348x90 LZMA_ra(0.91%), 138B } *
|  |--+ extra.index   { Int32 0x3 LZMA_ra, 19B } *
|  \--+ extra   { Bit1 0 LZMA_ra, 19B }
|--+ annotation   [  ]
|  |--+ id   { Str8 1348 LZMA_ra(38.4%), 5.5K } *
|  |--+ qual   { Float32 1348 LZMA_ra(2.26%), 122B } *
|  \--+ filter   { Int32,factor 1348 LZMA_ra(2.26%), 122B } *
\--+ sample.annotation   [  ]
   \--+ family   { Str8 90 LZMA_ra(57.1%), 222B }
```



## Cloud Storage

`gdsfile.open()` also opens a GDS file on cloud storage, read-only:
`http://` and `https://` URLs, Amazon S3 (`s3://bucket/key`, and S3-compatible
services such as MinIO, Ceph, Cloudflare R2, Wasabi or Backblaze B2 via a
custom endpoint), Google Cloud Storage (`gs://bucket/key`) and Azure Blob
Storage (`az://container/blob`). Only the blocks that are actually read are
fetched, using HTTP Range requests with an in-memory block cache, so random
access to a remote file works like access to a local one.

```python
import pycorearray as gds

f = gds.gdsfile()
f.open('s3://gds-stat/download/hapmap/hapmap_r23a.gds')   # a public file
f.root().index('position').read(start=[1000], count=[5])
## array([2409958, 2410723, 2410773, 2412474, 2413426], dtype=int32)
f.close()
```

Credentials are taken from, in this order, the settings for the longest
matching URL prefix, the settings of the session, and the usual environment
variables. A token can also be a function returning it, which is called each
time a file is opened, so that expiring tokens are refreshed:

```python
from pycorearray import cloud

cloud.config_s3(aws_access_key_id='...', aws_secret_access_key='...',
    region='us-west-2')
cloud.config_s3(endpoint='https://minio.example.org:9000', url='s3://my-bucket/')
cloud.config_gcs(access_token=lambda: subprocess.check_output(
    ['gcloud', 'auth', 'print-access-token'], text=True).strip())
cloud.config_azure(account_name='myaccount', sas_token='sv=...')
cloud.config_http(bearer_token='...', url='https://private.example.org/')
```

| Service | Environment variables |
|---|---|
| HTTP/HTTPS | `GDSCLOUD_HTTP_TOKEN` |
| Amazon S3 | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_DEFAULT_REGION`, `AWS_ENDPOINT_URL_S3` or `AWS_ENDPOINT_URL`, `GDSCLOUD_S3_PATH_STYLE` |
| Google Cloud Storage | `GCS_ACCESS_TOKEN` |
| Azure Blob Storage | `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_KEY`, `AZURE_STORAGE_SAS_TOKEN`, `AZURE_STORAGE_ACCESS_TOKEN`, `AZURE_STORAGE_ENDPOINT_SUFFIX`, `AZURE_STORAGE_SERVICE_ENDPOINT`, `AZURE_STORAGE_CONNECTION_STRING` |

Without credentials, requests are anonymous. `cloud.options()` sets the
timeouts, the number of retries after a transient failure and the cache size,
whose defaults can also be set by `GDSCLOUD_CONNECT_TIMEOUT`,
`GDSCLOUD_TIMEOUT`, `GDSCLOUD_MAX_RETRIES` and `GDSCLOUD_CACHE_SIZE_MB`.

Child processes started by `fork` inherit the settings; with `spawn` (the
default on macOS and Windows), install them in each worker:

```python
pool = multiprocessing.Pool(4, initializer=cloud.import_credentials,
    initargs=(cloud.export_credentials(),))
```

A read from cloud storage keeps the GIL while it waits for the network, so use
processes rather than threads to read in parallel.

The cloud access uses the C code of the R package
[gdscloud](https://github.com/zhengxwen/gdscloud), with the same environment
variables; the Python functions correspond to the R ones:

| R (gdscloud) | Python (`pycorearray.cloud`) |
|---|---|
| `gdsCloudOpen(url)` | `open(url)`, or `gdsfile().open(url)` |
| `gdsCloudConfigHTTP()`, `gdsCloudConfigS3()`, `gdsCloudConfigGCS()`, `gdsCloudConfigAzure()` | `config_http()`, `config_s3()`, `config_gcs()`, `config_azure()` |
| `gdsCloudOptions()` | `options()` |
| `gdsCloudCacheSize()`, `gdsCloudCacheClear()`, `gdsCloudCacheInfo()` | `cache_size()`, `cache_clear()`, `cache_info()` |
| `gdsCloudList()`, `gdsCloudSchemes()` | `list_streams()`, `schemes()` |
| `gdsCloudExportCredentials(cl)` | `export_credentials()` and `import_credentials()` |



## Also See

[PySeqArray](https://github.com/CoreArray/PySeqArray): data manipulation of whole-genome sequencing variants in Python
