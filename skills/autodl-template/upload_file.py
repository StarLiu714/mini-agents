#!/usr/bin/env python
# -*- coding: utf-8 -*-
import paramiko
import sys
import os
import ntpath

DEFAULT_REMOTE_DIR = "/root/autodl-tmp"


def upload_file(hostname, username, password, local_path, remote_path=None, port=22):
    """上传文件到远程服务器"""
    if remote_path is None:
        filename = os.path.basename(local_path)
        if filename == local_path:
            filename = ntpath.basename(local_path)
        remote_path = os.path.join(DEFAULT_REMOTE_DIR, filename)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print(f"正在连接到 {username}@{hostname}:{port}...")
        client.connect(hostname, port=port, username=username, password=password, timeout=10)
        print("[OK] 连接成功")

        # 使用 SFTP 上传文件
        sftp = client.open_sftp()

        # 确保远程目录存在
        remote_dir = os.path.dirname(remote_path)
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            print(f"创建远程目录: {remote_dir}")
            sftp.mkdir(remote_dir)

        print(f"正在上传文件...")
        print(f"  本地: {local_path}")
        print(f"  远程: {remote_path}")

        sftp.put(local_path, remote_path)

        print("[OK] 文件上传成功")

        # 验证文件
        file_stat = sftp.stat(remote_path)
        print(f"远程文件大小: {file_stat.st_size} 字节")

        sftp.close()
        return True

    except FileNotFoundError as e:
        print(f"[ERROR] 本地文件不存在: {e}")
        return False
    except paramiko.AuthenticationException:
        print("[ERROR] 认证失败：用户名或密码错误")
        return False
    except paramiko.SSHException as e:
        print(f"[ERROR] SSH 连接错误：{e}")
        return False
    except Exception as e:
        print(f"[ERROR] 上传失败：{e}")
        return False
    finally:
        client.close()

if __name__ == "__main__":
    # 使用前请替换为你的真实连接信息
    hostname = "connect.westd.seetacloud.com"
    username = "root"
    password = ""
    port = 24564

    # 使用前请替换为你的本地文件路径；默认上传到 HPC 的 /root/autodl-tmp/
    local_path = r"C:\path\to\your\file.md"
    remote_path = None

    success = upload_file(hostname, username, password, local_path, remote_path, port=port)
    sys.exit(0 if success else 1)
