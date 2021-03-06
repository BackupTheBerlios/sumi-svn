#!/bin/sh
RLS="0.8.15"

rm *.pyc
cd /home/jeff/p2p/
echo -n "archiving " 
tar -hvcf sumi-${RLS}.tar --exclude sumi/build --exclude sumi/dist --exclude sumi/incoming --exclude sumi/share --exclude sumi/otp --exclude sumi/c++-obsolete/sender --exclude sumi/c++-obsolete/sumiserv --exclude sumi/hash/mddriver --exclude sumi/hash/test --exclude sumi/rawproxd --exclude sumi/\*.pyc --exclude sumi/build\* --exclude sumi/dist\* --exclude sumi/\*.exe --exclude \*.svn\* --exclude \*.exe --exclude sumi/sumi-repos.gz --exclude doc/*.sql --exclude *.sql --exclude doc/wiki.xml.gz --exclude *.tmp sumi/*

echo -n "compressing "
bzip2 -f sumi-${RLS}.tar
echo 
ls -lh sumi-${RLS}.tar.bz2
cd sumi

