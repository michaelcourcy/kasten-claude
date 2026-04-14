# On the source cluster 

Create a backup 
```
BACKUP_DIR=/mnt/backup/backup
su - db2inst1 -c "db2 backup db BLUDB online to $BACKUP_DIR compress include logs"
```

Get the label used by the database 
```
su - db2inst1 -c "/mnt/blumeta0/home/db2inst1/sqllib/adm/db2pd -db BLUDB -encrypt" 
```

In the output of this command you get the master key label 
```
Master Key Label: DB2_SYSGEN_db2inst1_BLUDB_2026-03-19-01.33.01_8628446B
```

Notice that this is also this label that you can read on the /mnt/blumeta0/db2/keystore/keystore.p12
```
su - db2inst1 -c " ls /mnt/blumeta0/db2/keystore/"
su - db2inst1 -c "/opt/ibm/db2/V11.5.0.0/gskit/bin/gsk8capicmd_64 -cert -list -db /mnt/blumeta0/db2/keystore/keystore.p12 -stashed"
* default, - personal, ! trusted, # secret key
#       DB2_SYSGEN_db2inst1_BLUDB_2026-03-19-01.33.01_8628446B
```

Then create the raw file with its stached 
```
su - db2inst1 -c "/opt/ibm/db2/V11.5.0.0/gskit/bin/gsk8capicmd_64 -cert -export \
  -db /mnt/blumeta0/db2/keystore/keystore.p12 \
  -stashed \
  -label DB2_SYSGEN_db2inst1_BLUDB_2026-03-19-01.33.01_8628446B \
  -target /mnt/backup/backup/BLUDB-source.raw -target_type pkcs12 -target_stashed"
```
 
# With kasten export from the source and restore the backup pvc in the destination cluster 

Scaled down db2u, restore only the backup pvc then scale up.
 
Now import the key in the key store of the destination 
```
su - db2inst1 -c "opt/ibm/db2/V11.5.0.0/gskit/bin/gsk8capicmd_64 \
   -cert -import -db /mnt/backup/backup/BLUDB-source.raw \
   -stashed -target /mnt/blumeta0/db2/keystore/keystore.p12 \
   -target_stashed -target_type pkcs12"
``` 

find the timestamp of the backup 
```
su - db2inst1 "ls /mnt/backup/backup/"
```

From the output 
```
BLUDB.0.db2inst1.DBPART000.20260414085801.001  BLUDB-source.raw  BLUDB-source.sth  keystore.p12  keystore.sth
```

You get the timestamp `20260414085801`

Now restore 
```
su - db2inst1 "cd /mnt/backup/backup/ && \
    db2 force application all \
    db2 terminate \
    db2 deactivate db bludb \
    db2 restore db bludb  taken at 20260414085801 on /mnt/blumeta0/db2/databases dbpath on /mnt/blumeta0/db2/databases" \
    db2 rollforward sb bludh to end of logs and complete \
    db2 rollforward db bludb stop"
```

