"""Run the examples in the docstrings of pycorearray, as R CMD check runs the
examples of the help pages.

The examples assume ``import numpy as np`` and ``import pycorearray as gds``,
and the examples writing a file use the current directory, which is a
temporary directory here. Each docstring is a test, e.g.,
``test_examples[pycorearray.gdsnode.read]``.
"""

import copy
import doctest

import numpy as np
import pytest

import pycorearray
from pycorearray import cloud


def _examples(module):
	return [ t for t in doctest.DocTestFinder().find(module) if t.examples ]


def _run(test):
	test = copy.copy(test)
	test.globs = dict(test.globs, np=np, gds=pycorearray)
	runner = doctest.DocTestRunner(
		optionflags=doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE)
	out = []
	runner.run(test, out=out.append)
	assert runner.failures == 0, ''.join(out)


@pytest.mark.parametrize('test', _examples(pycorearray), ids=lambda t: t.name)
def test_examples(test, tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	_run(test)


@pytest.mark.skipif(not cloud.available(),
	reason='pycorearray was built without cloud access')
@pytest.mark.parametrize('test', _examples(cloud), ids=lambda t: t.name)
def test_cloud_examples(test, tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	# the examples change the cloud settings of the session
	saved = cloud.export_credentials()
	try:
		_run(test)
	finally:
		cloud.import_credentials(saved)
