# Licensed under a 3-clause BSD style license - see LICENSE.rst

# TEST_UNICODE_LITERALS
import numpy as np

from ... import units as u
from .. import SphericalRepresentation, Longitude, Latitude
from ...tests.helper import pytest
from ...utils.compat.numpycompat import NUMPY_LT_1_9


class TestManipulation():
    """Manipulation of Representation shapes.

    Checking that attributes are manipulated correctly.

    Even more exhaustive tests are done in time.tests.test_methods
    """

    def setup(self):
        lon = Longitude(np.arange(0, 24, 4), u.hourangle)
        lat = Latitude(np.arange(-90, 91, 30), u.deg)
        # With same-sized arrays
        self.s0 = SphericalRepresentation(
            lon[:, np.newaxis] * np.ones(lat.shape),
            lat * np.ones(lon.shape)[:, np.newaxis],
            np.ones(lon.shape + lat.shape) * u.kpc)
        # With unequal arrays -> these will be broadcast.
        self.s1 = SphericalRepresentation(lon[:, np.newaxis], lat, 1. * u.kpc)
        # For completeness on some tests, also a cartesian one
        self.c0 = self.s0.to_cartesian()

    def test_ravel(self):
        s0_ravel = self.s0.ravel()
        assert s0_ravel.shape == (self.s0.size,)
        assert np.all(s0_ravel.lon == self.s0.lon.ravel())
        assert np.may_share_memory(s0_ravel.lon, self.s0.lon)
        assert np.may_share_memory(s0_ravel.lat, self.s0.lat)
        assert np.may_share_memory(s0_ravel.distance, self.s0.distance)
        # Since s1 was broadcast, ravel needs to make a copy.
        s1_ravel = self.s1.ravel()
        assert s1_ravel.shape == (self.s1.size,)
        assert np.all(s1_ravel.lon == self.s1.lon.ravel())
        assert not np.may_share_memory(s1_ravel.lat, self.s1.lat)

    def test_flatten(self):
        s0_flatten = self.s0.flatten()
        assert s0_flatten.shape == (self.s0.size,)
        assert np.all(s0_flatten.lon == self.s0.lon.flatten())
        # Flatten always copies.
        assert not np.may_share_memory(s0_flatten.distance, self.s0.distance)
        s1_flatten = self.s1.flatten()
        assert s1_flatten.shape == (self.s1.size,)
        assert np.all(s1_flatten.lon == self.s1.lon.flatten())
        assert not np.may_share_memory(s1_flatten.lat, self.s1.lat)

    def test_transpose(self):
        s0_transpose = self.s0.transpose()
        assert s0_transpose.shape == (7, 6)
        assert np.all(s0_transpose.lon == self.s0.lon.transpose())
        assert np.may_share_memory(s0_transpose.distance, self.s0.distance)
        s1_transpose = self.s1.transpose()
        assert s1_transpose.shape == (7, 6)
        assert np.all(s1_transpose.lat == self.s1.lat.transpose())
        assert np.may_share_memory(s1_transpose.lat, self.s1.lat)
        # Only one check on T, since it just calls transpose anyway.
        # Doing it on the CartesianRepresentation just for variety's sake.
        c0_T = self.c0.T
        assert c0_T.shape == (7, 6)
        assert np.all(c0_T.x == self.c0.x.T)
        assert np.may_share_memory(c0_T.y, self.c0.y)

    def test_diagonal(self):
        s0_diagonal = self.s0.diagonal()
        assert s0_diagonal.shape == (6,)
        assert np.all(s0_diagonal.lat == self.s0.lat.diagonal())
        if not NUMPY_LT_1_9:
            assert np.may_share_memory(s0_diagonal.lat, self.s0.lat)

    def test_swapaxes(self):
        s1_swapaxes = self.s1.swapaxes(0, 1)
        assert s1_swapaxes.shape == (7, 6)
        assert np.all(s1_swapaxes.lat == self.s1.lat.swapaxes(0, 1))
        assert np.may_share_memory(s1_swapaxes.lat, self.s1.lat)

    def test_reshape(self):
        s0_reshape = self.s0.reshape(2, 3, 7)
        assert s0_reshape.shape == (2, 3, 7)
        assert np.all(s0_reshape.lon == self.s0.lon.reshape(2, 3, 7))
        assert np.all(s0_reshape.lat == self.s0.lat.reshape(2, 3, 7))
        assert np.all(s0_reshape.distance == self.s0.distance.reshape(2, 3, 7))
        assert np.may_share_memory(s0_reshape.lon, self.s0.lon)
        assert np.may_share_memory(s0_reshape.lat, self.s0.lat)
        assert np.may_share_memory(s0_reshape.distance, self.s0.distance)
        s1_reshape = self.s1.reshape(3, 2, 7)
        assert s1_reshape.shape == (3, 2, 7)
        assert np.all(s1_reshape.lat == self.s1.lat.reshape(3, 2, 7))
        assert np.may_share_memory(s1_reshape.lat, self.s1.lat)
        # For reshape(3, 14), copying is necessary for lon, lat, but not for d
        s1_reshape2 = self.s1.reshape(3, 14)
        assert s1_reshape2.shape == (3, 14)
        assert np.all(s1_reshape2.lon == self.s1.lon.reshape(3, 14))
        assert not np.may_share_memory(s1_reshape2.lon, self.s1.lon)
        assert s1_reshape2.distance.shape == (3, 14)
        assert np.may_share_memory(s1_reshape2.distance, self.s1.distance)

    def test_squeeze(self):
        s0_squeeze = self.s0.reshape(3, 1, 2, 1, 7).squeeze()
        assert s0_squeeze.shape == (3, 2, 7)
        assert np.all(s0_squeeze.lat == self.s0.lat.reshape(3, 2, 7))
        assert np.may_share_memory(s0_squeeze.lat, self.s0.lat)

    def test_add_dimension(self):
        s0_adddim = self.s0[:, np.newaxis, :]
        assert s0_adddim.shape == (6, 1, 7)
        assert np.all(s0_adddim.lon == self.s0.lon[:, np.newaxis, :])
        assert np.may_share_memory(s0_adddim.lat, self.s0.lat)

    def test_take(self):
        s0_take = self.s0.take((5, 2))
        assert s0_take.shape == (2,)
        assert np.all(s0_take.lon == self.s0.lon.take((5, 2)))
