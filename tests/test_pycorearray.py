"""Round-trip and read tests for pycorearray.

Run directly (``python tests/test_pycorearray.py``) or under pytest.
Covers: open/read of the bundled example, create/write/read round-trips for
all common numeric types and string data, every compressor (ZIP / LZMA / LZ4
and their _RA variants), multi-dimensional arrays, sub-region writes, append,
folders, attributes, setdim, exist and delete, and the run-length encoded
("rle.*"), sparse ("sp.*") and unsigned packed real ("packedreal*u") storage
types.
"""

import os
import tempfile

import numpy as np

import pycorearray


# ---------------------------------------------------------------------------

def test_open_example():
	f = pycorearray.gdsfile()
	f.open(pycorearray.get_example_path('ceu_exon.gds'))
	try:
		root = f.root()
		names = root.ls()
		assert 'sample.id' in names and 'genotype' in names
		sid = root.index('sample.id').read()
		assert list(sid[:2]) == ['NA06984', 'NA06985']
		pos = root.index('position').read()
		assert pos.dtype == np.int32 and pos.shape == (1348,)
		geno = root.index('genotype/data').read()
		assert geno.shape == (1348, 90, 2)
		assert int(np.asarray(geno).sum()) == 84331
		# factor decoded to strings
		filt = root.index('annotation/filter').read()
		assert filt[0] == 'PASS'
	finally:
		f.close()


def test_numeric_types():
	fn = os.path.join(tempfile.mkdtemp(), 'num.gds')
	cases = {
		'i8':  np.arange(-5, 5, dtype=np.int8),
		'u8':  np.arange(0, 10, dtype=np.uint8),
		'i16': np.arange(1000, 1010, dtype=np.int16),
		'i32': np.arange(100000, dtype=np.int32),
		'i64': np.arange(5, dtype=np.int64) * 10_000_000_000,
		'f32': (np.arange(20, dtype=np.float32) / 7),
		'f64': np.linspace(-1, 1, 333),
	}
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		for nm, v in cases.items():
			r.add(nm, v)
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		for nm, v in cases.items():
			got = r.index(nm).read()
			assert np.array_equal(got, v), (nm, got[:5], v[:5])
	finally:
		f.close()


def test_compressors():
	fn = os.path.join(tempfile.mkdtemp(), 'comp.gds')
	data = np.tile(np.arange(2000, dtype=np.int32), 3)  # compressible
	methods = ['', 'ZIP', 'ZIP_RA', 'LZMA', 'LZMA_RA', 'LZ4', 'LZ4_RA']
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		for m in methods:
			r.add('c_' + (m or 'none'), data, compress=m)
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		for m in methods:
			got = r.index('c_' + (m or 'none')).read()
			assert np.array_equal(got, data), m
	finally:
		f.close()


def test_multidim_and_subregion():
	fn = os.path.join(tempfile.mkdtemp(), 'md.gds')
	a = np.arange(24, dtype=np.int32).reshape(2, 3, 4)
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		n = r.add('cube', a, compress='LZMA_RA')
		# a sub-region write needs a writable (non-compressed) node
		m = r.add('mat', np.zeros((4, 4), dtype=np.int32))
		m.write(np.array([7, 7], dtype=np.int32), start=[1, 1], count=[1, 2])
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		cube = r.index('cube').read()
		assert cube.shape == (2, 3, 4) and np.array_equal(cube, a)
		mat = r.index('mat').read()
		assert mat[1, 1] == 7 and mat[1, 2] == 7 and mat[0, 0] == 0
	finally:
		f.close()


def test_strings_and_append():
	fn = os.path.join(tempfile.mkdtemp(), 'str.gds')
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		r.add('s', ['alpha', 'beta', 'gamma'], compress='LZ4_RA')
		n = r.add('grow', np.arange(3, dtype=np.int32))
		n.append(np.arange(3, 6, dtype=np.int32))
		n.append(np.arange(6, 9, dtype=np.int32))
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		assert list(r.index('s').read()) == ['alpha', 'beta', 'gamma']
		assert list(r.index('grow').read()) == list(range(9))
	finally:
		f.close()


def test_folders_attrs_exist_delete():
	fn = os.path.join(tempfile.mkdtemp(), 'tree.gds')
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		g = r.addfolder('grp')
		g.add('x', np.arange(4, dtype=np.int32))
		n = r.add('y', np.arange(4, dtype=np.int32))
		n.putattr('unit', 'm')
		n.putattr('k', 7)
		n.putattr('flag', True)
		n.putattr('vec', [1, 2, 3])
		n.putattr('drop', 'gone')
		n.delattr('drop')
		r.add('tmp', np.arange(3, dtype=np.int32))
		r.index('tmp').delete()
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		assert r.exist('grp/x') and not r.exist('grp/zzz')
		assert list(r.index('grp/x').read()) == [0, 1, 2, 3]
		at = r.index('y').getattr()
		assert at['unit'] == 'm' and at['k'] == 7 and at['flag'] is True
		assert at['vec'] == [1, 2, 3] and 'drop' not in at
		assert r.index('tmp', silent=True) is None
	finally:
		f.close()


def test_apply():
	fn = os.path.join(tempfile.mkdtemp(), 'apply.gds')
	mat = np.arange(24, dtype=np.int32).reshape(4, 6)  # 4 rows x 6 cols
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		f.root().add('m', mat)
		f.root().add('v', np.arange(4, dtype=np.int32))
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		m = f.root().index('m')
		# row sums (iterate axis 0): each slice is a length-6 row
		rs = m.apply(0, lambda x: int(x.sum()), as_is='list')
		assert rs == [np.sum(mat[i]) for i in range(4)], rs
		# column sums (iterate axis 1): each slice is a length-4 column
		cs = m.apply(1, lambda x: int(x.sum()), as_is='list')
		assert cs == [int(mat[:, j].sum()) for j in range(6)], cs
		# unlist
		u = m.apply(0, lambda x: x.astype(np.int64) * 0 + 1, as_is='unlist')
		assert u.size == mat.size
		# multi-node lock-step apply over matching margins
		v = f.root().index('v')
		out = pycorearray.apply_gdsn([m, v], [0, 0],
			lambda row, sv: int(row.sum()) + int(sv), as_is='list')
		assert out == [int(mat[i].sum()) + i for i in range(4)], out
	finally:
		f.close()


def test_digest_summarize_diagnosis():
	import hashlib
	fn = os.path.join(tempfile.mkdtemp(), 'dig.gds')
	v = np.arange(1000, dtype=np.int32)
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		f.root().add('v', v, compress='LZMA_RA')
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		n = f.root().index('v')
		md5 = n.digest('md5')
		assert md5 == hashlib.md5(v.tobytes()).hexdigest()
		assert n.digest('sha256') == hashlib.sha256(v.tobytes()).hexdigest()
		s = n.summarize()
		assert s['min'] == 0 and s['max'] == 999 and s['n'] == 1000
		assert abs(s['mean'] - 499.5) < 1e-9
		diag = f.diagnosis()
		assert diag['num_fragment'] >= 1 and diag['size'] > 0
	finally:
		f.close()


def test_moveto_copyto_assign_addfile():
	d = tempfile.mkdtemp()
	fn = os.path.join(d, 'mv.gds')
	# an external file to embed
	ext = os.path.join(d, 'payload.bin')
	payload = bytes(range(256)) * 4
	with open(ext, 'wb') as fh:
		fh.write(payload)

	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		r.add('a', np.arange(3, dtype=np.int32))
		r.add('b', np.arange(3, 6, dtype=np.int32))
		# copy a -> a_copy
		r.copyto('a_copy', r.index('a'))
		# assign: make c an empty node then assign from b
		c = r.add('c', np.array([0], dtype=np.int32))
		c.assign(r.index('b'))
		# move a to before b
		r.index('a').moveto(r.index('b'), 'before')
		# embed external file, then extract it
		r.addfile('payload', ext, compress='ZIP')
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		assert list(r.index('a_copy').read()) == [0, 1, 2]
		assert list(r.index('c').read()) == [3, 4, 5]
		out = os.path.join(d, 'out.bin')
		r.index('payload').getfile(out)
		with open(out, 'rb') as fh:
			assert fh.read() == payload
	finally:
		f.close()


def test_rle_types():
	fn = os.path.join(tempfile.mkdtemp(), 'rle.gds')
	# runs of one value, an arithmetic progression and a block of literals
	lit = (np.arange(300, dtype=np.int64) ** 2) % 1000
	mix = np.concatenate([np.full(500, 7), np.arange(10, 1010, 5), lit])
	prog = np.arange(0, 1.0001, 0.01)
	strs = ['A'] * 200 + ['s%d' % i for i in range(150)] + ['B'] * 100
	cases = {
		'u8':  ('rle.uint8',   (mix % 200).astype(np.uint8),  'RleUInt8'),
		'i32': ('rle.int32',   mix.astype(np.int32),          'RleInt32'),
		'i64': ('rle.int64',   mix.astype(np.int64) * 10**10, 'RleInt64'),
		'f32': ('rle.float32', prog.astype(np.float32),       'RleReal32'),
		'f64': ('rle.float64', prog,                          'RleReal64'),
	}
	# alternative storage names and the trait each should map to
	aliases = {'rle.int': 'RleInt32', 'rle.float': 'RleReal32',
		'rle.real': 'RleReal64', 'rle.double': 'RleReal64'}
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		for nm, (st, v, _tr) in cases.items():
			r.add(nm, v, storage=st)
		r.add('str', strs, storage='rle.string')
		r.index('i32').append(np.full(50, -3, dtype=np.int32))
		for k, st in enumerate(aliases):
			r.add('alias%d' % k, np.array([1, 1, 2], dtype=np.int32), storage=st)
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		cases['i32'] = (None, np.concatenate([cases['i32'][1],
			np.full(50, -3, dtype=np.int32)]), 'RleInt32')
		for nm, (_st, v, tr) in cases.items():
			n = r.index(nm)
			got = n.read()
			assert got.dtype == v.dtype and np.array_equal(got, v), nm
			assert n.description()['trait'] == tr, (nm, n.description()['trait'])
		# random access across run / progression / literal boundaries
		i32 = cases['i32'][1]
		got = r.index('i32').read(start=[495], count=[300])
		assert np.array_equal(got, i32[495:795])
		# an arithmetic progression is stored compactly
		assert r.index('f64').description()['size'] < 0.05 * prog.nbytes
		assert list(r.index('str').read()) == strs
		assert r.index('str').description()['trait'] == 'RleStr8'
		for k, st in enumerate(aliases):
			assert r.index('alias%d' % k).description()['trait'] == aliases[st], st
	finally:
		f.close()


def test_sparse_types():
	fn = os.path.join(tempfile.mkdtemp(), 'sp.gds')
	v = np.zeros(1000)
	v[[3, 77, 500, 999]] = [1, -2, 3, 4]
	cases = {
		'sp.int8': np.int8, 'sp.int16': np.int16, 'sp.int32': np.int32,
		'sp.int64': np.int64, 'sp.uint8': np.uint8, 'sp.uint16': np.uint16,
		'sp.uint32': np.uint32, 'sp.uint64': np.uint64,
		'sp.real32': np.float32, 'sp.real64': np.float64,
	}
	mat = np.zeros((4, 6), dtype=np.int32)
	mat[1, 2] = 5
	mat[3, 5] = -9
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		for st, dt in cases.items():
			x = np.abs(v) if np.dtype(dt).kind == 'u' else v
			r.add(st, x.astype(dt), storage=st)
		r.add('sp.int', np.array([0, 2, 0], dtype=np.int32), storage='sp.int')
		r.add('sp.real', np.array([0, 0.5, 0]), storage='sp.real')
		r.add('mat', mat, storage='sp.int32')
		n = r.add('grow', np.array([0, 0, 3, 0], dtype=np.int16), storage='sp.int16')
		n.append(np.array([0, 4], dtype=np.int16))
		r.add('dense', np.arange(3, dtype=np.int32))
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		for st, dt in cases.items():
			x = (np.abs(v) if np.dtype(dt).kind == 'u' else v).astype(dt)
			n = r.index(st)
			got = n.read()
			assert n.is_sparse() and np.array_equal(got, x), st
		assert r.index('sp.int').description()['trait'] == 'SparseInt32'
		assert r.index('sp.real').description()['trait'] == 'SparseReal64'
		got = r.index('mat').read()
		assert got.shape == (4, 6) and np.array_equal(got, mat)
		assert list(r.index('grow').read()) == [0, 0, 3, 0, 0, 4]
		assert not r.index('dense').is_sparse()
	finally:
		f.close()


def test_packedreal_unsigned():
	fn = os.path.join(tempfile.mkdtemp(), 'pr.gds')
	v = np.array([0.0, 0.25, 0.5, 1.0, 1.5])
	scale = 0.01
	traits = {'packedreal8u': 'PackedReal8U', 'packedreal16u': 'PackedReal16U',
		'packedreal24u': 'PackedReal24U', 'packedreal32u': 'PackedReal32U'}
	f = pycorearray.gdsfile(); f.create(fn)
	try:
		r = f.root()
		for st in traits:
			r.add(st, v, storage=st, offset=0.0, scale=scale)
	finally:
		f.close()

	f = pycorearray.gdsfile(); f.open(fn)
	try:
		r = f.root()
		for st, tr in traits.items():
			n = r.index(st)
			assert n.description()['trait'] == tr, st
			assert np.allclose(n.read(), v, atol=scale / 2), st
	finally:
		f.close()


def _run_all():
	import traceback
	fns = [v for k, v in sorted(globals().items())
		if k.startswith('test_') and callable(v)]
	ok = 0
	for fn in fns:
		try:
			fn()
			print('PASS', fn.__name__)
			ok += 1
		except Exception:
			print('FAIL', fn.__name__)
			traceback.print_exc()
	print('\n%d/%d passed' % (ok, len(fns)))
	return ok == len(fns)


if __name__ == '__main__':
	import sys
	sys.exit(0 if _run_all() else 1)
