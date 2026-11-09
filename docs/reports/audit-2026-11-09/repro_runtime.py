import json, os, socket, sqlite3, subprocess, sys, tempfile
from pathlib import Path
from stateforge.runtime.snapshot.protocol import _send, _recv
from stateforge.runtime.snapshot.l2_sqlite import L2Sqlite, L2Handle
from stateforge.runtime.snapshot.engine import _handle_freeze

root = Path(tempfile.mkdtemp(prefix='sf-runtime-audit-'))
live = root/'live.db'
with sqlite3.connect(live) as c:
    c.execute('create table items(x)')
l2 = L2Sqlite(str(live), capture='copy')
l2.snapshot_template('root')
# A completed leaf leaves live files dirty until the next restore.
with sqlite3.connect(live) as c:
    c.execute('insert into items values (99)')
a,b = socket.socketpair()
_handle_freeze(None, {'name':'child','build':0}, a, 'root', L2Handle(l2,'root'))
msg, fds = _recv(b)
child = socket.socket(fileno=fds[0]); child.settimeout(3)
print('FREEZE_EVENT', _recv(child)[0])
with sqlite3.connect(l2._tpl('root')) as c:
    print('ROOT_BASELINE_ROWS',c.execute('select * from items').fetchall())
with sqlite3.connect(l2._tpl('child')) as c:
    print('CHILD_BUILD_ZERO_ROWS',c.execute('select * from items').fetchall())
_send(child,{'cmd':'exit'}); os.waitpid(msg['pid'],0)
a.close(); b.close(); child.close()

# STREAM sockets can split one JSON packet across reads, or coalesce packets.
a,b = socket.socketpair(); a.sendall(b'{"cmd":')
try:
    _recv(b)
except Exception as e:
    print('FRAGMENTED_CONTROL',type(e).__name__)
a.close();b.close()
a,b=socket.socketpair(); a.sendall(b'{"cmd":"ping"}\n{"cmd":"rss"}\n')
try:
    _recv(b)
except Exception as e:
    print('COALESCED_CONTROL',type(e).__name__)
a.close();b.close()

# A read transaction does not block BEGIN IMMEDIATE, but prevents backup commit.
backup = L2Sqlite(str(live), tpl_root=str(root/'backup'))
backup.snapshot_template()
reader=sqlite3.connect(live); reader.execute('BEGIN'); reader.execute('select * from items').fetchall()
code='from stateforge.runtime.snapshot.l2_sqlite import L2Sqlite; import sys; L2Sqlite(sys.argv[1],tpl_root=sys.argv[2]).restore(); print("RESTORED")'
try:
    p=subprocess.run([sys.executable,'-c',code,str(live),str(root/'backup')],capture_output=True,text=True,timeout=2)
    print('READ_LOCK_RESTORE',p.returncode,p.stdout,p.stderr)
except subprocess.TimeoutExpired:
    print('READ_LOCK_RESTORE: HANG beyond 2s despite timeout=0')
finally:
    reader.close()
print('ARTIFACT_ROOT',root)
