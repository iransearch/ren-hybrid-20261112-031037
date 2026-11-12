#!/usr/bin/env python3
"""REN control process. HAProxy, not Python, carries all HTTP/WebSocket bytes."""
from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass, replace, asdict
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time

LOG = logging.getLogger('ren_control')
BUILD = 'ren-hybrid-4-origin-trial'
ROOT = Path(__file__).resolve().parent
ORIGIN_KEYS = {'ORIGIN_IP', 'ORIGIN_PORT', 'ORIGIN_SNI', 'ORIGIN_ENABLED', 'ORIGIN_ROUTES_JSON', 'ORIGIN_KEEPALIVE_DOMAINS', 'ORIGIN_ERROR_LOGS'}
HOSTNAME = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def number(env, name, default, *, integer=False, zero=False):
    raw = env.get(name, default)
    if integer and not re.fullmatch(r'[0-9]+', str(raw)):
        raise ValueError(f'{name}: must be a non-negative integer')
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{name}: invalid number') from None
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f'{name}: must be finite and {">= 0" if zero else "> 0"}')
    return value


def boolean(raw, name):
    if raw is True or str(raw).lower() == 'true': return True
    if raw is False or str(raw).lower() == 'false': return False
    raise ValueError(f'{name}: use true or false')


@dataclass(frozen=True)
class Route:
    domain: str
    origin_ip: str
    origin_sni: str
    origin_port: int


def parse_routes(raw, allow_private=False):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
        raise ValueError('ORIGIN_ROUTES_JSON exceeds 64 KiB or is not text')
    try: rows = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('ORIGIN_ROUTES_JSON must be valid JSON') from None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('ORIGIN_ROUTES_JSON must contain 1..100 mappings')
    routes=[]; seen=set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'domain','origin_ip','origin_sni','origin_port'}:
            raise ValueError('Each route needs domain, origin_ip, origin_sni, origin_port only')
        domain=str(row['domain']).strip().lower().rstrip('.')
        host=str(row['origin_sni']).strip().lower().rstrip('.')
        if not HOSTNAME.fullmatch(domain) or domain.endswith('.onrender.com'):
            raise ValueError('Route domain must be a custom DNS hostname')
        if not HOSTNAME.fullmatch(host): raise ValueError('Route SNI must be a DNS hostname')
        if domain in seen: raise ValueError('Duplicate route domain: '+domain)
        seen.add(domain)
        if not isinstance(row['origin_ip'],str): raise ValueError('Route IP must be a dotted IPv4 string')
        try: address=ipaddress.IPv4Address(row['origin_ip'])
        except (ipaddress.AddressValueError, TypeError): raise ValueError('Route IP must be IPv4') from None
        if (not address.is_global or address.is_multicast) and not allow_private: raise ValueError('Route IP must be public IPv4')
        port=number({'port':row['origin_port']},'port',443,integer=True)
        if port>65535: raise ValueError('Route port must be 1..65535')
        routes.append(Route(domain,str(address),host,port))
    return tuple(routes)


def parse_domain_list(raw):
    if not isinstance(raw,str) or len(raw)>65536: raise ValueError('Invalid ORIGIN_KEEPALIVE_DOMAINS')
    domains=tuple(sorted(set(item.strip().lower().rstrip('.') for item in raw.split(',') if item.strip())))
    if len(domains)>100 or any(not HOSTNAME.fullmatch(item) for item in domains):
        raise ValueError('ORIGIN_KEEPALIVE_DOMAINS needs comma-separated DNS hostnames')
    return domains


@dataclass(frozen=True)
class Settings:
    origin_ip: str = ''
    origin_sni: str = ''
    routes: tuple = ()
    keepalive_domains: tuple = ()
    error_logs: bool = False
    origin_port: int = 443
    origin_enabled: bool = True
    port: int = 10000
    max_concurrent: int = 0  # 0 = no application admission cap, as requested.
    connect_timeout: float = 12
    http_read_timeout: float = 120
    ws_handshake_timeout: float = 20
    tunnel_idle_timeout: float = 3600
    stats_interval: float = 10
    poll_interval: float = 5
    threads: int = 1
    ca_file: str = str(ROOT / 'cloudflare_origin_ca_roots.pem')


def load_settings(env=None):
    env = dict(os.environ if env is None else env)
    source = env.get('ORIGIN_CONFIG_FILE')
    if source:
        path = Path(source)
        if path.stat().st_size > 16384: raise ValueError('origin settings file is too large')
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - ORIGIN_KEYS:
            raise ValueError('origin settings file contains unsupported keys')
        env.update(data)
    allow_private = boolean(env.get('ALLOW_PRIVATE_ORIGIN', 'false'), 'ALLOW_PRIVATE_ORIGIN')
    routes = parse_routes(env['ORIGIN_ROUTES_JSON'], allow_private) if 'ORIGIN_ROUTES_JSON' in env else ()
    host = ''; address = ''
    if not routes:
        host = str(env.get('ORIGIN_SNI', '')).strip().lower().rstrip('.')
        if not HOSTNAME.fullmatch(host): raise ValueError('ORIGIN_SNI must be a DNS hostname')
        try: address = ipaddress.IPv4Address(env.get('ORIGIN_IP', ''))
        except ipaddress.AddressValueError: raise ValueError('ORIGIN_IP must be IPv4') from None
        if not address.is_global and not allow_private: raise ValueError('ORIGIN_IP must be public IPv4')
    ca = Path(env.get('ORIGIN_CA_FILE', str(ROOT / 'cloudflare_origin_ca_roots.pem'))).resolve()
    ssl.create_default_context(cafile=str(ca))  # Reject missing/invalid trust roots before launch.
    result = Settings(
        str(address), host, routes=routes,
        keepalive_domains=parse_domain_list(env.get('ORIGIN_KEEPALIVE_DOMAINS','')),
        error_logs=boolean(env.get('ORIGIN_ERROR_LOGS','false'),'ORIGIN_ERROR_LOGS'),
        origin_port=443 if routes else number(env,'ORIGIN_PORT',443,integer=True),
        origin_enabled=True if routes else boolean(env.get('ORIGIN_ENABLED','true'),'ORIGIN_ENABLED'),
        port=number(env,'PORT',10000,integer=True),
        max_concurrent=number(env,'MAX_CONCURRENT',0,integer=True,zero=True),
        connect_timeout=number(env,'CONNECT_TIMEOUT',12),
        http_read_timeout=number(env,'HTTP_READ_TIMEOUT',120),
        ws_handshake_timeout=number(env,'WS_HANDSHAKE_TIMEOUT',20),
        tunnel_idle_timeout=number(env,'TUNNEL_IDLE_TIMEOUT',3600),
        stats_interval=number(env,'STATS_INTERVAL',10),
        poll_interval=number(env,'CONFIG_POLL_INTERVAL',5),
        threads=number(env,'HAPROXY_THREADS',effective_cpus(),integer=True),
        ca_file=str(ca),
    )
    if result.port > 65535 or result.origin_port > 65535: raise ValueError('port must be 1..65535')
    if result.threads > 64: raise ValueError('HAPROXY_THREADS must be 1..64')
    return result


def effective_cpus():
    try: count = len(os.sched_getaffinity(0))
    except AttributeError: count = os.cpu_count() or 1
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max': count = min(count, math.ceil(int(quota)/int(period)))
    except (OSError, ValueError, ZeroDivisionError): pass
    return max(1, min(count, 8))


def fd_capacity():
    """Use existing OS budget; do not lower unlimited limits or force a fixed cap."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        try: soft = int(Path('/proc/sys/fs/nr_open').read_text())
        except (OSError,ValueError): soft = 1048576
    reserve = min(512, max(16, soft // 4))
    capacity = max(1, (soft - reserve) // 2)
    return capacity, soft, hard


def quoted(path):
    value = str(path)
    if any(c in value for c in '\n\r\x00$\\"'): raise ValueError('unsafe configuration path')
    return '"' + value + '"'


def render_config(s: Settings, run_dir: Path, capacity: int):
    def duration(value): return f"{max(1, math.ceil(value * 1000))}ms"
    # Resource ceiling applies to all frontend sockets, including idle HTTP.
    # Optional app admission control leaves additional room for health requests.
    frontend_limit = capacity if s.max_concurrent == 0 else min(capacity, s.max_concurrent + 256)
    admission = ''
    if s.max_concurrent:
        admission = (f'    http-request return status 503 content-type text/plain string "Service Unavailable" '
                     f'hdr Retry-After 5 if {{ be_conn(origin) ge {s.max_concurrent} }}\n')
    authority = s.origin_sni + (f':{s.origin_port}' if s.origin_port != 443 else '')
    server_limit = f' maxconn {s.max_concurrent} maxqueue 1' if s.max_concurrent else ''
    disabled = ' disabled' if not s.origin_enabled else ''
    error_logging = ''
    if s.error_logs:
        error_logging = """    log global
    option dontlog-normal
    option log-separate-errors
    log-format "REN_ORIGIN_ERROR backend=%b server=%s status=%ST termination=%tsc connect_ms=%Tc response_ms=%Tr total_ms=%Tt ssl_error=%[ssl_bc_err] ssl_detail=%{+Q}[ssl_bc_err_str]"
"""
    config = f'''global
    master-worker
    log stdout format raw local0 warning
    nbthread {s.threads}
    maxconn {frontend_limit}
    stats socket {quoted(run_dir / 'stats.sock')} mode 600 level admin expose-fd listeners
    stats timeout 2s
    ssl-default-server-options ssl-min-ver TLSv1.2

defaults
{error_logging}    mode http
    option http-server-close
    retries 0
    timeout connect {duration(s.connect_timeout)}
    timeout client {duration(s.http_read_timeout)}
    timeout server {duration(s.http_read_timeout)}
    timeout http-request 20s
    timeout http-keep-alive 15s
    timeout queue 100ms
    timeout tunnel {duration(s.tunnel_idle_timeout)}
    timeout client-fin 5s
    timeout server-fin 5s

frontend public
    bind 0.0.0.0:{s.port}
    http-request return status 200 content-type application/json string '{{"status":"ok"}}' if {{ path -m str /_ren/health }}
{admission}    http-request set-header Host {authority}
    default_backend origin

backend origin
    http-request set-timeout server {duration(s.ws_handshake_timeout)} if {{ hdr(Upgrade) -i websocket }}
    server target {s.origin_ip}:{s.origin_port} ssl verify required ca-file {quoted(run_dir / 'origin-ca.pem')} sni str({s.origin_sni}) verifyhost {s.origin_sni} alpn http/1.1{server_limit}{disabled}
'''
    if not s.routes: return config
    prefix=config.split('frontend public\n',1)[0]
    routing=f"frontend public\n    bind 0.0.0.0:{s.port}\n"
    if any(route.domain in s.keepalive_domains for route in s.routes):
        # Server-close is enabled if either frontend or backend asks for it.
        # Keep unselected backends inheriting server-close from defaults.
        routing+='    no option http-server-close\n    option http-keep-alive\n' 
    routing+="    http-request return status 200 content-type application/json string '{\"status\":\"ok\"}' if { path -m str /_ren/health }\n"
    routing+='    http-request set-var(txn.ren_route_host) req.hdr(host),lower,field(1,:)\n'
    for route in s.routes:
        routing+=f'    acl ren_known_host var(txn.ren_route_host) -m str {route.domain}\n'
    routing+='    http-request return status 421 content-type text/plain string "Unknown domain" unless ren_known_host\n'
    if s.max_concurrent:
        routing+='    http-request set-var(txn.ren_active) int(0)\n'
        for index in range(1,len(s.routes)+1):
            routing+=f'    http-request set-var(txn.ren_backend_active) be_conn(origin_{index})\n'
            routing+='    http-request set-var(txn.ren_active) var(txn.ren_active),add(txn.ren_backend_active)\n'
        routing+=f'    http-request return status 503 content-type text/plain string "Service Unavailable" hdr Retry-After 5 if {{ var(txn.ren_active) -m int ge {s.max_concurrent} }}\n'
    backends=[]
    for index,route in enumerate(s.routes,1):
        name=f'origin_{index}'
        routing+=f'    use_backend {name} if {{ var(txn.ren_route_host) -m str {route.domain} }}\n'
        authority=route.origin_sni+(f':{route.origin_port}' if route.origin_port!=443 else '')
        connection_policy = ''
        if route.domain in s.keepalive_domains:
            connection_policy = '    no option http-server-close\n    option http-keep-alive\n    http-reuse safe\n'
        backends.append(f"""backend {name}
{connection_policy}    http-request set-header Host {authority}
    http-request set-timeout server {duration(s.ws_handshake_timeout)} if {{ hdr(Upgrade) -i websocket }}
    server target {route.origin_ip}:{route.origin_port} ssl verify required ca-file {quoted(run_dir / 'origin-ca.pem')} sni str({route.origin_sni}) verifyhost {route.origin_sni} alpn http/1.1{server_limit}
""")
    return prefix+routing+'\n'+'\n'.join(backends)



def runtime_command(path: Path, command: str, timeout=2):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((command + '\n').encode())
        client.shutdown(socket.SHUT_WR)
        chunks=[]; length=0
        while True:
            data=client.recv(65536)
            if not data: break
            length += len(data)
            if length > 2*1024*1024: raise RuntimeError('runtime response exceeds budget')
            chunks.append(data)
        return b''.join(chunks).decode(errors='replace')


def process_tree(root):
    result=[]; pending=[root]
    while pending:
        pid=pending.pop()
        if pid in result: continue
        result.append(pid)
        try: pending.extend(int(v) for v in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
        except (OSError,ValueError): pass
    return result


def process_metrics(pids):
    rss=0; fds=0
    for pid in pids:
        try: rss += int(Path(f'/proc/{pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        except (OSError,ValueError,IndexError): pass
        try: fds += len(list(Path(f'/proc/{pid}/fd').iterdir()))
        except OSError: pass
    return {'rss_sum_mb':round(rss/2**20,2),'fds_sum':fds,'processes':len(pids)}


def cgroup_metrics():
    # Namespace root on Render/Linux containers, with a nested-path fallback.
    roots=[Path('/sys/fs/cgroup')]
    try:
        for line in Path('/proc/self/cgroup').read_text().splitlines():
            kind, controllers, path=line.split(':',2)
            if '..' in Path(path).parts: continue
            if kind=='0' and not controllers: roots.append(Path('/sys/fs/cgroup')/path.lstrip('/'))
            elif 'memory' in controllers.split(','): roots.append(Path('/sys/fs/cgroup/memory')/path.lstrip('/'))
    except (OSError,ValueError): pass
    roots.append(Path('/sys/fs/cgroup/memory'))
    for root in roots:
        for used_name,limit_name in [('memory.current','memory.max'),('memory.usage_in_bytes','memory.limit_in_bytes')]:
            try: used=int((root/used_name).read_text())
            except (OSError,ValueError): continue
            try: limit=int((root/limit_name).read_text())
            except (OSError,ValueError): limit=None
            if limit is not None and limit >= 2**60: limit=None
            return {'cgroup_mb':round(used/2**20,2),'cgroup_limit_mb':None if limit is None else round(limit/2**20,2)}
    return {'cgroup_mb':None,'cgroup_limit_mb':None}


def worker_processes(master_socket):
    text=runtime_command(master_socket,'show proc')
    current=[]; old=[]; old_section=False
    for line in text.splitlines():
        if line.startswith('# old workers'):
            old_section=True
            continue
        if line.startswith('# workers'):
            old_section=False
            continue
        fields=line.split()
        if len(fields) > 1 and fields[0].isdigit():
            if fields[1]=='worker': (old if old_section else current).append(int(fields[0]))
            elif fields[1]=='old' and len(fields)>2 and fields[2]=='worker': old.append(int(fields[0]))
    return current,old


def haproxy_stats(stats_socket, prefix=''):
    raw=runtime_command(stats_socket,prefix+'show stat')
    rows=list(csv.DictReader(io.StringIO(raw.removeprefix('# '))))
    result={'backends':{}}
    for row in rows:
        if (row.get('pxname')=='origin' or row.get('pxname','').startswith('origin_')) and row.get('svname')=='BACKEND':
            backend={}
            for source,target in [('scur','active'),('smax','peak'),('stot','accepted_total'),('bin','bytes_in_total'),('bout','bytes_out_total'),('econ','connect_errors_total'),('eresp','response_errors_total'),('qcur','queued'),('hrsp_5xx','origin_5xx_total')]:
                value=row.get(source,'')
                backend[target]=int(value) if value and value.isdigit() else None
                result[target]=(result.get(target) or 0)+int(value) if value and value.isdigit() else result.get(target)
            for source,target in [('connect','backend_connect_total'),('reuse','backend_reuse_total'),('ctime','avg_connect_ms')]:
                value=row.get(source,'');backend[target]=int(value) if value.isdigit() else None
            result['backends'][row['pxname']]=backend
        if row.get('pxname')=='public' and row.get('svname')=='FRONTEND':
            value=row.get('hrsp_5xx',''); result['frontend_5xx_total']=int(value) if value.isdigit() else None
    return result


class Controller:
    def __init__(self, env=None):
        self.env=dict(os.environ if env is None else env)
        self.settings=load_settings(self.env)
        self.binary=self.env.get('HAPROXY_BIN','haproxy')
        self.run_dir=Path(tempfile.mkdtemp(prefix='ren-'))
        os.chmod(self.run_dir,0o700)
        self.config=self.run_dir/'haproxy.cfg'
        self.stats_socket=self.run_dir/'stats.sock'; self.master_socket=self.run_dir/'master.sock'
        self.capacity,self.soft_fd,self.hard_fd=fd_capacity()
        self.process=None; self.stopping=False; self.reload_requested=False
        self.started=time.monotonic(); self.generation=0
        self.pending_reload=None; self.runtime_dirty=False

    def validate(self, settings):
        candidate=self.run_dir/'candidate.cfg'
        candidate.write_text(render_config(settings,self.run_dir,self.capacity))
        result=subprocess.run([self.binary,'-c','-f',str(candidate)],capture_output=True,text=True,timeout=10)
        if result.returncode:
            raise RuntimeError('HAProxy config validation failed: '+(result.stderr or result.stdout)[-3000:])
        return candidate

    def start(self):
        shutil.copyfile(self.settings.ca_file,self.run_dir/'origin-ca.pem')
        candidate=self.validate(self.settings); os.replace(candidate,self.config)
        self.process=subprocess.Popen([self.binary,'-W','-db','-f',str(self.config),'-S',str(self.master_socket)],start_new_session=True)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.process.poll() is not None: raise RuntimeError('HAProxy exited during startup')
            try:
                current,_=worker_processes(self.master_socket)
                if current and 'Name:' in runtime_command(self.stats_socket,'show info'): break
            except (OSError,TimeoutError): pass
            time.sleep(.05)
        else: raise RuntimeError('HAProxy readiness timed out')
        LOG.info('REN Hybrid online build=%s instance=%s python=%s max_concurrent=%s resource_capacity=%d fd_soft=%s fd_hard=%s threads=%d',
                 BUILD,os.getenv('RENDER_INSTANCE_ID') or socket.gethostname(),sys.version.split()[0],
                 self.settings.max_concurrent or 'unlimited',self.capacity,self.soft_fd,self.hard_fd,self.settings.threads)
        mapped={route.domain for route in self.settings.routes}
        LOG.info('REN_PERFORMANCE keepalive_domains=%s unmatched_domains=%s error_logs=%s',
                 json.dumps(sorted(mapped.intersection(self.settings.keepalive_domains))),
                 json.dumps(sorted(set(self.settings.keepalive_domains)-mapped)),self.settings.error_logs)


    def _confirm_reload(self):
        pending=self.pending_reload
        current,_=worker_processes(self.master_socket)
        text=runtime_command(self.master_socket,'show proc')
        failed=re.search(r'\[failed: (\d+)\]',text)
        failures=int(failed[1]) if failed else 0
        if current and current!=pending['workers']:
            self.settings=pending['settings']; self.generation+=1
            self.pending_reload=None; self.runtime_dirty=False
            LOG.info('REN_CONFIG graceful reload applied generation=%d',self.generation)
            return
        if failures>pending['failures']:
            restore=self.run_dir/'restore.cfg'
            restore.write_text(pending['previous']); os.replace(restore,self.config)
            self.pending_reload=None
            LOG.warning('REN_CONFIG reload failed; restored prior config; existing worker retained')
            return
        LOG.warning('REN_CONFIG reload not yet confirmed; retaining serving workers and deferring updates')

    def _runtime_enabled(self):
        raw=runtime_command(self.stats_socket,'show stat')
        for row in csv.DictReader(io.StringIO(raw.removeprefix('# '))):
            if row.get('pxname')=='origin' and row.get('svname')=='target':
                return not row.get('status','').startswith('MAINT')
        raise RuntimeError('origin runtime state unavailable')

    def apply_settings(self):
        if self.pending_reload is not None:
            self._confirm_reload(); return
        fresh=load_settings(self.env)
        if fresh==self.settings and not self.runtime_dirty: return
        candidate=self.validate(fresh)
        old_settings=self.settings
        if not fresh.routes and not old_settings.routes and replace(fresh,origin_enabled=old_settings.origin_enabled)==old_settings:
            # Mark before sending: a successful command can lose its reply.
            self.runtime_dirty=True
            if self._runtime_enabled()!=fresh.origin_enabled:
                command=('enable' if fresh.origin_enabled else 'disable')+' server origin/target'
                response=runtime_command(self.stats_socket,command)
                if response.strip(): raise RuntimeError('HAProxy runtime change rejected')
            os.replace(candidate,self.config)
            self.settings=fresh; self.runtime_dirty=False
            LOG.info('REN_CONFIG origin enabled=%s applied via runtime API',fresh.origin_enabled)
            return
        _,old=worker_processes(self.master_socket)
        if old:
            LOG.warning('REN_CONFIG update deferred; %d old worker(s) still draining',len(old)); return
        previous=self.config.read_text()
        before,_=worker_processes(self.master_socket)
        text=runtime_command(self.master_socket,'show proc')
        failed=re.search(r'\[failed: (\d+)\]',text)
        pending={'settings':fresh,'previous':previous,'workers':before,
                 'failures':int(failed[1]) if failed else 0}
        os.replace(candidate,self.config)
        self.pending_reload=pending
        try:
            runtime_command(self.master_socket,'reload',timeout=12)
        except (OSError,TimeoutError):
            # Observe master state on the next poll instead of disrupting healthy tunnels.
            LOG.warning('REN_CONFIG reload reply unavailable; checking master state')
        self._confirm_reload()

    def log_metrics(self):
        current,old=worker_processes(self.master_socket)
        sample={'build':BUILD,'instance':os.getenv('RENDER_INSTANCE_ID') or socket.gethostname(),
                'pid':os.getpid(),'uptime_s':round(time.monotonic()-self.started,1),'generation':self.generation,
                'max_concurrent':self.settings.max_concurrent,'resource_capacity':self.capacity,
                'current_workers':len(current),'draining_workers':len(old),
                'pending_reload':self.pending_reload is not None,'runtime_state_pending':self.runtime_dirty}
        sample.update(process_metrics(process_tree(os.getpid())))
        sample.update(cgroup_metrics())
        stats=haproxy_stats(self.stats_socket); backends=stats.pop('backends',{})
        sample.update(stats)
        sample['origins']=[{'domain':route.domain,'backend':f'origin_{index}',
                            'keepalive':route.domain in self.settings.keepalive_domains,
                            **backends.get(f'origin_{index}',{})}
                           for index,route in enumerate(self.settings.routes,1)]
        if not self.settings.routes:
            sample['origins']=[{'backend':'origin','keepalive':False,**backends.get('origin',{})}]

        # Old workers have their own counters. Query each via the master CLI.
        for pid in old:
            old_stats=haproxy_stats(self.master_socket,f'@!{pid} ')
            sample['draining_connections']=sample.get('draining_connections',0)+(old_stats.get('active') or 0)
        sample.setdefault('draining_connections',0)
        sample['active_current']=sample.get('active')
        sample['active']=(sample.get('active') or 0)+sample['draining_connections']
        LOG.info('REN_METRICS %s',json.dumps(sample,separators=(',',':')))

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            # Render's default shutdown budget is 30s; allow 20s for existing flows.
            self.process.send_signal(signal.SIGUSR1)
            try: self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGTERM)
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid,signal.SIGKILL); self.process.wait()
        shutil.rmtree(self.run_dir,ignore_errors=True)

    def run(self):
        def stop_requested(*_): self.stopping=True
        def reload_requested(*_): self.reload_requested=True
        signal.signal(signal.SIGTERM,stop_requested); signal.signal(signal.SIGINT,stop_requested)
        signal.signal(signal.SIGHUP,reload_requested)
        try:
            self.start(); next_stats=next_poll=0
            while not self.stopping:
                if self.process.poll() is not None: raise RuntimeError(f'HAProxy exited (status={self.process.returncode})')
                now=time.monotonic()
                if now>=next_stats:
                    try: self.log_metrics()
                    except (OSError,ValueError,RuntimeError) as exc: LOG.warning('REN_METRICS unavailable (%s)',type(exc).__name__)
                    next_stats=now+self.settings.stats_interval
                if self.reload_requested or ((self.pending_reload is not None or self.env.get('ORIGIN_CONFIG_FILE')) and now>=next_poll):
                    self.reload_requested=False
                    try: self.apply_settings()
                    except Exception as exc: LOG.warning('REN_CONFIG rejected; retaining current config (%s)',str(exc))
                    next_poll=now+self.settings.poll_interval
                time.sleep(.2)
        finally: self.stop()


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s:%(name)s:%(message)s')
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    try:
        controller=Controller()
        if args.check:
            try:
                shutil.copyfile(controller.settings.ca_file,controller.run_dir/'origin-ca.pem')
                controller.validate(controller.settings); print('REN configuration valid')
            finally: controller.stop()
        else: controller.run()
        return 0
    except Exception as exc:
        LOG.error('REN_FATAL %s',str(exc)); return 1

if __name__=='__main__': raise SystemExit(main())
