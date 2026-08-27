#!/usr/bin/env bash
# Test-data helper for the Aerospike ABS blueprint.
#
# Creates / verifies / mutates a deliberately tiny dataset (~1KB) in the
# Aerospike namespace "test", set "demo". Small on purpose: the point is a fast
# development loop, not a performance test.
#
# Usage:
#   ./test-data.sh load               # insert 10 known records (key1..key10)
#   ./test-data.sh verify             # print all records
#   ./test-data.sh count              # print object count per node
#   ./test-data.sh delete-all         # truncate the set (simulates data loss)
#   ./test-data.sh delete-one <key>   # delete one record (Gap A test)
#   ./test-data.sh add-one <key>      # insert one extra record (incremental test)
#   ./test-data.sh sql "<statement>"  # run an arbitrary aql statement
#
# Requires: kubectl context pointing at the cluster hosting aerospike-test.
set -euo pipefail

NS="${NS:-aerospike-test}"
SEED="${SEED:-aerocluster.${NS}.svc.cluster.local}"
ASNS="${ASNS:-test}"
ASSET="${ASSET:-demo}"
TOOLS_IMAGE="${TOOLS_IMAGE:-aerospike/aerospike-tools:13.0.2}"

# Pipe one or more ';'-terminated aql statements through a single throwaway pod.
aql_batch() {
  kubectl run "aql-$$-$RANDOM" -n "$NS" --image="$TOOLS_IMAGE" --restart=Never --rm -i --quiet \
    --command -- aql -h "$SEED" -p 3000 --timeout=10000 <<<"$1"
}

case "${1:-}" in
  load)
    stmts=""
    for i in $(seq 1 10); do
      stmts+="INSERT INTO ${ASNS}.${ASSET} (PK, id, name, city) VALUES ('key${i}', ${i}, 'user-${i}', 'city-${i}');"$'\n'
    done
    aql_batch "$stmts"
    echo "Loaded 10 records into ${ASNS}.${ASSET}"
    ;;
  verify)
    aql_batch "SELECT * FROM ${ASNS}.${ASSET};"
    ;;
  count)
    for p in 0 1 2; do
      echo -n "aerocluster-0-${p}: "
      kubectl exec -n "$NS" "aerocluster-0-${p}" -c aerospike-server -- \
        asinfo -v "namespace/${ASNS}" 2>/dev/null | tr ';' '\n' | grep -E "^(master_)?objects=" | tr '\n' ' '
      echo
    done
    ;;
  delete-all)
    aql_batch "TRUNCATE ${ASNS}.${ASSET};"
    echo "Truncated ${ASNS}.${ASSET}"
    ;;
  delete-one)
    aql_batch "DELETE FROM ${ASNS}.${ASSET} WHERE PK = '${2:?key required}';"
    echo "Deleted ${2}"
    ;;
  add-one)
    aql_batch "INSERT INTO ${ASNS}.${ASSET} (PK, id, name, city) VALUES ('${2:?key required}', 999, 'user-${2}', 'city-${2}');"
    echo "Inserted ${2}"
    ;;
  sql)
    aql_batch "${2:?statement required}"
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 1
    ;;
esac
