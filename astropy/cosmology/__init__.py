# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Cosmology with Astropy.

:mod:`~astropy.cosmology` contains classes and functions for cosmological distance
measures and other cosmology-related calculations.

See the :ref:`astropy-cosmology` for more detailed usage examples and references.
"""

from . import io, realizations, traits, units
from ._src.core import Cosmology, CosmologyError, FlatCosmologyMixin
from ._src.flrw import (
    FLRW,
    FlatFLRWMixin,
    FlatLambdaCDM,
    Flatw0waCDM,
    Flatw0wzCDM,
    FlatwCDM,
    FlatwpwaCDM,
    LambdaCDM,
    w0waCDM,
    w0wzCDM,
    wCDM,
    wpwaCDM,
)
from ._src.funcs import cosmology_equal, z_at_value
from ._src.parameter import Parameter
from .realizations import available, default_cosmology

__all__ = [  #  noqa: RUF100, RUF022
    # Public Submodules
    "realizations",
    "units",
    "traits",
    "io",
    # Core
    "Cosmology",
    "CosmologyError",
    "FlatCosmologyMixin",
    # FLRW
    "FLRW",
    "FlatFLRWMixin",
    "LambdaCDM",
    "FlatLambdaCDM",
    "wCDM",
    "FlatwCDM",
    "w0waCDM",
    "Flatw0waCDM",
    "w0wzCDM",
    "Flatw0wzCDM",
    "wpwaCDM",
    "FlatwpwaCDM",
    # Funcs
    "z_at_value",
    "cosmology_equal",
    # Parameter
    "Parameter",
    # Realizations
    "available",
    "default_cosmology",
    "WMAP1",
    "WMAP3",
    "WMAP5",
    "WMAP7",
    "WMAP9",
    "Planck13",
    "Planck15",
    "Planck18",
]


def __getattr__(name: str) -> Cosmology:
    """Get realizations using lazy import from ``PEP 562``.

    Raises
    ------
    AttributeError
        If "name" is not in :mod:`astropy.cosmology.realizations`
    """
    if name not in available:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}.")

    return getattr(realizations, name)


def __dir__() -> list[str]:
    """Directory, including lazily-imported objects."""
    return __all__
