#!/usr/bin/env python
# -*- coding: utf-8 -*-
import paramiko
import sys


WIFI_INTERFACE = "en0"
IP_BOUND_IF = 25


def _load_wifi_dependencies():
    global ipaddress, re, socket, subprocess
    import ipaddress, re, socket, subprocess


def _new_client():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def _command_output(command):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)


def _field(output, name):
    match = re.search(rf"^\s*{re.escape(name)}:\s*(\S+)", output, re.MULTILINE)
    if not match:
        raise RuntimeError(f"无法从系统路由信息读取 {name}")
    return match.group(1)


def _ensure_wifi_host_route(ip_address):
    route = _command_output(["/sbin/route", "-n", "get", ip_address])
    if _field(route, "interface") == WIFI_INTERFACE:
        return

    wifi_default = _command_output(
        ["/sbin/route", "-n", "get", "-ifscope", WIFI_INTERFACE, "default"]
    )
    gateway = _field(wifi_default, "gateway")
    ipaddress.ip_address(ip_address)
    ipaddress.ip_address(gateway)
    route_command = f"/sbin/route -n add -host {ip_address} {gateway}"
    apple_script = f'do shell script "{route_command}" with administrator privileges'
    subprocess.run(["/usr/bin/osascript", "-e", apple_script], check=True)


def _wifi_socket(hostname, port, timeout):
    address = socket.gethostbyname(hostname)
    _ensure_wifi_host_route(address)
    wifi_ip = _command_output(["/usr/sbin/ipconfig", "getifaddr", WIFI_INTERFACE]).strip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(WIFI_INTERFACE))
        sock.bind((wifi_ip, 0))
        sock.connect((address, port))
        return sock
    except Exception:
        sock.close()
        raise


def _connect_with_wifi_fallback(hostname, username, password, port, timeout=10):
    client = _new_client()
    try:
        client.connect(hostname, port=port, username=username, password=password, timeout=timeout)
        return client, False
    except paramiko.AuthenticationException:
        client.close()
        raise
    except (OSError, paramiko.SSHException):
        client.close()

    print("[WARN] 常规 SSH 握手失败，改用 Wi-Fi 直连重试...")
    _load_wifi_dependencies()
    sock = _wifi_socket(hostname, port, timeout)
    client = _new_client()
    try:
        client.connect(
            hostname,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            sock=sock,
        )
        return client, True
    except Exception:
        client.close()
        sock.close()
        raise


def ssh_connect(hostname, username, password, port=22):
    """建立 SSH 连接并执行基本命令验证"""
    client = None

    try:
        print(f"正在连接到 {username}@{hostname}:{port}...")
        client, used_wifi_fallback = _connect_with_wifi_fallback(
            hostname, username, password, port, timeout=10
        )
        print("[OK] 连接成功！\n")
        if used_wifi_fallback:
            print(f"[OK] 已通过 {WIFI_INTERFACE} Wi-Fi 直连\n")

        # 执行一些基本命令来验证连接
        commands = [
            "whoami",
            "hostname",
            "uname -a",
            "pwd",
            "ls -la"
        ]

        print("=== 服务器信息 ===\n")
        for cmd in commands:
            print(f"$ {cmd}")
            stdin, stdout, stderr = client.exec_command(cmd)
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')

            if output:
                print(output)
            if error:
                print(f"错误: {error}")
            print()

        print("=== 连接验证完成 ===")
        print(f"\n连接信息：")
        print(f"  主机: {hostname}")
        print(f"  用户: {username}")
        print(f"  端口: {port}")
        print(f"\n提示：连接已建立并验证成功。")
        print(f"你可以使用以下命令手动连接：")
        print(f"  ssh {username}@{hostname}")

        return True

    except paramiko.AuthenticationException:
        print("[ERROR] 认证失败：用户名或密码错误")
        return False
    except paramiko.SSHException as e:
        print(f"[ERROR] SSH 连接错误：{e}")
        return False
    except Exception as e:
        print(f"[ERROR] 连接失败：{e}")
        return False
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    # 使用前请替换为你的真实连接信息
    hostname = "connect.westd.seetacloud.com"
    username = "root"
    password = ""
    port = 00000

    success = ssh_connect(hostname, username, password, port=port)
    sys.exit(0 if success else 1)
