pycorearray: Python Interface to CoreArray Genomic Data Structure (GDS) Files
===

[![CI](https://github.com/CoreArray/pycorearray/actions/workflows/ci.yml/badge.svg)](https://github.com/CoreArray/pycorearray/actions/workflows/ci.yml)

![LGPLv3](http://www.gnu.org/graphics/lgplv3-88x31.png)
[GNU Lesser General Public License, LGPL-3](https://www.gnu.org/licenses/lgpl.html)

v0.2, renamed from `pygds` in Oct 2026 (v0.1 was `pygds`, from 2017)


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

### Open an existing file

`gdsfile.open()` opens a file read-only by default. An example file comes
with the package:

```python
import pycorearray as gds

f = gds.gdsfile()
f.open(gds.get_example_path('ceu_exon.gds'))
f.show()
```

```
File: /.../pycorearray/data/ceu_exon.gds (32.5K)
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

```python
f.index('sample.id').read(start=[0], count=[3])
## array(['NA06984', 'NA06985', 'NA06986'], dtype=object)
f.index('position').read(start=[0], count=[5])
## array([1105366, 1105411, 1110294, 3537996, 3538692], dtype=int32)
g = f.index('genotype/data').read()   # variant x sample x allele
g.shape
## (1348, 90, 2)
f.close()
```


## Cloud Storage

A GDS file on cloud storage can be opened directly, read-only, without
downloading the whole file: only the blocks that are read are fetched.
`http://` and `https://` URLs, Amazon S3 (`s3://`, also S3-compatible
services), Google Cloud Storage (`gs://`) and Azure Blob Storage (`az://`) are
supported. For example, a public file on Amazon S3:

```python
import pycorearray as gds

f = gds.gdsfile()
f.open('s3://gds-stat/download/hapmap/hapmap_r23a.gds')
f.show()
```

```
File: s3://gds-stat/download/hapmap/hapmap_r23a.gds (86.5M)
+    [  ] *
|--+ description   [  ] *
|--+ sample.id   { Str8 270 LZMA_ra(17.3%), 381B } *
|--+ variant.id   { Int32 4098136 LZMA_ra(3.19%), 511.4K } *
|--+ position   { Int32 4098136 LZMA_ra(48.2%), 7.5M } *
|--+ chromosome   { Str8 4098136 LZMA_ra(0.02%), 1.7K } *
|--+ allele   { Str8 4098136 LZMA_ra(12.8%), 2.0M } *
|--+ genotype   [  ] *
|  |--+ data   { Bit2 4098136x270x2 LZMA_ra(12.4%), 65.4M } *
|  |--+ extra.index   { Int32 0x3 LZMA_ra, 18B } *
|  \--+ extra   { Int16 0 LZMA_ra, 18B }
|--+ phase   [  ]
|  |--+ data   { Bit1 4098136x270 LZMA_ra(0.01%), 19.8K } *
|  |--+ extra.index   { Int32 0x3 LZMA_ra, 18B } *
|  \--+ extra   { Bit1 0 LZMA_ra, 18B }
|--+ annotation   [  ]
|  |--+ id   { Str8 4098136 LZMA_ra(27.7%), 11.1M } *
|  |--+ qual   { Float32 4098136 LZMA_ra(0.02%), 2.5K } *
|  |--+ filter   { Int32,factor 4098136 LZMA_ra(0.02%), 2.5K } *
|  |--+ info   [  ]
|  \--+ format   [  ]
\--+ sample.annotation   [  ]
   |--+ family   { Str8 270 LZMA_ra(17.3%), 381B } *
   |--+ father   { Str8 270 LZMA_ra(28.2%), 261B } *
   |--+ mother   { Str8 270 LZMA_ra(28.2%), 261B } *
   |--+ sex   { Str8 270 LZMA_ra(27.0%), 153B } *
   \--+ phenotype   { Int32 270 LZMA_ra(8.70%), 101B } *
```

```python
f.index('position').read(start=[1000], count=[5])
## array([2409958, 2410723, 2410773, 2412474, 2413426], dtype=int32)
f.close()
```

Credentials for private files are taken from the usual environment variables
of each service, or set with `pycorearray.cloud`; see `help(pycorearray.cloud)`.
