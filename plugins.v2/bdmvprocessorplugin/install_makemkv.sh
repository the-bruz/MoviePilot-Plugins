#!/bin/bash
# MakeMKV 容器内轻量化编译安装脚本 (无 GUI 版)
# 适用环境: MoviePilot v2 容器内 (Debian 12 Bookworm)
# 
# 用法 (在宿主机执行):
# docker exec -it moviepilot /bin/bash /path/to/install_makemkv.sh

set -e

MAKEMKV_VERSION="1.18.4"
BUILD_DIR="/tmp/makemkv_build"

echo "=========================================="
echo "开始在容器内编译安装 MakeMKV ${MAKEMKV_VERSION} (无 GUI)"
echo "=========================================="

# 1. 更新软件源并安装编译基础依赖
echo "[1/5] 安装编译依赖..."
apt-get update
# libavcodec59 作为运行时库，显式安装以免被后续 autoremove 清理
apt-get install -y wget pkg-config libc6-dev libssl-dev libexpat1-dev libavcodec-dev zlib1g-dev build-essential libavcodec59

# 2. 准备编译环境并下载源码
echo "[2/5] 下载源码包..."
mkdir -p ${BUILD_DIR}
cd ${BUILD_DIR}

wget -O makemkv-oss-${MAKEMKV_VERSION}.tar.gz https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz
wget -O makemkv-bin-${MAKEMKV_VERSION}.tar.gz https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz

tar -zxvf makemkv-oss-${MAKEMKV_VERSION}.tar.gz
tar -zxvf makemkv-bin-${MAKEMKV_VERSION}.tar.gz

# 3. 编译 OSS 模块 (使用 --disable-gui 避免引入臃肿的 Qt 依赖)
echo "[3/5] 编译开源核心 (OSS)..."
cd makemkv-oss-${MAKEMKV_VERSION}
./configure --disable-gui
make -j$(nproc)
make install

# 4. 编译并安装 BIN 模块 (闭源核心)
echo "[4/5] 编译闭源核心 (BIN)..."
cd ../makemkv-bin-${MAKEMKV_VERSION}
mkdir -p tmp && touch tmp/eula_accepted  # 跳过需要手动同意的 EULA 提示
make
make install

# 5. 清理战场 (过河拆桥)
echo "[5/5] 清理编译环境..."
cd /
rm -rf ${BUILD_DIR}

# 卸载仅用于编译的开发包
apt-get remove -y build-essential pkg-config libc6-dev libssl-dev libexpat1-dev libavcodec-dev zlib1g-dev
apt-get autoremove -y
apt-get clean

# 测试运行
echo "=========================================="
echo "安装完成！测试运行 makemkvcon --info"
echo "=========================================="
makemkvcon --info
