#!/usr/bin/env python
# -*- coding: utf-8 -*-
import paramiko
import sys

def explore_server(hostname, username, password, port=22):
    """深度探索服务器目录结构"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(hostname, port=port, username=username, password=password, timeout=10)

        commands = [
            ("系统信息", "cat /etc/os-release"),
            ("磁盘使用", "df -h"),
            ("内存使用", "free -h"),
            ("运行的服务", "systemctl list-units --type=service --state=running | head -20"),
            ("/root 目录详情", "ls -lah /root"),
            ("/root/backups 内容", "ls -lah /root/backups"),
            ("/opt 目录", "ls -lah /opt"),
            ("/www 目录", "ls -lah /www 2>/dev/null || echo '目录不存在'"),
            ("Docker 容器", "docker ps -a 2>/dev/null || echo 'Docker 未安装或未运行'"),
            ("网络监听端口", "ss -tlnp | head -20"),
        ]

        for title, cmd in commands:
            print(f"\n{'='*60}")
            print(f"【{title}】")
            print(f"{'='*60}")
            stdin, stdout, stderr = client.exec_command(cmd)
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')

            if output:
                print(output)
            if error and "目录不存在" not in error:
                print(f"[stderr] {error}")

        return True

    except Exception as e:
        print(f"[ERROR] {e}")
        return False
    finally:
        client.close()

if __name__ == "__main__":
    # 使用前请替换为你的真实连接信息
    hostname = "connect.westd.seetacloud.com"
    username = "root"
    password = ""
    port = 24564

    success = explore_server(hostname, username, password, port=port)
    sys.exit(0 if success else 1)
