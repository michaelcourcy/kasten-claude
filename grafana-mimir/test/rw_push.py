#!/usr/bin/env python3
"""
Dependency-free Prometheus remote-write pusher for Grafana Mimir.

Hand-rolls the WriteRequest protobuf and a literal-only Snappy block so it runs
on a plain python:3.x-slim image with NO pip installs. Used only to seed a small,
known, verifiable dataset for backup/restore validation.

Usage:
  rw_push.py <push-url> <tenant> [value_offset]

Pushes 5 series named `kasten_backup_test` with label series="0".."4",
sample value = 100 + i + value_offset, timestamp = now (ms).
"""
import sys, time, struct, urllib.request

# ---- minimal protobuf wire encoding ----------------------------------------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def _tag(field, wire):
    return _varint((field << 3) | wire)

def _len_delim(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload

def _string(field, s):
    return _len_delim(field, s.encode("utf-8"))

def _label(name, value):                       # Label{1:name,2:value}
    return _string(1, name) + _string(2, value)

def _sample(value, ts_ms):                      # Sample{1:double value,2:int64 ts}
    return _tag(1, 1) + struct.pack("<d", value) + _tag(2, 0) + _varint(ts_ms)

def _timeseries(labels, value, ts_ms):          # TimeSeries{1:labels,2:samples}
    body = b"".join(_len_delim(1, _label(n, v)) for n, v in labels)
    body += _len_delim(2, _sample(value, ts_ms))
    return body

def _write_request(series_list):                # WriteRequest{1:timeseries}
    return b"".join(_len_delim(1, ts) for ts in series_list)

# ---- literal-only snappy block ("compression" via raw literals) -------------
def snappy_literals(data):
    out = bytearray(_varint(len(data)))         # preamble: uncompressed length
    i = 0
    n = len(data)
    while i < n:
        chunk = data[i:i + 60]                   # literal len <= 60 -> 1 tag byte
        out.append((len(chunk) - 1) << 2)        # tag: (len-1)<<2 | 0b00 (literal)
        out.extend(chunk)
        i += 60
    return bytes(out)

def main():
    url = sys.argv[1]
    tenant = sys.argv[2]
    offset = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    ts_ms = int(time.time() * 1000)

    series = []
    for i in range(5):
        labels = [("__name__", "kasten_backup_test"),
                  ("series", str(i)),
                  ("job", "kasten-validation")]
        series.append(_timeseries(labels, 100 + i + offset, ts_ms))

    body = snappy_literals(_write_request(series))
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-protobuf")
    req.add_header("Content-Encoding", "snappy")
    req.add_header("X-Prometheus-Remote-Write-Version", "0.1.0")
    req.add_header("X-Scope-OrgID", tenant)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        print(f"push OK: HTTP {resp.status}, {len(series)} series at ts={ts_ms} (offset={offset})")
    except urllib.error.HTTPError as e:
        print(f"push FAILED: HTTP {e.code}: {e.read().decode(errors='replace')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
