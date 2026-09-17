# -*- coding: utf-8 -*-
"""
测试上传、下载、权限功能
连接宿主机 server.py，验证管理员码完整权限 + 普通码只读
"""
import sys
import socket
import json

HOST = '192.168.1.3'
PORT = 47810

def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect((HOST, PORT))
    return s

def read_line(s):
    data = b''
    while not data.endswith(b'\n'):
        data += s.recv(1)
    return data.decode('utf-8').strip()

def send_line(s, line):
    s.sendall((line + '\n').encode('utf-8'))

def pair(s, device_id, pair_code, device_name):
    send_line(s, f'PT-PAIR {device_id} {pair_code} {device_name}')
    resp = read_line(s)
    if resp.startswith('PT-PAIR-OK '):
        token = resp.removeprefix('PT-PAIR-OK ').strip()
        print(f'  ✓ 配对成功，token={token[:16]}...')
        return token
    else:
        print(f'  ✗ 配对失败：{resp}')
        return None

def auth(s, device_id, token):
    send_line(s, f'PT-AUTH {device_id} {token}')
    resp = read_line(s)
    if resp.startswith('PT-AUTH-OK'):
        print(f'  ✓ 认证成功')
        return True
    else:
        print(f'  ✗ 认证失败：{resp}')
        return False

def list_dir(s, token, path):
    send_line(s, f'LIST {path}')
    resp = read_line(s)
    if resp.startswith('PT-JSON'):
        length = int(resp[7:].strip())
        data = b''
        while len(data) < length:
            data += s.recv(length - len(data))
        items = json.loads(data.decode('utf-8'))
        return items
    else:
        print(f'  ✗ LIST 失败：{resp}')
        return None

def upload(s, token, remote_path, content):
    send_line(s, f'UPLOAD {remote_path} Content-Length {len(content)}')
    resp = read_line(s)
    if resp.startswith('PT-OK'):
        s.sendall(content)
        return None  # 成功
    else:
        print(f'  ✗ UPLOAD 失败：{resp}')
        return resp

def download(s, token, remote_path):
    send_line(s, f'DOWNLOAD {remote_path}')
    resp = read_line(s)
    if resp.startswith('PT-OK Content-Length '):
        length = int(resp.removeprefix('PT-OK Content-Length ').strip())
        data = b''
        while len(data) < length:
            data += s.recv(length - len(data))
        return data
    else:
        print(f'  ✗ DOWNLOAD 失败：{resp}')
        return None

def delete(s, token, path):
    send_line(s, f'DELETE {path}')
    resp = read_line(s)
    if resp.startswith('PT-OK'):
        return None
    else:
        return resp

def main():
    print('=' * 60)
    print('测试上传、下载、权限功能')
    print('=' * 60)
    
    # 1. 管理员码配对
    print('\n[1] 管理员码配对 (888888)')
    s = connect()
    token = pair(s, 'android-admin-test', '888888', 'AdminTest')
    if not token:
        return 1
    
    # 2. 认证
    print('\n[2] 认证')
    auth_ok = auth(s, 'android-admin-test', token)
    if not auth_ok:
        return 1
    
    # 3. 测试上传
    print('\n[3] 上传文件')
    content = b'Hello PhotoTrans Upload Test! 123456'
    err = upload(s, token, '/upload_test.txt', content)
    if err is None:
        print(f'  ✓ 上传成功 (len={len(content)} bytes)')
    else:
        print(f'  ✗ 上传失败：{err}')
        return 1
    
    # 3. 测试下载
    print('\n[3] 下载文件验证')
    data = download(s, token, '/upload_test.txt')
    if data is not None:
        print(f'  ✓ 下载成功 (len={len(data)} bytes)')
        if data == content:
            print(f'  ✓ 内容一致：{data.decode()}')
        else:
            print(f'  ✗ 内容不一致：{data.decode()}')
            return 1
    else:
        print(f'  ✗ 下载失败')
        return 1
    
    # 4. 测试删除
    print('\n[4] 删除文件')
    err = delete(s, token, '/upload_test.txt')
    if err is None:
        print(f'  ✓ 删除成功')
    else:
        print(f'  ✗ 删除失败：{err}')
    s.close()
    
    # 5. 普通码测试只读权限
    print('\n[5] 普通码配对 (123456) - 只读权限')
    s2 = connect()
    ro_token = pair(s2, 'android-ro-test', '123456', 'ReadOnlyTest')
    if ro_token:
        auth(s2, 'android-ro-test', ro_token)
        
        # 尝试上传（应该被拒绝）
        print('\n[6] 只读权限上传测试（应该被拒绝）')
        err = upload(s2, ro_token, '/ro_test.txt', b'x')
        if err is not None:
            print(f'  ✓ 上传被拒绝：{err}')
        else:
            print(f'  ✗ 上传未被拒绝（BUG！）')
            return 1
        
        # 尝试删除（应该被拒绝）
        print('\n[7] 只读权限删除测试（应该被拒绝）')
        err = delete(s2, ro_token, '/testfolder123')
        if err is not None:
            print(f'  ✓ 删除被拒绝：{err}')
        else:
            print(f'  ✗ 删除未被拒绝（BUG！）')
            return 1
        
        # 浏览应该可以
        print('\n[8] 只读权限浏览测试（应该允许）')
        items = list_dir(s2, ro_token, '/')
        if items is not None:
            print(f'  ✓ 浏览成功：{len(items)} 项')
        else:
            print(f'  ✗ 浏览失败')
            return 1
    else:
        print(f'  ✗ 普通码配对失败')
        return 1
    
    s2.close()
    
    print('\n' + '=' * 60)
    print('✓ 全部测试通过！')
    print('=' * 60)
    return 0

if __name__ == '__main__':
    sys.exit(main())