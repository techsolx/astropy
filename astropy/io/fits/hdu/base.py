# Licensed under a 3-clause BSD style license - see PYFITS.rst


import datetime
import numbers
import os
import sys
import warnings
from contextlib import suppress
from inspect import Parameter, signature

import numpy as np

from astropy.io.fits import conf
from astropy.io.fits.file import _File
from astropy.io.fits.header import Header, _BasicHeader, _DelayedHeader, _pad_length
from astropy.io.fits.util import (
    _extract_number,
    _free_space_check,
    _get_array_mmap,
    _is_int,
    decode_ascii,
    first,
    itersubclasses,
)
from astropy.io.fits.verify import _ErrList, _Verify
from astropy.utils import lazyproperty
from astropy.utils.exceptions import AstropyUserWarning

__all__ = [
    "DELAYED",
    "ExtensionHDU",
    # classes
    "InvalidHDUException",
    "NonstandardExtHDU",
]


class _Delayed:
    pass


DELAYED = _Delayed()


BITPIX2DTYPE = {
    8: "uint8",
    16: "int16",
    32: "int32",
    64: "int64",
    -32: "float32",
    -64: "float64",
}
"""Maps FITS BITPIX values to Numpy dtype names."""

DTYPE2BITPIX = {
    "int8": 8,
    "uint8": 8,
    "int16": 16,
    "uint16": 16,
    "int32": 32,
    "uint32": 32,
    "int64": 64,
    "uint64": 64,
    "float32": -32,
    "float64": -64,
}
"""
Maps Numpy dtype names to FITS BITPIX values (this includes unsigned
integers, with the assumption that the pseudo-unsigned integer convention
will be used in this case.
"""


class InvalidHDUException(Exception):
    """
    A custom exception class used mainly to signal to _BaseHDU.__new__ that
    an HDU cannot possibly be considered valid, and must be assumed to be
    corrupted.
    """


def _hdu_class_from_header(cls, header):
    """
    Iterates through the subclasses of _BaseHDU and uses that class's
    match_header() method to determine which subclass to instantiate.

    It's important to be aware that the class hierarchy is traversed in a
    depth-last order.  Each match_header() should identify an HDU type as
    uniquely as possible.  Abstract types may choose to simply return False
    or raise NotImplementedError to be skipped.

    If any unexpected exceptions are raised while evaluating
    match_header(), the type is taken to be _CorruptedHDU.

    Used primarily by _BaseHDU._readfrom_internal and _BaseHDU._from_data to
    find an appropriate HDU class to use based on values in the header.
    """
    klass = cls  # By default, if no subclasses are defined
    if header:
        for c in reversed(list(itersubclasses(cls))):
            try:
                # HDU classes built into astropy.io.fits are always considered,
                # but extension HDUs must be explicitly registered
                if not (
                    c.__module__.startswith("astropy.io.fits.")
                    or c in cls._hdu_registry
                ):
                    continue
                if c.match_header(header):
                    klass = c
                    break
            except NotImplementedError:
                continue
            except Exception as exc:
                warnings.warn(
                    "An exception occurred matching an HDU header to the "
                    f"appropriate HDU type: {exc}",
                    AstropyUserWarning,
                )
                warnings.warn(
                    "The HDU will be treated as corrupted.", AstropyUserWarning
                )
                klass = _CorruptedHDU
                del exc
                break

    return klass


# TODO: Come up with a better __repr__ for HDUs (and for HDULists, for that
# matter)
class _BaseHDU:
    """Base class for all HDU (header data unit) classes."""

    _hdu_registry = set()

    # This HDU type is part of the FITS standard
    _standard = True

    # Byte to use for padding out blocks
    _padding_byte = "\x00"

    _default_name = ""

    # _header uses a descriptor to delay the loading of the fits.Header object
    # until it is necessary.
    _header = _DelayedHeader()

    def __init__(self, data=None, header=None, *args, **kwargs):
        if header is None:
            header = Header()
        self._header = header
        self._header_str = None
        self._file = None
        self._buffer = None
        self._header_offset = None
        self._data_offset = None
        self._data_size = None

        # This internal variable is used to track whether the data attribute
        # still points to the same data array as when the HDU was originally
        # created (this does not track whether the data is actually the same
        # content-wise)
        self._data_replaced = False
        self._data_needs_rescale = False
        self._new = True
        self._output_checksum = False

        if "DATASUM" in self._header and "CHECKSUM" not in self._header:
            self._output_checksum = "datasum"
        elif "CHECKSUM" in self._header:
            self._output_checksum = True

    def __init_subclass__(cls, **kwargs):
        # Add the same data.deleter to all HDUs with a data property.
        # It's unfortunate, but there's otherwise no straightforward way
        # that a property can inherit setters/deleters of the property of the
        # same name on base classes.
        data_prop = cls.__dict__.get("data", None)
        if isinstance(data_prop, (lazyproperty, property)) and data_prop.fdel is None:
            # Don't do anything if the class has already explicitly
            # set the deleter for its data property
            def data(self):
                # The deleter
                if self._file is not None and self._data_loaded:
                    # sys.getrefcount is CPython specific and not on PyPy.
                    has_getrefcount = hasattr(sys, "getrefcount")
                    if has_getrefcount:
                        data_refcount = sys.getrefcount(self.data)

                    # Manually delete *now* so that FITS_rec.__del__
                    # cleanup can happen if applicable
                    del self.__dict__["data"]

                    # Don't even do this unless the *only* reference to the
                    # .data array was the one we're deleting by deleting
                    # this attribute; if any other references to the array
                    # are hanging around (perhaps the user ran ``data =
                    # hdu.data``) don't even consider this:
                    if has_getrefcount and data_refcount == 2:
                        self._file._maybe_close_mmap()

            cls.data = data_prop.deleter(data)

        return super().__init_subclass__(**kwargs)

    @property
    def header(self):
        return self._header

    @header.setter
    def header(self, value):
        self._header = value

    @property
    def name(self):
        # Convert the value to a string to be flexible in some pathological
        # cases (see ticket #96)
        return str(self._header.get("EXTNAME", self._default_name))

    @name.setter
    def name(self, value):
        if not isinstance(value, str):
            raise TypeError("'name' attribute must be a string")
        if not conf.extension_name_case_sensitive:
            value = value.upper()
        if "EXTNAME" in self._header:
            self._header["EXTNAME"] = value
        else:
            self._header["EXTNAME"] = (value, "extension name")

    @property
    def ver(self):
        return self._header.get("EXTVER", 1)

    @ver.setter
    def ver(self, value):
        if not _is_int(value):
            raise TypeError("'ver' attribute must be an integer")
        if "EXTVER" in self._header:
            self._header["EXTVER"] = value
        else:
            self._header["EXTVER"] = (value, "extension value")

    @property
    def level(self):
        return self._header.get("EXTLEVEL", 1)

    @level.setter
    def level(self, value):
        if not _is_int(value):
            raise TypeError("'level' attribute must be an integer")
        if "EXTLEVEL" in self._header:
            self._header["EXTLEVEL"] = value
        else:
            self._header["EXTLEVEL"] = (value, "extension level")

    @property
    def is_image(self):
        return self.name == "PRIMARY" or (
            "XTENSION" in self._header
            and (
                self._header["XTENSION"] == "IMAGE"
                or (
                    self._header["XTENSION"] == "BINTABLE"
                    and "ZIMAGE" in self._header
                    and self._header["ZIMAGE"] is True
                )
            )
        )

    @property
    def _data_loaded(self):
        return "data" in self.__dict__ and self.data is not DELAYED

    @property
    def _has_data(self):
        return self._data_loaded and self.data is not None

    @classmethod
    def register_hdu(cls, hducls):
        cls._hdu_registry.add(hducls)

    @classmethod
    def unregister_hdu(cls, hducls):
        if hducls in cls._hdu_registry:
            cls._hdu_registry.remove(hducls)

    @classmethod
    def match_header(cls, header):
        raise NotImplementedError

    @classmethod
    def fromstring(cls, data, checksum=False, ignore_missing_end=False, **kwargs):
        """
        Creates a new HDU object of the appropriate type from a string
        containing the HDU's entire header and, optionally, its data.

        Note: When creating a new HDU from a string without a backing file
        object, the data of that HDU may be read-only.  It depends on whether
        the underlying string was an immutable Python str/bytes object, or some
        kind of read-write memory buffer such as a `memoryview`.

        Parameters
        ----------
        data : str, bytes, memoryview, ndarray
            A byte string containing the HDU's header and data.

        checksum : bool, optional
            Check the HDU's checksum and/or datasum.

        ignore_missing_end : bool, optional
            Ignore a missing end card in the header data.  Note that without the
            end card the end of the header may be ambiguous and resulted in a
            corrupt HDU.  In this case the assumption is that the first 2880
            block that does not begin with valid FITS header data is the
            beginning of the data.

        **kwargs : optional
            May consist of additional keyword arguments specific to an HDU
            type--these correspond to keywords recognized by the constructors of
            different HDU classes such as `PrimaryHDU`, `ImageHDU`, or
            `BinTableHDU`.  Any unrecognized keyword arguments are simply
            ignored.
        """
        return cls._readfrom_internal(
            data, checksum=checksum, ignore_missing_end=ignore_missing_end, **kwargs
        )

    @classmethod
    def readfrom(cls, fileobj, checksum=False, ignore_missing_end=False, **kwargs):
        """
        Read the HDU from a file.  Normally an HDU should be opened with
        :func:`open` which reads the entire HDU list in a FITS file.  But this
        method is still provided for symmetry with :func:`writeto`.

        Parameters
        ----------
        fileobj : file-like
            Input FITS file.  The file's seek pointer is assumed to be at the
            beginning of the HDU.

        checksum : bool
            If `True`, verifies that both ``DATASUM`` and ``CHECKSUM`` card
            values (when present in the HDU header) match the header and data
            of all HDU's in the file.

        ignore_missing_end : bool
            Do not issue an exception when opening a file that is missing an
            ``END`` card in the last header.
        """
        # TODO: Figure out a way to make it possible for the _File
        # constructor to be a noop if the argument is already a _File
        if not isinstance(fileobj, _File):
            fileobj = _File(fileobj)

        hdu = cls._readfrom_internal(
            fileobj, checksum=checksum, ignore_missing_end=ignore_missing_end, **kwargs
        )

        # If the checksum had to be checked the data may have already been read
        # from the file, in which case we don't want to seek relative
        fileobj.seek(hdu._data_offset + hdu._data_size, os.SEEK_SET)
        return hdu

    def writeto(self, name, output_verify="exception", overwrite=False, checksum=False):
        """
        Write the HDU to a new file. This is a convenience method to
        provide a user easier output interface if only one HDU needs
        to be written to a file.

        Parameters
        ----------
        name : path-like or file-like
            Output FITS file.  If the file object is already opened, it must
            be opened in a writeable mode.

        output_verify : str
            Output verification option.  Must be one of ``"fix"``,
            ``"silentfix"``, ``"ignore"``, ``"warn"``, or
            ``"exception"``.  May also be any combination of ``"fix"`` or
            ``"silentfix"`` with ``"+ignore"``, ``"+warn"``, or ``"+exception"``
            (e.g. ``"fix+warn"``).  See :ref:`astropy:verify` for more info.

        overwrite : bool, optional
            If ``True``, overwrite the output file if it exists. Raises an
            ``OSError`` if ``False`` and the output file exists. Default is
            ``False``.

        checksum : bool
            When `True` adds both ``DATASUM`` and ``CHECKSUM`` cards
            to the header of the HDU when written to the file.

        Notes
        -----
        gzip, zip, bzip2 and lzma compression algorithms are natively supported.
        Compression mode is determined from the filename extension
        ('.gz', '.zip', '.bz2' or '.xz' respectively).  It is also possible to
        pass a compressed file object, e.g. `gzip.GzipFile`.
        """
        from .hdulist import HDUList

        hdulist = HDUList([self])
        hdulist.writeto(name, output_verify, overwrite=overwrite, checksum=checksum)

    @classmethod
    def _from_data(cls, data, header, **kwargs):
        """
        Instantiate the HDU object after guessing the HDU class from the
        FITS Header.
        """
        klass = _hdu_class_from_header(cls, header)
        return klass(data=data, header=header, **kwargs)

    @classmethod
    def _readfrom_internal(
        cls, data, header=None, checksum=False, ignore_missing_end=False, **kwargs
    ):
        """
        Provides the bulk of the internal implementation for readfrom and
        fromstring.

        For some special cases, supports using a header that was already
        created, and just using the input data for the actual array data.
        """
        hdu_buffer = None
        hdu_fileobj = None
        header_offset = 0

        if isinstance(data, _File):
            if header is None:
                header_offset = data.tell()
                try:
                    # First we try to read the header with the fast parser
                    # from _BasicHeader, which will read only the standard
                    # 8 character keywords to get the structural keywords
                    # that are needed to build the HDU object.
                    header_str, header = _BasicHeader.fromfile(data)
                except Exception:
                    # If the fast header parsing failed, then fallback to
                    # the classic Header parser, which has better support
                    # and reporting for the various issues that can be found
                    # in the wild.
                    data.seek(header_offset)
                    header = Header.fromfile(data, endcard=not ignore_missing_end)
            hdu_fileobj = data
            data_offset = data.tell()  # *after* reading the header
        else:
            try:
                # Test that the given object supports the buffer interface by
                # ensuring an ndarray can be created from it
                np.ndarray((), dtype="ubyte", buffer=data)
            except TypeError:
                raise TypeError(
                    f"The provided object {data!r} does not contain an underlying "
                    "memory buffer.  fromstring() requires an object that "
                    "supports the buffer interface such as bytes, buffer, "
                    "memoryview, ndarray, etc.  This restriction is to ensure "
                    "that efficient access to the array/table data is possible."
                )

            if header is None:

                def block_iter(nbytes):
                    idx = 0
                    while idx < len(data):
                        yield data[idx : idx + nbytes]
                        idx += nbytes

                header_str, header = Header._from_blocks(
                    block_iter, True, "", not ignore_missing_end, True
                )

                if len(data) > len(header_str):
                    hdu_buffer = data
            elif data:
                hdu_buffer = data

            header_offset = 0
            data_offset = len(header_str)

        # Determine the appropriate arguments to pass to the constructor from
        # self._kwargs.  self._kwargs contains any number of optional arguments
        # that may or may not be valid depending on the HDU type
        cls = _hdu_class_from_header(cls, header)
        sig = signature(cls.__init__)
        new_kwargs = kwargs.copy()
        if Parameter.VAR_KEYWORD not in (x.kind for x in sig.parameters.values()):
            # If __init__ accepts arbitrary keyword arguments, then we can go
            # ahead and pass all keyword arguments; otherwise we need to delete
            # any that are invalid
            for key in kwargs:
                if key not in sig.parameters:
                    del new_kwargs[key]

        try:
            hdu = cls(data=DELAYED, header=header, **new_kwargs)
        except TypeError:
            # This may happen because some HDU class (e.g. GroupsHDU) wants
            # to set a keyword on the header, which is not possible with the
            # _BasicHeader. While HDU classes should not need to modify the
            # header in general, sometimes this is needed to fix it. So in
            # this case we build a full Header and try again to create the
            # HDU object.
            if isinstance(header, _BasicHeader):
                header = Header.fromstring(header_str)
                hdu = cls(data=DELAYED, header=header, **new_kwargs)
            else:
                raise

        # One of these may be None, depending on whether the data came from a
        # file or a string buffer--later this will be further abstracted
        hdu._file = hdu_fileobj
        hdu._buffer = hdu_buffer

        hdu._header_offset = header_offset  # beginning of the header area
        hdu._data_offset = data_offset  # beginning of the data area

        # data area size, including padding
        size = hdu.size
        hdu._data_size = size + _pad_length(size)

        if isinstance(hdu._header, _BasicHeader):
            # Delete the temporary _BasicHeader.
            # We need to do this before an eventual checksum computation,
            # since it needs to modify temporarily the header
            #
            # The header string is stored in the HDU._header_str attribute,
            # so that it can be used directly when we need to create the
            # classic Header object, without having to parse again the file.
            del hdu._header
            hdu._header_str = header_str

        # Checksums are not checked on invalid HDU types
        if checksum and checksum != "remove" and isinstance(hdu, _ValidHDU):
            hdu._verify_checksum_datasum()

        return hdu

    def _get_raw_data(self, shape, code, offset):
        """
        Return raw array from either the HDU's memory buffer or underlying
        file.
        """
        if isinstance(shape, numbers.Integral):
            shape = (shape,)

        if self._buffer:
            return np.ndarray(shape, dtype=code, buffer=self._buffer, offset=offset)
        elif self._file:
            return self._file.readarray(offset=offset, dtype=code, shape=shape)
        else:
            return None

    def _postwriteto(self):
        pass

    def _writeheader(self, fileobj):
        offset = 0
        with suppress(AttributeError, OSError):
            offset = fileobj.tell()

        self._header.tofile(fileobj)

        try:
            size = fileobj.tell() - offset
        except (AttributeError, OSError):
            size = len(str(self._header))

        return offset, size

    def _writedata(self, fileobj):
        size = 0
        fileobj.flush()
        try:
            offset = fileobj.tell()
        except (AttributeError, OSError):
            offset = 0

        if self._data_loaded or self._data_needs_rescale:
            if self.data is not None:
                size += self._writedata_internal(fileobj)
            # pad the FITS data block
            # to avoid a bug in the lustre filesystem client, don't
            # write zero-byte objects
            if size > 0 and _pad_length(size) > 0:
                padding = _pad_length(size) * self._padding_byte
                # TODO: Not that this is ever likely, but if for some odd
                # reason _padding_byte is > 0x80 this will fail; but really if
                # somebody's custom fits format is doing that, they're doing it
                # wrong and should be reprimanded harshly.
                fileobj.write(padding.encode("ascii"))
                size += len(padding)
        else:
            # The data has not been modified or does not need need to be
            # rescaled, so it can be copied, unmodified, directly from an
            # existing file or buffer
            size += self._writedata_direct_copy(fileobj)

        # flush, to make sure the content is written
        fileobj.flush()

        # return both the location and the size of the data area
        return offset, size

    def _writedata_internal(self, fileobj):
        """
        The beginning and end of most _writedata() implementations are the
        same, but the details of writing the data array itself can vary between
        HDU types, so that should be implemented in this method.

        Should return the size in bytes of the data written.
        """
        fileobj.writearray(self.data)
        return self.data.size * self.data.itemsize

    def _writedata_direct_copy(self, fileobj):
        """Copies the data directly from one file/buffer to the new file.

        For now this is handled by loading the raw data from the existing data
        (including any padding) via a memory map or from an already in-memory
        buffer and using Numpy's existing file-writing facilities to write to
        the new file.

        If this proves too slow a more direct approach may be used.
        """
        raw = self._get_raw_data(self._data_size, "ubyte", self._data_offset)
        if raw is not None:
            fileobj.writearray(raw)
            return raw.nbytes
        else:
            return 0

    # TODO: This is the start of moving HDU writing out of the _File class;
    # Though right now this is an internal private method (though still used by
    # HDUList, eventually the plan is to have this be moved into writeto()
    # somehow...
    def _writeto(self, fileobj, inplace=False, copy=False):
        try:
            dirname = os.path.dirname(fileobj._file.name)
        except (AttributeError, TypeError):
            dirname = None

        with _free_space_check(self, dirname):
            self._writeto_internal(fileobj, inplace, copy)

    def _writeto_internal(self, fileobj, inplace, copy):
        # For now fileobj is assumed to be a _File object
        if not inplace or self._new:
            header_offset, _ = self._writeheader(fileobj)
            data_offset, data_size = self._writedata(fileobj)

            # Set the various data location attributes on newly-written HDUs
            if self._new:
                self._header_offset = header_offset
                self._data_offset = data_offset
                self._data_size = data_size
            return

        hdrloc = self._header_offset
        hdrsize = self._data_offset - self._header_offset
        datloc = self._data_offset
        datsize = self._data_size

        if self._header._modified:
            # Seek to the original header location in the file
            self._file.seek(hdrloc)
            # This should update hdrloc with he header location in the new file
            hdrloc, hdrsize = self._writeheader(fileobj)

            # If the data is to be written below with self._writedata, that
            # will also properly update the data location; but it should be
            # updated here too
            datloc = hdrloc + hdrsize
        elif copy:
            # Seek to the original header location in the file
            self._file.seek(hdrloc)
            # Before writing, update the hdrloc with the current file position,
            # which is the hdrloc for the new file
            hdrloc = fileobj.tell()
            fileobj.write(self._file.read(hdrsize))
            # The header size is unchanged, but the data location may be
            # different from before depending on if previous HDUs were resized
            datloc = fileobj.tell()

        if self._data_loaded:
            if self.data is not None:
                # Seek through the array's bases for an memmap'd array; we
                # can't rely on the _File object to give us this info since
                # the user may have replaced the previous mmap'd array
                if copy or self._data_replaced:
                    # Of course, if we're copying the data to a new file
                    # we don't care about flushing the original mmap;
                    # instead just read it into the new file
                    array_mmap = None
                else:
                    array_mmap = _get_array_mmap(self.data)

                if array_mmap is not None:
                    array_mmap.flush()
                else:
                    self._file.seek(self._data_offset)
                    datloc, datsize = self._writedata(fileobj)
        elif copy:
            datsize = self._writedata_direct_copy(fileobj)

        self._header_offset = hdrloc
        self._data_offset = datloc
        self._data_size = datsize
        self._data_replaced = False

    def _close(self, closed=True):
        # If the data was mmap'd, close the underlying mmap (this will
        # prevent any future access to the .data attribute if there are
        # not other references to it; if there are other references then
        # it is up to the user to clean those up
        if closed and self._data_loaded and _get_array_mmap(self.data) is not None:
            del self.data


# For backwards-compatibility, though nobody should have
# been using this directly:
_AllHDU = _BaseHDU

# For convenience...
# TODO: register_hdu could be made into a class decorator which would be pretty
# cool, but only once 2.6 support is dropped.
register_hdu = _BaseHDU.register_hdu
unregister_hdu = _BaseHDU.unregister_hdu


class _CorruptedHDU(_BaseHDU):
    """
    A Corrupted HDU class.

    This class is used when one or more mandatory `Card`s are
    corrupted (unparsable), such as the ``BITPIX``, ``NAXIS``, or
    ``END`` cards.  A corrupted HDU usually means that the data size
    cannot be calculated or the ``END`` card is not found.  In the case
    of a missing ``END`` card, the `Header` may also contain the binary
    data

    .. note::
       In future, it may be possible to decipher where the last block
       of the `Header` ends, but this task may be difficult when the
       extension is a `TableHDU` containing ASCII data.
    """

    @property
    def size(self):
        """
        Returns the size (in bytes) of the HDU's data part.
        """
        # Note: On compressed files this might report a negative size; but the
        # file is corrupt anyways so I'm not too worried about it.
        if self._buffer is not None:
            return len(self._buffer) - self._data_offset

        return self._file.size - self._data_offset

    def _summary(self):
        return (self.name, self.ver, "CorruptedHDU")

    def verify(self):
        pass


class _NonstandardHDU(_BaseHDU, _Verify):
    """
    A Non-standard HDU class.

    This class is used for a Primary HDU when the ``SIMPLE`` Card has
    a value of `False`.  A non-standard HDU comes from a file that
    resembles a FITS file but departs from the standards in some
    significant way.  One example would be files where the numbers are
    in the DEC VAX internal storage format rather than the standard
    FITS most significant byte first.  The header for this HDU should
    be valid.  The data for this HDU is read from the file as a byte
    stream that begins at the first byte after the header ``END`` card
    and continues until the end of the file.
    """

    _standard = False

    @classmethod
    def match_header(cls, header):
        """
        Matches any HDU that has the 'SIMPLE' keyword but is not a standard
        Primary or Groups HDU.
        """
        # The SIMPLE keyword must be in the first card
        card = header.cards[0]
        return card.keyword == "SIMPLE" and card.value is False

    @property
    def size(self):
        """
        Returns the size (in bytes) of the HDU's data part.
        """
        if self._buffer is not None:
            return len(self._buffer) - self._data_offset

        return self._file.size - self._data_offset

    def _writedata(self, fileobj):
        """
        Differs from the base class :class:`_writedata` in that it doesn't
        automatically add padding, and treats the data as a string of raw bytes
        instead of an array.
        """
        offset = 0
        size = 0

        fileobj.flush()
        try:
            offset = fileobj.tell()
        except OSError:
            offset = 0

        if self.data is not None:
            fileobj.write(self.data)
            # flush, to make sure the content is written
            fileobj.flush()
            size = len(self.data)

        # return both the location and the size of the data area
        return offset, size

    def _summary(self):
        return (self.name, self.ver, "NonstandardHDU", len(self._header))

    @lazyproperty
    def data(self):
        """
        Return the file data.
        """
        return self._get_raw_data(self.size, "ubyte", self._data_offset)

    def _verify(self, option="warn"):
        errs = _ErrList([], unit="Card")

        # verify each card
        for card in self._header.cards:
            errs.append(card._verify(option))

        return errs


class _ValidHDU(_BaseHDU, _Verify):
    """
    Base class for all HDUs which are not corrupted.
    """

    def __init__(self, data=None, header=None, name=None, ver=None, **kwargs):
        super().__init__(data=data, header=header)

        if header is not None and not isinstance(header, (Header, _BasicHeader)):
            # TODO: Instead maybe try initializing a new Header object from
            # whatever is passed in as the header--there are various types
            # of objects that could work for this...
            raise ValueError("header must be a Header object")

        # NOTE:  private data members _checksum and _datasum are used by the
        # utility script "fitscheck" to detect missing checksums.
        self._checksum = None
        self._checksum_valid = None
        self._datasum = None
        self._datasum_valid = None

        if name is not None:
            self.name = name
        if ver is not None:
            self.ver = ver

    @classmethod
    def match_header(cls, header):
        """
        Matches any HDU that is not recognized as having either the SIMPLE or
        XTENSION keyword in its header's first card, but is nonetheless not
        corrupted.

        TODO: Maybe it would make more sense to use _NonstandardHDU in this
        case?  Not sure...
        """
        return first(header.keys()) not in ("SIMPLE", "XTENSION")

    @property
    def size(self):
        """
        Size (in bytes) of the data portion of the HDU.
        """
        return self._header.data_size

    def filebytes(self):
        """
        Calculates and returns the number of bytes that this HDU will write to
        a file.
        """
        f = _File()
        # TODO: Fix this once new HDU writing API is settled on
        return self._writeheader(f)[1] + self._writedata(f)[1]

    def fileinfo(self):
        """
        Returns a dictionary detailing information about the locations
        of this HDU within any associated file.  The values are only
        valid after a read or write of the associated file with no
        intervening changes to the `HDUList`.

        Returns
        -------
        dict or None
            The dictionary details information about the locations of
            this HDU within an associated file.  Returns `None` when
            the HDU is not associated with a file.

            Dictionary contents:

            ========== ================================================
            Key        Value
            ========== ================================================
            file       File object associated with the HDU
            filemode   Mode in which the file was opened (readonly, copyonwrite,
                       update, append, ostream)
            hdrLoc     Starting byte location of header in file
            datLoc     Starting byte location of data block in file
            datSpan    Data size including padding
            ========== ================================================
        """
        if hasattr(self, "_file") and self._file:
            return {
                "file": self._file,
                "filemode": self._file.mode,
                "hdrLoc": self._header_offset,
                "datLoc": self._data_offset,
                "datSpan": self._data_size,
            }
        else:
            return None

    def copy(self):
        """
        Make a copy of the HDU, both header and data are copied.
        """
        if self.data is not None:
            data = self.data.copy()
        else:
            data = None
        return self.__class__(data=data, header=self._header.copy())

    def _verify(self, option="warn"):
        errs = _ErrList([], unit="Card")

        is_valid = BITPIX2DTYPE.__contains__

        # Verify location and value of mandatory keywords.
        # Do the first card here, instead of in the respective HDU classes, so
        # the checking is in order, in case of required cards in wrong order.
        if isinstance(self, ExtensionHDU):
            firstkey = "XTENSION"
            firstval = self._extension
        else:
            firstkey = "SIMPLE"
            firstval = True

        self.req_cards(firstkey, 0, None, firstval, option, errs)
        self.req_cards(
            "BITPIX", 1, lambda v: (_is_int(v) and is_valid(v)), 8, option, errs
        )
        self.req_cards(
            "NAXIS", 2, lambda v: (_is_int(v) and 0 <= v <= 999), 0, option, errs
        )

        naxis = self._header.get("NAXIS", 0)
        if naxis < 1000:
            for ax in range(3, naxis + 3):
                key = "NAXIS" + str(ax - 2)
                self.req_cards(
                    key,
                    ax,
                    lambda v: (_is_int(v) and v >= 0),
                    _extract_number(self._header[key], default=1),
                    option,
                    errs,
                )

            # Remove NAXISj cards where j is not in range 1, naxis inclusive.
            for keyword in self._header:
                if keyword.startswith("NAXIS") and len(keyword) > 5:
                    try:
                        number = int(keyword[5:])
                        if number <= 0 or number > naxis:
                            raise ValueError
                    except ValueError:
                        err_text = (
                            f"NAXISj keyword out of range ('{keyword}' when "
                            f"NAXIS == {naxis})"
                        )

                        def fix(self=self, keyword=keyword):
                            del self._header[keyword]

                        errs.append(
                            self.run_option(
                                option=option,
                                err_text=err_text,
                                fix=fix,
                                fix_text="Deleted.",
                            )
                        )

        # Verify that the EXTNAME keyword exists and is a string
        if "EXTNAME" in self._header:
            if not isinstance(self._header["EXTNAME"], str):
                err_text = "The EXTNAME keyword must have a string value."
                fix_text = "Converted the EXTNAME keyword to a string value."

                def fix(header=self._header):
                    header["EXTNAME"] = str(header["EXTNAME"])

                errs.append(
                    self.run_option(
                        option, err_text=err_text, fix_text=fix_text, fix=fix
                    )
                )

        # verify each card
        for card in self._header.cards:
            errs.append(card._verify(option))

        return errs

    def _prewriteto(self, inplace=False):
        # Handle checksum
        self._update_checksum()

    # TODO: Improve this API a little bit--for one, most of these arguments
    # could be optional
    def req_cards(self, keyword, pos, test, fix_value, option, errlist):
        """
        Check the existence, location, and value of a required `Card`.

        Parameters
        ----------
        keyword : str
            The keyword to validate

        pos : int, callable
            If an ``int``, this specifies the exact location this card should
            have in the header.  Remember that Python is zero-indexed, so this
            means ``pos=0`` requires the card to be the first card in the
            header.  If given a callable, it should take one argument--the
            actual position of the keyword--and return `True` or `False`.  This
            can be used for custom evaluation.  For example if
            ``pos=lambda idx: idx > 10`` this will check that the keyword's
            index is greater than 10.

        test : callable
            This should be a callable (generally a function) that is passed the
            value of the given keyword and returns `True` or `False`.  This can
            be used to validate the value associated with the given keyword.

        fix_value : str, int, float, complex, bool, None
            A valid value for a FITS keyword to use if the given ``test``
            fails to replace an invalid value.  In other words, this provides
            a default value to use as a replacement if the keyword's current
            value is invalid.  If `None`, there is no replacement value and the
            keyword is unfixable.

        option : str
            Output verification option.  Must be one of ``"fix"``,
            ``"silentfix"``, ``"ignore"``, ``"warn"``, or
            ``"exception"``.  May also be any combination of ``"fix"`` or
            ``"silentfix"`` with ``"+ignore"``, ``+warn``, or ``+exception"
            (e.g. ``"fix+warn"``).  See :ref:`astropy:verify` for more info.

        errlist : list
            A list of validation errors already found in the FITS file; this is
            used primarily for the validation system to collect errors across
            multiple HDUs and multiple calls to `req_cards`.

        Notes
        -----
        If ``pos=None``, the card can be anywhere in the header.  If the card
        does not exist, the new card will have the ``fix_value`` as its value
        when created.  Also check the card's value by using the ``test``
        argument.
        """
        errs = errlist
        fix = None

        try:
            index = self._header.index(keyword)
        except ValueError:
            index = None

        fixable = fix_value is not None

        insert_pos = len(self._header) + 1

        # If pos is an int, insert at the given position (and convert it to a
        # lambda)
        if _is_int(pos):
            insert_pos = pos
            pos = lambda x: x == insert_pos

        # if the card does not exist
        if index is None:
            err_text = f"'{keyword}' card does not exist."
            fix_text = f"Fixed by inserting a new '{keyword}' card."
            if fixable:
                # use repr to accommodate both string and non-string types
                # Boolean is also OK in this constructor
                card = (keyword, fix_value)

                def fix(self=self, insert_pos=insert_pos, card=card):
                    self._header.insert(insert_pos, card)

            errs.append(
                self.run_option(
                    option,
                    err_text=err_text,
                    fix_text=fix_text,
                    fix=fix,
                    fixable=fixable,
                )
            )
        else:
            # if the supposed location is specified
            if pos is not None:
                if not pos(index):
                    err_text = f"'{keyword}' card at the wrong place (card {index})."
                    fix_text = (
                        f"Fixed by moving it to the right place (card {insert_pos})."
                    )

                    def fix(self=self, index=index, insert_pos=insert_pos):
                        card = self._header.cards[index]
                        del self._header[index]
                        self._header.insert(insert_pos, card)

                    errs.append(
                        self.run_option(
                            option, err_text=err_text, fix_text=fix_text, fix=fix
                        )
                    )

            # if value checking is specified
            if test:
                val = self._header[keyword]
                if not test(val):
                    err_text = f"'{keyword}' card has invalid value '{val}'."
                    fix_text = f"Fixed by setting a new value '{fix_value}'."

                    if fixable:

                        def fix(self=self, keyword=keyword, val=fix_value):
                            self._header[keyword] = fix_value

                    errs.append(
                        self.run_option(
                            option,
                            err_text=err_text,
                            fix_text=fix_text,
                            fix=fix,
                            fixable=fixable,
                        )
                    )

        return errs

    def add_datasum(self, when=None, datasum_keyword="DATASUM"):
        """
        Add the ``DATASUM`` card to this HDU with the value set to the
        checksum calculated for the data.

        Parameters
        ----------
        when : str, optional
            Comment string for the card that by default represents the
            time when the checksum was calculated

        datasum_keyword : str, optional
            The name of the header keyword to store the datasum value in;
            this is typically 'DATASUM' per convention, but there exist
            use cases in which a different keyword should be used

        Returns
        -------
        checksum : int
            The calculated datasum

        Notes
        -----
        For testing purposes, provide a ``when`` argument to enable the comment
        value in the card to remain consistent.  This will enable the
        generation of a ``CHECKSUM`` card with a consistent value.
        """
        cs = self._calculate_datasum()

        if when is None:
            when = f"data unit checksum updated {self._get_timestamp()}"

        self._header[datasum_keyword] = (str(cs), when)
        return cs

    def add_checksum(
        self,
        when=None,
        override_datasum=False,
        checksum_keyword="CHECKSUM",
        datasum_keyword="DATASUM",
    ):
        """
        Add the ``CHECKSUM`` and ``DATASUM`` cards to this HDU with
        the values set to the checksum calculated for the HDU and the
        data respectively.  The addition of the ``DATASUM`` card may
        be overridden.

        Parameters
        ----------
        when : str, optional
            comment string for the cards; by default the comments
            will represent the time when the checksum was calculated
        override_datasum : bool, optional
            add the ``CHECKSUM`` card only
        checksum_keyword : str, optional
            The name of the header keyword to store the checksum value in; this
            is typically 'CHECKSUM' per convention, but there exist use cases
            in which a different keyword should be used

        datasum_keyword : str, optional
            See ``checksum_keyword``

        Notes
        -----
        For testing purposes, first call `add_datasum` with a ``when``
        argument, then call `add_checksum` with a ``when`` argument and
        ``override_datasum`` set to `True`.  This will provide consistent
        comments for both cards and enable the generation of a ``CHECKSUM``
        card with a consistent value.
        """
        if not override_datasum:
            # Calculate and add the data checksum to the header.
            data_cs = self.add_datasum(when, datasum_keyword=datasum_keyword)
        else:
            # Just calculate the data checksum
            data_cs = self._calculate_datasum()

        if when is None:
            when = f"HDU checksum updated {self._get_timestamp()}"

        # Add the CHECKSUM card to the header with a value of all zeros.
        if datasum_keyword in self._header:
            self._header.set(checksum_keyword, "0" * 16, when, before=datasum_keyword)
        else:
            self._header.set(checksum_keyword, "0" * 16, when)

        csum = self._calculate_checksum(data_cs, checksum_keyword=checksum_keyword)
        self._header[checksum_keyword] = csum

    def verify_datasum(self):
        """
        Verify that the value in the ``DATASUM`` keyword matches the value
        calculated for the ``DATASUM`` of the current HDU data.

        Returns
        -------
        valid : int
            - 0 - failure
            - 1 - success
            - 2 - no ``DATASUM`` keyword present
        """
        if "DATASUM" in self._header:
            datasum = self._calculate_datasum()
            if datasum == int(self._header["DATASUM"]):
                return 1
            else:
                # Failed
                return 0
        else:
            return 2

    def verify_checksum(self):
        """
        Verify that the value in the ``CHECKSUM`` keyword matches the
        value calculated for the current HDU CHECKSUM.

        Returns
        -------
        valid : int
            - 0 - failure
            - 1 - success
            - 2 - no ``CHECKSUM`` keyword present
        """
        if "CHECKSUM" in self._header:
            if "DATASUM" in self._header:
                datasum = self._calculate_datasum()
            else:
                datasum = 0
            checksum = self._calculate_checksum(datasum)
            if checksum == self._header["CHECKSUM"]:
                return 1
            else:
                # Failed
                return 0
        else:
            return 2

    def _verify_checksum_datasum(self):
        """
        Verify the checksum/datasum values if the cards exist in the header.
        Simply displays warnings if either the checksum or datasum don't match.
        """
        if "CHECKSUM" in self._header:
            self._checksum = self._header["CHECKSUM"]
            self._checksum_valid = self.verify_checksum()
            if not self._checksum_valid:
                warnings.warn(
                    f"Checksum verification failed for HDU {self.name, self.ver}.\n",
                    AstropyUserWarning,
                )

        if "DATASUM" in self._header:
            self._datasum = self._header["DATASUM"]
            self._datasum_valid = self.verify_datasum()
            if not self._datasum_valid:
                warnings.warn(
                    f"Datasum verification failed for HDU {self.name, self.ver}.\n",
                    AstropyUserWarning,
                )

    def _update_checksum(self, checksum_keyword="CHECKSUM", datasum_keyword="DATASUM"):
        """Update the 'CHECKSUM' and 'DATASUM' keywords in the header (or
        keywords with equivalent semantics given by the ``checksum_keyword``
        and ``datasum_keyword`` arguments--see for example ``CompImageHDU``
        for an example of why this might need to be overridden).
        """
        # If the data is loaded it isn't necessarily 'modified', but we have no
        # way of knowing for sure
        modified = self._header._modified or self._data_loaded

        if self._output_checksum == "remove":
            self._header.remove(checksum_keyword, ignore_missing=True)
            self._header.remove(datasum_keyword, ignore_missing=True)
        elif (
            modified
            or self._new
            or (
                self._output_checksum
                and (
                    "CHECKSUM" not in self._header
                    or "DATASUM" not in self._header
                    or not self._checksum_valid
                    or not self._datasum_valid
                )
            )
        ):
            if self._output_checksum == "datasum":
                self.add_datasum(datasum_keyword=datasum_keyword)
            elif self._output_checksum:
                self.add_checksum(
                    checksum_keyword=checksum_keyword, datasum_keyword=datasum_keyword
                )

    def _get_timestamp(self):
        """
        Return the current timestamp in ISO 8601 format, with microseconds
        stripped off.

        Ex.: 2007-05-30T19:05:11
        """
        return datetime.datetime.now().isoformat()[:19]

    def _calculate_datasum(self):
        """
        Calculate the value for the ``DATASUM`` card in the HDU.
        """
        if not self._data_loaded:
            # This is the case where the data has not been read from the file
            # yet.  We find the data in the file, read it, and calculate the
            # datasum.
            if self.size > 0:
                raw_data = self._get_raw_data(
                    self._data_size, "ubyte", self._data_offset
                )
                return self._compute_checksum(raw_data)
            else:
                return 0
        elif self.data is not None:
            return self._compute_checksum(self.data.view("ubyte"))
        else:
            return 0

    def _calculate_checksum(self, datasum, checksum_keyword="CHECKSUM"):
        """
        Calculate the value of the ``CHECKSUM`` card in the HDU.
        """
        old_checksum = self._header[checksum_keyword]
        self._header[checksum_keyword] = "0" * 16

        # Convert the header to bytes.
        s = self._header.tostring().encode("utf8")

        # Calculate the checksum of the Header and data.
        cs = self._compute_checksum(np.frombuffer(s, dtype="ubyte"), datasum)

        # Encode the checksum into a string.
        s = self._char_encode(~cs)

        # Return the header card value.
        self._header[checksum_keyword] = old_checksum

        return s

    def _compute_checksum(self, data, sum32=0):
        """
        Compute the ones-complement checksum of a sequence of bytes.

        Parameters
        ----------
        data
            a memory region to checksum

        sum32
            incremental checksum value from another region

        Returns
        -------
        ones complement checksum
        """
        # Possibly split data in blocks to avoid overflow in the uint64 sum
        # (logically, the maximum is (2**64-1) / (2**32-1) = 2**32+1 uint32
        # data, so ~2**34 bytes, but 4GB is a lot and better safe than sorry).
        blocklen = 1 << 32
        # The cast to uint32 is not needed by the tests, and seems odd, since
        # higher bits should be dealt with. But in numpy>=2.0, out-of-bound
        # raises OverflowError, so keeping the cast makes the code more secure.
        s = int(np.uint32(sum32))
        for piece in np.split(data, range(blocklen, len(data), blocklen)):
            if extra := piece.nbytes % 4:
                # Pad with zeros to complete the last big-endian uint32.
                last = bytes(piece[-extra:]) + b"\00" * (4 - extra)
                s += int.from_bytes(last, byteorder="big")
                piece = piece[:-extra]
            s += int(piece.view(">u4").sum(dtype="u8"))
            while hi := (s >> 32):
                s = (s & 0xFFFFFFFF) + hi
        return np.uint32(s)

    # _MASK and _EXCLUDE used for encoding the checksum value into a character
    # string.
    _MASK = [0xFF000000, 0x00FF0000, 0x0000FF00, 0x000000FF]

    _EXCLUDE = [0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40,
                0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60]  # fmt: skip

    def _encode_byte(self, byte):
        """
        Encode a single byte.
        """
        quotient = byte // 4 + ord("0")
        remainder = byte % 4

        ch = np.array(
            [(quotient + remainder), quotient, quotient, quotient], dtype="int32"
        )

        check = True
        while check:
            check = False
            for x in self._EXCLUDE:
                for j in [0, 2]:
                    if ch[j] == x or ch[j + 1] == x:
                        ch[j] += 1
                        ch[j + 1] -= 1
                        check = True
        return ch

    def _char_encode(self, value):
        """
        Encodes the checksum ``value`` using the algorithm described
        in SPR section A.7.2 and returns it as a 16 character string.

        Parameters
        ----------
        value
            a checksum

        Returns
        -------
        ascii encoded checksum
        """
        value = np.uint32(value)

        asc = np.zeros((16,), dtype="byte")
        ascii = np.zeros((16,), dtype="byte")

        for i in range(4):
            byte = (value & self._MASK[i]) >> ((3 - i) * 8)
            ch = self._encode_byte(byte)
            for j in range(4):
                asc[4 * j + i] = ch[j]

        for i in range(16):
            ascii[i] = asc[(i + 15) % 16]

        return decode_ascii(ascii.tobytes())


class ExtensionHDU(_ValidHDU):
    """
    An extension HDU class.

    This class is the base class for the `TableHDU`, `ImageHDU`, and
    `BinTableHDU` classes.
    """

    _extension = ""

    @classmethod
    def match_header(cls, header):
        """
        This class should never be instantiated directly.  Either a standard
        extension HDU type should be used for a specific extension, or
        NonstandardExtHDU should be used.
        """
        raise NotImplementedError

    def writeto(self, name, output_verify="exception", overwrite=False, checksum=False):
        """
        Works similarly to the normal writeto(), but prepends a default
        `PrimaryHDU` are required by extension HDUs (which cannot stand on
        their own).
        """
        from .hdulist import HDUList
        from .image import PrimaryHDU

        hdulist = HDUList([PrimaryHDU(), self])
        hdulist.writeto(name, output_verify, overwrite=overwrite, checksum=checksum)

    def _verify(self, option="warn"):
        errs = super()._verify(option=option)

        # Verify location and value of mandatory keywords.
        naxis = self._header.get("NAXIS", 0)
        self.req_cards(
            "PCOUNT", naxis + 3, lambda v: (_is_int(v) and v >= 0), 0, option, errs
        )
        self.req_cards(
            "GCOUNT", naxis + 4, lambda v: (_is_int(v) and v == 1), 1, option, errs
        )

        return errs


class NonstandardExtHDU(ExtensionHDU):
    """
    A Non-standard Extension HDU class.

    This class is used for an Extension HDU when the ``XTENSION``
    `Card` has a non-standard value.  In this case, Astropy can figure
    out how big the data is but not what it is.  The data for this HDU
    is read from the file as a byte stream that begins at the first
    byte after the header ``END`` card and continues until the
    beginning of the next header or the end of the file.
    """

    _standard = False

    @classmethod
    def match_header(cls, header):
        """
        Matches any extension HDU that is not one of the standard extension HDU
        types.
        """
        card = header.cards[0]
        xtension = card.value
        if isinstance(xtension, str):
            xtension = xtension.rstrip()
        # A3DTABLE is not really considered a 'standard' extension, as it was
        # sort of the prototype for BINTABLE; however, since our BINTABLE
        # implementation handles A3DTABLE HDUs it is listed here.
        standard_xtensions = ("IMAGE", "TABLE", "BINTABLE", "A3DTABLE")
        # The check that xtension is not one of the standard types should be
        # redundant.
        return card.keyword == "XTENSION" and xtension not in standard_xtensions

    def _summary(self):
        axes = tuple(self.data.shape)
        return (self.name, self.ver, "NonstandardExtHDU", len(self._header), axes)

    @lazyproperty
    def data(self):
        """
        Return the file data.
        """
        return self._get_raw_data(self.size, "ubyte", self._data_offset)
