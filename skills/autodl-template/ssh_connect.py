#!/usr/bin/env python
# -*- coding: utf-8 -*-
import paramiko
import sys

def ssh_connect(hostname, username, password, port=22):
    """建立 SSH 连接并执行基本命令验证"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print(f"正在连接到 {username}@{hostname}:{port}...")
        client.connect(hostname, port=port, username=username, password=password, timeout=10)
        print("[OK] 连接成功！\n")

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
        client.close()

if __name__ == "__main__":
    # 使用前请替换为你的真实连接信息
    hostname = "connect.westd.seetacloud.com"
    username = "root"
    password = ""
    port = 24564

    success = ssh_connect(hostname, username, password, port=port)
    sys.exit(0 if success else 1)
