#!/bin/sh

. /usr/local/share/rt/bin/activate
cd /usr/local/share/telco.chat
nohup python app.py >/tmp/telco.log 2>&1 &
