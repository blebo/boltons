# -*- coding: utf-8 -*-

import time
import socket


try:
    from typeutils import make_sentinel
    _UNSET = make_sentinel(var_name='_MISSING')
except ImportError:
    _UNSET = object()


DEFAULT_TIMEOUT = 10  # 10 seconds
DEFAULT_MAXSIZE = 32 * 1024  # 32kb


class BufferedSocket(object):
    """Mainly provides recv_until and recv_size. recv, send, sendall, and
    peek all function as similarly as possible to the built-in socket
    API.

    This type has been tested against both the built-in socket type as
    well as those from gevent and eventlet. It also features support
    for sockets with timeouts set to 0 (aka nonblocking), provided the
    caller is prepared to handle the EWOULDBLOCK exceptions. Much like
    the built-in socket, the BufferedSocket is not intrinsically
    threadsafe for higher-level protocols.
    """
    # TODO: recv_close()  # receive until socket closed
    def __init__(self, sock,
                 timeout=DEFAULT_TIMEOUT, maxsize=DEFAULT_MAXSIZE):
        self.sock = sock
        self.sock.settimeout(None)
        self.rbuf = b''
        self.sbuf = []
        self.timeout = timeout
        self.maxsize = maxsize

    def fileno(self):
        return self.sock.fileno()

    def settimeout(self, timeout):
        self.timeout = timeout

    def setmaxsize(self, maxsize):
        self.maxsize = maxsize

    def recv(self, size, flags=0, timeout=_UNSET):
        if timeout is _UNSET:
            timeout = self.timeout
        if flags:
            raise ValueError("non-zero flags not supported: %r" % flags)
        if len(self.rbuf) >= size:
            data, self.rbuf = self.rbuf[:size], self.rbuf[size:]
            return data
        size -= len(self.rbuf)
        self.sock.settimeout(timeout)
        data = self.rbuf + self.sock.recv(size)
        # don't empty buffer till after network communication is complete,
        # to avoid data loss on transient / retry-able errors (e.g. read
        # timeout)
        self.rbuf = b''
        return data

    def peek(self, n, timeout=_UNSET):
        'peek n bytes from the socket and keep them in the buffer'
        if len(self.rbuf) >= n:
            return self.rbuf[:n]
        data = self.recv_size(n, timeout=timeout)
        self.rbuf = data + self.rbuf
        return data

    def recv_until(self, marker, timeout=_UNSET, maxsize=_UNSET):
        'read off of socket until the marker is found'
        if maxsize is _UNSET:
            maxsize = self.maxsize
        if timeout is _UNSET:
            timeout = self.timeout
        recvd = bytearray(self.rbuf)
        start = time.time()
        sock = self.sock
        if not timeout:  # covers None (no timeout) and 0 (nonblocking)
            sock.settimeout(timeout)
        try:
            while 1:
                if maxsize is not None and len(recvd) >= maxsize:
                    raise NotFound(marker, len(recvd))  # check rbuf attr
                if timeout:
                    cur_timeout = timeout - (time.time() - start)
                    if cur_timeout <= 0.0:
                        raise socket.timeout()
                    sock.settimeout(cur_timeout)
                nxt = sock.recv(maxsize)
                if not nxt:
                    msg = ('connection closed after reading %s bytes without'
                           ' finding symbol: %r' % (len(recvd), marker))
                    raise ConnectionClosed(msg)  # check the rbuf attr for more
                recvd.extend(nxt)
                offset = recvd.find(marker, -len(nxt) - len(marker))
                if offset >= 0:
                    offset += len(marker)  # include marker in the return
                    break
        except socket.timeout:
            self.rbuf = bytes(recvd)
            msg = ('read %s bytes without finding marker: %r'
                   % (len(recvd), marker))
            raise Timeout(timeout, msg)  # check the rbuf attr for more
        except Exception:
            self.rbuf = bytes(recvd)
            raise
        val, self.rbuf = bytes(recvd[:offset]), bytes(recvd[offset:])
        return val

    def recv_size(self, size, timeout=_UNSET):
        'read off of socket until size bytes have been read'
        if timeout is _UNSET:
            timeout = self.timeout
        chunks = []
        total_bytes = 0
        try:
            start = time.time()
            self.sock.settimeout(timeout)
            nxt = self.rbuf or self.sock.recv(size)
            while nxt:
                total_bytes += len(nxt)
                if total_bytes >= size:
                    break
                chunks.append(nxt)
                if timeout:
                    cur_timeout = timeout - (time.time() - start)
                    if cur_timeout <= 0.0:
                        raise socket.timeout()
                    self.sock.settimeout(cur_timeout)
                nxt = self.sock.recv(size - total_bytes)
            else:
                msg = ('connection closed after reading %s of %s requested'
                       ' bytes' % (total_bytes, size))
                raise ConnectionClosed(msg)  # check rbuf attribute for more
        except socket.timeout:
            self.rbuf = b''.join(chunks)
            msg = 'read %s of %s bytes' % (total_bytes, size)
            raise Timeout(timeout, msg)  # check rbuf attribute for more
        except Exception:
            # received data is still buffered in the case of errors
            self.rbuf = b''.join(chunks)
            raise
        extra_bytes = total_bytes - size
        if extra_bytes:
            last, self.rbuf = nxt[:-extra_bytes], nxt[-extra_bytes:]
        else:
            last, self.rbuf = nxt, b''
        chunks.append(last)
        return b''.join(chunks)

    def send(self, data, flags=0, timeout=_UNSET):
        if timeout is _UNSET:
            timeout = self.timeout
        if flags:
            raise ValueError("flags not supported")
        sbuf = self.sbuf
        sbuf.append(data)
        if len(sbuf) > 1:
            sbuf[:] = [b''.join(sbuf)]
        self.sock.settimeout(timeout)
        start = time.time()
        try:
            while sbuf[0]:
                sent = self.sock.send(sbuf[0])
                sbuf[0] = sbuf[0][sent:]
                if timeout:
                    cur_timeout = timeout - (time.time() - start)
                    if cur_timeout <= 0.0:
                        raise socket.timeout()
                    self.sock.settimeout(cur_timeout)
        except socket.timeout:
            raise Timeout(timeout, '%s bytes unsent' % len(sbuf[0]))

    sendall = send

    def flush(self):
        self.send(b'')

    def buffer(self, data):
        self.sbuf.append(data)


class Error(socket.error, Exception):
    pass


class ConnectionClosed(Error):
    pass


class Timeout(socket.timeout, Error):
    def __init__(self, timeout, extra=""):
        msg = 'socket operation timed out'
        if timeout is not None:
            msg += ' after %sms' % (timeout * 1000)
        if extra:
            msg += '.' + extra
        super(Timeout, self).__init__(msg)


class NotFound(Error):
    def __init__(self, marker, bytes_read):
        msg = 'read %s bytes without finding marker: %r' % (marker, bytes_read)
        super(NotFound, self).__init__(msg)


class NetstringSocket(object):
    """
    Reads and writes using the netstring protocol.

    More info: https://en.wikipedia.org/wiki/Netstring
    Even more info: http://cr.yp.to/proto/netstrings.txt
    """
    def __init__(self, sock, timeout=30, maxsize=32 * 1024):
        self.bsock = BufferedSocket(sock)
        self.timeout = timeout
        self.maxsize = maxsize
        self._msgsize_maxsize = len(str(maxsize)) + 1  # len(str()) == log10

    def fileno(self):
        return self.bsock.fileno()

    def settimeout(self, timeout):
        self.timeout = timeout

    def setmaxsize(self, maxsize):
        self.maxsize = maxsize
        self._msgsize_maxsize = len(str(maxsize)) + 1  # len(str()) == log10

    def read_ns(self, timeout=_UNSET, maxsize=_UNSET):
        if timeout is _UNSET:
            timeout = self.timeout

        size_prefix = self.bsock.recv_until(b':',
                                            timeout=self.timeout,
                                            maxsize=self._msgsize_maxsize)

        size_bytes, sep, _ = size_prefix.partition(b':')

        if not sep:
            raise NetstringInvalidSize('netstring messages must start with'
                                       ' "size:", not %r' % size_prefix)
        try:
            size = int(size_bytes)
        except ValueError:
            raise NetstringInvalidSize('netstring message size must be valid'
                                       ' integer, not %r' % size_bytes)

        if size > self.maxsize:
            raise NetstringMessageTooLong(size, self.maxsize)
        payload = self.bsock.recv_size(size)
        if self.bsock.recv(1) != b',':
            raise NetstringProtocolError("expected trailing ',' after message")

        return payload

    def write_ns(self, payload):
        size = len(payload)
        if size > self.maxsize:
            raise NetstringMessageTooLong(size, self.maxsize)
        data = str(size).encode('ascii') + b':' + payload + b','
        self.bsock.send(data)


class NetstringProtocolError(Error):
    pass


class NetstringInvalidSize(NetstringProtocolError):
    def __init__(self, msg):
        super(NetstringMessageTooLong, self).__init__(msg)


class NetstringMessageTooLong(NetstringProtocolError):
    def __init__(self, size, maxsize):
        msg = ('netstring message length exceeds configured maxsize: %s > %s'
               % (size, maxsize))
        super(NetstringMessageTooLong, self).__init__(msg)
