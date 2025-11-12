#
# Copyright (c) 2025 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#
# syslog_to_mongodb connector
# for /var/log/maillog messages (NetBSD 10 postfix + dovecot)
#

import pymongo, time, os, re, ipinfo
from datetime import datetime, timezone

MCONN = os.getenv('MONGODB_IST_MEDIA')
MBASE = "1_media_demo"

collection = pymongo.MongoClient(MCONN)[MBASE]["syslog"]
LOG_FILE = "/var/log/maillog"

access_token = 'd81edaace34b9d' # for ipinfo, registered under my name
handler = ipinfo.getHandler(access_token)

def lookup_ip(ip):
    details = handler.getDetails(ip)
    return {
        "country": getattr(details, "country", None) or " - "
    }

def follow_rotating_file(path):
    """Yields lines from a file that might be rotated"""
    file = open(path, "r")
    file.seek(0, os.SEEK_END)

    inode = os.fstat(file.fileno()).st_ino

    while True:
        line = file.readline()
        if line:
            yield line.strip()
        else:
            try:
                if os.stat(path).st_ino != inode:
                    # Datei wurde ersetzt (rotation)
                    file.close()
                    file = open(path, "r")
                    inode = os.fstat(file.fileno()).st_ino
                else:
                    time.sleep(0.5)
            except FileNotFoundError:
                time.sleep(0.5)

def follow(file):
    file.seek(0, os.SEEK_END)
    while True:
        line = file.readline()
        if not line:
            time.sleep(0.5)
            continue
        yield line.strip()

def parse_syslog_line(line):
    result = {
        "timestamp": None,
        "type": "unknown",
        "raw": line
    }

    # Extract timestamp, hostname, and message
    match = re.match(r"^(\w+\s+\d+\s+\d+:\d+:\d+)\s+(\S+)\s+(.*)$", line)
    if not match:
        return result

    timestamp_str, hostname, message = match.groups()
    try:
        parsed_date = datetime.strptime(timestamp_str, "%b %d %H:%M:%S")
        parsed_date = parsed_date.replace(
            year=datetime.now(timezone.utc).year,
            tzinfo=timezone.utc
        )
        result["timestamp"] = parsed_date
    except ValueError:
        pass

    # SSH: publickey login
    if "sshd" in message and "Accepted publickey for" in message:
        result["type"] = "ssh_auth"
        m = re.search(r"Accepted publickey for (\S+) from ([\d\.]+) port (\d+)", message)
        if m:
            result.update({
                "user": m.group(1),
                "remote_ip": m.group(2),
                "remote_port": int(m.group(3)),
                "method": "publickey"
            })

    # SSH: key exchange / auth detail
    elif "sshd" in message and "SSH: Server;Ltype:" in message:
        result["type"] = "ssh_detail"
        parts = dict(re.findall(r"(\w+):\s*([^;]+)", message))
        result.update(parts)

    # Dovecot: IMAP login
    elif "dovecot: imap-login:" in message and "Login:" in message:
        result["type"] = "imap_login"
        m = re.search(r"user=<([^>]+)>, method=(\S+), rip=([\d\.]+), lip=([\d\.]+).*session=<([^>]+)>", message)
        if m:
            result.update({
                "user": m.group(1),
                "method": m.group(2),
                "remote_ip": m.group(3),
                "local_ip": m.group(4),
                "session": m.group(5),
                "tls": "TLS" in message
            })

    # Dovecot: IMAP disconnect
    elif "dovecot: imap(" in message and "Disconnected:" in message:
        result["type"] = "imap_disconnect"
        m = re.search(r"imap\(([^)]+)\)<\d+><([^>]+)>: Disconnected: (.+?) in=(\d+) out=(\d+)", message)
        if m:
            result.update({
                "user": m.group(1),
                "session": m.group(2),
                "reason": m.group(3).strip(),
                "bytes_in": int(m.group(4)),
                "bytes_out": int(m.group(5))
            })

        extras = {
            "deleted":      r"deleted=(\d+)",
            "expunged":     r"expunged=(\d+)",
            "trashed":      r"trashed=(\d+)",
            "hdr_count":    r"hdr_count=(\d+)",
            "hdr_bytes":    r"hdr_bytes=(\d+)",
            "body_count":   r"body_count=(\d+)",
            "body_bytes":   r"body_bytes=(\d+)"
        }
        for key, pattern in extras.items():
            match = re.search(pattern, message)
            if match:
                result[key] = int(match.group(1))

        state_match = re.search(r"state=([\w\-]+)", message)
        if state_match:
            result["state"] = state_match.group(1)

    # Logrotate
    elif "newsyslog" in message and "log file turned over" in message:
        result["type"] = "logrotate"

    # Postfix local delivery
    elif "postfix/local" in message and "status=sent" in message:
        result["type"] = "postfix_delivery"
        m = re.search(
            r'postfix/local\[\d+\]: ([A-F0-9]+): to=<([^>]+)>, orig_to=<([^>]+)>, relay=(\S+), delay=([\d\.]+), delays=([\d\.\/]+), dsn=([\d\.]+), status=(\w+)',
            message
        )
        if m:
            result.update({
                "queue_id": m.group(1),
                "to": m.group(2),
                "orig_to": m.group(3),
                "relay": m.group(4),
                "delay_total": float(m.group(5)),
                "delay_breakdown": m.group(6),
                "dsn": m.group(7),
                "delivery_status": m.group(8)
            })

    # Postfix: client connection
    elif "postfix/smtpd" in message and "client=" in message:
        result["type"] = "postfix_smtpd_client"
        m = re.search(r'client=([^[]+)\[([\d\.]+)\]', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "client_ip": m.group(2)
            })

    # Postfix: cleanup creates message-id
    elif "postfix/cleanup" in message and "message-id=" in message:
        result["type"] = "postfix_cleanup"
        m = re.search(r'message-id=<([^>]+)>', message)
        if m:
            result["message_id"] = m.group(1)

    # Postfix: queue manager - accepted into queue
    elif "postfix/qmgr" in message and "from=" in message and "queue active" in message:
        result["type"] = "postfix_qmgr_enqueue"
        m = re.search(r'([A-F0-9]+): from=<([^>]+)>, size=(\d+), nrcpt=(\d+)', message)
        if m:
            result.update({
                "queue_id": m.group(1),
                "from": m.group(2),
                "size": int(m.group(3)),
                "nrcpt": int(m.group(4))
            })

    # Postfix: message removed from queue
    elif "postfix/qmgr" in message and "removed" in message:
        result["type"] = "postfix_qmgr_remove"
        m = re.search(r'([A-F0-9]+): removed', message)
        if m:
            result["queue_id"] = m.group(1)

    # Postfix: smtpd disconnect summary
    elif "postfix/smtpd" in message and "disconnect from" in message:
        result["type"] = "postfix_smtpd_disconnect"
        m = re.search(r'disconnect from ([^[]+)\[([\d\.]+)\].*commands=(\d+)', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "client_ip": m.group(2),
                "command_count": int(m.group(3))
            })

    # Postfix: postscreen connect
    elif "postfix/postscreen" in message and "CONNECT from" in message:
        result["type"] = "postfix_postscreen_connect"
        m = re.search(r'CONNECT from \[([\d\.]+)\]:\d+ to \[([\d\.]+)\]:\d+', message)
        if m:
            result.update({
                "remote_ip": m.group(1),
                "local_ip": m.group(2)
            })

    # Postfix: postscreen PREGREET
    elif "postfix/postscreen" in message and "PREGREET" in message:
        result["type"] = "postfix_postscreen_pregreet"
        m = re.search(r'PREGREET (\d+) after ([\d\.]+) from \[([\d\.]+)\]:\d+', message)
        if m:
            result.update({
                "early_bytes": int(m.group(1)),
                "delay": float(m.group(2)),
                "remote_ip": m.group(3)
            })

    # Postfix: DNSBL-Eintrag
    elif "postfix/dnsblog" in message and "listed by domain" in message:
        result["type"] = "postfix_dnsblog_listed"
        m = re.search(r'addr ([\d\.]+) listed by domain ([\w\.-]+)', message)
        if m:
            result.update({
                "remote_ip": m.group(1),
                "dnsbl_domain": m.group(2)
            })

    # Postfix: DNSBL rank
    elif "postfix/postscreen" in message and "DNSBL rank" in message:
        result["type"] = "postfix_postscreen_dnsbl_rank"
        m = re.search(r'DNSBL rank (\d+) for \[([\d\.]+)\]:\d+', message)
        if m:
            result.update({
                "dnsbl_rank": int(m.group(1)),
                "remote_ip": m.group(2)
            })

    # Postfix: postscreen disconnect
    elif "postfix/postscreen" in message and "DISCONNECT" in message:
        result["type"] = "postfix_postscreen_disconnect"
        m = re.search(r'DISCONNECT \[([\d\.]+)\]:\d+', message)
        if m:
            result["remote_ip"] = m.group(1)

    # Postfix: postscreen PASS OLD
    elif "postfix/postscreen" in message and "PASS OLD" in message:
        result["type"] = "postfix_postscreen_pass"
        m = re.search(r'PASS OLD \[([\d\.]+)\]:\d+', message)
        if m:
            result["remote_ip"] = m.group(1)

    # Postfix: smtpd connect from client
    elif "postfix/smtpd" in message and "connect from" in message:
        result["type"] = "postfix_smtpd_connect"
        m = re.search(r'connect from ([^[]+)\[([\d\.]+)\]', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "client_ip": m.group(2)
            })

    # Postfix: smtpd TLS connection
    elif "postfix/smtpd" in message and "TLS connection established" in message:
        result["type"] = "postfix_smtpd_tls"
        m = re.search(
            r'from ([^[]+)\[([\d\.]+)\]: TLSv([\d\.]+) with cipher (\S+) \((\d+)/(\d+) bits\) key-exchange (\S+) server-signature (\S+) \((\d+) bits\) server-digest (\S+)',
            message
        )
        if m:
            result.update({
                "client_host": m.group(1),
                "client_ip": m.group(2),
                "tls_version": "TLSv" + m.group(3),
                "cipher": m.group(4),
                "cipher_bits": int(m.group(5)),
                "cipher_bits_max": int(m.group(6)),
                "key_exchange": m.group(7),
                "server_signature": m.group(8),
                "server_key_bits": int(m.group(9)),
                "server_digest": m.group(10)
            })

    # Postfix: SMTP remote delivery
    elif "postfix/smtp" in message and "status=sent" in message:
        result["type"] = "postfix_smtp_delivery"
        m = re.search(
            r'([A-F0-9]+): to=<([^>]+)>, relay=([^\[]+)\[([\d\.]+)\]:\d+, delay=([\d\.]+), delays=([\d\.\/]+), dsn=([\d\.]+), status=(\w+)',
            message
        )
        if m:
            result.update({
                "queue_id": m.group(1),
                "to": m.group(2),
                "relay_host": m.group(3),
                "relay_ip": m.group(4),
                "delay_total": float(m.group(5)),
                "delay_breakdown": m.group(6),
                "dsn": m.group(7),
                "delivery_status": m.group(8)
            })
        mq = re.search(r'queued as ([A-F0-9]+)', message)
        if mq:
            result["remote_queue_id"] = mq.group(1)

    # Postfix: postscreen hangup before handshake
    elif "postfix/postscreen" in message and "HANGUP" in message:
        result["type"] = "postfix_postscreen_hangup"
        m = re.search(r'HANGUP after (\d+) from \[([\d\.]+)\]:\d+', message)
        if m:
            result.update({
                "hangup_delay": int(m.group(1)),
                "remote_ip": m.group(2)
            })

    elif "postfix/smtpd" in message and "lost connection after STARTTLS" in message:
        result["type"] = "postfix_smtpd_tls_lost"
        m = re.search(r'lost connection after STARTTLS from ([^\[]+)\[([\d\.]+)\]', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "client_ip": m.group(2)
            })

    elif "postfix/anvil" in message and "statistics: max cache size" in message:
        result["type"] = "postfix_anvil_stat_cache"
        m = re.search(r'statistics: max cache size (\d+) at (.+)$', message)
        if m:
            result.update({
                "cache_size": int(m.group(1)),
                "event_time": m.group(2)
            })

    elif "postfix/anvil" in message and "statistics: max connection rate" in message:
        result["type"] = "postfix_anvil_stat_rate"
        m = re.search(r'max connection rate (\d+)/(\d+)s for \(([^:]+):([\d\.]+)\) at (.+)$', message)
        if m:
            result.update({
                "rate_count": int(m.group(1)),
                "rate_window_sec": int(m.group(2)),
                "protocol": m.group(3),
                "remote_ip": m.group(4),
                "event_time": m.group(5)
            })

    elif "postfix/anvil" in message and "statistics: max connection count" in message:
        result["type"] = "postfix_anvil_stat_count"
        m = re.search(r'max connection count (\d+) for \(([^:]+):([\d\.]+)\) at (.+)$', message)
        if m:
            result.update({
                "connection_count": int(m.group(1)),
                "protocol": m.group(2),
                "remote_ip": m.group(3),
                "event_time": m.group(4)
            })

    elif "postfix/smtpd" in message and "NOQUEUE: reject: RCPT from" in message:
        result["type"] = "postfix_smtpd_reject"
        m = re.search(
            r'RCPT from ([^\[]+)\[([\d\.]+)\]: (\d+) ([\d\.]+) <([^>]+)>: ([^;]+); from=<([^>]+)> to=<([^>]+)> proto=(\S+) helo=<([^>]+)>',
            message
        )
        if m:
            result.update({
                "client_host": m.group(1),
                "remote_ip": m.group(2),
                "smtp_code": m.group(3),
                "smtp_status": m.group(4),
                "to": m.group(5),
                "reason": m.group(6),
                "from": m.group(7),
                "proto": m.group(8),
                "helo": m.group(9)
            })

    elif "postfix/smtpd" in message and "improper command pipelining after CONNECT" in message:
        result["type"] = "postfix_smtpd_pipelining_violation"
        m = re.search(r'from (\S+)\[([\d\.]+)\]', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "remote_ip": m.group(2)
            })

    elif "postfix/smtpd" in message and "non-SMTP command from" in message:
        result["type"] = "postfix_smtpd_non_smtp_command"
        m = re.search(r'from (\S+)\[([\d\.]+)\]: (.+)$', message)
        if m:
            result.update({
                "client_host": m.group(1),
                "remote_ip": m.group(2),
                "non_smtp_command": m.group(3)
            })

    elif "postfix/smtpd" in message and "does not resolve to address" in message:
        result["type"] = "postfix_smtpd_dns_mismatch"
        m = re.search(r'hostname (\S+) does not resolve to address ([\d\.]+)', message)
        if m:
            result.update({
                "hostname": m.group(1),
                "remote_ip": m.group(2)
            })

    elif "dovecot: imap-login:" in message and "SSL_accept() failed" in message:
        result["type"] = "dovecot_ssl_protocol_error"
        m = re.search(r'rip=([\d\.]+), lip=([\d\.]+).*session=<([^>]+)>', message)
        if m:
            result.update({
                "remote_ip": m.group(1),
                "local_ip": m.group(2),
                "session": m.group(3)
            })

    # add geo information
    for ip_field in ("remote_ip", "client_ip"):
        if ip_field in result:
            result.update(lookup_ip(result[ip_field]))

    return result

for line in follow_rotating_file(LOG_FILE):
    doc = parse_syslog_line(line)
    doc["host"] = os.uname().nodename
    collection.insert_one(doc)
