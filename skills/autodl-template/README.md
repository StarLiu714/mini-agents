# SSH 服务器管理工具

## 使用前必改
- **服务器**: `<SERVER_HOST>`
- **用户名**: `<SERVER_USER>`
- **密码**: `<SERVER_PASSWORD>`
- **端口**: `<SERVER_PORT>`（默认 22）

## 连接方法

### 自动连接（推荐）
```bash
python ssh_connect.py
```

### 手动连接
```bash
ssh <SERVER_USER>@<SERVER_HOST> -p <SERVER_PORT>
```

## 可用脚本

### 1. `ssh_connect.py`
连接服务器并验证基本信息（`whoami`、`hostname`、`uname`、目录列表等）

### 2. `upload_file.py`
上传文件到服务器目标目录，默认传到 HPC 的 `/root/autodl-tmp/`

### 3. `explore_server.py`
深度分析服务器环境（系统信息、磁盘、内存、服务、Docker 容器、网络端口等）

## 安全提醒
- 不要把真实 IP、用户名、密码写入公开文档或代码仓库。
- 分享前请再次全局搜索 `password`、`token`、`secret` 等关键词。
