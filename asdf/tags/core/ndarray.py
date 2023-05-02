import mmap
import sys
import weakref

import numpy as np
from numpy import ma

from asdf import _types, util
from asdf._jsonschema import ValidationError
from asdf._block.options import Options
from asdf.config import config_context

_datatype_names = {
    "int8": "i1",
    "int16": "i2",
    "int32": "i4",
    "int64": "i8",
    "uint8": "u1",
    "uint16": "u2",
    "uint32": "u4",
    "uint64": "u8",
    "float32": "f4",
    "float64": "f8",
    "complex64": "c8",
    "complex128": "c16",
    "bool8": "b1",
}


_string_datatype_names = {"ascii": "S", "ucs4": "U"}


def asdf_byteorder_to_numpy_byteorder(byteorder):
    if byteorder == "big":
        return ">"

    if byteorder == "little":
        return "<"

    msg = f"Invalid ASDF byteorder '{byteorder}'"
    raise ValueError(msg)


def asdf_datatype_to_numpy_dtype(datatype, byteorder=None):
    if byteorder is None:
        byteorder = sys.byteorder

    if isinstance(datatype, str) and datatype in _datatype_names:
        datatype = _datatype_names[datatype]
        byteorder = asdf_byteorder_to_numpy_byteorder(byteorder)
        return np.dtype(str(byteorder + datatype))

    if (
        isinstance(datatype, list)
        and len(datatype) == 2
        and isinstance(datatype[0], str)
        and isinstance(datatype[1], int)
        and datatype[0] in _string_datatype_names
    ):
        length = datatype[1]
        byteorder = asdf_byteorder_to_numpy_byteorder(byteorder)
        datatype = str(byteorder) + str(_string_datatype_names[datatype[0]]) + str(length)

        return np.dtype(datatype)

    if isinstance(datatype, dict):
        if "datatype" not in datatype:
            msg = f"Field entry has no datatype: '{datatype}'"
            raise ValueError(msg)

        name = datatype.get("name", "")
        byteorder = datatype.get("byteorder", byteorder)
        shape = datatype.get("shape")
        datatype = asdf_datatype_to_numpy_dtype(datatype["datatype"], byteorder)

        if shape is None:
            return (str(name), datatype)

        return (str(name), datatype, tuple(shape))

    if isinstance(datatype, list):
        datatype_list = []
        for subdatatype in datatype:
            np_dtype = asdf_datatype_to_numpy_dtype(subdatatype, byteorder)
            if isinstance(np_dtype, tuple):
                datatype_list.append(np_dtype)

            elif isinstance(np_dtype, np.dtype):
                datatype_list.append(("", np_dtype))

            else:
                msg = "Error parsing asdf datatype"
                raise RuntimeError(msg)

        return np.dtype(datatype_list)

    msg = f"Unknown datatype {datatype}"
    raise ValueError(msg)


def numpy_byteorder_to_asdf_byteorder(byteorder, override=None):
    if override is not None:
        return override

    if byteorder == "=":
        return sys.byteorder

    if byteorder == "<":
        return "little"

    return "big"


def numpy_dtype_to_asdf_datatype(dtype, include_byteorder=True, override_byteorder=None):
    dtype = np.dtype(dtype)
    if dtype.names is not None:
        fields = []
        for name in dtype.names:
            field = dtype.fields[name][0]
            d = {}
            d["name"] = name
            field_dtype, byteorder = numpy_dtype_to_asdf_datatype(field, override_byteorder=override_byteorder)
            d["datatype"] = field_dtype
            if include_byteorder:
                d["byteorder"] = byteorder
            if field.shape:
                d["shape"] = list(field.shape)
            fields.append(d)
        return fields, numpy_byteorder_to_asdf_byteorder(dtype.byteorder, override=override_byteorder)

    if dtype.subdtype is not None:
        return numpy_dtype_to_asdf_datatype(dtype.subdtype[0], override_byteorder=override_byteorder)

    if dtype.name in _datatype_names:
        return dtype.name, numpy_byteorder_to_asdf_byteorder(dtype.byteorder, override=override_byteorder)

    if dtype.name == "bool":
        return "bool8", numpy_byteorder_to_asdf_byteorder(dtype.byteorder, override=override_byteorder)

    if dtype.name.startswith("string") or dtype.name.startswith("bytes"):
        return ["ascii", dtype.itemsize], "big"

    if dtype.name.startswith("unicode") or dtype.name.startswith("str"):
        return (
            ["ucs4", int(dtype.itemsize / 4)],
            numpy_byteorder_to_asdf_byteorder(dtype.byteorder, override=override_byteorder),
        )

    msg = f"Unknown dtype {dtype}"
    raise ValueError(msg)


def inline_data_asarray(inline, dtype=None):
    # np.asarray doesn't handle structured arrays unless the innermost
    # elements are tuples.  To do that, we drill down the first
    # element of each level until we find a single item that
    # successfully converts to a scalar of the expected structured
    # dtype.  Then we go through and convert everything at that level
    # to a tuple.  This probably breaks for nested structured dtypes,
    # but it's probably good enough for now.  It also won't work with
    # object dtypes, but ASDF explicitly excludes those, so we're ok
    # there.
    if dtype is not None and dtype.fields is not None:

        def find_innermost_match(line, depth=0):
            if not isinstance(line, list) or not len(line):
                msg = "data can not be converted to structured array"
                raise ValueError(msg)
            try:
                np.asarray(tuple(line), dtype=dtype)
            except ValueError:
                return find_innermost_match(line[0], depth + 1)
            else:
                return depth

        depth = find_innermost_match(inline)

        def convert_to_tuples(line, data_depth, depth=0):
            if data_depth == depth:
                return tuple(line)

            return [convert_to_tuples(x, data_depth, depth + 1) for x in line]

        inline = convert_to_tuples(inline, depth)

        return np.asarray(inline, dtype=dtype)

    def handle_mask(inline):
        if isinstance(inline, list):
            if None in inline:
                inline_array = np.asarray(inline)
                nones = np.equal(inline_array, None)
                return np.ma.array(np.where(nones, 0, inline), mask=nones)

            return [handle_mask(x) for x in inline]

        return inline

    inline = handle_mask(inline)

    inline = np.ma.asarray(inline, dtype=dtype)
    if not ma.is_masked(inline):
        return inline.data

    return inline


def numpy_array_to_list(array):
    def tolist(x):
        if isinstance(x, (np.ndarray, NDArrayType)):
            x = x.astype("U").tolist() if x.dtype.char == "S" else x.tolist()

        if isinstance(x, (list, tuple)):
            return [tolist(y) for y in x]

        return x

    def ascii_to_unicode(x):
        # Convert byte string arrays to unicode string arrays, since YAML
        # doesn't handle the former.
        if isinstance(x, list):
            return [ascii_to_unicode(y) for y in x]

        if isinstance(x, bytes):
            return x.decode("ascii")

        return x

    return ascii_to_unicode(tolist(array))


class NDArrayType(_types._AsdfType):
    name = "core/ndarray"
    version = "1.0.0"
    supported_versions = {"1.0.0", "1.1.0"}
    types = [np.ndarray, ma.MaskedArray]

    def __init__(self, source, shape, dtype, offset, strides, order, mask):
        # source can be a:
        # - list of numbers for an inline block
        # - string for an external block
        # - a data callback for an internal block
        self._source = source
        self._array = None
        self._mask = mask

        if isinstance(source, list):
            self._array = inline_data_asarray(source, dtype)
            self._array = self._apply_mask(self._array, self._mask)
            if shape is not None and (
                (shape[0] == "*" and self._array.shape[1:] != tuple(shape[1:])) or (self._array.shape != tuple(shape))
            ):
                msg = "inline data doesn't match the given shape"
                raise ValueError(msg)

        self._shape = shape
        self._dtype = dtype
        self._offset = offset
        self._strides = strides
        self._order = order

    def _make_array(self):
        # If the ASDF file has been updated in-place, then there's
        # a chance that the block's original data object has been
        # closed and replaced.  We need to check here and re-generate
        # the array if necessary, otherwise we risk segfaults when
        # memory mapping.
        if self._array is not None:
            base = util.get_array_base(self._array)
            if isinstance(base, np.memmap) and isinstance(base.base, mmap.mmap) and base.base.closed:
                self._array = None

        if self._array is None:
            if callable(self._source):
                # cached data is used here so that multiple NDArrayTypes will all use
                # the same base array
                data = self._source(_attr="cached_data")
            else:
                data = self._source

            if hasattr(data, "base") and isinstance(data.base, mmap.mmap) and data.base.closed:
                raise OSError("Attempt to read data from a closed file")

            # streaming blocks have 0 data size
            shape = self.get_actual_shape(
                self._shape,
                self._strides,
                self._dtype,
                data.size,
            )
            self._array = np.ndarray(shape, self._dtype, data, self._offset, self._strides, self._order)
            self._array = self._apply_mask(self._array, self._mask)
        return self._array

    def _apply_mask(self, array, mask):
        if isinstance(mask, (np.ndarray, NDArrayType)):
            # Use "mask.view()" here so the underlying possibly
            # memmapped mask array is freed properly when the masked
            # array goes away.
            array = ma.array(array, mask=mask.view())
            return array

        if np.isscalar(mask):
            if np.isnan(mask):
                return ma.array(array, mask=np.isnan(array))

            return ma.masked_values(array, mask)

        return array

    def __array__(self):
        return self._make_array()

    def __repr__(self):
        # repr alone should not force loading of the data
        if self._array is None:
            return (
                f"<{'array' if self._mask is None else 'masked array'} "
                f"(unloaded) shape: {self._shape} dtype: {self._dtype}>"
            )
        return repr(self._make_array())

    def __str__(self):
        # str alone should not force loading of the data
        if self._array is None:
            return (
                f"<{'array' if self._mask is None else 'masked array'} "
                f"(unloaded) shape: {self._shape} dtype: {self._dtype}>"
            )
        return str(self._make_array())

    def get_actual_shape(self, shape, strides, dtype, block_size):
        """
        Get the actual shape of an array, by computing it against the
        block_size if it contains a ``*``.
        """
        num_stars = shape.count("*")
        if num_stars == 0:
            return shape

        if num_stars == 1:
            if shape[0] != "*":
                msg = "'*' may only be in first entry of shape"
                raise ValueError(msg)

            stride = strides[0] if strides is not None else np.prod(shape[1:]) * dtype.itemsize

            missing = int(block_size / stride)
            return [missing] + shape[1:]

        msg = f"Invalid shape '{shape}'"
        raise ValueError(msg)

    @property
    def shape(self):
        if self._shape is None or self._array is not None:
            return self.__array__().shape
        if "*" in self._shape:
            if isinstance(self._source, str):
                return self._make_array().shape
            data_size = self._source(_attr="header")["data_size"]
            if not data_size:
                return self._make_array().shape
            return tuple(
                self.get_actual_shape(
                    self._shape,
                    self._strides,
                    self._dtype,
                    data_size,
                )
            )
        return tuple(self._shape)

    @property
    def dtype(self):
        if self._array is None:
            return self._dtype

        return self._make_array().dtype

    def __len__(self):
        if self._array is None:
            return self._shape[0]

        return len(self._make_array())

    def __getattr__(self, attr):
        # We need to ignore __array_struct__, or unicode arrays end up
        # getting "double casted" and upsized.  This also reduces the
        # number of array creations in the general case.
        if attr == "__array_struct__":
            raise AttributeError
        # AsdfFile.info will call hasattr(obj, "__asdf_traverse__") which
        # will trigger this method, making the array, and loading the array
        # data. Intercept this and raise AttributeError as this class does
        # not support that method
        # see: https://github.com/asdf-format/asdf/issues/1553
        if attr == "__asdf_traverse__":
            raise AttributeError
        return getattr(self._make_array(), attr)

    def __setitem__(self, *args):
        # This workaround appears to be necessary in order to avoid a segfault
        # in the case that array assignment causes an exception. The segfault
        # originates from the call to __repr__ inside the traceback report.
        try:
            self._make_array().__setitem__(*args)
        except Exception:
            self._array = None

            raise

    def __getattribute__(self, name):
        # The presence of these attributes on an NDArrayType instance
        # can cause problems when the array is passed to other
        # libraries.
        # See https://github.com/asdf-format/asdf/issues/1015
        if name in ("name", "version", "supported_versions"):
            msg = f"'{self.__class__.name}' object has no attribute '{name}'"
            raise AttributeError(msg)

        return _types._AsdfType.__getattribute__(self, name)

    @classmethod
    def from_tree(cls, node, ctx):
        if isinstance(node, list):
            instance = cls(node, None, None, None, None, None, None)
            ctx._blocks._set_array_storage(instance, "inline")
            return instance

        if isinstance(node, dict):
            source = node.get("source")
            data = node.get("data")
            if source and data:
                msg = "Both source and data may not be provided at the same time"
                raise ValueError(msg)
            if data:
                source = data
            shape = node.get("shape", None)
            byteorder = sys.byteorder if data is not None else node["byteorder"]
            dtype = asdf_datatype_to_numpy_dtype(node["datatype"], byteorder) if "datatype" in node else None
            offset = node.get("offset", 0)
            strides = node.get("strides", None)
            mask = node.get("mask", None)

            if isinstance(source, int):
                data = ctx._blocks._get_data_callback(source)
                instance = cls(data, shape, dtype, offset, strides, "A", mask)
                ctx._blocks.blocks.assign_object(instance, ctx._blocks.blocks[source])
                ctx._blocks._data_callbacks.assign_object(instance, data)
            elif isinstance(source, str):
                # external
                def data(_attr=None, _ref=weakref.ref(ctx)):
                    ctx = _ref()
                    if ctx is None:
                        msg = "Failed to resolve reference to AsdfFile to read external block"
                        raise OSError(msg)
                    array = ctx.open_external(source)._blocks.blocks[0].cached_data
                    ctx._blocks._set_array_storage(array, "external")
                    return array

                instance = cls(data, shape, dtype, offset, strides, "A", mask)
            else:
                # inline
                instance = cls(source, shape, dtype, offset, strides, "A", mask)
                ctx._blocks._set_array_storage(instance, "inline")

            if not ctx._blocks.lazy_load:
                instance._make_array()
            return instance

        msg = "Invalid ndarray description."
        raise TypeError(msg)

    @classmethod
    def to_tree(cls, obj, ctx):
        data = obj
        # The ndarray-1.0.0 schema does not permit 0 valued strides.
        # Perhaps we'll want to allow this someday, to efficiently
        # represent an array of all the same value.
        if any(stride == 0 for stride in data.strides):
            data = np.ascontiguousarray(data)

        # The view computations that follow assume that the base array
        # is contiguous.  If not, we need to make a copy to avoid
        # writing a nonsense view.
        base = util.get_array_base(data)
        if not base.flags.forc:
            data = np.ascontiguousarray(data)
            base = util.get_array_base(data)

        shape = data.shape

        if isinstance(obj, NDArrayType) and isinstance(obj._source, str):
            # this is an external block, if we have no other settings, keep it as external
            options = ctx._blocks.options.lookup_by_object(data)
            if options is None:
                options = Options("external")
        else:
            options = ctx._blocks.options.get_options(data)

        with config_context() as cfg:
            if cfg.all_array_storage is not None:
                options.storage_type = cfg.all_array_storage
            if cfg.all_array_compression != "input":
                options.compression = cfg.all_array_compression
                options.compression_kwargs = cfg.all_array_compression_kwargs
            inline_threshold = cfg.array_inline_threshold

        if inline_threshold is not None and options.storage_type in ("inline", "internal"):
            if data.size < inline_threshold:
                options.storage_type = "inline"
            else:
                options.storage_type = "internal"
        ctx._blocks.options.set_options(data, options)

        # Compute the offset relative to the base array and not the
        # block data, in case the block is compressed.
        offset = data.ctypes.data - base.ctypes.data

        strides = None if data.flags.c_contiguous else data.strides
        dtype, byteorder = numpy_dtype_to_asdf_datatype(
            data.dtype,
            # include_byteorder=(block.array_storage != "inline"),
            include_byteorder=(options.storage_type != "inline"),
        )

        result = {}

        result["shape"] = list(shape)
        if options.storage_type == "streamed":
            result["shape"][0] = "*"

        # if block.array_storage == "inline":
        if options.storage_type == "inline":
            listdata = numpy_array_to_list(data)
            result["data"] = listdata
            result["datatype"] = dtype

        else:
            result["shape"] = list(shape)
            if options.storage_type == "streamed":
                result["shape"][0] = "*"

            # result["source"] = ctx._blocks.get_source(block)
            # convert data to byte array
            if options.storage_type == "streamed":
                ctx._blocks.set_streamed_block(base, data)
                result["source"] = -1
            else:
                result["source"] = ctx._blocks.make_write_block(base, options, obj)
            result["datatype"] = dtype
            result["byteorder"] = byteorder

            if offset > 0:
                result["offset"] = offset

            if strides is not None:
                result["strides"] = list(strides)

        if isinstance(data, ma.MaskedArray) and np.any(data.mask):
            # if block.array_storage == "inline":
            if options.storage_type == "inline":
                # ctx._blocks.set_array_storage(ctx._blocks[data.mask], "inline")
                ctx._blocks._set_array_storage(data.mask, "inline")

            result["mask"] = data.mask

        return result

    @classmethod
    def _assert_equality(cls, old, new, func):
        if old.dtype.fields:
            if not new.dtype.fields:
                # This line is safe because this is actually a piece of test
                # code, even though it lives in this file:
                msg = "arrays not equal"
                raise AssertionError(msg)
            for a, b in zip(old, new):
                cls._assert_equality(a, b, func)
        else:
            old = old.__array__()
            new = new.__array__()
            if old.dtype.char in "SU":
                if old.dtype.char == "S":
                    old = old.astype("U")
                if new.dtype.char == "S":
                    new = new.astype("U")
                old = old.tolist()
                new = new.tolist()
                # This line is safe because this is actually a piece of test
                # code, even though it lives in this file:
                assert old == new  # noqa: S101
            else:
                func(old, new)

    @classmethod
    def assert_equal(cls, old, new):
        from numpy.testing import assert_array_equal

        cls._assert_equality(old, new, assert_array_equal)

    @classmethod
    def assert_allclose(cls, old, new):
        from numpy.testing import assert_allclose, assert_array_equal

        if old.dtype.kind in "iu" and new.dtype.kind in "iu":
            cls._assert_equality(old, new, assert_array_equal)
        else:
            cls._assert_equality(old, new, assert_allclose)


def _make_operation(name):
    def operation(self, *args):
        return getattr(self._make_array(), name)(*args)

    return operation


classes_to_modify = [*NDArrayType.__versioned_siblings, NDArrayType]
for op in [
    "__neg__",
    "__pos__",
    "__abs__",
    "__invert__",
    "__complex__",
    "__int__",
    "__long__",
    "__float__",
    "__oct__",
    "__hex__",
    "__lt__",
    "__le__",
    "__eq__",
    "__ne__",
    "__gt__",
    "__ge__",
    "__cmp__",
    "__rcmp__",
    "__add__",
    "__sub__",
    "__mul__",
    "__floordiv__",
    "__mod__",
    "__divmod__",
    "__pow__",
    "__lshift__",
    "__rshift__",
    "__and__",
    "__xor__",
    "__or__",
    "__div__",
    "__truediv__",
    "__radd__",
    "__rsub__",
    "__rmul__",
    "__rdiv__",
    "__rtruediv__",
    "__rfloordiv__",
    "__rmod__",
    "__rdivmod__",
    "__rpow__",
    "__rlshift__",
    "__rrshift__",
    "__rand__",
    "__rxor__",
    "__ror__",
    "__iadd__",
    "__isub__",
    "__imul__",
    "__idiv__",
    "__itruediv__",
    "__ifloordiv__",
    "__imod__",
    "__ipow__",
    "__ilshift__",
    "__irshift__",
    "__iand__",
    "__ixor__",
    "__ior__",
    "__getitem__",
    "__delitem__",
    "__contains__",
]:
    [setattr(cls, op, _make_operation(op)) for cls in classes_to_modify]
del classes_to_modify


def _get_ndim(instance):
    if isinstance(instance, list):
        array = inline_data_asarray(instance)
        return array.ndim

    if isinstance(instance, dict):
        if "shape" in instance:
            return len(instance["shape"])

        if "data" in instance:
            array = inline_data_asarray(instance["data"])
            return array.ndim

    if isinstance(instance, (np.ndarray, NDArrayType)):
        return len(instance.shape)

    return None


def validate_ndim(validator, ndim, instance, schema):
    in_ndim = _get_ndim(instance)

    if in_ndim != ndim:
        yield ValidationError(f"Wrong number of dimensions: Expected {ndim}, got {in_ndim}", instance=repr(instance))


def validate_max_ndim(validator, max_ndim, instance, schema):
    in_ndim = _get_ndim(instance)

    if in_ndim > max_ndim:
        yield ValidationError(
            f"Wrong number of dimensions: Expected max of {max_ndim}, got {in_ndim}",
            instance=repr(instance),
        )


def validate_datatype(validator, datatype, instance, schema):
    if isinstance(instance, list):
        array = inline_data_asarray(instance)
        in_datatype, _ = numpy_dtype_to_asdf_datatype(array.dtype)
    elif isinstance(instance, dict):
        if "datatype" in instance:
            in_datatype = instance["datatype"]
        elif "data" in instance:
            array = inline_data_asarray(instance["data"])
            in_datatype, _ = numpy_dtype_to_asdf_datatype(array.dtype)
        else:
            msg = "Not an array"
            raise ValidationError(msg)
    elif isinstance(instance, (np.ndarray, NDArrayType)):
        in_datatype, _ = numpy_dtype_to_asdf_datatype(instance.dtype)
    else:
        msg = "Not an array"
        raise ValidationError(msg)

    if datatype == in_datatype:
        return

    if schema.get("exact_datatype", False):
        yield ValidationError(f"Expected datatype '{datatype}', got '{in_datatype}'")

    np_datatype = asdf_datatype_to_numpy_dtype(datatype)
    np_in_datatype = asdf_datatype_to_numpy_dtype(in_datatype)

    if not np_datatype.fields:
        if np_in_datatype.fields:
            yield ValidationError(f"Expected scalar datatype '{datatype}', got '{in_datatype}'")

        if not np.can_cast(np_in_datatype, np_datatype, "safe"):
            yield ValidationError(f"Can not safely cast from '{in_datatype}' to '{datatype}' ")

    else:
        if not np_in_datatype.fields:
            yield ValidationError(f"Expected structured datatype '{datatype}', got '{in_datatype}'")

        if len(np_in_datatype.fields) != len(np_datatype.fields):
            yield ValidationError(f"Mismatch in number of columns: Expected {len(datatype)}, got {len(in_datatype)}")

        for i in range(len(np_datatype.fields)):
            in_type = np_in_datatype[i]
            out_type = np_datatype[i]
            if not np.can_cast(in_type, out_type, "safe"):
                yield ValidationError(
                    "Can not safely cast to expected datatype: "
                    f"Expected {numpy_dtype_to_asdf_datatype(out_type)[0]}, "
                    f"got {numpy_dtype_to_asdf_datatype(in_type)[0]}",
                )
