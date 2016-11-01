from rsqueakvm.primitives import index1_0, bytelist, char
from rsqueakvm.error import PrimitiveFailedError
from rsqueakvm.model.variable import W_BytesObject
from rsqueakvm.plugins.plugin import Plugin

from rpython.rlib.rarithmetic import r_uint, intmask
from rpython.rlib import jit


MiscPrimitivePlugin = Plugin()

@jit.look_inside_iff(lambda bytes, start: jit.isconstant(len(bytes)) and jit.isconstant(start))
def _bytesHashLoop(bytes, start):
    hash = start
    for byte in bytes:
        hash = hash + ord(byte)
        low = r_uint(hash & 16383)
        hash = (0x260D * low +
                (((0x260D * (hash >> 14) + (0x0065 * low))
                  & 16383) * 16384)) & r_uint(0x0FFFFFFF)
    return intmask(hash)

@MiscPrimitivePlugin.expose_primitive(unwrap_spec=[object, bytelist, r_uint])
def primitiveStringHash(interp, s_frame, w_rcvr, thebytes, initialHash):
    hash = r_uint(initialHash) & r_uint(0xFFFFFFF)
    return interp.space.wrap_smallint_unsafe(_bytesHashLoop(thebytes, hash))

@MiscPrimitivePlugin.expose_primitive(unwrap_spec=[object, char, bytelist, index1_0])
def primitiveIndexOfAsciiInString(interp, s_frame, w_rcvr, thechar, thebytes, start):
    if start < 0:
        raise PrimitiveFailedError
    try:
        res = thebytes.index(thechar, start) + 1
    except ValueError:
        res = 0
    return interp.space.wrap_smallint_unsafe(res)
