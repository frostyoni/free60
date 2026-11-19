import struct, sys

class AXML:
    def __init__(self, data):
        self.d = data
        self.strings = []
        self.utf8 = False
        self.parse_stringpool()

    def parse_stringpool(self):
        d = self.d
        if struct.unpack('<H', d[0:2])[0] != 0x0003:
            raise ValueError("not AXML")
        off = 8
        while off < len(d):
            ctype, chsz, csize = struct.unpack('<HHI', d[off:off+8])
            if ctype == 0x0001:
                self.parse_pool(off)
                return
            if csize == 0:
                break
            off += csize

    def parse_pool(self, off):
        d = self.d
        ctype, hsz, csize = struct.unpack('<HHI', d[off:off+8])
        stringCount, styleCount, flags, stringsStart, stylesStart = struct.unpack('<IIIII', d[off+8:off+28])
        self.utf8 = bool(flags & (1 << 8))
        idx = off + hsz
        offs = struct.unpack('<%dI' % stringCount, d[idx:idx + 4 * stringCount])
        base = off + stringsStart
        for o in offs:
            p = base + o
            if self.utf8:
                n = d[p]; p += 1
                if n & 0x80:
                    n = ((n & 0x7f) << 8) | d[p]; p += 1
                n2 = d[p]; p += 1
                if n2 & 0x80:
                    n2 = ((n2 & 0x7f) << 8) | d[p]; p += 1
                self.strings.append(d[p:p + n2].decode('utf-8', 'replace'))
            else:
                n = struct.unpack('<H', d[p:p + 2])[0]; p += 2
                if n & 0x8000:
                    n = ((n & 0x7fff) << 16) | struct.unpack('<H', d[p:p + 2])[0]; p += 2
                self.strings.append(d[p:p + 2 * n].decode('utf-16-le', 'replace'))

    def s(self, i):
        return self.strings[i] if 0 <= i < len(self.strings) else '?'

    def walk(self):
        d = self.d
        off = 8
        out = []
        while off + 8 <= len(d):
            ctype, chsz, csize = struct.unpack('<HHI', d[off:off + 8])
            if csize == 0:
                break
            if ctype == 0x0102:
                # node header 16B (chunk hdr + lineNumber + comment), then ns, name, ...
                ns, name_i = struct.unpack('<II', d[off + 16:off + 24])
                attrStart, attrSize, attrCount = struct.unpack('<HHH', d[off + 24:off + 30])
                attrs = []
                ap = off + 16 + attrStart
                for i in range(attrCount):
                    a = ap + i * attrSize
                    a_ns, a_name, a_raw = struct.unpack('<III', d[a:a + 12])
                    v_size, v_res0, v_type = struct.unpack('<HBB', d[a + 12:a + 16])
                    v_data = struct.unpack('<i', d[a + 16:a + 20])[0]
                    aname = self.s(a_name)
                    if a_raw:
                        val = self.s(a_raw)
                    elif v_type == 0x03:
                        val = self.s(v_data)
                    elif v_type == 0x12:
                        val = bool(v_data)
                    elif v_type == 0x10:
                        val = v_data
                    elif v_type == 0x11:
                        val = '0x%08x' % (v_data & 0xffffffff)
                    elif v_type == 0x01:
                        val = '#ref@0x%08x' % (v_data & 0xffffffff)
                    elif v_type == 0x02:
                        val = '<attr 0x%08x>' % (v_data & 0xffffffff)
                    else:
                        val = 't0x%x:0x%x' % (v_type, v_data & 0xffffffff)
                    attrs.append((aname, val))
                out.append(('START', self.s(name_i), attrs))
            elif ctype == 0x0103:
                ns, name_i = struct.unpack('<II', d[off + 16:off + 24])
                out.append(('END', self.s(name_i), []))
            off += csize
        return out


if __name__ == '__main__':
    for path in sys.argv[1:]:
        print("=" * 70)
        print(path)
        print("=" * 70)
        a = AXML(open(path, 'rb').read())
        depth = 0
        for kind, name, attrs in a.walk():
            if kind == 'START':
                line = ' '.join('%s=%r' % (k, v) for k, v in attrs)
                print('%s<%s %s>' % ('  ' * depth, name, line))
                depth += 1
            else:
                depth = max(0, depth - 1)
                print('%s</%s>' % ('  ' * depth, name))
