#!/usr/bin/env python3
"""Audit OpenWrt rpcd ACL by asking session.access; NEVER call write methods.

Run on a trusted workstation: python tools/audit_acl.py https://router.example
Use a trusted CA or SHA-256 certificate pin by configuring trust in the OS.
"""
import getpass
import json
import sys
import urllib.request

if len(sys.argv) != 2 or not sys.argv[1].startswith('https://'):
    raise SystemExit('Usage: python tools/audit_acl.py https://router.example')
url = sys.argv[1].rstrip('/') + '/ubus'
username = input('Read-only RPC username: ')
password = getpass.getpass('RPC password: ')


def rpc(token, obj, method, args):
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":"call","params":[token,obj,method,args]}).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data=json.load(resp)
    if not isinstance(data.get('result'),list) or data['result'][0] != 0:
        raise RuntimeError('RPC error: '+str(data))
    return data['result'][1]


token = rpc('0'*32, 'session', 'login', {"username":username,"password":password,"timeout":300})['ubus_rpc_session']
checks = [
    ("ha_netmon","snapshot",True),
    ("system","info",True),
    ("network.device","status",True),
    ("hostapd.phy0-ap0","get_clients",True),
    ("uci","set",False),
    ("uci","commit",False),
    ("network","restart",False),
    ("network.interface","remove",False),
    ("network.wireless","down",False),
    ("system","signal",False),
    ("system","reboot",False),
    ("file","exec",False),
    ("file","write",False),
]
failed = False
print('Only session.access is called, no state-changing RPC method is executed.')
for obj, method, expected in checks:
    access = rpc(token,'session','access',{"ubus_rpc_session":token,"scope":"ubus","object":obj,"function":method})['access']
    passed = (access == expected)
    # hostapd BSS name varies on every OpenWrt; don't require this specific object.
    if obj.startswith('hostapd.'):
        print(f'{obj}.{method}: access={access} (example name; check actual BSS separately)')
        continue
    print(f'{"PASS" if passed else "FAIL"} {obj}.{method}: access={access}, expected={expected}')
    failed |= not passed
raise SystemExit(1 if failed else 0)
