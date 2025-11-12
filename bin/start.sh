#!/bin/sh
. /usr/local/share/rt/bin/activate
cd /usr/local/share/telco.chat || exit 1

/usr/pkg/sbin/daemonize \
  -u bjjl \
  -c /usr/local/share/telco.chat \
  -o /tmp/telco.log -e /tmp/telco.log \
  -p /var/run/telco.pid -l /var/run/telco.lock \
  /usr/local/share/rt/bin/python app.py
