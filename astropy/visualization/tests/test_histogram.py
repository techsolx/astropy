# Licensed under a 3-clause BSD style license - see LICENSE.rst


from numpy.testing import assert_allclose

from astropy.utils.compat.optional_deps import HAS_PLT, HAS_SCIPY

if HAS_PLT:
    from matplotlib.figure import Figure

import numpy as np
import pytest

from astropy.stats import histogram
from astropy.visualization import hist


@pytest.mark.skipif(not HAS_PLT, reason="requires matplotlib")
def test_hist_basic():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(100)

    fig = Figure()
    ax = fig.add_subplot()
    for range in [None, (-2, 2)]:
        n1, bins1, patches1 = ax.hist(x, 10, range=range)
        n2, bins2, patches2 = hist(x, 10, range=range, ax=ax)

        assert_allclose(n1, n2)
        assert_allclose(bins1, bins2)


@pytest.mark.skipif(not HAS_PLT, reason="requires matplotlib")
def test_hist_specify_ax():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(100)

    fig = Figure()
    ax = fig.subplots(2)

    n1, bins1, patches1 = hist(x, 10, ax=ax[0])
    assert patches1[0].axes is ax[0]

    n2, bins2, patches2 = hist(x, 10, ax=ax[1])
    assert patches2[0].axes is ax[1]


@pytest.mark.skipif(not HAS_PLT, reason="requires matplotlib")
def test_hist_autobin():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(100)

    # 'knuth' bintype depends on scipy that is optional dependency
    if HAS_SCIPY:
        bintypes = [10, np.arange(-3, 3, 10), "knuth", "scott", "freedman", "blocks"]
    else:
        bintypes = [10, np.arange(-3, 3, 10), "scott", "freedman", "blocks"]

    for bintype in bintypes:
        for range in [None, (-3, 3)]:
            n1, bins1 = histogram(x, bintype, range=range)
            n2, bins2, patches = hist(x, bintype, range=range)
            assert_allclose(n1, n2)
            assert_allclose(bins1, bins2)


def test_histogram_pathological_input():
    # Regression test for https://github.com/astropy/astropy/issues/7758

    # The key feature of the data below is that one of the points is very,
    # very different than the rest. That leads to a large number of bins.
    data = [
        9.99999914e05,
        -8.31312483e-03,
        6.52755852e-02,
        1.43104653e-03,
        -2.26311017e-02,
        2.82660007e-03,
        1.80307521e-02,
        9.26294279e-03,
        5.06606026e-02,
        2.05418011e-03,
    ]

    with pytest.raises(ValueError):
        hist(data, bins="freedman", max_bins=10000)
