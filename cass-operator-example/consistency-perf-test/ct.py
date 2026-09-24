"""Cassandra consistency test under load: ledger writer/reader, watermark, verifier.

Modes (first argument):

  schema     Create the keyspace and tables, and wait until every node agrees on the schema.
             Run it ONCE, as a Job, before starting the load. This is a precaution: concurrent
             CREATE TABLE statements from several clients are a known source of schema
             disagreement in Cassandra, and a test harness should not add its own failure mode
             to the one it is trying to measure.

  load       Run by every pod of the `ct-load` Deployment. Each pod is one WRITER, identified
             by its pod name. It writes numbered rows (seq = 1, 2, 3, ...) to its own ledger,
             each row carrying a CRC32 of its content, with many writes in flight at once. It
             retries a failed write until it succeeds, so its sequence has no deliberate holes.
             It publishes ACKED, the highest seq such that EVERY seq <= ACKED has been
             acknowledged at QUORUM, to the `progress` table once a second. Reader threads read
             random already-acknowledged rows and check their CRC, to add read load and catch
             corruption while the cluster is running.

  fill N     Deduplication measurement (Step 6 in AGENTS.md): write N rows of random data for the
             writer "dedup", then exit. Not used by the consistency test.

  change PCT N
             Rewrite PCT percent of the N "dedup" rows, chosen at random, with new random payloads
             (the realistic delta between two backups), then exit.

  watermark  Print {writer: acked} for every writer, read at QUORUM. Run it immediately before
             triggering the backup: every row at or below the watermark was acknowledged before
             the blueprint's flush started, so it MUST be in the restore point.

  verify     Read the watermark JSON (path in WATERMARK_FILE) and check the restored ledger:
               - every row at or below the watermark is present          -> else FAIL
               - every row present has a correct CRC                      -> else FAIL
               - rows above the watermark (written during the backup) are counted, and holes
                 among them are reported. Those are writes acknowledged while the nodes were
                 being snapshotted; whether each one is in the restore point depends on the
                 exact instant each node's snapshot was cut, so they are reported, not judged.

Environment: CASSANDRA_HOST, CASSANDRA_DC, CASSANDRA_USER, CASSANDRA_PASSWORD, and for load:
MAX_INFLIGHT (default 64), READER_THREADS (default 4), PAYLOAD_BYTES (default 512).
CT_KEYSPACE (default consistency) selects the keyspace; use a new one for each run.
"""

import json
import os
import random
import socket
import sys
import threading
import time
import zlib

from cassandra import ConsistencyLevel
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy

# One keyspace per test run, so a run never has to drop the previous one (a schema change on a
# cluster that is still replaying hints or repairing is itself a risk the test should not add).
KS = os.environ.get("CT_KEYSPACE", "consistency")
BUCKET = 10000  # rows per partition; keeps partitions around 5-10 MB


def connect(cl=ConsistencyLevel.QUORUM):
    profile = ExecutionProfile(
        load_balancing_policy=TokenAwarePolicy(DCAwareRoundRobinPolicy(local_dc=os.environ["CASSANDRA_DC"])),
        consistency_level=cl,
        request_timeout=30,
    )
    cluster = Cluster(
        [os.environ["CASSANDRA_HOST"]],
        auth_provider=PlainTextAuthProvider(os.environ["CASSANDRA_USER"], os.environ["CASSANDRA_PASSWORD"]),
        execution_profiles={EXEC_PROFILE_DEFAULT: profile},
        protocol_version=5,
    )
    return cluster, cluster.connect()


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def crc(writer, seq, payload):
    return zlib.crc32(f"{writer}|{seq}|".encode() + payload)


def ensure_schema(s):
    s.execute(f"CREATE KEYSPACE IF NOT EXISTS {KS} WITH replication = "
              "{'class': 'NetworkTopologyStrategy', '%s': 3}" % os.environ["CASSANDRA_DC"])
    s.execute(f"CREATE TABLE IF NOT EXISTS {KS}.ledger (writer text, bucket int, seq bigint, "
              "payload blob, crc bigint, PRIMARY KEY ((writer, bucket), seq))")
    s.execute(f"CREATE TABLE IF NOT EXISTS {KS}.progress (writer text PRIMARY KEY, acked bigint, "
              "updated timestamp)")


def load():
    writer = socket.gethostname()
    inflight_max = int(os.environ.get("MAX_INFLIGHT", "64"))
    readers = int(os.environ.get("READER_THREADS", "4"))
    size = int(os.environ.get("PAYLOAD_BYTES", "512"))

    while True:
        try:
            cluster, s = connect()
            if cluster.metadata.keyspaces.get(KS) and "ledger" in cluster.metadata.keyspaces[KS].tables:
                break
            print("waiting for the schema (run the ct-schema Job)", flush=True)
            cluster.shutdown()
        except Exception as e:  # cluster still starting
            print(f"waiting for cluster: {e}", flush=True)
        time.sleep(10)

    ins = s.prepare(f"INSERT INTO {KS}.ledger (writer, bucket, seq, payload, crc) VALUES (?, ?, ?, ?, ?)")
    sel = s.prepare(f"SELECT payload, crc FROM {KS}.ledger WHERE writer=? AND bucket=? AND seq=?")
    sel_all = s.prepare(f"SELECT payload, crc FROM {KS}.ledger WHERE writer=? AND bucket=? AND seq=?")
    sel_all.consistency_level = ConsistencyLevel.ALL
    prog = s.prepare(f"INSERT INTO {KS}.progress (writer, acked, updated) VALUES (?, ?, toTimestamp(now()))")

    lock = threading.Lock()
    sem = threading.Semaphore(inflight_max)
    done = set()          # acknowledged seqs above `acked`
    state = {"acked": 0, "writes": 0, "werr": 0, "reads": 0, "rerr": 0, "bad": 0}

    def submit(seq):
        payload = random.randbytes(size)
        fut = s.execute_async(ins, (writer, seq // BUCKET, seq, payload, crc(writer, seq, payload)))
        fut.add_callbacks(on_ok, on_err, callback_args=(seq,), errback_args=(seq,))

    def on_ok(_, seq):
        with lock:
            state["writes"] += 1
            done.add(seq)
            while state["acked"] + 1 in done:
                state["acked"] += 1
                done.discard(state["acked"])
        sem.release()

    def on_err(exc, seq):
        with lock:
            state["werr"] += 1
        # Retry the SAME seq, so the only holes in the ledger are the ones a backup creates.
        # A timer, not time.sleep: this callback runs on the driver's I/O thread.
        threading.Timer(0.5, submit, (seq,)).start()

    def reader():
        while True:
            hi = state["acked"]
            if hi < 1:
                time.sleep(1)
                continue
            seq = random.randint(1, hi)
            try:
                rs = s.execute(sel, (writer, seq // BUCKET, seq))
                row = rs.one()
                with lock:
                    state["reads"] += 1
                if row is None or row.crc != crc(writer, seq, row.payload):
                    with lock:
                        state["bad"] += 1
                    what = "missing" if row is None else "bad crc"
                    coord = rs.response_future.coordinator_host if rs.response_future else "?"
                    # Re-read at ALL to tell a transient read anomaly from data that is really
                    # missing or wrong on the replicas.
                    later = []
                    for delay in (1, 5):
                        time.sleep(delay)
                        r2 = s.execute(sel_all, (writer, seq // BUCKET, seq)).one()
                        later.append("missing" if r2 is None else
                                     ("ok" if r2.crc == crc(writer, seq, r2.payload) else "bad crc"))
                    print(f"{ts()} READ CHECK FAILED seq={seq} row={what} coordinator={coord} "
                          f"reread_ALL_after_1s={later[0]} reread_ALL_after_6s={later[1]}", flush=True)
            except Exception:
                with lock:
                    state["rerr"] += 1
                time.sleep(0.5)

    def reporter():
        last_w = last_r = 0
        while True:
            time.sleep(1)
            try:
                s.execute(prog, (writer, state["acked"]))
            except Exception:
                pass
            now = int(time.time())
            if now % 10 == 0:
                with lock:
                    w, r = state["writes"], state["reads"]
                    print(f"{ts()} acked={state['acked']} writes/s={(w - last_w) / 10:.0f} reads/s={(r - last_r) / 10:.0f} "
                          f"write_errors={state['werr']} read_errors={state['rerr']} read_check_failures={state['bad']}",
                          flush=True)
                    last_w, last_r = w, r

    for _ in range(readers):
        threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()

    # Resume after the pod restarts on the same name (it does not: Deployment pods get new
    # names, so a restarted pod is a NEW writer and never collides with an old ledger).
    seq = 0
    while True:
        sem.acquire()
        seq += 1
        submit(seq)


def schema():
    cluster, s = connect()
    ensure_schema(s)
    # Wait for every node to agree on the schema before any client writes.
    if not cluster.control_connection.wait_for_schema_agreement(wait_time=120):
        print("ERROR: nodes do not agree on the schema after 120s")
        sys.exit(1)
    print(f"schema ready: keyspace {KS}, tables ledger and progress, all nodes agree")
    cluster.shutdown()


def _bulk(s, seqs, size):
    """Write `seqs` for writer "dedup" with fresh random payloads, 256 in flight at a time."""
    ins = s.prepare(f"INSERT INTO {KS}.ledger (writer, bucket, seq, payload, crc) VALUES (?, ?, ?, ?, ?)")
    futures = []
    for seq in seqs:
        payload = random.randbytes(size)
        futures.append(s.execute_async(ins, ("dedup", seq // BUCKET, seq, payload, crc("dedup", seq, payload))))
        if len(futures) >= 256:
            for f in futures:
                f.result()
            futures = []
    for f in futures:
        f.result()


def fill():
    n = int(sys.argv[2])
    cluster, s = connect()
    _bulk(s, range(1, n + 1), int(os.environ.get("PAYLOAD_BYTES", "256")))
    print(f"{ts()} wrote {n} rows of {os.environ.get('PAYLOAD_BYTES', '256')} random bytes to {KS}.ledger")
    cluster.shutdown()


def change():
    pct, total = float(sys.argv[2]), int(sys.argv[3])
    cluster, s = connect()
    picked = random.sample(range(1, total + 1), int(total * pct / 100))
    _bulk(s, picked, int(os.environ.get("PAYLOAD_BYTES", "256")))
    print(f"{ts()} rewrote {len(picked)} of {total} rows ({pct}%) with new random payloads")
    cluster.shutdown()


def watermark():
    cluster, s = connect()
    rows = s.execute(f"SELECT writer, acked FROM {KS}.progress")
    print(json.dumps({r.writer: r.acked for r in rows}, sort_keys=True))
    cluster.shutdown()


def verify():
    with open(os.environ["WATERMARK_FILE"]) as f:
        wm = json.load(f)
    cl = ConsistencyLevel.name_to_value[os.environ.get("VERIFY_CONSISTENCY", "ALL")]
    cluster, s = connect(cl)
    q = s.prepare(f"SELECT seq, payload, crc FROM {KS}.ledger WHERE writer=? AND bucket=?")
    q.fetch_size = 2000

    ok = True
    total = {"expected": 0, "missing": 0, "bad_crc": 0, "after": 0, "holes_after": 0}
    print(f"{'writer':40} {'watermark':>10} {'missing':>8} {'bad_crc':>8} {'after_wm':>9} {'max_seq':>8} {'holes_after':>11}")
    for writer, acked in sorted(wm.items()):
        seqs = set()
        bad = 0
        b = 0
        while True:
            rows = list(s.execute(q, (writer, b)))
            if not rows and b * BUCKET > acked:
                break
            for r in rows:
                seqs.add(r.seq)
                if r.crc != crc(writer, r.seq, r.payload):
                    bad += 1
            b += 1
        missing = sum(1 for i in range(1, acked + 1) if i not in seqs)
        after = [i for i in seqs if i > acked]
        mx = max(seqs) if seqs else 0
        holes = (mx - acked - len(after)) if after else 0
        print(f"{writer:40} {acked:>10} {missing:>8} {bad:>8} {len(after):>9} {mx:>8} {holes:>11}")
        total["expected"] += acked
        total["missing"] += missing
        total["bad_crc"] += bad
        total["after"] += len(after)
        total["holes_after"] += holes
        if missing or bad:
            ok = False
    print(json.dumps(total))
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    cluster.shutdown()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    {"schema": schema, "load": load, "watermark": watermark, "verify": verify,
     "fill": fill, "change": change}[sys.argv[1]]()
